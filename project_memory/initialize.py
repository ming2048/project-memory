"""First handover: bounded reference intake, no fabricated work or test results."""
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

from . import bootstrap as B, discover as D, registry as R
from .docs import discover_docs, validate_source
from .handoff import load_events, recover
from .secrets import find_secret, redact_lines
from .util import (FileLock, PMError, atomic_write_json, atomic_write_text, compact_ts,
                   emit_frontmatter, is_abs_any, is_link_like, now_iso, parse_frontmatter, path_key,
                   read_json, read_text, safe_join)


def _block(proj):
    if not os.path.isdir(proj['path']):
        return 'directory_missing'
    cur = os.path.abspath(proj['path'])
    while cur != os.path.dirname(cur):
        if is_link_like(cur):
            return 'unsafe_project_path'
        cur = os.path.dirname(cur)
    if 'archived' in proj.get('flags', []):
        return 'archived'
    if proj.get('conflicts') or set(proj.get('flags', [])) & {'conflict', 'ledger_path_outdated', 'not_in_ledger'}:
        return 'identity_needs_verification'
    return None


def _listeners():
    """Only aggregate local occupancy; no service changes or port allocation. Windows: PowerShell; Linux: /proc/net; macOS/BSD: netstat."""
    if os.name == 'nt':
        cmd = "$ErrorActionPreference='Stop'; $tcpCount=@(Get-NetTCPConnection -State Listen).Count; $udpCount=@(Get-NetUDPEndpoint).Count; @{tcp_listeners=$tcpCount;udp_endpoints=$udpCount}|ConvertTo-Json -Compress"
        proc = subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=30,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.returncode:
            raise PMError('listener_check_failed', '未能核实实时 TCP/UDP 占用，尚未登记项目；未更改任何服务')
        return json.loads(proc.stdout)
    if sys.platform == 'linux':
        return {'tcp_listeners': _proc_net_state_count('tcp') + _proc_net_state_count('tcp6', missing_ok=True),
                'udp_endpoints': _proc_net_row_count('udp') + _proc_net_row_count('udp6', missing_ok=True)}
    return _netstat_listeners()


def _proc_net_rows(name, missing_ok=False):
    # missing_ok 用于 tcp6/udp6：内核禁用 IPv6 时这些文件不存在，计为 0；
    # IPv4（tcp/udp）读不到仍按 listener_check_failed 拒绝登记。
    try:
        with open('/proc/net/' + name, 'r', encoding='ascii', errors='replace') as stream:
            return [line.split() for line in stream.readlines()[1:] if line.strip()]
    except FileNotFoundError:
        if not missing_ok:
            raise PMError('listener_check_failed', '未能核实实时 TCP/UDP 占用，尚未登记项目；未更改任何服务')
        return []
    except OSError:
        raise PMError('listener_check_failed', '未能核实实时 TCP/UDP 占用，尚未登记项目；未更改任何服务')


def _proc_net_state_count(name, missing_ok=False):
    # /proc/net/tcp{,6} 的 state 列：十六进制 0A 表示 LISTEN
    return sum(1 for row in _proc_net_rows(name, missing_ok) if len(row) > 3 and row[3] == '0A')


def _proc_net_row_count(name, missing_ok=False):
    return len(_proc_net_rows(name, missing_ok))


def _netstat_listeners():
    out = {}
    for proto in ('tcp', 'udp'):
        try:
            proc = subprocess.run(['netstat', '-an', '-p', proto], capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=30)
        except OSError:
            raise PMError('listener_check_failed', '未能核实实时 TCP/UDP 占用，尚未登记项目；未更改任何服务')
        if proc.returncode:
            raise PMError('listener_check_failed', '未能核实实时 TCP/UDP 占用，尚未登记项目；未更改任何服务')
        lines = [line for line in proc.stdout.splitlines() if line.strip().startswith(proto)]
        if proto == 'tcp':
            out['tcp_listeners'] = sum('LISTEN' in line.upper() for line in lines)
        else:
            out['udp_endpoints'] = len(lines)
    return out


def _register_in_registry(cfg, path, name):
    """无台账模式：只写派生 registry 与项目身份；不读取、不创建、不修改任何台账文件。"""
    with R.registry_lock(cfg):
        reg = R.load_registry(cfg)
        key = path_key(path)
        for p in reg['projects'].values():
            if path_key(p['path']) == key:
                return {'status': 'already_registered', 'port_allocated': False, 'mode': 'registry_only'}
        if any(p['name'].casefold() == name.casefold() for p in reg['projects'].values()):
            raise PMError('registration_ambiguous', '已有同名项目，请先核对身份或提供明确名称')
        pid = str(uuid.uuid4())
        proj = {'project_id': pid, 'name': name, 'aliases': [], 'path': path, 'path_history': [],
                'memory_dir': R.memory_dirname(name, pid), 'first_seen': now_iso(), 'sources': [],
                'conflicts': [], 'flags': [], 'ledger_status': None, 'exists': True, 'in_ledger': False,
                'last_discovered': now_iso(), 'docs_candidates': D.discover_docs(path),
                'duplicate_name_with': [], 'nested_in': None}
        proj['state'] = R.compute_state(proj)
        reg['projects'][pid] = proj
        R.save_registry(cfg, reg)
    return {'status': 'registered', 'port_allocated': False, 'mode': 'registry_only',
            'notice': '无台账模式：仅登记到 registry，未创建或修改任何台账文件。'}


