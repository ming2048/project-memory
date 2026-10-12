"""台账盘点：只按表头识别项目名/路径/状态列，只抽取必要元数据，不复制原文。

安全约束：
- 只读名称、路径、状态三个单元格；其余单元格（端口、口令、连接串、备注等）一律不读取内容。
- 状态单元格只映射为固定枚举；章节标题只用于判断“历史/归档”，不保存标题文本。
- 名称/路径单元格若出现秘密值，整行拒绝，只记录行号。
- 台账中的运行状态只是“声明”，不作为现场事实。
"""

import os
import re
import uuid

from . import registry as R
from .docs import discover_docs
from .secrets import find_secret
from .util import (PMError, atomic_write_json, atomic_write_text, compact_ts, is_abs_any, native_path, now_iso,
                   parse_frontmatter, path_key, read_text, is_link_like, key_within)

SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
ARCHIVE_RE = re.compile(r"历史|归档|废弃|停用|弃用|下线|旧项目|archive|deprecated|legacy|retired", re.I)
NAME_ARCHIVE_RE = re.compile(r"已归档|已废弃|已停用|已下线|\barchived\b|\bdeprecated\b", re.I)
NAME_ARCHIVE_STRIP_RE = re.compile(r"[（(\[]\s*(?:已归档|已废弃|已停用|已下线|archived|deprecated)\s*[）)\]]"
                                   r"|已归档|已废弃|已停用|已下线", re.I)
PORT_ONLY_RE = re.compile(r"^(端口|port)?\s*[:：#]?\s*\d{2,5}(\s*[/,、]\s*\d{2,5})*$", re.I)
FILE_EXT_RE = re.compile(r"\.(md|markdown|txt|json|ya?ml|toml|ini|conf|cfg|log|ps1|bat|cmd|sh|py|js|ts|exe|"
                         r"html?|pdf|docx?|xlsx?|csv|env|lnk|url)$", re.I)
WIN_PATH_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]:[\\/][^`|<>\"*?\r\n]*)")
# POSIX 绝对路径（至少两级，避免把中文散文里的「本地/远程」类片段当路径）；
# 不以字母数字和 :/ 结尾的前缀开头，排除 URL、协议相对地址与日期片段
POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9:/])(/(?:[^\s`|<>\"*?\r\n]+/)+[^\s`|<>\"*?\r\n]*)")
CUT_RE = re.compile(r"\s+[（(]|[（，；;。]|\s[-–—]\s|,\s|\s{2,}")

STATUS_MAP = (
    ("archived", re.compile(r"归档|历史|废弃|停用|弃用|下线|archived?|deprecated|legacy|retired", re.I)),
    ("paused", re.compile(r"暂停|搁置|冻结|挂起|paused|on.?hold|suspended", re.I)),
    ("active", re.compile(r"运行|在用|使用中|进行中|开发中|活跃|维护|正常|上线|启用|active|running|online|wip", re.I)),
)

