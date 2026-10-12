# 第三方组件

本仓库核心只使用 Python 标准库，不 vendor 任何第三方代码。以下为可选功能涉及的外部组件：

## QMD（可选语义检索运行时）

- 包：`@tobilu/qmd`（npm），本仓库按 2.8.3 版本锁定使用；许可证为 MIT（以官方发布为准）。
- 本仓库**不分发**其代码：用户自行执行 `npm install @tobilu/qmd@2.8.3` 安装到仓库内 `runtime/qmd/`（该目录被 .gitignore 忽略，不入版本控制）。
- `scripts/semantic_worker.mjs` 以一次性子进程方式调用其 SDK，仅使用 CPU，不启动常驻服务。

## Qwen3-Embedding-0.6B（可选语义模型）

- 模型文件 `Qwen3-Embedding-0.6B-Q8_0.gguf` 需用户自行从官方发布页下载：<https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF>，并按模型发布方的许可条款使用。
- 本仓库**不分发**模型文件。下载后请校验：大小 639,150,592 字节，SHA256 `06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439`。
- 默认存放位置与配置键 `semantic_model_file` 见 README.md 与 config.example.json。

## 其他

- CI 使用 GitHub 官方 actions（actions/checkout、actions/setup-python），按其各自许可运行。
- 本仓库本体许可证为 MIT，见 LICENSE。
