# dsh-longmemory（Harness 插件包）

<!-- CURRENT_VERSION: 0.5.8 -->

把 DSH 长期记忆内核接进 Harness：会话消息增量投递内核切分事件、蒸馏经验与认知，
并向模型暴露 `memory_search` / `memory_get` / `memory_recent` / `memory_report` /
`memory_feedback` / `memory_self_write` / `memory_note` 7 个工具。

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
│   ├── model-config.js # 三槽模型设置：默认值 / 合并 / 直连端点 / embedding
│   ├── llm-bridge.js   # 内核 llm.request → 宿主模型或自定义端点
│   ├── tools-bridge.js # 内核 tools.schema → ctx.tools（查询向量化后带 query_vector）
│   ├── ingest-bridge.js# session/event → ingest.request
│   ├── core-prompt.js  # 核心记忆 → 系统提示词段 / 自述 → 动态上下文
│   ├── commands.js     # /memory 斜杠命令
│   └── stats-route.js  # /longmemory/{stats,api,test,logs}
├── kernel/             # Python 内核（由 src/*.py 构建）
├── locale/{en,zh}.json # meta.title / meta.description
└── icon.svg
```

## 前置条件

- Python **3.12.x**；当前发布验证基线为 3.12.10。安装后可先运行
  `python scripts/preflight.py` 检查核心、向量与 ANN 三档能力。
- Harness 的 Python 环境能 `import pydantic`（内核依赖 pydantic v2）。
- `pythonPath` 指向该环境；Windows 上通常写成 `python` 或解释器绝对路径。
- 模型路由：`provider` / `model` 必填（如 `deepseek-official` / `deepseek-v4-flash`）。
  内核不持有 provider/model/key，所有模型调用经 `ctx.llm`。
  这两个值只是**三槽配置的默认值**，装好后可以在「设置 → 长期记忆 → 模型」里逐槽改。
- 向量检索（可选）：内核所在 Python 环境装 `sqlite-vec`（`pip install sqlite-vec`）。
  没装也能用，检索自动退回纯 FTS；装了但没配 embedding 端点同样只降级。
- ANN 加速（可选）：再装 `usearch` 与 `numpy`（`pip install usearch numpy`）。
  装了就走向量近似检索（HNSW），没装自动退回 `sqlite-vec` 精确 KNN，接口不变；
  「模型」页会显示当前走的是 `ANN usearch` 还是「精确 KNN」。
- Embedding 端点（可选）：OpenAI 兼容（`/v1/embeddings`）或 Ollama 原生
  （`/api/embed`）。本地 Ollama 示例：

  ```powershell
  ollama pull bge-m3     # 拉模型；之后让服务一直跑着（桌面端或 ollama serve）
  # 默认值已指向 http://127.0.0.1:11434/v1 · bge-m3 · 1024 维
  ```

  端点没跑起来时：保存并测试会报「连不上 …（ECONNREFUSED）」，重建索引会中止并报错
  （不会写空索引、也不会假报完成）。

## 安装

源码仓库开发与发布基线可先检查：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe scripts\preflight.py
.\.venv\Scripts\python.exe scripts\release_gate.py
```

`release_gate.py` 覆盖环境、版本/工具/文档一致性、Python/JavaScript 测试、召回质量样例、SQLite 热态性能和发布产物；真实 DSH UI 与宿主链路仍按 `docs/real_dsh_acceptance.md` 人工验收。

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
| `provider` / `model` | 空 | 三槽配置的默认值（`model.jev` / `model.extract`）；为空时内核收到 1004，按降级策略处理 |
| `maxTokens` | 适配器默认 | 单次输出上限 |
| `agentId` | `dsh-longmemory` | 写入信封的 agent 标识 |
| `ingestDebounceMs` | 2500 | 会话事件去抖窗口 |
| `requestTimeoutMs` | 30000 | 外壳等内核应答的超时 |
| `llmTimeoutMs` | 120000 | 单次模型调用超时 |
| `coreSectionOrder` | -2000 | 核心记忆在系统提示词中的段顺序（最前，先于 harness 身份 -1000） |
| `coreContextOrder` | 850 | 自述自我在动态上下文中的顺序 |

