"""有界文档发现与 source 校验：只看项目根的 README*/AGENTS.md/CLAUDE.md 和 docs/ 最多两层，只返回路径。"""

import os
import re

from .util import PMError, is_link_like, safe_join

ALLOWED_EXT = {".md", ".markdown", ".txt", ".rst"}
DENY_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", ".idea", ".vscode",
             "site-packages", ".tox", ".mypy_cache", ".pytest_cache"}
DENY_NAME_RE = re.compile(r"(^\.env)|secret|credential|password|passwd|token|apikey|api[_\-]key|"
                          r"private[_\-]?key|id_rsa|\.pem$|\.key$|\.pfx$|\.p12$|口令|密码|密钥", re.I)
ROOT_NAME_RE = re.compile(r"^(readme[^/\\]*|agents\.md|claude\.md)$", re.I)
MAX_CANDIDATES = 40
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def _denied(name):
    return name.lower() in DENY_DIRS or bool(DENY_NAME_RE.search(name))


def _ok_file(entry):
    if _denied(entry.name) or is_link_like(entry.path):
        return False
    if not entry.is_file(follow_symlinks=False):
        return False
    return os.path.splitext(entry.name)[1].lower() in ALLOWED_EXT


def _scan(path):
    try:
        return sorted(os.scandir(path), key=lambda e: e.name.lower())
    except OSError:
        return []


def discover_docs(root):
    """返回相对路径列表（正斜杠）；不读取任何文件内容。"""
    out = []
    if not os.path.isdir(root) or is_link_like(root):
        return out
    for e in _scan(root):
        if ROOT_NAME_RE.match(e.name) and _ok_file(e):
            out.append(e.name)
    docs = os.path.join(root, "docs")
    if os.path.isdir(docs) and not is_link_like(docs):
        for e in _scan(docs):
            if len(out) >= MAX_CANDIDATES:
                break
            if _ok_file(e):
                out.append("docs/" + e.name)
            elif e.is_dir(follow_symlinks=False) and not _denied(e.name) and not is_link_like(e.path):
                for e2 in _scan(e.path):
                    if len(out) >= MAX_CANDIDATES:
                        break
                    if _ok_file(e2):
                        out.append(f"docs/{e.name}/{e2.name}")
    return out[:MAX_CANDIDATES]


def validate_source(project_root, rel):
    """校验一个待批准/待读取的 source：位于项目根内、无链接、扩展名白名单、拒绝敏感文件名与目录。"""
    rel = (rel or "").replace("\\", "/").strip()
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(_denied(p) for p in parts):
        raise PMError("source_rejected", "路径包含被拒绝的目录或敏感文件名", path=rel)
    if os.path.splitext(rel)[1].lower() not in ALLOWED_EXT:
        raise PMError("source_rejected", "只允许 .md/.markdown/.txt/.rst 文档", path=rel)
    full = safe_join(project_root, rel)
    if not os.path.isfile(full):
        raise PMError("source_missing", "文件不存在", path=rel)
    if os.path.getsize(full) > MAX_SOURCE_BYTES:
        raise PMError("source_rejected", "文件过大（>2MB）", path=rel)
    return "/".join(parts), full
