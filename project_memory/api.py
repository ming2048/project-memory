"""CLI 与 MCP 共用的核心操作入口（同一套逻辑、同一份配置）。"""

import os

from . import bootstrap as B
from . import discover as D
from . import handoff as H
from . import index as I
from . import registry as R
from . import resume as RS


def op_discover(cfg, ledger=None, no_report=False, dry_run=False):
    return D.discover(cfg, ledger_path=ledger, write_report=not no_report, dry_run=dry_run)


def op_projects(cfg, state=None, query=None):
    return RS.projects(cfg, state=state, query=query)


def op_bootstrap(cfg, project=None, all_existing=False, approve=None):
    return B.bootstrap(cfg, project=project, all_existing=all_existing, approve=approve)


def op_sources(cfg, project, approve=None, revoke=None):
    return B.sources_cmd(cfg, project, approve=approve, revoke=revoke)


def op_search(cfg, query, project=None, limit=None, kinds=None, mode=None):
    from .util import PMError
    selected=mode or ('hybrid' if cfg.semantic_enabled else 'keyword')
    if selected not in ('keyword','semantic','hybrid'):
        raise PMError('invalid_search_mode','mode 只能为 keyword / semantic / hybrid')
    reg=R.load_registry(cfg)
    result=I.search(cfg, reg, query, project=project, limit=limit, kinds=kinds)
    result['backend']='keyword'
    if selected=='keyword': return result
    from . import semantic as S
    try:
        hits,info=S.retrieve(cfg,reg,query,project,result['limit'],kinds)
        result['results']=hits[:result['limit']] if selected=='semantic' else S.fuse(result['results'],hits,result['limit'])
        result['count']=len(result['results'])
        result['backend']=selected
        result['semantic']=info
    except (PMError, OSError, TimeoutError) as error:
        result['backend']='keyword_fallback'
        result['semantic']={'status':'unavailable','error_code':getattr(error,'code',type(error).__name__),
                            'notice':'语义不可用，已返回关键词结果；不代表语义命中。'}
    return result


def op_read(cfg, project, ref, start_line=None, end_line=None):
    return I.read(cfg, R.load_registry(cfg), project, ref, start_line, end_line)


def op_handoff(cfg, payload):
    return H.handoff(cfg, payload)


def op_recover(cfg, project):
    return H.recover(cfg, project)


def op_initialize(cfg, project=None, cwd=None, register_missing=False, name=None):
    from . import initialize as N
    return N.initialize(cfg, project, cwd, register_missing, name)


def op_resume(cfg, project=None, cwd=None, git=True, initialize=True):
    from .util import PMError
    selected_cwd = cwd or (os.getcwd() if not project else None)
    initialized = None
    if initialize:
        try:
            initialized = op_initialize(cfg, project, selected_cwd)
        except PMError as error:
            error.details['workflow'] = RS.workflow()
            if error.code == 'project_not_found':
                error.details['initialization'] = {
                    'status': 'needs_registration', 'tool': 'memory_initialize',
                    'notice': '先确认客户端真实项目根目录，再用 register_missing=true 登记并初始化；'
                              '配置了台账时只向台账追加无端口登记，未配置台账时只写 registry；'
                              '仅读取不会登记或分配端口。'}
            raise
    result = RS.resume(cfg, project=project, cwd=selected_cwd, git=git)
    result['initialization'] = initialized['initialization'] if initialized else {'status': 'disabled'}
    return result


def op_relocate(cfg, project, new_path, force=False):
    return R.relocate(cfg, project, new_path, force=force)


def op_alias(cfg, project, add=None, remove=None):
    return R.alias(cfg, project, add=add, remove=remove)


def op_reindex(cfg, rebuild=False):
    reg = R.load_registry(cfg)
    stats = I.rebuild(cfg, reg) if rebuild else I.sync(cfg, reg)
    return {"rebuilt": rebuild, "sync": stats, "index": I.index_status(cfg)}


def op_status(cfg, project=None):
    reg = R.load_registry(cfg) if os.path.isfile(cfg.registry_path) else R.empty_registry()
    by_state = {}
    for p in reg["projects"].values():
        by_state[p.get("state")] = by_state.get(p.get("state"), 0) + 1
    out = {
        "semantic": {"enabled": cfg.semantic_enabled, "model_exists": os.path.isfile(cfg.semantic_model_file),
                     "index_exists": os.path.isfile(cfg.semantic_db_path), "cpu_only": True,
                     "model_path": cfg.semantic_model_file, "index_path": cfg.semantic_db_path},
        "config": cfg.public(),
        "ledger": {"path": cfg.ledger_path, "enabled": bool(cfg.ledger_path),
                   "exists": bool(cfg.ledger_path) and os.path.isfile(cfg.ledger_path),
                   "note": "status 不读取台账内容"},
        "memory_root": {"path": cfg.memory_root, "exists": os.path.isdir(cfg.memory_root)},
        "registry": {"exists": os.path.isfile(cfg.registry_path), "projects": len(reg["projects"]),
                     "by_state": by_state, "updated_at": reg.get("updated_at")},
        "index": I.index_status(cfg),
    }
    if project:
        proj = R.resolve_project(reg, project)
        mdir = R.memory_dir(cfg, proj)
        detail = {**R.summary(proj), "memory_dir": mdir, "bootstrapped": os.path.isdir(mdir)}
        if os.path.isdir(mdir):
            events, unreadable = H.load_events(mdir)
            ok, rev, fname, _, cm = H.current_consistency(mdir, events)
            detail.update({
                "head_revision": rev, "current_revision": (cm or {}).get("revision"),
                "current_consistent": ok, "history_events": len(events),
                "conflict_events": sum(1 for _, m in events if m.get("event_status") == "conflict"),
                "unreadable_history_files": unreadable,
                "approved_sources": I.approved_sources(cfg, proj),
            })
        out["project"] = detail
    return out
