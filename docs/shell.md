# JS 外壳（src/js）与插件包（release/）

> 内核协议见 [Transmission.md](Transmission.md)；工具契约见 [tools.md](tools.md)。
> 安装说明与配置表见 [release/README.md](../release/README.md)。

## 1. 职责边界

| 层 | 位置 | 负责 | 不负责 |
|---|---|---|---|
| Python 内核 | `src/*.py` | 切分、摘要、裁决、存储、检索 | 网络、模型凭据、宿主事件 |
| JS 外壳 | `src/js/` | 进程管理、帧转发、会话订阅、工具注册 | 记忆算法 |
| 插件包 | `release/` | Harness 清单、补丁、展示资源、打包产物 | — |

模型调用一律由外壳经 `ctx.llm` 完成：内核只发 `llm.request`（messages + purpose），
外壳回 `llm.result`。这保证内核无凭据、可离线测试。

## 2. 外壳结构

```
src/js/
├── index.js        插件 entry（name / inject / Config / apply）+ createShell
├── client.js       浏览器侧：设置 → 「长期记忆」页（settings.section）+ 右侧栏浏览器
├── kernel.js       spawn Python、按行收发、request_id 配对、stderr 落盘
├── frames.js       信封构造/解析（protocol_version 1.0）
├── model-config.js 三槽模型设置：默认值、合并、直连 OpenAI 兼容端点 / embedding
├── llm-bridge.js   llm.request → prepareCall（或自定义端点）→ llm.result
├── tools-bridge.js tools.schema → defineTool 注册；调用回灌 tools.calls（含 query_vector）
├── ingest-bridge.js session/event → 按会话去抖 → ingest.request
├── core-prompt.js  核心记忆 → 系统提示词段（-2000）/ 自述 → 动态上下文（850）
├── commands.js     斜杠命令 /memory（立即切分当前会话）
└── stats-route.js  /longmemory/{stats,api,test,logs}（设置页数据 + 管理 API，仅回环）
```

### 用户可见入口

| 入口 | 实现 | 说明 |
|---|---|---|
| `/memory` 斜杠命令 | `ctx.commands.register` | 立即 flush 去抖缓冲并切分；命令结果就地回显，不进入模型消息 |
| 设置 → 长期记忆 | `settings.section` 槽位（`id: longmemory`） | 概况 + **模型**（三槽配置/连通性测试）+ 事件/经验/认知/洞见/自我设定/模型自述/消息/引用/工作记忆（通用表格增删查改）+ 日志 |
| 右侧栏「长期记忆」 | `sidebarRightTabs` + 槽位 | 事件/经验/认知/洞见的只读搜索浏览（分页 + 详情） |
| `GET /longmemory/stats` | `webServer` 前缀路由 | 概况数据（含脱敏后的三槽模型配置）；内核不可用时回 503 |
| `POST /longmemory/api` | 同上 | 管理 API：`tables / list / get / insert / update / delete / settings.* / vec.* / self.* / maintenance.status / event.resummarize / insight.induce / core.preview` |
| `POST /longmemory/test` | 同上 | 模型连通性测试：`{kind: jev|extract|embedding}`，回模型名/维度/耗时 |
| `GET /longmemory/logs` | 同上 | `shell.log` / `kernel.stderr.log` 尾部（≤1000 行） |

### 管理界面（增删查改）

- **边界**：模型工具仍然只追加（`memory_search/get/recent` 无写入口）；增删改只经
  「设置 → 长期记忆」这个**人工入口**，且路由只接受**本机回环**请求（非回环回 403）。
- **表驱动**：内核 `admin` 的 `tables` op 返回每张表的列与可写列，前端据此渲染表单；
  新增表只改内核 `TABLES`，前端不用动。
- **删除的连带处理**：删事件 → 解绑 `memory.event_id`、清 FTS、断开前后链；
  删消息 → 连带删引用；删 l3 → 清 FTS。正文删除不可撤销。
- **写入**：l3 的 `source_event_id` 是 NOT NULL，人工写入用 `0` 作哨兵；
  `digest` 走 upsert（保留 revision 语义）。

启动顺序（`createShell().start()`）：

