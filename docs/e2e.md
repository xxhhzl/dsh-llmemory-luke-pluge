# 端到端测试（模拟外壳）

> 脚本：`tests/shell_sim.py`（外壳模拟器）、`tests/run_e2e.py`（驱动）。
> 不依赖真实 JS 外壳，用真实 DSH 会话 + 本地模型跑完整链路。

## 1. 它验证什么

```
真实会话消息
  → ingest.request
  → 内核：入库 → 切分（JEV）→ 摘要（A）→ 合并裁决（B）→ FTS + digest
  → ingest.completed（计数 / digest / decisions）
  → tools.calls：memory_search / memory_get
  → 直接打开 SQLite 核对行数与状态
```

覆盖：消息幂等入库、事件切分与 `memory.event_id` 回填、摘要与 l3 落库、supersede/invalidate、FTS 召回、`memory_get` 正文回取。

## 2. 用法

```powershell
# 取会话前 40 条（快，约 3 次模型调用）
.\.venv\Scripts\python.exe tests\run_e2e.py --session <SESSION_ID> --limit 40

# 300 条 + 制造一次「换库」冲突，验证 supersede
.\.venv\Scripts\python.exe tests\run_e2e.py --session <SESSION_ID> --limit 300 --contradiction --keep

# 整段会话（1330 条，几十次模型调用）
.\.venv\Scripts\python.exe tests\run_e2e.py --session <SESSION_ID> --limit 0 --keep
```

| 参数 | 说明 |
|---|---|
| `--session` | 会话 id 子串 |
| `--limit` | 截取条数；`0` 表示全部 |
| `--model` | 本地模型 id，默认 `glm-5.3` |
| `--contradiction` | 预置一条旧认知 + 追加「换个话题：数据库换成 Postgres」，验证 supersede |
| `--keep` | 保留 `.tmp/e2e/<时间戳>/`（含 `kernel.stderr.log` 与 `memory.db`） |

## 3. 外壳模拟器怎么工作

- 子进程启动 `src/main.py`，cwd 指向临时目录（数据库落在临时目录，不污染项目）；
- 读线程解析下行帧：`llm.request` 转成本地 chat 调用后回 `llm.result`，其余帧入队；
- 模型访问凭据由本机环境提供；脚本不打印、不落日志。公开测试记录不得包含凭据来源或值。

## 4. 已知限制

- 切分默认**增量**：只判定「最后一条已绑定消息之后」的新消息，续写段复用已有事件的
  点数/摘要/预算（`open_event`）；空库或找不到锚点时才回退全量扫描。
  实测 200 条消息分 4 批写入：旧的全量重切 246 次模型判定 → 增量 99 次；
- 单事件失败不再中断整个 job：失败数进 `ingest.completed` 的 `failed` 字段；
- 已闭合事件被续写时按阈值补跑摘要（新增 ≥5 条 / 增量 ≥50 token 且 ≥20% / 距上次 ≥24h），
  重跑带 `previous_summary`，只更新事件摘要与新条目，不新建事件；
- 真实模型可能不给 `experiences`/`cognitions`，此时只落事件摘要，属正常；
- 摘要标题按 24 字符硬截断，可能截在词中间。
