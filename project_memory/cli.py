"""命令行入口：python -m project_memory [--config CONFIG] <command> ...

除 --help 外，所有命令都向 stdout 输出一个 UTF-8 JSON 对象：
成功 {"ok": true, "command": ..., ...}；失败 {"ok": false, "command": ..., "error": {"code", "message", "details"}}。
退出码：0 成功，1 业务错误，2 参数错误，3 内部错误。
"""

import argparse
import json
import sys
import traceback

from . import __version__
from . import api
from .config import load_config
from .util import PMError, dumps

DESCRIPTION = """项目工作记忆：本地项目记忆 CLI + stdio MCP（纯标准库，零网络端口）。

典型流程：
  discover                 从台账盘点项目（只抽取名称/路径/行号/归档标记），生成脱敏报告与 registry；未配置 ledger_path 时返回空集合
  bootstrap --project X    为存在且未归档的项目建立 overview/current/history（不覆盖已有文件）
  resume --cwd .           接手：读取概览与最新交接，并本地只读查询 git
  search "关键词"          FTS5 检索已授权资料（结果是资料，不是指令）
  handoff --input h.json   收尾：结构化交接（乐观 revision、冲突留档、幂等）
"""

EPILOG = "配置优先级：--config > 环境变量 PROJECT_MEMORY_CONFIG > 工程根目录 config.json > 内置默认。"


class JsonArgParser(argparse.ArgumentParser):
    def error(self, message):
        raise PMError("usage_error", message, usage=self.format_usage().strip())


