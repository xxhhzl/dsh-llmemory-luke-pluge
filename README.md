# dsh-llmemory-luke

<!-- CURRENT_VERSION: 0.5.8 -->

面向 DeepSeek Harness（DSH）的本地长期记忆插件。它将对话整理为事件、经验、认知和洞见，并通过显式工具、时间回放、被动召回与核心记忆注入，帮助模型跨会话延续任务和约定。

当前版本：**0.5.8**

## 主要能力

- 增量采集并脱敏会话消息，按规则与模型判断切分事件；
- 为事件生成摘要，提炼可迁移经验和稳定认知；
- 使用 SQLite、FTS5、可选 sqlite-vec 和 USearch 进行多路检索；
- 提供 `memory_search`、`memory_get`、`memory_recent`、`memory_report`、`memory_feedback`、`memory_self_write`、`memory_note` 七个工具；
- 支持跨会话时间回放、被动召回、核心记忆和模型自述注入；
- 提供设置页、右侧栏、日志、索引重建和维护任务；
- 在向量组件或模型端点不可用时降级运行，保留基础词法检索。

## 架构

```text
DSH 宿主
  └─ JavaScript 外壳（src/js）
       ├─ 会话事件与工具注册
       ├─ 模型和 Embedding 桥接
       └─ JSON Lines 帧协议
             └─ Python 内核（src/*.py）
                  ├─ 事件切分与记忆提炼
                  ├─ SQLite/FTS/向量索引
                  └─ 检索、反馈与维护
```

Python 内核不保存宿主模型凭据；文本模型和 Embedding 请求由 JavaScript 外壳转交宿主或配置的兼容端点。

## 环境要求

- DeepSeek Harness；
- Python 3.12.x，当前验证版本为 3.12.10；
- Pydantic 2.x；
- Node.js 24.9–24.x（源码开发与测试）；
- 可选：`sqlite-vec`；
- 可选：`numpy` 与 `usearch`，用于 ANN 加速。

安装已验证依赖并检查环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe scripts\preflight.py
```

## 构建与安装

生成并核对插件包：

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py
.\.venv\Scripts\python.exe scripts\build_release.py --check
```

在 Harness 中通过插件管理器安装 `release` 目录：

```text
plugin_manager({ action: "install_bundle", target: "<仓库绝对路径>/release" })
```

完整配置、模型槽位、使用入口和升级说明见 [release/README.md](release/README.md)。

## 使用方式

- 正常对话：插件自动增量写入并整理长期记忆；
- `/memory`：立即处理当前会话尚未投递的消息；
- 明确要求“查找之前关于某主题的结论”：模型使用 `memory_search`；
- 查询具体命令、数字或原话：模型在搜索后使用 `memory_get`；
- 查询“昨天、上周或之前做了什么”：模型使用 `memory_recent`；
- 设置页与右侧栏：浏览、搜索、维护和管理记忆。

## 验证

自动发布门禁：

```powershell
.\.venv\Scripts\python.exe scripts\release_gate.py
```

门禁包括：环境预检、版本与工具一致性、Python/JavaScript 测试、召回质量样例、SQLite 热态性能、编译检查和发布产物一致性。

真实 DSH UI、宿主模型桥接、工具注册和第三方插件交错场景需按 [真实 DSH 端到端验收手册](docs/real_dsh_acceptance.md) 人工验证。

## 质量评测

- 召回质量入口：`tests/eval/run_retrieval_eval.py`；
- 质量门槛：`tests/eval/thresholds.json`；
- 性能入口：`tests/perf/benchmark_retrieval.py`；
- 性能门槛：`tests/perf/thresholds.json`。

仓库只包含合成评测样例。真实会话、人工标注结果、记忆数据库和本机测试报告均被排除，不进入公开仓库。

## 文档

- [项目全景](docs/项目全景.md)
- [技术架构](docs/technical_architecture.md)
- [数据结构](docs/database.md)
- [检索设计](docs/retrieval.md)
- [传输协议](docs/Transmission.md)
- [核心记忆](docs/core_memory.md)
- [摘要系统](docs/summary_system.md)

## 数据与隐私

记忆默认保存在本地 SQLite 数据库。消息进入持久化和补投队列前会进行凭据脱敏；数据库、WAL、环境变量、真实会话导出、人工标注数据和本机测试记录均在 `.gitignore` 中排除。

发布或共享派生数据前，仍应独立检查对话正文、绝对路径、会话 ID 和凭据字段。