PATH_HEADERS = ("本地路径", "项目路径", "路径", "目录", "位置")
PATH_HEADERS_EN = re.compile(r"\b(path|location|dir|directory|folder)\b", re.I)
STATUS_HEADERS = ("状态",)
STATUS_HEADERS_EN = re.compile(r"\bstatus\b", re.I)
NAME_EXCLUDE = ("号", "编号", "端口", "说明", "备注", "描述", "用途", "类型", "负责人")


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def clean_md(cell):
    s = cell
    s = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("**", "").replace("__", "").replace("~~", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


def classify_header(cells):
    """返回 (name_idx, path_idx, status_idx)。路径列优先级：本地路径/项目路径/路径 > 目录/位置。"""
    path_c, name_c, status_idx = [], [], None
    for i, raw in enumerate(cells):
        h = clean_md(raw)
        hl = h.lower()
        if any(k in h for k in PATH_HEADERS) or PATH_HEADERS_EN.search(hl):
            score = 4 if "归档位置" in h else (2 if ("路径" in h or "path" in hl) else 1)
            path_c.append((score, i))
            continue
        if any(k in h for k in STATUS_HEADERS) or STATUS_HEADERS_EN.search(hl):
            if status_idx is None:
                status_idx = i
            continue
        if any(k in h for k in NAME_EXCLUDE) or re.search(r"\b(id|port|no)\b", hl):
            continue
        if "名称" in h or "项目名" in h or hl in ("name", "project name", "名字"):
            name_c.append((3, i))
        elif hl in ("项目", "project", "仓库", "repo", "工具"):
            name_c.append((2, i))
        elif "项目" in h or "project" in hl:
            name_c.append((1, i))
    path_idx = sorted(path_c, key=lambda x: (-x[0], x[1]))[0][1] if path_c else None
    name_idx = sorted(name_c, key=lambda x: (-x[0], x[1]))[0][1] if name_c else None
    return name_idx, path_idx, status_idx


def _pathlike(s):
    """单元格中可识别的绝对路径：Windows 盘符路径两个平台都认；POSIX 绝对路径只在非 Windows 主机识别（不改变 Windows 行为）。"""
    if re.match(r"^[A-Za-z]:[\\/]", s):
        return True
    return os.name != "nt" and s.startswith("/") and not s.startswith("//")


def extract_paths(cell):
    cands = []
    for m in re.finditer(r"`([^`]+)`", cell):
        s = m.group(1).strip()
        if s.lower().startswith("file:///"):
            s = s[7:]  # 保留一个斜杠：file:///home/x → /home/x
            if re.match(r"^[A-Za-z]:[\\/]", s[1:]):  # file:///E:/x → E:/x
                s = s[1:]
        if _pathlike(s):
            cands.append(s)
    if not cands:
        text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1 \2", cell)
        text = re.sub(r"file://(?=/|[A-Za-z]:)", "", text, flags=re.I)
        for m in WIN_PATH_RE.finditer(text):
            s = CUT_RE.split(m.group(1), maxsplit=1)[0]
            cands.append(s)
        if os.name != "nt":
            for m in POSIX_PATH_RE.finditer(text):
                s = CUT_RE.split(m.group(1), maxsplit=1)[0]
                cands.append(s)
    out = []
    for s in cands:
        s = s.strip().rstrip(" .。)）]>*")
        s = s.replace("**", "")
        if _pathlike(s):
            out.append(s)
    return out


def status_enum(cell):
    # A directory named 归档 is not a declaration that the project is archived.
    s = re.sub(r"`[^`]*`", " ", cell or "")
    s = WIN_PATH_RE.sub(" ", s)
    s = clean_md(s)
    for name, rx in STATUS_MAP:
        if rx.search(s):
            return name
    return "unknown"


def parse_ledger_text(text):
    """解析台账，返回 (entries, stats)。entries 只含规范化后的元数据。"""
    lines = text.splitlines()
    stats = {"lines": len(lines), "tables_total": 0, "tables_used": 0, "rows_considered": 0, "rows_matched": 0,
             "rows_skipped": {}, "sensitive_rows_rejected": [], "tables": [], "unresolved_rows": []}
    entries = []

    def skip(reason):
        stats["rows_skipped"][reason] = stats["rows_skipped"].get(reason, 0) + 1

    in_fence, heading, i, t_no = False, "", 0, 0
    archive_root = None
    while i < len(lines):
        s = lines[i].strip()
        if "归档策略" in s and "源码入" in s:
            roots = extract_paths(s)
            if roots:
                archive_root = native_path(roots[0])
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        m = HEADING_RE.match(s)
        if m:
            heading = m.group(2)
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and SEP_RE.match(lines[i + 1].strip()):
            t_no += 1
            header = split_row(s)
            rows, j = [], i + 2
            while j < len(lines):
                if lines[j].strip().startswith("<!--") and lines[j].strip().endswith("-->"):
                    j += 1
                    continue
                if not lines[j].strip():
                    continuation = j + 1
                    while continuation < len(lines) and not lines[continuation].strip():
                        continuation += 1
                    if (continuation < len(lines) and lines[continuation].strip().startswith("|")
                            and not (continuation + 1 < len(lines) and SEP_RE.match(lines[continuation + 1].strip()))):
                        j = continuation
                    else:
                        break
                if not lines[j].strip().startswith("|"):
                    break
                rows.append((j + 1, split_row(lines[j])))
                j += 1
            ni, pi, si = classify_header(header)
            archived_section = bool(ARCHIVE_RE.search(heading))
            stats["tables_total"] += 1
            stats["tables"].append({"table": t_no, "header_line": i + 1, "rows": len(rows),
                                    "has_name_col": ni is not None, "has_path_col": pi is not None,
                                    "has_status_col": si is not None, "archived_section": archived_section})
            if ni is None:
                if pi is None:
                    skip("table_without_path_column")
                skip("table_without_project_name")
                i = j
                continue
            stats["tables_used"] += 1
            for line_no, cells in rows:
                stats["rows_considered"] += 1
                path_cell = cells[pi] if pi is not None and pi < len(cells) else ""
                name_cell = cells[ni] if ni is not None and ni < len(cells) else ""
                if find_secret(path_cell) or find_secret(name_cell):
                    stats["sensitive_rows_rejected"].append(line_no)
                    continue
                paths = extract_paths(path_cell)
                if not paths and pi is None:
                    paths = extract_paths(name_cell)
                if not paths and archived_section and archive_root and pi is not None and "归档位置" in clean_md(header[pi]):
                    relative = clean_md(path_cell)
                    if relative and relative not in ("-", "—") and not re.search(r"…|\.\.\.", relative):
                        if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", archive_root):
                            # Windows 风格归档根目录在 POSIX 主机上无法落到本机文件系统，按声明做字符串拼接
                            paths = [archive_root.rstrip("\\/") + "\\" + relative]
                        else:
                            try:
                                from .util import safe_join
                                paths = [safe_join(archive_root, relative)]
                            except PMError:
                                pass
                if not paths:
                    skip("no_windows_abs_path")
                    stats["unresolved_rows"].append({"line": line_no, "reason": "no_resolvable_project_path"})
                    continue
                path = paths[0]
                if FILE_EXT_RE.search(path.rstrip("\\/")):
                    skip("path_is_file_not_project_dir")
                    continue
                name = clean_md(name_cell)
                if extract_paths(name_cell):
                    name = re.split(r"[（(]", name, maxsplit=1)[0]
                    name = re.split(r"[A-Za-z]:[\\/]", name, maxsplit=1)[0].strip(" ：:")
                key = path_key(path)
                if (key == path_key(os.path.expanduser("~")) or re.fullmatch(r"[a-z]:/", key)
                        or re.search(r"其它工具目录|其他工具目录|静态站点群|素材项目|工作区集合", name)):
                    skip("collection_not_project")
                    stats["unresolved_rows"].append({"line": line_no, "reason": "collection_not_project"})
                    continue
                name_from_path = False
                if not name or PORT_ONLY_RE.match(name) or is_abs_any(name) or name in ("-", "—"):
                    name = os.path.basename(path.rstrip("\\/").replace("/", "\\").rstrip("\\")) or path
                    name = name.split("\\")[-1]
                    name_from_path = True
                name_archive_hint = bool(NAME_ARCHIVE_RE.search(name[:80]))
                if not name_from_path:
                    name = re.sub(r"\s+", " ", NAME_ARCHIVE_STRIP_RE.sub("", name)).strip(" -")
                name = (name or os.path.basename(path))[:80]
                st = status_enum(cells[si]) if si is not None and si < len(cells) else "unknown"
                archived_hint = archived_section or "~~" in name_cell or name_archive_hint
                entries.append({
                    "path": native_path(path), "key": path_key(path), "name": name,
                    "name_from_path": name_from_path, "line": line_no, "table": t_no,
                    "status": st, "archived_hint": archived_hint,
                    "multi_path": len({path_key(p) for p in paths}) > 1,
                })
                stats["rows_matched"] += 1
            i = j
            continue
        i += 1
    return entries, stats


def scan_memory_root(cfg):
    """从 memory_root/*/overview.md 的 frontmatter 恢复 path→(project_id, 目录名)，用于 registry 丢失后保持 UUID。"""
    out = {}
    root = cfg.memory_root
    if not os.path.isdir(root):
        return out
    for e in os.scandir(root):
        if not e.is_dir(follow_symlinks=False) or is_link_like(e.path):
            continue
        from .util import safe_join, read_json
        try:
            identity = read_json(safe_join(e.path, "identity.json"), default={}) or {}
            ov = safe_join(e.path, "overview.md")
        except (PMError, ValueError, OSError):
            continue
        if not os.path.isfile(ov):
            continue
        try:
            with open(ov, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue
        meta, _, _ = parse_frontmatter(head)
        pid, p = identity.get("project_id") or meta.get("project_id"), identity.get("path") or meta.get("path")
        if isinstance(pid, str) and isinstance(p, str):
            try:
                uuid.UUID(pid)
            except ValueError:
                continue
            out[path_key(p)] = (pid, e.name)
            for old in identity.get("path_history", []):
                if isinstance(old, dict) and isinstance(old.get("path"), str):
                    out.setdefault(path_key(old["path"]), (pid, e.name))
    return out


def _apply_ledger(cfg, proj, ents, outdated, ledger_file):
    names, seen = [], set()
    for e in ents:
        if e["name_from_path"]:
            continue
        k = e["name"].casefold()
        if k not in seen:
            seen.add(k)
            names.append({"name": e["name"], "line": e["line"]})
    conflicts = []
    if len(names) > 1:
        conflicts.append({"type": "name_mismatch", "values": names})
    primary = names[0]["name"] if names else ents[0]["name"]
    proj["name"] = primary
    proj["ledger_alt_names"] = [n["name"] for n in names[1:]]

    archived_rows = [e["line"] for e in ents if e["archived_hint"] or e["status"] == "archived"]
    live_rows = [e["line"] for e in ents
                 if e["status"] in ("active", "paused") and not e["archived_hint"]]
    flags = set()
    if archived_rows and live_rows:
        flags.add("status_conflict")
        conflicts.append({"type": "status_mismatch", "archived_lines": archived_rows, "active_lines": live_rows})
        ledger_status = "conflict"
    elif archived_rows:
        flags.add("archived")
        ledger_status = "archived"
    else:
        sts = {e["status"] for e in ents if e["status"] != "unknown"}
        ledger_status = sts.pop() if len(sts) == 1 else ("conflict" if sts else "unknown")
        if ledger_status == "conflict":
            flags.add("status_conflict")
            conflicts.append({"type": "status_mismatch", "lines": [e["line"] for e in ents]})
    if any(e["multi_path"] for e in ents):
        conflicts.append({"type": "multiple_paths_in_cell", "lines": [e["line"] for e in ents if e["multi_path"]]})
    if outdated:
        flags.add("ledger_path_outdated")
        conflicts.append({"type": "ledger_path_outdated", "ledger_path": ents[0]["path"],
                          "lines": [e["line"] for e in ents]})
    exists = os.path.isdir(proj["path"])
    if not exists:
        flags.add("missing")
    if conflicts:
        flags.add("conflict")
    if any(e["name_from_path"] for e in ents) and not names:
        flags.add("name_from_path")
    proj.setdefault("sources", [])
    proj["sources"] = [s for s in proj["sources"] if s.get("file") != ledger_file]
    proj["sources"] += [{"file": ledger_file, "line": e["line"], "table": e["table"]} for e in ents]
    proj["conflicts"] = [c for c in proj.get("conflicts", []) if c.get("type") == "ledger_lists_old_and_new_path"]
    proj["conflicts"] += conflicts
    proj["flags"] = sorted(flags | ({"conflict"} if proj["conflicts"] else set()))
    proj["ledger_status"] = ledger_status
    proj["exists"] = exists
    proj["in_ledger"] = True
    proj["last_discovered"] = now_iso()
    proj["docs_candidates"] = discover_docs(proj["path"]) if exists else []


def discover(cfg, ledger_path=None, write_report=True, dry_run=False):
    lp = ledger_path or cfg.ledger_path
    if not lp:
        # 无台账模式：返回空集合、不报错，也不读取/创建/修改任何台账文件或 registry
        counts = {s: 0 for s in R.STATES}
        counts["total"] = 0
        return {"ledger": {"mode": "disabled", "path": None,
                           "note": "未配置 ledger_path（无台账模式）：不读取、不创建任何台账文件"},
                "counts": counts, "created": 0, "registry_path": cfg.registry_path, "dry_run": dry_run,
                "reports": {}, "projects": [],
                "notice": "无台账模式：discover 返回空集合；项目通过 initialize --register 登记到 registry。"}
    lp = os.path.abspath(lp)
    if not os.path.isfile(lp):
        raise PMError("ledger_not_found", "台账文件不存在", path=lp)
    entries, stats = parse_ledger_text(read_text(lp))
    groups = {}
    for e in entries:
        groups.setdefault(e["key"], []).append(e)

    with R.registry_lock(cfg):
        reg = R.load_registry(cfg)
        projects = reg["projects"]
        by_key = {path_key(p["path"]): p for p in projects.values()}
        by_hist = {}
        for p in projects.values():
            for h in p.get("path_history", []):
                by_hist.setdefault(path_key(h["path"]), p)
        mem = scan_memory_root(cfg)
        touched, created = {}, 0
        for key, ents in groups.items():
            proj, outdated = by_key.get(key), False
            if proj is None and key in by_hist:
                proj, outdated = by_hist[key], True
            if proj is not None and proj["project_id"] in touched:
                # 台账同时列出了迁移前后的路径：合并到同一项目，并记冲突
                proj["sources"] += [{"file": lp, "line": e["line"], "table": e["table"]} for e in ents]
                proj["conflicts"].append({"type": "ledger_lists_old_and_new_path",
                                          "lines": [e["line"] for e in ents]})
                proj["flags"] = sorted(set(proj["flags"]) | {"conflict", "ledger_path_outdated"})
                continue
            if proj is None:
                pid, mdir = mem.get(key, (None, None))
                if not pid or pid in projects:
                    pid = str(uuid.uuid4())
                    mdir = None
                proj = {"project_id": pid, "name": ents[0]["name"], "aliases": [], "path": ents[0]["path"],
                        "path_history": [], "memory_dir": mdir or R.memory_dirname(ents[0]["name"], pid),
                        "first_seen": now_iso()}
                if mdir:
                    from .util import safe_join, read_json
                    identity = read_json(safe_join(R.memory_dir(cfg, proj), "identity.json"), default={}) or {}
                    if identity.get("project_id") == pid:
                        proj["aliases"] = identity.get("aliases", [])
                        proj["path_history"] = identity.get("path_history", [])
                        if isinstance(identity.get("path"), str) and is_abs_any(identity["path"]):
                            proj["path"] = identity["path"]
                            outdated = path_key(proj["path"]) != key
                projects[pid] = proj
                by_key[path_key(proj["path"])] = proj
                created += 1
            _apply_ledger(cfg, proj, ents, outdated, lp)
            touched[proj["project_id"]] = True

        for pid, p in projects.items():
            if pid in touched:
                continue
            p["in_ledger"] = False
            p["exists"] = os.path.isdir(p["path"])
            fl = set(p.get("flags", [])) | {"not_in_ledger"}
            fl.discard("missing")
            if not p["exists"]:
                fl.add("missing")
            p["flags"] = sorted(fl)

        # 同名不同路径：只做标记（解析时返回歧义清单），不合并
        by_name = {}
        for p in projects.values():
            by_name.setdefault(p["name"].casefold(), []).append(p["project_id"])
        keys = {pid: path_key(p["path"]) for pid, p in projects.items()}
        for pid, p in projects.items():
            dup = [x for x in by_name[p["name"].casefold()] if x != pid]
            p["duplicate_name_with"] = dup
            fl = set(p["flags"]) - {"duplicate_name"}
            if dup:
                fl.add("duplicate_name")
            p["flags"] = sorted(fl)
            parents = [(len(k), o) for o, k in keys.items() if o != pid and keys[pid] != k and key_within(keys[pid], k)]
            p["nested_in"] = max(parents)[1] if parents else None
            p["state"] = R.compute_state(p)
        if not dry_run:
            R.save_registry(cfg, reg)

    report = build_report(lp, stats, reg, created)
    paths = {}
    if write_report and not dry_run:
        paths = write_reports(cfg, report)
    return {"ledger": report["ledger"], "counts": report["counts"], "created": created,
            "registry_path": cfg.registry_path, "dry_run": dry_run, "reports": paths,
            "projects": report["projects"],
            "notice": "台账中的状态只是声明，不是现场事实；目录存在性为本次实查结果。"}


def build_report(lp, stats, reg, created):
    projs = []
    counts = {s: 0 for s in R.STATES}
    for p in sorted(reg["projects"].values(), key=lambda x: (x["state"], x["name"].casefold())):
        counts[p["state"]] = counts.get(p["state"], 0) + 1
        projs.append({
            "project_id": p["project_id"], "name": p["name"], "path": p["path"], "state": p["state"],
            "flags": p.get("flags", []), "exists": p.get("exists"), "ledger_status_claim": p.get("ledger_status"),
            "source_lines": [s["line"] for s in p.get("sources", [])],
            "conflicts": p.get("conflicts", []), "duplicate_name_with": p.get("duplicate_name_with", []),
            "docs_candidates": p.get("docs_candidates", []), "memory_dir": p.get("memory_dir"),
        })
    counts["total"] = len(projs)
    ledger = {k: v for k, v in stats.items() if k != "tables"}
    ledger["path"] = lp
    ledger["tables"] = stats["tables"]
    return {"generated_at": now_iso(), "ledger": ledger, "counts": counts, "created": created, "projects": projs,
            "notice": "脱敏盘点报告：只含项目名、路径、行号、枚举状态；不含台账原文、端口、口令或连接串。"}


STATE_ZH = {"active": "可用", "archived": "归档/历史", "missing": "目录缺失", "needs_verification": "需核实"}


def render_report_md(rep):
    L = rep["ledger"]
    c = rep["counts"]
    out = [
        "# 项目盘点报告（脱敏）", "",
        f"- 生成时间：{rep['generated_at']}",
        f"- 台账：`{L['path']}`（共 {L['lines']} 行；表格 {L['tables_total']} 个，含路径列 {L['tables_used']} 个）",
        f"- 匹配行：{L['rows_matched']} / 考察行 {L['rows_considered']}；因敏感内容拒绝的行号：{L['sensitive_rows_rejected'] or '无'}",
        f"- 跳过原因统计：{'；'.join(f'{k} {v} 行/表' for k, v in L['rows_skipped'].items()) or '无'}",
        f"- 项目总数 {c['total']}：可用 {c.get('active', 0)}，需核实 {c.get('needs_verification', 0)}，"
        f"目录缺失 {c.get('missing', 0)}，归档/历史 {c.get('archived', 0)}；本次新增 {rep['created']}",
        "", "> 台账中的运行状态只是声明，不是现场事实。本报告不含端口、口令、连接串或台账原文。", "",
        "| 状态 | 项目 | 路径 | 来源行号 | 标记 | 候选文档数 |",
        "|---|---|---|---|---|---|",
    ]
    for p in rep["projects"]:
        name = p["name"].replace("|", "\\|")
        out.append(f"| {STATE_ZH.get(p['state'], p['state'])} | {name} | `{p['path']}` | "
                   f"{', '.join(map(str, p['source_lines'])) or '-'} | {', '.join(p['flags']) or '-'} | "
                   f"{len(p['docs_candidates'])} |")
    conf = [p for p in rep["projects"] if p["conflicts"]]
    if conf:
        out += ["", "## 冲突与需核实", ""]
        for p in conf:
            for cf in p["conflicts"]:
                out.append(f"- **{p['name']}**（{p['project_id'][:8]}）：{cf['type']} → "
                           f"{_conflict_desc(cf)}")
    dup = [p for p in rep["projects"] if p["duplicate_name_with"]]
    if dup:
        out += ["", "## 重名项目（解析时需用 project_id 或路径）", ""]
        for p in dup:
            out.append(f"- {p['name']}：`{p['path']}`（{p['project_id']}）")
    return "\n".join(out) + "\n"


def _conflict_desc(cf):
    if cf["type"] == "name_mismatch":
        return "；".join(f"第{v['line']}行“{v['name']}”" for v in cf["values"])
    parts = []
    for k, v in cf.items():
        if k != "type":
            parts.append(f"{k}={v}")
    return "，".join(parts)


def write_reports(cfg, rep):
    os.makedirs(cfg.report_dir, exist_ok=True)
    ts = compact_ts()
    md = render_report_md(rep)
    out = {}
    for stem in (f"discover-{ts}", "discover-latest"):
        jp = os.path.join(cfg.report_dir, stem + ".json")
        mp = os.path.join(cfg.report_dir, stem + ".md")
        atomic_write_json(jp, rep)
        atomic_write_text(mp, md)
        out.setdefault("json", jp)
        out.setdefault("markdown", mp)
    return out
