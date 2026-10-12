# project-memory：跨工具的本地项目工作记忆

**English** — project-memory is a local, single-machine project memory shared across AI coding tools (Claude Code, Codex, Cursor, or any MCP client). Each project gets human-readable Markdown notes (`overview.md`, `current.md`, and an append-only `history/`); a tool takes over a project with `resume` and saves a structured handoff with `handoff` under optimistic revision checks, so concurrent tools never silently overwrite each other. The core is Python 3.12+ standard library only (needs SQLite with FTS5), exposes a CLI and a stdio MCP server, opens no network ports, and sends nothing anywhere. An optional local semantic search uses a CPU embedding model you download yourself. The rest of this README is in Chinese.

## 是什么、为什么

多个 AI 编程工具交替接手同一个项目时，"上次做到哪、验证了什么、还剩什么"往往散落在各自会话里，换个工具就要从头交代。project-memory 把这些放进本地的 Markdown 记忆库，任何工具按同一套协议读写：

- **Markdown 是正本**：`memory_root` 下每个项目一个目录——`overview.md`（项目概览）、`current.md`（当前交接投影）、`history/`（只追加的权威历史事件）、`operations.md` / `project-profile.json`（运维手册与环境关联）。SQLite 索引、registry 都是可重建的派生数据。
- **版本号 + 只追加**：每次交接是 `history/` 里一个不可覆盖的事件；提交必须带 `expected_revision`，对不上就保存为冲突事件并返回 `conflict`，双方历史都保留。
- **资料不是指令**：resume/search/read 返回的项目内容都标注为参考资料，不是指令；`workflow` 字段单独返回用户配置的工作规范（`rules/PROJECT_MEMORY_RULES.md`）。
- **秘密拒绝**：交接内容出现口令 / token / 连接串等秘密值时整条拒绝写入；索引与读取对敏感行过滤（显示 `[已过滤敏感行]`）。
- **本地、零端口**：核心纯标准库；CLI 与 stdio MCP 都不监听网络端口，不做任何网络请求。

## 快速开始

要求 Python ≥ 3.12（自带 SQLite 需启用 FTS5，官方发行版默认满足）。

### Windows

```bat
git clone <本仓库地址> C:\tools\project-memory-oss
cd C:\tools\project-memory-oss
pm.cmd status
mkdir C:\path\to\demo && pm.cmd initialize --project C:\path\to\demo --register
pm.cmd resume --cwd C:\path\to\demo
```

### macOS / Linux

```sh
git clone <本仓库地址> ~/tools/project-memory-oss
cd ~/tools/project-memory-oss
sh ./pm status
mkdir -p ~/demo && sh ./pm initialize --project ~/demo --register
sh ./pm resume --cwd ~/demo
```

`pm` 脚本用 `python3 -m project_memory`（找不到时回退 `python`）；也可以 `chmod +x pm` 后直接 `./pm`。三种等价入口任选：`pm` / `python -m project_memory` / `pip install -e .` 后的 `project-memory` 命令。

默认**无需任何配置**：记忆放 `~/.project-memory/memory`，派生数据放 `~/.project-memory/data`，不使用台账。需要自定义时，把 `config.example.json` 复制为仓库根目录的 `config.json`（已被 `.gitignore` 忽略）修改。配置优先级：`--config` > 环境变量 `PROJECT_MEMORY_CONFIG` > 工程根目录 `config.json` > 内置默认；相对路径按**配置文件所在目录**解析。

| 键 | 默认 | 说明 |
|---|---|---|
| `ledger_path` | `null` | 可选 Markdown 项目台账；`null` 为无台账模式（见下节） |
| `memory_root` | `~/.project-memory/memory` | 正式记忆（正本） |
| `db_path` | `~/.project-memory/data/index.sqlite` | FTS5 索引（缓存，可重建） |
| `registry_path` | `~/.project-memory/data/registry.json` | 派生注册表 |
| `report_dir` | `~/.project-memory/data/reports` | 盘点报告 |
| `stale_days` | 14 | 交接多少天没更新算过期 |
| `semantic_enabled` | `false` | 语义检索开关（见「可选语义检索」） |
| `semantic_model_file` | `~/.project-memory/data/models/Qwen3-Embedding-0.6B-Q8_0.gguf` | 语义模型文件 |
| `semantic_db_path` | `~/.project-memory/data/semantic/index.sqlite` | 语义索引 |

### 台账是可选的

