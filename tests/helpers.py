"""测试辅助：每个测试一个隔离临时目录（位于 work/_testtmp），全部通过真实子进程调用 CLI/MCP。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_BASE = os.path.join(ROOT, "work", "_testtmp")

# 台账样本里的假秘密：任何输出、registry、报告、索引都不允许出现。
# 密钥样式值在运行时拼接，仓库文本中不出现可被密钥扫描器命中的字面量。
FAKE_API_KEY = "sk-" + "abcdefghijklmnopqrstuv123"
FAKE_TOKEN = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWX"
SECRETS = ("hunter2-SECRET", "S3cretPass", FAKE_API_KEY, FAKE_TOKEN)


def rd(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def rdb(path):
    with open(path, "rb") as f:
        return f.read()


def make_junction(target, link):
    import _winapi
    _winapi.CreateJunction(target, link)


class World:
    def __init__(self):
        os.makedirs(TMP_BASE, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="t-", dir=TMP_BASE)
        self.memory_root = os.path.join(self.dir, "memory")
        self.ledger = os.path.join(self.dir, "ledger", "项目台账.md")
        self.config = os.path.join(self.dir, "config.json")
        # 显式写出 config.PATH_KEYS 的全部路径键，且都解析到测试临时目录内；
        # 缺项会回退到内置默认 ~/.project-memory/...，进程内 semantic.export 会写进真实家目录。
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"ledger_path": self.ledger, "memory_root": self.memory_root, "db_path": "data/index.sqlite",
                       "registry_path": "data/registry.json", "report_dir": "data/reports",
                       "semantic_model_file": "data/models/fake-model.gguf",
                       "semantic_db_path": "data/semantic/index.sqlite"}, f, ensure_ascii=False)
        self.db = os.path.join(self.dir, "data", "index.sqlite")
        self.registry = os.path.join(self.dir, "data", "registry.json")
        self.reports = os.path.join(self.dir, "data", "reports")

    def cleanup(self):
        # 先拆 junction，避免 rmtree 穿透到目标
        for root, dirs, _ in os.walk(self.dir):
            for d in list(dirs):
                p = os.path.join(root, d)
                if os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
                    try:
                        os.rmdir(p)
                    except OSError:
                        os.unlink(p)
                    dirs.remove(d)
        shutil.rmtree(self.dir, ignore_errors=True)

    def env(self, extra=None):
        e = os.environ.copy()
        e["PYTHONPATH"] = ROOT
        e["PYTHONIOENCODING"] = "utf-8"
        # 兜底：把子进程的家目录重定向到测试临时目录内，任何默认 ~ 路径都不会写到真实家目录
        home = os.path.join(self.dir, "home")
        os.makedirs(home, exist_ok=True)
        e["HOME"] = home
        e["USERPROFILE"] = home
        e.pop("PROJECT_MEMORY_CONFIG", None)
        e.pop("PROJECT_MEMORY_FAULT", None)
        if extra:
            e.update(extra)
        return e

    def cmd(self, *args):
        return [sys.executable, "-m", "project_memory", "--config", self.config, *args]

    def run(self, *args, stdin=None, extra_env=None, cwd=None):
        p = subprocess.run(self.cmd(*args), cwd=cwd or ROOT, input=stdin.encode("utf-8") if stdin else None,
                           capture_output=True, env=self.env(extra_env), timeout=120)
        out = p.stdout.decode("utf-8")
        try:
            data = json.loads(out) if out.strip() else None
        except ValueError:
            raise AssertionError(f"stdout 不是 JSON: {out[:500]} / stderr: {p.stderr.decode('utf-8', 'replace')[:2000]}")
        return p.returncode, data, out, p.stderr.decode("utf-8", "replace")

    def ok(self, *args, **kw):
        code, data, out, err = self.run(*args, **kw)
        if code != 0 or not data or not data.get("ok"):
            raise AssertionError(f"命令失败 {args}: code={code} out={out[:3000]} err={err[-3000:]}")
        return data

    def handoff(self, payload, **kw):
        return self.run("handoff", "--input", "-", stdin=json.dumps(payload, ensure_ascii=False), **kw)

    # ------------------------------------------------------------ 样本世界
    def p(self, *parts):
        return os.path.join(self.dir, "projects", *parts)

    def build(self):
        alpha, beta, gamma = self.p("video-workshop"), self.p("dashboard"), self.p("dashboard-v2")
        old, ghost, nested = self.p("old-crawler"), self.p("ghost"), self.p("video-workshop", "sub")
        for d in (alpha, beta, gamma, old, nested):
            os.makedirs(d, exist_ok=True)
        files = {
            (alpha, "README.md"): "# 视频二创工坊\n\n蓝鲸协议说明：这是只在 README 中出现的词。\n",
            (alpha, "AGENTS.md"): "# agents\n",
            (alpha, ".env"): "API_KEY=" + FAKE_API_KEY + "\n",
            (alpha, "main.py"): "print('code is never loaded')\n",
            (alpha, os.path.join("docs", "guide.md")): "# 指南\n\n使用说明 guide\n",
            (alpha, os.path.join("docs", "secret-notes.md")): "secret\n",
            (alpha, os.path.join("docs", "sub", "deep.md")): "# 二层\n",
            (alpha, os.path.join("docs", "sub", "sub2", "too-deep.md")): "# 三层\n",
            (alpha, os.path.join("node_modules", "x", "README.md")): "# nm\n",
            (beta, "README.md"): "# 数据看板\n",
        }
        for (base, rel), text in files.items():
            fp = os.path.join(base, rel)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(text)
        ops = self.p("ops", "运维手册.md")
        alpha_fwd = alpha.replace("\\", "/")
        ledger = f"""# 项目台账

