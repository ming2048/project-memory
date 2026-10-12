"""bootstrap：为存在且未归档的项目建立 overview.md / current.md / history/，只写待核实信息，绝不覆盖。
sources：显式批准/撤销项目内文档进入索引（未批准的文档永不索引）。
"""

import os

from . import index as I
from . import registry as R
from .docs import validate_source
from .util import FileLock, PMError, atomic_write_json, emit_frontmatter, now_iso, read_json, write_new_text, safe_join


def _overview(cfg, proj):
    meta = {
        "project_id": proj["project_id"], "name": proj["name"], "path": proj["path"],
        "doc_type": "overview", "status": "needs_verification", "created_at": now_iso(),
        "generator": "project_memory bootstrap",
    }
    lines = [ln for ln in (f"  - 台账第 {s['line']} 行（表 {s['table']}）" for s in proj.get("sources", []))]
    docs = proj.get("docs_candidates", [])
    body = [
        f"# {proj['name']} 项目概览（待核实）", "",
        "> 本文件由 bootstrap 根据台账元数据自动生成，只包含待核实信息；"
        "未记录任何已完成的工作、测试或运行状态。请在实际接手后逐项核实并改写。", "",
        "## 基本信息（待核实）", "",
        f"- 项目 ID：`{proj['project_id']}`",
        f"- 本地路径：`{proj['path']}`",
        f"- 目录存在性：生成时实查为{'存在' if proj.get('exists') else '不存在'}",
        f"- 台账状态声明：{proj.get('ledger_status') or 'unknown'}（仅为台账声明，不是现场事实）",
        "- 台账来源：", *(lines or ["  - 无"]), "",
        "## 目标与范围", "", "- 待核实", "",
        "## 技术栈与入口", "", "- 待核实", "",
        "## 关键约定与边界", "", "- 待核实", "",
        "## 候选文档（未索引，需用 `sources --approve` 显式批准）", "",
        *([f"- `{d}`" for d in docs] or ["- 未发现"]), "",
    ]
    return emit_frontmatter(meta) + "\n" + "\n".join(body)


def _current(proj):
    meta = {
        "project_id": proj["project_id"], "doc_type": "current", "revision": 0, "updated_at": now_iso(),
        "status": "no_handoff_yet", "source_event": None,
    }
    body = [
        f"# {proj['name']} 当前交接（revision 0）", "",
        "> 尚无交接记录。此处没有任何已完成工作或验证结果；"
        "第一次收尾时请用 `handoff`（expected_revision=0）写入。", "",
        "## 摘要", "", "- 待核实", "",
        "## 待办", "", "- 待核实", "",
    ]
    return emit_frontmatter(meta) + "\n" + "\n".join(body)


def _eligible(proj):
    if not proj.get("exists") or not os.path.isdir(proj["path"]):
        return "directory_missing"
    if "archived" in proj.get("flags", []):
        return "archived"
    return None


def bootstrap_project(cfg, proj, approve=None):
    reason = _eligible(proj)
    if reason:
        return {"project": R.summary(proj), "status": "skipped", "reason": reason}
    mdir = R.memory_dir(cfg, proj)
    created, kept = [], []
    with FileLock(os.path.join(mdir, ".lock"), timeout=cfg.lock_timeout):
        os.makedirs(safe_join(mdir, "history"), exist_ok=True)
        for name, text in (("overview.md", _overview(cfg, proj)), ("current.md", _current(proj))):
            try:
                write_new_text(safe_join(mdir, name), text)
                created.append(name)
            except FileExistsError:
                kept.append(name)
        R.persist_identity(cfg, proj)
    approved = []
    from .project_docs import ensure
    created += ensure(cfg, proj)
    for rel in approve or []:
        approved += set_sources(cfg, proj, approve=[rel])["approved_now"]
    return {"project": R.summary(proj), "status": "ok", "memory_dir": mdir, "created": created,
            "kept_existing": kept, "approved_sources": approved}


def bootstrap(cfg, project=None, all_existing=False, approve=None):
    reg = R.load_registry(cfg)
    if bool(project) == bool(all_existing):
        raise PMError("invalid_arguments", "必须且只能指定 --project 或 --all-existing 之一")
    if approve and not project:
        raise PMError("invalid_arguments", "--approve-doc 只能与 --project 一起使用")
    if project:
        proj = R.resolve_project(reg, project)
        reason = _eligible(proj)
        if reason:
            raise PMError("bootstrap_refused", "项目目录不存在或已归档，拒绝建立记忆", reason=reason,
                          project=R.summary(proj))
        results = [bootstrap_project(cfg, proj, approve)]
    else:
        results = [bootstrap_project(cfg, p) for p in reg["projects"].values()]
    ids = [r["project"]["project_id"] for r in results if r["status"] == "ok"]
    sync = I.sync(cfg, reg, ids) if ids else None
    return {"results": results, "bootstrapped": len(ids),
            "skipped": sum(1 for r in results if r["status"] == "skipped"), "index_sync": sync}


def set_sources(cfg, proj, approve=None, revoke=None):
    mdir = R.memory_dir(cfg, proj)
    if not os.path.isdir(mdir):
        raise PMError("not_bootstrapped", "项目尚未 bootstrap，无法登记 sources", project=R.summary(proj))
    path = os.path.join(mdir, "sources.json")
    with FileLock(os.path.join(mdir, ".lock"), timeout=cfg.lock_timeout):
        data = read_json(path, default={}) or {}
        cur = [s for s in data.get("approved", []) if isinstance(s, str)]
        now_ok = []
        for rel in approve or []:
            rel_n, _ = validate_source(proj["path"], rel)
            if rel_n not in cur:
                cur.append(rel_n)
            now_ok.append(rel_n)
        removed = []
        for rel in revoke or []:
            rel_n = rel.replace("\\", "/").strip().strip("/")
            if rel_n in cur:
                cur.remove(rel_n)
                removed.append(rel_n)
        if approve or revoke:
            atomic_write_json(path, {"approved": cur, "updated_at": now_iso(),
                                     "note": "显式批准进入索引的项目内文档（相对项目根）"})
    return {"project": R.summary(proj), "approved": cur, "approved_now": now_ok, "revoked": removed}


def sources_cmd(cfg, project, approve=None, revoke=None):
    reg = R.load_registry(cfg)
    proj = R.resolve_project(reg, project)
    res = set_sources(cfg, proj, approve, revoke)
    res["index_sync"] = I.sync(cfg, reg, [proj["project_id"]])
    res["candidates"] = proj.get("docs_candidates", [])
    return res
