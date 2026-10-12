"""通用工具：错误类型、时间、原子写、跨进程锁、frontmatter、路径安全。"""

import datetime
import json
import os
import re
import stat
import tempfile
import time

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - 非 Windows 平台
    import fcntl


class PMError(Exception):
    """带结构化错误码的业务错误，CLI 与 MCP 共用。"""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        d = {"code": self.code, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d


# ---------------------------------------------------------------- 时间

def now():
    return datetime.datetime.now().astimezone()


def now_iso():
    return now().isoformat(timespec="seconds")


def compact_ts(dt=None):
    return (dt or now()).strftime("%Y%m%dT%H%M%S%z")


def mtime_iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- JSON

def dumps(obj, indent=2):
    return json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=False)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


# ---------------------------------------------------------------- 文件写入

def _replace_with_retry(src, dst, attempts=50):
    # Windows 上目标被其他进程短暂打开时 os.replace 会 PermissionError，重试即可。
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05)


def atomic_write_text(path, text):
    """写临时文件 + fsync + os.replace，读者永远看不到半截文件。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj):
    atomic_write_text(path, dumps(obj) + "\n")


def write_new_text(path, text):
    """独占创建（'x'），已存在则抛 FileExistsError，保证不覆盖。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def atomic_create_text(path, text):
    """原子且不覆盖地创建文件：先写完整临时文件再改名；目标已存在则 FileExistsError。
    读者要么看不到该文件，要么看到完整内容（崩溃不会留下半截历史）。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "nt":
            os.rename(tmp, path)  # Windows 上目标存在时抛 FileExistsError，不会覆盖
        else:  # pragma: no cover
            os.link(tmp, path)
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace", newline=None) as f:
        return f.read()


# ---------------------------------------------------------------- 锁

class FileLock:
    """跨进程排他锁：Windows 用 msvcrt.locking，其他平台用 fcntl.flock。进程退出时由 OS 释放。"""

    def __init__(self, path, timeout=30.0):
        self.path = path
        self.timeout = timeout
        self.f = None

    def __enter__(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.f = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self.f.close()
                    self.f = None
                    raise PMError("lock_timeout", "等待锁超时", lock=os.path.basename(self.path))
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.f is None:
            return
        try:
            if os.name == "nt":
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        finally:
            self.f.close()
            self.f = None


# ---------------------------------------------------------------- frontmatter
# 格式：--- / key: <JSON 值> / ---。JSON 标量与数组同时是合法 YAML，Obsidian 可直接识别。

def emit_frontmatter(meta):
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def parse_frontmatter(text):
    """返回 (meta, body, body_first_line_no)；行号从 1 开始。"""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text, 1
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta = {}
            for raw in lines[1:i]:
                if ":" not in raw:
                    continue
                k, v = raw.split(":", 1)
                v = v.strip()
                try:
                    meta[k.strip()] = json.loads(v)
                except ValueError:
                    meta[k.strip()] = v
            return meta, "".join(lines[i + 1:]), i + 2
    return {}, text, 1


# ---------------------------------------------------------------- 路径

_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def is_abs_any(p):
    """跨平台绝对路径判断：Windows 盘符路径或 POSIX 绝对路径都算。"""
    return bool(p) and (bool(_WIN_ABS.match(p)) or p.startswith("/") or p.startswith("\\\\"))


def path_key(p):
    """规范化路径比较键：统一正斜杠、去尾斜杠；Windows 风格大小写不敏感。"""
    s = (p or "").strip().strip("`").strip()
    s = s.replace("\\", "/")
    s = re.sub(r"/{2,}", "/", s) if not s.startswith("//") else "//" + re.sub(r"/{2,}", "/", s[2:])
    parts = []
    for seg in s.split("/"):
        if seg == ".":
            continue
        if seg == ".." and parts and parts[-1] not in ("", ".."):
            parts.pop()
            continue
        parts.append(seg)
    s = "/".join(parts)
    if len(s) > 1 and s.endswith("/") and not re.match(r"^[A-Za-z]:/$", s):
        s = s.rstrip("/")
    if re.match(r"^[A-Za-z]:$", s):
        s += "/"
    if _WIN_ABS.match(s) or os.name == "nt":
        s = s.casefold()
    return s


def key_within(child, parent):
    if child == parent:
        return True
    p = parent if parent.endswith("/") else parent + "/"
    return child.startswith(p)


def native_path(p):
    p = p.strip()
    if os.name == "nt":
        p = p.replace("/", "\\")
        if len(p) > 3 and p.endswith("\\"):
            p = p.rstrip("\\")
    return p


def is_link_like(p):
    """symlink、junction 以及任何 reparse point 都视为链接。"""
    try:
        st = os.lstat(p)
    except (FileNotFoundError, NotADirectoryError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return True
    isj = getattr(os.path, "isjunction", None)
    return bool(isj and isj(p))


def _real(p):
    return os.path.normcase(os.path.realpath(p))


def safe_join(base, rel):
    """把相对路径安全地拼到 base 下：拒绝绝对路径、..、盘符/ADS、任一级链接、以及 realpath 越界。"""
    if not isinstance(rel, str) or not rel.strip():
        raise PMError("path_rejected", "路径为空")
    if is_abs_any(rel) or re.match(r"^[A-Za-z]:", rel):
        raise PMError("path_rejected", "只接受相对路径", path=rel)
    parts = [x for x in re.split(r"[\\/]+", rel) if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts) or any(":" in x for x in parts):
        raise PMError("path_rejected", "路径越界或非法", path=rel)
    if is_link_like(base):
        raise PMError("symlink_rejected", "根目录是链接", path=rel)
    cur = os.path.abspath(base)
    for part in parts:
        cur = os.path.join(cur, part)
        if is_link_like(cur):
            raise PMError("symlink_rejected", "路径中包含 symlink/junction", path=rel)
    rb, rc = _real(base), _real(cur)
    try:
        inside = os.path.commonpath([rb, rc]) == rb
    except ValueError:
        inside = False
    if not inside:
        raise PMError("path_rejected", "解析后越出授权目录", path=rel)
    return cur