> 本文件含口令与端口，仅供测试。

## 项目号表
| 项目号 | 项目 | 端口 | 本地路径 | 状态 |
|---|---|---|---|---|
| P01 | 视频二创工坊 | 3000 | `{alpha_fwd}` | 运行中 |
| P02 | 数据看板 | 8080 | {beta} | 开发中 |

## 详细信息
| 名称 | 目录 | 说明 |
|---|---|---|
| **视频二创工坊** | `{alpha}\\` | 二创流水线 password: hunter2-SECRET |
| 数据看板 | `{gamma}` | v2 重写 |
| 子模块 | {nested} | 嵌套项目 |

## 端口
| 端口 | 用途 | 连接串 |
|---|---|---|
| 5432 | 数据库 | postgres://admin:S3cretPass@localhost:5432/db |

| 项目 | 路径 |
|---|---|
| 运维文档 | {ops} |
| 3001 | `{alpha}` |
| api_key={FAKE_API_KEY} | {beta} |

```
| 项目 | 路径 |
|---|---|
| 代码块里的表 | C:\\should\\not\\parse |
```

## 历史项目
| 项目 | 路径 | 状态 |
|---|---|---|
| 旧爬虫 | {old} | 已停用 |

## 其他
| 项目 | 路径 | 备注 |
|---|---|---|
| 幽灵项目 | {ghost} | token: {FAKE_TOKEN} |
| 子模块（新名） | {nested} | 改名未同步 |
"""
        os.makedirs(os.path.dirname(self.ledger), exist_ok=True)
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write(ledger)
        self.paths = {"alpha": alpha, "beta": beta, "gamma": gamma, "old": old, "ghost": ghost, "nested": nested}
        return self.paths

    def discover(self):
        res = self.ok("discover")
        self.ids = {}
        for k, path in self.paths.items():
            for pr in res["projects"]:
                if os.path.normcase(pr["path"].rstrip("\\/")) == os.path.normcase(path):
                    self.ids[k] = pr["project_id"]
        return res

    def memdir(self, key):
        reg = json.loads(rd(self.registry))
        return os.path.join(self.memory_root, reg["projects"][self.ids[key]]["memory_dir"])

    def all_text_outputs(self):
        """收集 registry、报告、索引库的全部字节，用于检查秘密泄露。"""
        blobs = []
        for base in (os.path.join(self.dir, "data"),):
            for root, _, files in os.walk(base):
                for fn in files:
                    with open(os.path.join(root, fn), "rb") as f:
                        blobs.append((fn, f.read()))
        return blobs
