"""派生项目注册表（registry JSON）：UUID 持久、alias、relocate、项目解析。

注册表不是新的端口台账：不含端口、口令、运行状态原文，只有台账元数据的脱敏摘要和本系统自己的状态。
"""

import os
import re

from .util import (FileLock, PMError, atomic_write_json, is_abs_any, key_within, native_path, now_iso,
                   path_key, read_json, safe_join)
from .secrets import find_secret

SCHEMA = 1
STATES = ("active", "archived", "missing", "needs_verification")


def empty_registry():
    return {
        "schema": SCHEMA,
        "note": "派生注册表：由台账元数据与 memory_root 生成，可重建；不是端口台账，不含秘密。",
        "updated_at": None,
        "projects": {},
    }


def load_registry(cfg):
    data = read_json(cfg.registry_path)
    if data is None:
        return empty_registry()
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        raise PMError("registry_invalid", "registry JSON 结构不正确", path=cfg.registry_path)
    return data


def save_registry(cfg, reg):
    reg["updated_at"] = now_iso()
    atomic_write_json(cfg.registry_path, reg)


def registry_lock(cfg):
    return FileLock(cfg.registry_path + ".lock", timeout=cfg.lock_timeout)


def memory_dirname(name, pid):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name or "project").strip(" ._") or "project"
    return f"{safe[:40]}__{pid[:8]}"


def memory_dir(cfg, proj):
    return safe_join(cfg.memory_root, proj["memory_dir"])


def persist_identity(cfg, proj):
    mdir = memory_dir(cfg, proj)
    if os.path.isdir(mdir):
        atomic_write_json(safe_join(mdir, "identity.json"), {
            "project_id": proj["project_id"], "name": proj["name"], "path": proj["path"],
            "aliases": proj.get("aliases", []), "path_history": proj.get("path_history", []),
            "memory_dir": proj["memory_dir"], "updated_at": now_iso(),
        })


def summary(proj):
    return {
        "project_id": proj["project_id"],
        "name": proj["name"],
        "path": proj["path"],
        "state": proj.get("state"),
    }


def _names(proj):
    out = [proj.get("name") or ""]
    out += proj.get("aliases") or []
    out += proj.get("ledger_alt_names") or []
    return {n.casefold() for n in out if n}


def resolve_by_cwd(reg, cwd):
    key = path_key(cwd)
    hits = []
    for p in reg["projects"].values():
        pk = path_key(p["path"])
        if key_within(key, pk):
            hits.append((len(pk), p))
    if not hits:
        raise PMError("project_not_found", "该路径不属于任何已登记项目", cwd=cwd)
    hits.sort(key=lambda x: -x[0])
    best = [p for n, p in hits if n == hits[0][0]]
    if len(best) > 1:
        raise PMError("ambiguous_project", "多个项目共享同一路径，请用 project_id 指定",
                      candidates=[summary(p) for p in best])
    return best[0]


def resolve_project(reg, ref=None, cwd=None):
    """按 project_id / 绝对路径（最长前缀）/ 名称或别名 解析；多个同名必须返回歧义清单。"""
    projects = reg["projects"]
    if ref:
        ref = ref.strip()
        if ref in projects:
            return projects[ref]
        if is_abs_any(ref):
            return resolve_by_cwd(reg, ref)
        if re.fullmatch(r"[0-9a-fA-F\-]{8,36}", ref):
            pref = [p for pid, p in projects.items() if pid.lower().startswith(ref.lower())]
            if len(pref) == 1:
                return pref[0]
        folded = ref.casefold()
        exact = [p for p in projects.values() if folded in _names(p)]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise PMError("ambiguous_project", f"名称“{ref}”匹配到多个项目，请改用 project_id 或路径",
                          candidates=[summary(p) for p in exact])
        similar = [summary(p) for p in projects.values()
                   if any(folded in n or n in folded for n in _names(p))][:10]
        raise PMError("project_not_found", f"未找到项目“{ref}”", suggestions=similar)
    if cwd:
        return resolve_by_cwd(reg, cwd)
    raise PMError("project_required", "需要提供 project（id/名称/路径）或 cwd")


def relocate(cfg, ref, new_path, force=False):
    """显式迁移项目路径：保留 project_id 与记忆目录，旧路径进入 path_history。"""
    if find_secret(new_path):
        raise PMError("secret_detected", "路径疑似含秘密值，已拒绝")
    if not is_abs_any(new_path):
        raise PMError("invalid_path", "新路径必须是绝对路径")
    new_path = native_path(new_path)
    with registry_lock(cfg):
        reg = load_registry(cfg)
        proj = resolve_project(reg, ref)
        nk = path_key(new_path)
        for p in reg["projects"].values():
            if p is not proj and path_key(p["path"]) == nk:
                raise PMError("path_in_use", "新路径已被其他项目使用", other=summary(p))
        exists = os.path.isdir(new_path)
        if not exists and not force:
            raise PMError("path_missing", "新路径不存在（确需登记可加 --force）", path=new_path)
        old = proj["path"]
        if path_key(old) != nk:
            hist = proj.setdefault("path_history", [])
            hist.append({"path": old, "until": now_iso()})
            proj["path"] = new_path
        proj["exists"] = exists
        proj["flags"] = [f for f in proj.get("flags", []) if f not in ("missing",)]
        if not exists:
            proj["flags"].append("missing")
        proj["state"] = compute_state(proj)
        save_registry(cfg, reg)
        persist_identity(cfg, proj)
        return {"project": summary(proj), "old_path": old, "new_path": new_path}


def alias(cfg, ref, add=None, remove=None):
    if any(find_secret(a) for a in (add or []) + (remove or [])):
        raise PMError("secret_detected", "别名疑似含秘密值，已拒绝")
    with registry_lock(cfg):
        reg = load_registry(cfg)
        proj = resolve_project(reg, ref)
        al = proj.setdefault("aliases", [])
        for a in add or []:
            a = a.strip()
            if a and a not in al:
                al.append(a)
        for a in remove or []:
            if a in al:
                al.remove(a)
        save_registry(cfg, reg)
        persist_identity(cfg, proj)
        return {"project": summary(proj), "aliases": al}


def compute_state(proj):
    flags = set(proj.get("flags", []))
    if "archived" in flags:
        return "archived"
    if "missing" in flags:
        return "missing"
    if flags & {"conflict", "ledger_path_outdated", "status_conflict", "not_in_ledger"}:
        return "needs_verification"
    return "active"