def register(cfg, path, name=None):
    if not is_abs_any(path) or not os.path.isdir(path):
        raise PMError('registration_refused', '登记需要现存项目根目录的绝对路径')
    path = os.path.normpath(path)
    # Reject drive/home roots and any link in the ancestry.
    if path_key(path) in (path_key(os.path.expanduser('~')), path_key(os.path.abspath(os.sep))):
        raise PMError('registration_refused', '不能将用户目录或磁盘根目录登记为项目')
    if os.path.dirname(path) == path:
        raise PMError('registration_refused', '不能登记磁盘根目录')
    safe_join(os.path.dirname(path), os.path.basename(path))
    cur = path
    while cur != os.path.dirname(cur):
        if is_link_like(cur):
            raise PMError('registration_refused', '项目路径含链接，不能自主登记')
        cur = os.path.dirname(cur)
    name = (name or os.path.basename(path)).strip()
    if not name or re.search(r'[|`\r\n\x00-\x1f]', name + path) or find_secret(name) or find_secret(path):
        raise PMError('registration_refused', '项目名或路径不适合安全登记')
    if not cfg.ledger_path:
        return _register_in_registry(cfg, path, name)
    checked = _listeners()
    with FileLock(cfg.ledger_path + '.project-memory.lock', timeout=cfg.lock_timeout):
        with open(cfg.ledger_path, 'rb') as stream:
            raw = stream.read()
        text = raw.decode('utf-8-sig')
        entries, _ = D.parse_ledger_text(text)
        if any(e['key'] == path_key(path) for e in entries):
            return {'status': 'already_registered', 'port_allocated': False, 'occupancy_check': checked}
        if any(e['name'].casefold() == name.casefold() for e in entries):
            raise PMError('registration_ambiguous', '已有同名项目，请先核对身份或提供明确名称')
        backup_dir = os.path.join(os.path.dirname(cfg.registry_path), 'local-backups', 'registration-' + compact_ts() + '-' + uuid.uuid4().hex[:8])
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(cfg.ledger_path, os.path.join(backup_dir, os.path.basename(cfg.ledger_path)))
        newline = '\r\n' if b'\r\n' in raw else '\n'
        section = (f'\n\n### 共享记忆首次登记 {now_iso()}\n\n'
                   '| 项目 | 本地路径 | 服务与端口 | 状态 |\n|---|---|---|---|\n'
                   f'| {name} | `{path}` | 未分配端口或项目号；未启动服务 | 待核实 |\n')
        marker='<!-- project-directory:end -->'
        if text.count(marker)==1:
            # Compact ledger contains only identity, location and associations.
            row=f'| {name} | 待分类 | 待补充项目说明 | `{path}` | 未分配 | 待初始化 | 待核实 | 本地存在 |\n'
            new=text.replace(marker,row+marker)
        else:
            new=text.rstrip()+section
        new = new.replace('\r\n', '\n').replace('\n', newline)
        if raw.startswith(b'\xef\xbb\xbf'):
            new = '\ufeff' + new
        with open(cfg.ledger_path, 'rb') as stream:
            if stream.read() != raw:
                raise PMError('ledger_changed', '登记期间台账已改变，请重新核对后重试')
        atomic_write_text(cfg.ledger_path, new)
    return {'status': 'registered', 'port_allocated': False, 'occupancy_check': checked,
            'backup_dir': backup_dir}