1. spawn 内核 → 轮询 `health` 直到就绪；
2. `config.set` 注入 `store.db_path`（如果配置了 `dbPath`）；
3. `settings.raw` 读三槽模型设置（失败退回「继承宿主」默认值）；
4. 拉 `tools.schema`，逐个 `ctx.tools.register(defineTool(...))`；
5. `bridge.attach()` 订阅 `session/event`，并开始应答 `embed.request`。

## 3. 帧转换要点

| 方向 | 帧 | 转换 |
|---|---|---|
| 宿主 → 内核 | `ingest.request` | `session/event` 的 `user/message` / `assistant/message` / `tool/result` → `{msg_id, seq, role, content, ts}`；`seq` 用事件序号，按会话增量（`cursor.last_seq`） |
| 内核 → 宿主 | `llm.request` | 按 `purpose` 选槽（`jev` → JEV 槽，其余 → 提取槽）；`mode=custom` 直连端点，否则 system 文本拼成 `system`、user 文本转 `createUserMessage`，流式 chunk 用 `BlockAssembler` 收成文本 |
| 内核 → 宿主 | `embed.request` | `{request_id, input[]}` → `embedTexts`（openai `/v1/embeddings` 或 ollama `/api/embed`）→ `embed.result{vectors}`；失败回 `error.message`，内核只降级不重试 |
| 内核 → 宿主 | `settings.raw` | 未脱敏的三槽设置（`app_setting` 表）；只走帧通道，不经 HTTP |
| 宿主 → 内核 | `tools.calls` | 工具 `execute` 发一次调用；`memory_search` 会先用 embedding 端点算好 `query_vector` 一并带上 |
| 内核 → 宿主 | `tools.schema` | JSON Schema → 工具 DSL：`anyOf[非 null]` 取首个分支、`enum` 透传、`required` 展开；`{ok:false}` 转成带 `code` 的异常 |

## 4. 错误与生命周期

- 内核错误帧（`type: "error"`）按 `request_id` 拒绝对应 Promise，`code` 保留在 Error 上；
- 模型失败以 `llm.result.error` 回给内核，由内核的重试/降级策略处理（不抛到宿主）；
- 宿主 `ctx.effect` 的 disposer 里：解绑会话事件 → 投递剩余缓冲 → 注销工具 → `shutdown` 内核；
- 内核崩溃：拒绝所有在途请求并 `emit('exit')`，不在外壳内重启（避免重启风暴）。

## 5. 打包

`scripts/build_release.py` 把 `src/js/*.js` 复制到 `release/lib/`、`src/*.py` 复制到
`release/kernel/`，`--check` 模式只比对不写。`release/index.js` 只是
`export { ... } from './lib/index.js'`，所以源码与产物可以逐字节核对。

## 6. 验证现状

| 项 | 方式 | 结果 |
|---|---|---|
| 帧构造/解析、内容块提取、路由准入、投递回滚、三槽配置、embedding 调用 | `node tests/js/unit.test.mjs` | ✅ 31 条 |
| 生命周期（重启策略、dispose 顺序、订阅去重） | `node tests/js/shell.test.mjs` | ✅ 7 条（假内核，不需要 Python） |
| 外壳↔内核进程契约（启动/工具/ingest/坏字节/坏信封） | `python -m unittest tests.test_shell_contract` | ✅ 11 条（复刻外壳动作；沙箱里 Node 不能管道抓子进程输出） |
| 向量索引 / embedding 代理 / RRF 融合 / 设置脱敏 | `python -m unittest tests.test_vec tests.test_admin` | ✅ 75 条（未装 sqlite-vec 时向量用例自动 skip） |
| ANN 检索（与精确 KNN 排序一致、增量同步、降级） | `python -m unittest tests.test_ann` | ✅ 7 条（未装 usearch 时整组 skip） |
| 维护任务（重跑摘要 / 归纳洞见 / 快照） | `python -m unittest tests.test_maintenance` | ✅ 8 条 |
| 内核预热（numpy 线程导入死锁的规避） | `python -m unittest tests.test_main` | ✅ 3 条 |
| 真实链路（kernel → embed.request → Ollama → vec0/ANN → 检索） | `tests/run_vec_e2e.py`、`.tmp/e2e_maint.py` | ✅ 实测 bge-m3 1024 维；维护任务与洞见检索同样跑通 |
| 打包一致性与语法 | `build_release.py --check` + `node --check` | ✅（含版本号一致性） |
| `ctx.llm` / `ctx.tools` 两条桥 | 需在 Harness 内安装后验收 | ⏳ 见 release/README.md「验收」 |