内核侧 `core.*`（`max_chars` / `max_insights` / `max_experiences` / `max_cognitions` /
`max_self_configured` / `max_self_notes` / `enabled`）与 `embedding.dim` 可用 `config.set` 热更新；
注入结构与标签见源码仓库的 `docs/core_memory.md`。

### 三槽模型（设置页 · 模型）

三处模型调用分别可配，存在记忆库的 `app_setting` 表（与记忆同库，本地明文保存 key）：

| 槽 | 管什么 | 默认 |
|---|---|---|
| `model.jev` | 事件切分：判断一条消息是新事件 / 延续 / 重开旧事件 | 继承宿主 `ctx.llm` |
| `model.extract` | 事件摘要、经验、认知，以及合并、洞见、模型自述等文本调用 | 继承宿主 `ctx.llm` |
| `embedding` | 检索向量化（写入索引 + 查询向量） | OpenAI 兼容 `http://127.0.0.1:11434/v1` · `bge-m3` · 1024 维 |

每个文本槽有两种通道：

- **宿主模型**：走 Harness 的 `ctx.llm`（填 `provider` + `model`），凭据由宿主管理；
- **自定义端点**：填 `baseUrl` + `model` + `apiKey`，外壳直接请求 OpenAI 兼容
  `/chat/completions`（`temperature=0`，非流式）。

按钮说明：

- **保存并测试**：先写库（内核推 `notify{kind:"settings"}` 让外壳立刻换新配置），
  再调 `POST /longmemory/test`，返回模型名/维度/耗时；
- **重建向量索引**：后台把现有经验/认知/洞见重新 embed 一遍（默认每批 16 条）。
  换 embedding 模型或维度后必须点一次；进度显示在卡片上。
- 页面上的 API Key 永远是掩码（`…` + 尾 4 位）：留空或原样回传都表示**保持原 key**，
  想换新 key 直接粘贴新的。

## 使用

| 入口 | 用法 |
|---|---|
| 自动记忆 | 正常聊天即可；消息经去抖后自动切分、摘要、落库 |
| 工具 | 模型自行调用 `memory_search` / `memory_get` / `memory_recent`；也可以直接说「查一下之前关于 X 的结论」 |
| 命令 | 输入框发 `/memory`：立即切分当前会话（跳过去抖），结果就地回显 |
| 设置页 · 概况 | 统计、库路径、最近事件；**维护**区：重跑待复核事件摘要、归纳洞见（后台跑，显示进度/结果） |
| 设置页 · 模型 | JEV / 提取 / Embedding 三槽配置，保存即测试；向量索引状态（含 ANN 后端）与重建 |
| 设置页 · 数据 | 事件 / 经验 / 认知 / 洞见 / 消息 / 引用 / 工作记忆：搜索、分页、**新增、编辑、删除** |
| 设置页 · 自我 | 「自我设定」（进系统提示词）四类输入 + 「模型自述」（进动态上下文），可预览注入文本 |
| 右侧栏 · 长期记忆 | 会话 header 的「记忆」按钮或设置页「在右侧栏打开」：按事件/经验/认知/洞见浏览、搜索、看详情 |
| 设置页 · 日志 | `shell.log` 与 `kernel.stderr.log` 尾部，可自动刷新 |
| 管理接口 | `POST /longmemory/api`（仅回环）+ `GET /longmemory/logs` + `POST /longmemory/test` |
| 状态接口 | `GET /longmemory/stats[?session_id=...]`，只读 JSON |

## 数据流

```
session/event ──ingest-bridge──▶ ingest.request ─▶ 内核：入库 → 切分 → 摘要 → 裁决 → 落库
                                                   │
                                                   └─▶ embed.request ─▶ 外壳 embedding 端点
                                                        └─ embed.result ─▶ 写 vec0 表
内核 tools.schema ──tools-bridge──▶ ctx.tools.register(defineTool × 7)
内核 llm.request  ──llm-bridge────▶ 宿主 ctx.llm / 自定义端点 ─▶ llm.result
```