- **无台账模式（默认）**：`ledger_path` 为 `null`。`discover` 返回空集合不报错；`initialize --project <绝对路径> --register` 只写 registry 和记忆目录，**不读取、不创建任何台账文件**。项目身份（UUID、路径、别名）保存在 registry 与各项目 `identity.json` 中。
- **台账模式**：配置 `ledger_path` 指向一个 Markdown 表格文件后，`discover` 从中盘点项目（只读名称/路径/状态三列，其余单元格内容一律不读），`register` 先核实实时 TCP/UDP 占用、备份台账，再只追加一行"未分配端口"的登记。两类模式的 resume/search/handoff 行为完全一致。

## 接手（resume）与收尾（handoff）

**接手**：`pm resume --cwd <当前目录>`（或 `--project <id|名称|路径>`）。路径按最长前缀匹配，嵌套项目命中最里层；名称对应多个项目时返回 `ambiguous_project` 与候选清单。返回内容包括 overview/current 简报、最新一次交接、`expected_revision_for_next_handoff`、警告（`no_handoff_yet`、`handoff_stale`、`current_out_of_sync`、`unresolved_conflicts`、`branch_differs_from_handoff`、`worktree_dirty` 等）与本地只读 git 现场状态（不 fetch、不改 index、不输出文件名）。索引损坏或被删时 resume 仍可用；已登记且无交接的正常项目会在首次 resume 时自主初始化（有界读取根目录说明、脱敏、标注待核实），`--no-initialize` 可禁用。

**收尾**：`pm handoff --input h.json`（`--input -` 从 stdin 读，或 `--json "{...}"`）：

```json
{
  "project": "<id|名称|路径>", "actor": "claude-code", "summary": "本次做了什么",
  "completed": ["..."], "validation": ["实际跑过的命令与结果"], "pending": ["..."],
  "next_steps": ["..."], "evidence": ["文件/提交/报告路径"],
  "expected_revision": 3, "session_id": "可选", "branch": "可选", "idempotency_key": "可选"
}
```

- `expected_revision` 必填，取自 resume 的 `expected_revision_for_next_handoff`。
- 内容含秘密值（`password=…`、`Bearer …`、带凭据或数据库的连接串、`sk-…` / `ghp_…` 等密钥格式）会被整条拒绝（`secret_detected`），什么都不写入；"修复 token 过期"这类只提关键词的描述可以写入，但索引/读取时该行会被过滤。
- 幂等：同一 `idempotency_key`（不传时由 session_id 与内容自动派生）重复提交返回 `duplicate`；同 key 不同内容报 `idempotency_key_reused`。

**冲突与恢复**：`expected_revision` 对不上时保存为 `history/*__conflict__*.md`，返回 `conflict` 与 `current_revision`，不改 current；处理方法是 resume 后合并、用新 revision 重提。进程在"历史已落盘、current 未更新"之间崩溃时，`status --project X` 显示 `current_consistent: false`，`recover --project X`（或下一次 handoff）从权威历史重建 current。

**项目身份**：每个项目有持久 UUID（registry + overview frontmatter + identity.json 三处一致）；registry 丢失后重新 discover 可从 overview 找回。路径迁移用 `relocate --project X --path <新路径>`（保留 id 与记忆，旧路径进 `path_history`）；别名用 `alias --project X --add 别名`。

所有命令向 stdout 输出一个 UTF-8 JSON 对象（`--help` 除外）：成功 `{"ok": true, ...}`，失败 `{"ok": false, "error": {"code", "message", "details"}}`；退出码 0 / 1（业务错误）/ 2（参数错误）/ 3（内部错误）。

## MCP 接入

stdio JSON-RPC 2.0（按行分隔），工具：`memory_projects`、`memory_resume`、`memory_initialize`、`memory_search`、`memory_read`、`memory_handoff`、`memory_status`。MCP 服务端的当前目录不代表客户端目录，`memory_resume` 必须显式传 `project` 或 `cwd`（绝对路径）。

客户端配置示例（路径换成你的 clone 位置）：

```json
{"mcpServers": {"project-memory": {
  "command": "C:\\tools\\project-memory-oss\\pm.cmd", "args": ["mcp"]
}}}
```

```json
{"mcpServers": {"project-memory": {
  "command": "/home/you/tools/project-memory-oss/pm", "args": ["mcp"]
}}}
```

也可以直接用 Python 入口（适合客户端无法执行脚本时）：

```json
{"mcpServers": {"project-memory": {
  "command": "python", "args": ["-X", "utf8", "/path/to/project-memory-oss/mcp_entry.py"]
}}}
```

stdout 只输出协议 JSON，日志全写 stderr；MCP 与 CLI 共用同一套核心代码和配置。是否在任务结束前自主调用 `memory_handoff` 取决于各客户端的规则配置（见下"局限"）。