## 7. 本机安装记录（2026-11-03 安装 / 2026-11-08 更新到 0.3.2）

按 profile `desktop` 的既有约定安装（与 `@local/dsh-custom-mode` 的 `link:` 同型）：

| 位置 | 改动 |
|---|---|
| `~/.dsh/profiles/desktop/package.json` | dependencies 增加 `"@local/dsh-longmemory": "file:C:/.../release"`；`dsh.profile.bundles` 末尾追加 `"@local/dsh-longmemory"` |
| `~/.dsh/profiles/desktop/node_modules/@local/dsh-longmemory` | **实体目录**（复制 release/；先试过目录联接，宿主按 `routeLinked` 解析不到宿主包） |
| `~/.dsh/profiles/desktop/cordis.patch.yml` | 追加 `id: dsh-longmemory` 覆盖行（pythonPath / dbPath / provider=workbuddy / model=deepseek-v4.1-flash） |
| 备份 | 两个文件各有 `.bak-<时间戳>` |

更新流程（幂等，改完源码就走这三步）：

```powershell
python scripts/build_release.py            # 装配 release/
python scripts/build_release.py --check    # 逐字节核对（含版本号一致性）
# 安装目录与 release/ 是**硬链接农场**：已有文件的改动会跟着同步，
# 但新增文件不会自动出现，必须显式建链接（或复制）：
$inst = "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\@local\dsh-longmemory"
New-Item -ItemType HardLink -Path "$inst\lib\model-config.js" -Target "$PWD\release\lib\model-config.js"
Copy-Item release\README.md "$inst\README.md" -Force
```

判断是不是硬链接：`fsutil hardlink list <安装目录>\kernel\config.py`，同时列出
`release\kernel\config.py` 就是（改内容会同步，**新增/删除文件仍要手动处理**）。

当前产物 **0.3.2**、45 个文件（`lib/` 14 个 JS + `kernel/` 23 个 py + 根 6 个 + `locale/` 2 个），
与 `release/` 哈希逐一相同。版本号改了之后，运行中的进程仍报旧值，**重启 Harness** 才会刷新。

生效方式：**重启 Harness**（插件解析表在启动时构建；`ResolutionRouter` 对新增包也要求重启）。

回滚：

```powershell
$prof = "$env:USERPROFILE\.dsh\profiles\desktop"
Remove-Item "$prof\node_modules\@local\dsh-longmemory" -Recurse -Force
# 从两个 .bak-<时间戳> 恢复 package.json / cordis.patch.yml
```

已知注意点：

- 当前产物 45 个文件（`lib/` 14 个 JS、`kernel/` 23 个 py、根 6 个 + `locale/` 2 个）；
  同步方式见上面的「更新流程」（硬链接农场：改内容自动同步，新增文件要手动链接）；
- 沙箱里 **Python 子进程不能写 profile 目录**（PowerShell 可以），所以安装走 PowerShell；
- Node 在 Harness 外解析不到 `@deepseek-ai/dsh-tools|dsh-llm` 属正常：这两个是宿主提供的包，
  由 Harness 的解析表按 installation scope 解析（`@nanmicoder/dsh-agent-teams` 同理）。
- **清单必须声明 `peerDependencies`**：宿主对链接包按 `readPeerNames` 决定是否把宿主包
  拦截到安装侧（`dsh-app-boot` 的 `routeLinked`）；缺了这一段，入口 `import` 直接失败，
  插件行不会报「内核没起来」，而是完全不执行。已工作插件 `@nanmicoder/dsh-agent-teams`
  的 `peerDependencies` 同样列出这些宿主包。
- 入口模块层写一行启动痕迹到 `~/.dsh/dsh-longmemory/shell.log`：有 `module loaded`
  说明模块解析通过，后续失败才可能是运行期问题。
- **内核启动必须先预热 numpy**（`main.warm_imports`）：Windows 下若主线程正阻塞在
  `for line in sys.stdin`，工作线程首次 `import numpy` 会永久卡住（实测 ingest
  永远不出 `ingest.completed`，也不报错）。`warm_imports` 在主线程先加载
  numpy / sqlite_vec / usearch / insight / ann，工作线程只拿缓存。