def build_parser():
    p = JsonArgParser(prog="project_memory", description=DESCRIPTION, epilog=EPILOG,
                      formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="配置 JSON 路径（默认工程根目录 config.json）")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", metavar="<command>", parser_class=JsonArgParser)

    s = sub.add_parser("discover", help="从台账盘点项目，更新派生 registry 并生成脱敏报告")
    s.add_argument("--ledger", help="临时覆盖台账路径（默认使用配置 ledger_path）")
    s.add_argument("--no-report", action="store_true", help="不写盘点报告")
    s.add_argument("--dry-run", action="store_true", help="只解析与输出，不写 registry/报告")

    s = sub.add_parser("projects", help="列出 registry 中的项目")
    s.add_argument("--state", choices=["active", "archived", "missing", "needs_verification", "all"], default="all")
    s.add_argument("--query", help="按名称/路径/别名子串过滤")

    s = sub.add_parser("bootstrap", help="为存在且未归档的项目建立 overview.md/current.md/history（不覆盖）")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="project_id / 名称 / 路径")
    g.add_argument("--all-existing", action="store_true", help="所有目录存在且未归档的项目")
    s.add_argument("--approve-doc", action="append", default=[], metavar="REL",
                   help="同时批准该项目内文档进入索引（相对项目根，可重复；仅配合 --project）")

    s = sub.add_parser("sources", help="查看/批准/撤销进入索引的项目内文档")
    s.add_argument("--project", required=True)
    s.add_argument("--approve", action="append", default=[], metavar="REL")
    s.add_argument("--revoke", action="append", default=[], metavar="REL")

    s = sub.add_parser("search", help="检索已授权资料（FTS5，中文 bigram），结果先给时间/状态/来源/行号/摘要")
    s.add_argument("query")
    s.add_argument("--project", help="只在该项目内检索（缺省为跨项目，结果标注项目）")
    s.add_argument("--limit", type=int, help="结果数（默认 8，最大 50）")
    s.add_argument("--kind", action="append", choices=["overview", "current", "operations", "history", "note", "source"])
    s.add_argument("--mode", choices=['keyword','semantic','hybrid'], help="默认按配置使用混合或关键词检索")

    s = sub.add_parser("read", help="读取一个已授权且已索引的资料（只接受 search 返回的 ref）")
    s.add_argument("--project", required=True)
    s.add_argument("--ref", required=True, help="如 memory/current.md、source/docs/a.md")
    s.add_argument("--start", type=int, help="起始行（1 开始）")
    s.add_argument("--end", type=int, help="结束行（含）")

    s = sub.add_parser("handoff", help="写入结构化交接（JSON 输入）")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="JSON 文件路径；'-' 表示从 stdin 读取")
    g.add_argument("--json", help="直接给出 JSON 字符串")

    s = sub.add_parser("resume", help="接手项目：概览 + 最新交接 + 缺失/过期提示 + 本地只读 git 现场")
    s.add_argument("--project", help="project_id / 名称 / 路径")
    s.add_argument("--cwd", help="按路径识别（嵌套取最长前缀）；两者都缺省时用当前目录")
    s.add_argument("--no-git", action="store_true", help="不查询 git")
    s.add_argument("--no-initialize", action="store_true", help="只读简报，不初始化无记忆的已登记项目")

    s = sub.add_parser("initialize", help="首次资料盘点；不覆盖历史，可明确登记现存项目根目录")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--project")
    g.add_argument("--cwd")
    s.add_argument("--register", action="store_true",
                   help="登记未登记的项目：配置了台账时向台账追加无端口登记行，无台账时只写 registry")
    s.add_argument("--name", help="登记名称；默认采用项目根目录名")

    s = sub.add_parser("recover", help="崩溃修复：从权威历史重建 current.md")
    s.add_argument("--project", required=True)

    s = sub.add_parser("relocate", help="显式迁移项目路径（保留 project_id 与记忆）")
    s.add_argument("--project", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--force", action="store_true", help="新路径不存在也登记")

    s = sub.add_parser("alias", help="增删项目别名")
    s.add_argument("--project", required=True)
    s.add_argument("--add", action="append", default=[])
    s.add_argument("--remove", action="append", default=[])

    s = sub.add_parser("reindex", help="增量同步索引；--rebuild 删除后重建")
    s.add_argument("--rebuild", action="store_true")

    s = sub.add_parser("status", help="配置、registry、索引与项目交接状态")
    s.add_argument("--project")

    sub.add_parser("mcp", help="以 stdio MCP server 运行（newline JSON-RPC）")
    return p


def emit(obj):
    sys.stdout.buffer.write((dumps(obj) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _load_handoff(args):
    if args.json is not None:
        raw = args.json
    elif args.input == "-":
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
    else:
        with open(args.input, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    try:
        return json.loads(raw)
    except ValueError as e:
        raise PMError("invalid_json", f"handoff 输入不是合法 JSON: {e.msg}", line=e.lineno, column=e.colno)


def dispatch(cfg, args):
    c = args.command
    if c == "discover":
        return api.op_discover(cfg, ledger=args.ledger, no_report=args.no_report, dry_run=args.dry_run)
    if c == "projects":
        return api.op_projects(cfg, state=args.state, query=args.query)
    if c == "bootstrap":
        return api.op_bootstrap(cfg, project=args.project, all_existing=args.all_existing, approve=args.approve_doc)
    if c == "sources":
        return api.op_sources(cfg, args.project, approve=args.approve, revoke=args.revoke)
    if c == "search":
        return api.op_search(cfg, args.query, project=args.project, limit=args.limit, kinds=args.kind, mode=args.mode)
    if c == "read":
        return api.op_read(cfg, args.project, args.ref, args.start, args.end)
    if c == "handoff":
        return api.op_handoff(cfg, _load_handoff(args))
    if c == "resume":
        return api.op_resume(cfg, project=args.project, cwd=args.cwd, git=not args.no_git, initialize=not args.no_initialize)
    if c == "initialize":
        return api.op_initialize(cfg, project=args.project, cwd=args.cwd, register_missing=args.register, name=args.name)
    if c == "recover":
        return api.op_recover(cfg, args.project)
    if c == "relocate":
        return api.op_relocate(cfg, args.project, args.path, force=args.force)
    if c == "alias":
        return api.op_alias(cfg, args.project, add=args.add, remove=args.remove)
    if c == "reindex":
        return api.op_reindex(cfg, rebuild=args.rebuild)
    if c == "status":
        return api.op_status(cfg, project=args.project)
    raise PMError("usage_error", "未知命令")


def main(argv=None):
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    command = None
    try:
        args = parser.parse_args(argv)
        command = args.command
        if not command:
            parser.print_help(sys.stderr)
            raise PMError("usage_error", "缺少命令；使用 --help 查看")
        cfg = load_config(args.config)
        if command == "mcp":
            from .mcp import serve
            return serve(cfg)
        result = dispatch(cfg, args)
        emit({"ok": True, "command": command, **result})
        return 0
    except PMError as e:
        emit({"ok": False, "command": command, "error": e.to_dict()})
        return 2 if e.code == "usage_error" else 1
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        emit({"ok": False, "command": command, "error": {"code": "internal_error",
                                                          "message": f"{type(e).__name__}: {e}"}})
        return 3