## 设计原则

1. **Markdown 正本、派生可重建**：正式记忆只有 Markdown 与 identity/sources 元数据；索引与 registry 删掉都能重建（`reindex --rebuild`、重新 discover）。
2. **乐观版本号、只追加历史**：交接永不覆盖；冲突双方留档，由人合并。
3. **资料不是指令**：检索与历史内容不构成对当前会话的授权；工作规范与项目资料分开返回。
4. **秘密拒绝与脱敏**：交接整条拒绝秘密值；索引/读取前过滤敏感行；台账盘点只读三个单元格。
5. **路径安全与项目隔离**：read 只接受 search 返回的 ref，重新做安全解析；拒绝 `..`、绝对路径、ADS、路径上任何一级 symlink/junction/reparse point、realpath 越界；检索按 project_id 精确隔离。
6. **本地、零端口、零网络**：CLI 与 stdio MCP 不监听端口、不发外部请求；git 只做本地只读查询。

## 可选语义检索（本地 CPU）

关键词检索（SQLite FTS5，中文 bigram）开箱即用。语义检索是可选增强：本地 CPU 向量 + 关键词混合，失败时明确回退 `keyword_fallback`（不把回退称作语义命中）。需要自行安装两样东西，仓库不分发它们：

1. **Node.js 与 QMD 2.8.3**（MIT，见 [THIRD_PARTY.md](THIRD_PARTY.md)）：

   ```sh
   mkdir -p runtime/qmd
   cd runtime/qmd
   npm install @tobilu/qmd@2.8.3
   ```

   `runtime/` 不进版本控制。`scripts/semantic_worker.mjs` 从 `../runtime/qmd/node_modules/@tobilu/qmd/dist/index.js` 加载 SDK。

2. **Qwen3-Embedding-0.6B-Q8_0.gguf 模型**：从官方发布页下载 <https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF>，按模型发布方的许可使用。下载后校验再启用：

   - 大小：639,150,592 字节
   - SHA256：`06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439`

   校验命令：Linux/macOS `sha256sum <文件>`；Windows `certutil -hashfile <文件> SHA256`。默认放到 `~/.project-memory/data/models/Qwen3-Embedding-0.6B-Q8_0.gguf`（即 `semantic_model_file` 默认值）。

然后在 `config.json` 设置 `semantic_enabled: true`。使用：

- `pm search "中断后如何接着做" --project "项目路径"` 默认混合检索；`--mode keyword` 快速关键词；`--mode semantic` 单独检查向量召回（MCP `memory_search.mode` 相同）。
- 检索按需启动 CPU 辅助进程（查询 1 上下文 4 线程，结束释放）；搜索的增量向量化预算为 0 份文档，积压不进入查询时限；全库首次或补库运行 `python -X utf8 scripts/semantic_index.py`（外层时限 7200 秒，中断重跑会续补；成功以 `worker.status.needsEmbedding=0` 为准）。
- 每份资料以开头/中间/末尾的有界片段建立轻量语义表示，完整正文仍由关键词索引覆盖，不能描述成"全文已向量化"。

## 局限

- **只有客户端真的调用 handoff 才保存**：记忆服务不抓取其他软件的会话；异常退出可能来不及保存，只有返回 accepted/duplicate 才算已保存。
- **不检查服务**：服务和端口一律不检查、不推测；登记台账里的运行状态只是声明。
- **资料可能过期**：历史交接是"交接时的描述"，git/服务状态需现场核实；模板内容标注待核实。
- **macOS 支持靠 CI 覆盖**：Windows 与 Linux 经真机验收，macOS 由 GitHub Actions 矩阵覆盖，未逐一真机验证。

## 测试

```sh
python -X utf8 -m unittest discover -s tests -v
```

测试全部用真实子进程，跑在 `work/_testtmp/` 下的临时目录里，不碰真实记忆。覆盖：中文台账盘点与凭据排除、中文检索与项目隔离、索引更新/删除、read 越界与链接拒绝、重名歧义、两进程并发交接、幂等、崩溃恢复、无台账模式、真子进程 MCP。崩溃测试通过环境变量 `PROJECT_MEMORY_FAULT=after_history_write` 注入故障，该变量只供测试使用。测试夹具中的密钥样式字符串在运行时拼接，避免仓库文本被密钥扫描器误报。

## 许可

MIT，见 [LICENSE](LICENSE)；第三方组件（QMD、Qwen3 模型）见 [THIRD_PARTY.md](THIRD_PARTY.md)，均由用户自行下载并按其各自许可使用。
