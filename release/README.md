# dsh-longmemory（Harness 插件包）

把 DSH 长期记忆内核接进 Harness：会话消息增量投递内核切分事件、蒸馏经验与认知，
并向模型暴露 `memory_search` / `memory_get` / `memory_recent` 三把工具。

## 目录

```
release/
├── package.json        # bundle 清单（dsh.bundle.patch 指向补丁）
├── cordis.patch.yml    # 插入插件行
├── index.js            # 入口：re-export ./lib/index.js
├── lib/                # 外壳实现（由 src/js 构建）
│   ├── index.js        # 插件 entry：配置、装配、生命周期
│   ├── kernel.js       # Python 内核进程 + 帧收发
│   ├── frames.js       # 协议信封
│   ├── llm-bridge.js   # 内核 llm.request → ctx.llm
│   ├── tools-bridge.js # 内核 tools.schema → ctx.tools
│   └── ingest-bridge.js# session/event → ingest.request
├── kernel/             # Python 内核（由 src/*.py 构建）
├── locale/{en,zh}.json # meta.title / meta.description
└── icon.svg
```

## 前置条件

- Harness 的 Python 环境能 `import pydantic`（内核依赖 pydantic v2）。
- `pythonPath` 指向该环境；Windows 上通常写成 `python` 或解释器绝对路径。
- 模型路由：`provider` / `model` 必填（如 `deepseek-official` / `deepseek-v4-flash`）。
  内核不持有 provider/model/key，所有模型调用经 `ctx.llm`。

## 安装

在 Harness 里用 `plugin_manager` 的 `install_bundle`，`target` 传本目录的**绝对路径**：

```
plugin_manager({ action: "install_bundle", target: "<...>/release" })
```

安装前建议先跑一次构建校验：

```powershell
python scripts/build_release.py --check
python scripts/build_release.py          # 有差异时重新生成 lib/ 与 kernel/
```

## 配置

`cordis.patch.yml` 里已给出默认行；需要模型路由时补两项：

```yaml
- insert:
    - id: dsh-longmemory
      name: '@local/dsh-longmemory'
      config:
        pythonPath: python
        provider: deepseek-official
        model: deepseek-v4-flash
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `pythonPath` | `python` | Python 解释器 |
| `kernelDir` | 包内 `kernel/` | 含 `main.py` 的内核目录 |
| `dbPath` | 包内 `.data/memory.db`（绝对路径） | 记忆库路径；启动时既通过环境变量、也用 `config.set` 注入 |
| `provider` / `model` | 空 | 模型路由；为空时内核收到 1004，按降级策略处理 |
| `maxTokens` | 适配器默认 | 单次输出上限 |
| `agentId` | `dsh-longmemory` | 写入信封的 agent 标识 |
| `ingestDebounceMs` | 2500 | 会话事件去抖窗口 |
| `requestTimeoutMs` | 30000 | 外壳等内核应答的超时 |
| `llmTimeoutMs` | 120000 | 单次模型调用超时 |

## 使用

| 入口 | 用法 |
|---|---|
| 自动记忆 | 正常聊天即可；消息经去抖后自动切分、摘要、落库 |
| 工具 | 模型自行调用 `memory_search` / `memory_get` / `memory_recent`；也可以直接说「查一下之前关于 X 的结论」 |
| 命令 | 输入框发 `/memory`：立即切分当前会话（跳过去抖），结果就地回显 |
| 设置页 · 概况 | 统计、库路径、最近事件 |
| 设置页 · 数据 | 事件 / 经验 / 认知 / 消息 / 引用 / 工作记忆：搜索、分页、**新增、编辑、删除** |
| 设置页 · 日志 | `shell.log` 与 `kernel.stderr.log` 尾部，可自动刷新 |
| 管理接口 | `POST /longmemory/api`（仅回环）+ `GET /longmemory/logs` |
| 状态接口 | `GET /longmemory/stats[?session_id=...]`，只读 JSON |

## 数据流

```
session/event ──ingest-bridge──▶ ingest.request ─▶ 内核：入库 → 切分 → 摘要 → 裁决 → 落库
                                                   │
内核 tools.schema ──tools-bridge──▶ ctx.tools.register(defineTool × 3)
内核 llm.request  ──llm-bridge────▶ ctx.llm.prepareCall/stream ─▶ llm.result
```

## 验收

安装后：

1. `cordis_inspect_query` 确认插件行已 `applied`，且出现三把 `memory_*` 工具。
2. 让模型调用一次 `memory_search`，确认返回 `{items, count, scope, limit}`（空库时 `count: 0`）。
3. 发几轮对话，等待去抖窗口后确认库里出现事件（`memory_recent` 能查到区间内事件）。
4. 插件详情里确认标题/描述来自 `locale/*.json`，图标为包内 `icon.svg`。

离线可先验证外壳与内核的进程契约（不需要 Harness）：

```powershell
python -m unittest tests.test_shell_contract   # health / schema / 调用 / ingest 帧序
node tests/js/unit.test.mjs                    # 帧与内容提取
```

> 沙箱里 Node 不能用管道捕捉子进程输出（`spawn EPERM`），因此内核往返测试放在
> Python 侧（`tests/test_shell_contract.py` 复刻外壳的启动顺序）；真实 Harness
> 环境不受此限制。

## 已知限制

- 只支持单轮模型调用（内核的 jev / summary 都是 system + 单条 user）。
- 会话事件按 `event.seq` 增量投递；同一会话修改历史不会回填（内核只追加）。
- 工具注册发生在启动握手之后；握手失败时插件仍存在但不提供工具（日志会给出原因）。
