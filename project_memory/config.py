"""配置加载：--config > 环境变量 PROJECT_MEMORY_CONFIG > 工程根目录 config.json > 内置默认。"""

import json
import os
from dataclasses import dataclass

from .util import PMError

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(PKG_ROOT, "config.json")

# 内置默认全部落在用户主目录下，clone 后无需任何配置即可使用。
# ledger_path 为 null（或空字符串）时是“无台账模式”：不读取、不创建任何台账文件。
DEFAULTS = {
    "ledger_path": None,
    "memory_root": "~/.project-memory/memory",
    "db_path": "~/.project-memory/data/index.sqlite",
    "registry_path": "~/.project-memory/data/registry.json",
    "report_dir": "~/.project-memory/data/reports",
    "stale_days": 14,
    "search_default_limit": 8,
    "search_max_limit": 50,
    "read_max_lines": 400,
    "lock_timeout": 30,
    "semantic_enabled": False,
    "semantic_model_file": "~/.project-memory/data/models/Qwen3-Embedding-0.6B-Q8_0.gguf",
    "semantic_db_path": "~/.project-memory/data/semantic/index.sqlite",
}

PATH_KEYS = ("ledger_path", "memory_root", "db_path", "registry_path", "report_dir", "semantic_model_file", "semantic_db_path")


@dataclass
class Config:
    config_path: str
    config_exists: bool
    ledger_path: str | None
    memory_root: str
    db_path: str
    registry_path: str
    report_dir: str
    stale_days: int
    search_default_limit: int
    search_max_limit: int
    read_max_lines: int
    lock_timeout: float
    semantic_enabled: bool
    semantic_model_file: str
    semantic_db_path: str

    def public(self):
        return {k: getattr(self, k) for k in ("config_path", "config_exists") + PATH_KEYS}


def load_config(path=None):
    explicit = path or os.environ.get("PROJECT_MEMORY_CONFIG")
    cfg_path = os.path.abspath(explicit or DEFAULT_CONFIG_PATH)
    data = dict(DEFAULTS)
    exists = os.path.isfile(cfg_path)
    if exists:
        try:
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                user = json.load(f)
        except ValueError as e:
            raise PMError("config_invalid", f"配置 JSON 无法解析: {e}", config=cfg_path)
        if not isinstance(user, dict):
            raise PMError("config_invalid", "配置必须是 JSON 对象", config=cfg_path)
        unknown = sorted(set(user) - set(DEFAULTS) - {"$comment"})
        if unknown:
            raise PMError("config_invalid", "配置含未知字段", fields=unknown, config=cfg_path)
        data.update({k: v for k, v in user.items() if k in DEFAULTS})
    elif explicit:
        raise PMError("config_not_found", "指定的配置文件不存在", config=cfg_path)
    base = os.path.dirname(cfg_path)
    for k in PATH_KEYS:
        v = data[k]
        if k == "ledger_path" and (v is None or (isinstance(v, str) and not v.strip())):
            data[k] = None  # 无台账模式：不读取、不创建任何台账文件
            continue
        if not isinstance(v, str) or not v.strip():
            raise PMError("config_invalid", f"{k} 必须是非空字符串")
        v = os.path.expanduser(v)
        if not os.path.isabs(v):
            v = os.path.join(base, v)
        data[k] = os.path.normpath(v)
    if not isinstance(data['semantic_enabled'], bool):
        raise PMError('config_invalid', 'semantic_enabled 必须是布尔值')
    return Config(
        config_path=cfg_path,
        config_exists=exists,
        ledger_path=data["ledger_path"],
        memory_root=data["memory_root"],
        db_path=data["db_path"],
        registry_path=data["registry_path"],
        report_dir=data["report_dir"],
        stale_days=int(data["stale_days"]),
        search_default_limit=int(data["search_default_limit"]),
        search_max_limit=int(data["search_max_limit"]),
        read_max_lines=int(data["read_max_lines"]),
        lock_timeout=float(data["lock_timeout"]),
        semantic_enabled=data['semantic_enabled'],
        semantic_model_file=data['semantic_model_file'],
        semantic_db_path=data['semantic_db_path'],
    )