## 验收

安装后：

1. `cordis_inspect_query` 确认插件行已 `applied`，且出现 7 个 `memory_*` 工具。
2. 设置 → 长期记忆 → **模型**：三张卡片的默认值与 `cordis.patch.yml` 一致；
   点「保存并测试」应回 `Embedding OK：1024 维 · 2 条…` / `文本模型 OK：<模型> → OK`。
   embedding 端点没起来时会明确报「连不上 …（ECONNREFUSED）」而不是只说 fetch failed。
3. 概况页内核版本应与仓库根目录 `VERSION` 一致（当前为 **0.5.8**）；版本没变说明仍在运行旧进程，需要重启 Harness。
4. **升级上来的一次性动作**：老条目没有向量，点一次「重建向量索引」
   （实测 87 条 / 6 次 embedding 请求 / 2.0s）。**点下去后卡片会一直显示
   「重建中 n/N · 已用 xxs」直到内核报完成**；中途读不到状态也保留进度。
   若 embedding 写不进任何向量，会显示「重建失败：…（已完成 a/N）」并中止，
   不会假报完成——先把端点跑起来（`ollama serve` / 桌面端）再重试。
5. **维护区**：概况页底部「待复核事件 N」应等于库里 `needs_review=1` 的事件数；
   点「重跑待复核事件摘要」，进度行显示 `重跑中 n/N · 成功 x / 失败 y`，
   结束后事件标题/摘要更新、`needs_review` 清零、该事件的经验认知落库。
6. 点「归纳洞见」：进度显示 `归纳中 n/N 簇 · 新增 x 条`。**洞见需要同主题 ≥3 条经验**
   （线索相同或向量相似度 ≥0.65 才成簇）；不足时结果是「没有可归纳的簇」，属正常。
   已有同文洞见不会重复写入。
7. 确认「模型」页的向量索引行显示 `ANN usearch（首次检索时装载）`；跑一次
   `memory_search` 后刷新应变成 `ANN usearch（n 条已装载）`。
8. 让模型调用一次 `memory_search`，确认返回 `{items, count, scope, limit}`（空库时 `count: 0`）。
9. 发几轮对话，等待去抖窗口后确认库里出现事件（`memory_recent` 能查到区间内事件）；
   模型页的向量索引条数应随新条目增长。
10. 插件详情里确认标题/描述来自 `locale/*.json`，图标为包内 `icon.svg`。

离线可先验证外壳与内核的进程契约（不需要 Harness）：

```powershell
python -m unittest tests.test_shell_contract   # health / schema / 调用 / ingest 帧序
python -m unittest tests.test_vec tests.test_ann tests.test_maintenance
node tests/js/unit.test.mjs                    # 帧、内容提取、三槽配置与 embed 调用
```

> 沙箱里 Node 不能用管道捕捉子进程输出（`spawn EPERM`），因此内核往返测试放在
> Python 侧（`tests/test_shell_contract.py` 复刻外壳的启动顺序）；真实 Harness
> 环境不受此限制。

## 已知限制

- 只支持单轮模型调用（内核的 jev / summary 都是 system + 单条 user）。
- ANN 索引是**进程内缓存**：重启后首次向量检索要全量装载（当前几十条 <10ms，
  万级约 1s 量级），不落盘以免索引与库不一致；`usearch`/`numpy` 缺失时自动退回
  `sqlite-vec` 精确 KNN。换 embedding 模型或维度后向量表会重建，必须点一次
  「重建向量索引」。
- 洞见归纳要「同主题 ≥3 条经验」：线索（cues[0]）相同或向量相似度 ≥0.65 才成簇；
  经验还很分散时归纳不出东西是正常的。
- 查询向量由外壳现算：本地 Ollama 热态约 30ms，首次加载模型 3s 左右。
- 会话事件按 `event.seq` 增量投递；同一会话修改历史不会回填（内核只追加）。
- 工具注册发生在启动握手之后；握手失败时插件仍存在但不提供工具（日志会给出原因）。
