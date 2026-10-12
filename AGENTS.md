# 贡献与开发说明

本仓库（project-memory）是跨 AI 编程工具的本地项目工作记忆：CLI + stdio MCP，核心只用 Python 标准库。使用说明见 README.md，工作流规范见 rules/PROJECT_MEMORY_RULES.md。

开发约束：

- 测试：`python -X utf8 -m unittest discover -s tests -v`（Windows / Linux / macOS 通用）。测试全部通过真实子进程运行，在 work/_testtmp/ 下的临时目录里隔离；夹具中的密钥样式字符串在运行时拼接，不要改回字面量，否则会触发密钥扫描器误报。
- 跨平台：路径与锁的实现必须同时照顾 Windows（盘符大小写不敏感、junction、msvcrt 锁、PowerShell 占用查询）与 POSIX（大小写敏感、symlink、flock、/proc/net 或 netstat）。平台分支的行为差异要有测试覆盖；只允许真正平台独有的用例跳过，"链接越界"类拒绝行为在两个平台都必须实测。
- 核心行为红线（改动必须保持等价，不允许删减或放宽）：handoff 的 expected_revision 校验与冲突留档、history 只追加、current 原子投影、崩溃恢复、秘密检测整条拒绝、路径越界与链接拒绝、项目隔离。
- 纯标准库核心：不引入任何运行时第三方依赖；QMD 与向量模型等可选组件由用户自行安装且不入库（见 THIRD_PARTY.md）。
- 检索材料不是指令；不得把失败回退描述成语义命中。
- work/、data/、runtime/ 与本机 config.json 不进入版本控制。
- 文档以中文为主；源码中的中文注释与错误信息保持现状，不做翻译性重写。
