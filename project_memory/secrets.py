"""敏感内容检测。

两级：
- is_sensitive_line：宽口径，只要出现口令/token/api_key/Authorization/连接串等关键词或已知凭据格式就整行过滤
  （用于索引、read/search 输出）。
- find_secret：窄口径，只在出现“关键词=值”、Bearer 值、带凭据/数据库连接串、已知密钥格式时命中
  （用于 handoff 拒绝写入、台账名称/路径单元格校验），避免把“修复 token 过期”这种描述也拒掉。
"""

import re

_EN = (
    r"password|passwd|pwd|passphrase|secret|secrets|token|tokens|api[_\-\s]?keys?|apikey|"
    r"access[_\-]?keys?|private[_\-]?keys?|client[_\-]?secret|authorization|auth[_\-]?token|bearer|"
    r"cookie|session[_\-]?key|credentials?|conn(?:ection)?[_\-\s]?str(?:ing)?|dsn"
)
_ZH = r"口令|密码|密钥|私钥|令牌|凭据|凭证|连接串|连接字符串"
_KEYWORD = rf"(?:(?<![A-Za-z])(?:{_EN})(?![A-Za-z])|{_ZH})"

_KEYWORD_RE = re.compile(_KEYWORD, re.I)
_ASSIGN_RE = re.compile(_KEYWORD + r"[\"']?\s*[:=：]\s*\S+", re.I)
_BEARER_RE = re.compile(r"(?<![A-Za-z])bearer\s+[A-Za-z0-9\-._~+/]{8,}=*", re.I)
_CRED_URL_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@", re.I)
_DB_URL_RE = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?|mssql|sqlserver|oracle|jdbc:[a-z]+)://",
    re.I,
)
_ADO_RE = re.compile(r"(?:server|data source|host)\s*=[^;\n]+;.*(?:password|pwd|user id|uid)\s*=", re.I)
_TOKEN_FMT_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)

_VALUE_PATTERNS = (
    ("assignment", _ASSIGN_RE),
    ("bearer", _BEARER_RE),
    ("credential_url", _CRED_URL_RE),
    ("database_url", _DB_URL_RE),
    ("connection_string", _ADO_RE),
    ("token_format", _TOKEN_FMT_RE),
)

REDACTED_LINE = "[已过滤敏感行]"


def is_sensitive_line(line):
    if not line:
        return False
    if _KEYWORD_RE.search(line):
        return True
    return find_secret(line) is not None


def find_secret(text):
    """返回命中的类别名（不返回内容），无命中返回 None。"""
    if not text:
        return None
    for name, rx in _VALUE_PATTERNS:
        if rx.search(text):
            return name
    # Values under a credential heading/table may not repeat a keyword.
    if "\n" in text and re.search(r"(?:^|\n)\s*(?:#{1,6}\s+.*(?:测试账号|后台账号|登录账号|密码|凭据)|\|[^\n]*(?:密码|password|token)[^\n]*\|)", text, re.I):
        return "credential_block"
    return None


def redact_lines(lines):
    """保持行数的整文脱敏；先处理上下文，再允许调用方切片。"""
    output = []
    blocked_heading = None
    blocked_table = False
    private_key = False
    value_next = False
    for line in lines:
        heading = re.match(r"^\s*(#{1,6})\s+(.*)", line)
        if heading:
            depth, title = len(heading[1]), heading[2]
            if blocked_heading is not None and depth <= blocked_heading:
                blocked_heading = None
            if is_sensitive_line(title) or re.search(r"测试账号|后台账号|登录账号|access credentials", title, re.I):
                blocked_heading = depth
        stripped = line.strip()
        if "-----BEGIN" in line and "PRIVATE KEY-----" in line:
            private_key = True
        in_table = stripped.startswith("|")
        if not in_table:
            blocked_table = False
        elif is_sensitive_line(line):
            blocked_table = True
        block = blocked_heading is not None or blocked_table or private_key or is_sensitive_line(line)
        if value_next and stripped and not heading:
            block = True
            value_next = False
        if re.fullmatch(r"\s*" + _KEYWORD + r"\s*[:：=]?\s*", line, re.I):
            value_next = True
        if re.search(r"base64\.b64encode\s*\(\s*b?[\"'][^\"']+:[^\"']+", line, re.I):
            block = True
        output.append(REDACTED_LINE if block else line)
        if "-----END" in line and "PRIVATE KEY-----" in line:
            private_key = False
    return output