def initialize_project(cfg, proj):
    reason = _block(proj)
    if reason:
        return {'status': 'blocked', 'reason': reason, 'notice': '未创建或覆盖项目记忆；请先核实该项目。'}
    mdir = R.memory_dir(cfg, proj)
    marker_path = safe_join(mdir, 'initialization.json')
    if os.path.isdir(mdir):
        from .project_docs import ensure
        ensure(cfg, proj)
        events, _ = load_events(mdir)
        if events:
            current = safe_join(mdir, 'current.md')
            if not os.path.isfile(current):
                recover(cfg, proj['project_id'])
            return {'status': 'existing_history', 'notice': '沿用已有交接，不重新初始化。'}
        marker = read_json(marker_path, default={})
        if marker and all(os.path.isfile(safe_join(mdir, n)) for n in ('overview.md', 'current.md')):
            return {'status': 'already_initialized', 'notice': '已完成首次资料盘点，尚无历史交接；不覆盖现有概览。'}
        current = safe_join(mdir, 'current.md')
        if os.path.isfile(current):
            meta, _, _ = parse_frontmatter(read_text(current))
            if meta.get('revision', 0) != 0:
                return {'status': 'blocked', 'reason': 'history_missing', 'notice': '当前交接声明非零版本但历史缺失，需要恢复备份，不能重置。'}

    result = B.bootstrap_project(cfg, proj)
    # The user-approved first-read workflow grants bounded intake of root docs.
    selected = [x for x in discover_docs(proj['path']) if '/' not in x][:8]
    references = []
    for rel in selected:
        try:
            _, full = validate_source(proj['path'], rel)
            # Redact the complete document before slicing credential sections.
            text = read_text(full)
            lines = redact_lines(text.splitlines())
            excerpt = '\n'.join(lines[:24])[:3000]
            references.append({'ref': 'source/' + rel, 'excerpt': excerpt, 'line_start': 1,
                               'line_end': min(24, len(lines)), 'date_basis': 'file_modified_time',
                               'modified_at': datetime.fromtimestamp(os.path.getmtime(full)).astimezone().isoformat()})
        except (PMError, OSError):
            continue
    if references:
        B.set_sources(cfg, proj, approve=[r['ref'][7:] for r in references])
    entry_files = []
    for name in ('package.json', 'pyproject.toml', 'requirements.txt', 'Cargo.toml', 'go.mod', 'composer.json', 'Dockerfile'):
        try:
            fp = safe_join(proj['path'], name)
            if os.path.isfile(fp):
                entry_files.append(name)
        except PMError:
            continue
    assessment = {'project_id': proj['project_id'], 'created_at': now_iso(), 'status': 'needs_verification',
                  'references': references, 'entry_files': entry_files,
                  'notice': '首次有界资料盘点；只核实文件存在、读取说明，未运行测试、安装依赖或启动服务。文档时间为文件修改时间，不是工作发生时间。'}
    overview_path = safe_join(mdir, 'overview.md')
    with FileLock(os.path.join(mdir, '.lock'), timeout=cfg.lock_timeout):
        # Preserve a handoff or an independently edited overview that appeared
        # while docs were read. Only extend our untouched bootstrap template.
        events, _ = load_events(mdir)
        meta, body, _ = parse_frontmatter(read_text(overview_path))
        can_extend = (not events and meta.get('generator') == 'project_memory bootstrap'
                      and meta.get('status') == 'needs_verification' and not meta.get('initial_assessment_at'))
        if can_extend:
            appendix = ['\n## 首次资料盘点（未运行测试）\n', assessment['notice'],
                        '\n### 技术入口文件线索\n', *['- `' + n + '`（仅确认存在；技术栈及执行方式待核实）' for n in entry_files],
                        '\n### 项目说明与规则原文线索（参考资料）\n']
            for ref in references:
                appendix += [f"- {ref['ref']}，第 1–{ref['line_end']} 行；文件修改时间 {ref['modified_at']}，内容尚未现场复验。",
                             '\n    ' + ref['excerpt'].replace('\n', '\n    ') + '\n']
            if not references:
                appendix += ['- 没有找到可安全读取的根目录说明；目标、入口与当前进度待核实。']
            meta['initial_assessment_at'] = assessment['created_at']
            atomic_write_text(overview_path, emit_frontmatter(meta) + body + '\n' + '\n'.join(appendix))
        assessment['overview_updated'] = can_extend
        atomic_write_json(marker_path, assessment)
    from . import index as I
    I.sync(cfg, R.load_registry(cfg), [proj['project_id']])
    return {'status': 'initialized', 'created': result['created'], 'documents_approved': len(references),
            'overview_updated': can_extend, 'entry_files': entry_files, 'notice': assessment['notice']}


def initialize(cfg, project=None, cwd=None, register_missing=False, name=None):
    ref = project or cwd
    if not ref:
        raise PMError('project_required', '初始化需要明确的客户端项目路径或已登记项目')
    reg = R.load_registry(cfg)
    registration = None
    try:
        proj = R.resolve_project(reg, project, cwd)
    except PMError as error:
        if error.code != 'project_not_found':
            raise
        D.discover(cfg, write_report=False)
        try:
            proj = R.resolve_project(R.load_registry(cfg), project, cwd)
        except PMError as refreshed:
            if refreshed.code != 'project_not_found' or not register_missing:
                raise
            if not is_abs_any(ref):
                raise PMError('registration_refused', '未登记项目必须先确定真实项目根目录，不能只凭名称登记')
            registration = register(cfg, ref, name)
            D.discover(cfg, write_report=False)
            proj = R.resolve_project(R.load_registry(cfg), ref)
    result = initialize_project(cfg, proj)
    return {'project': R.summary(proj), 'initialization': result, 'registration': registration}
