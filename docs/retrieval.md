# 记忆检索（retrieval）

> 实现：`src/retrieval.py`；工具入口：`src/tools.py`（`memory_search` / `memory_get` / `memory_recent`）。

## 1. 流水线

```
① 召回：FTS5 trigram 取 top-50（<3 字符退回 LIKE）
         范围过滤（scope=project）在**召回前**用 memory_fts 的 origin_project 列完成，
         避免范围外条目占满 top-k
         + 联想扩展：同 subject / 同 cues 的 l3 以 0.5 分进候选（总量上限 20 条）
② 融合：单路时 rel = 1/(1+rank)；多路（v2 向量）时 RRF(k=60)
③ 精排：final = α·rel_norm + (1-α)·Strength      α = 0.6
④ 选择：同事件簇最多 2 条；留 10% 探索位给 use_count=0 的条目
⑤ 计数：只对最终返回的条目 use_count+1、刷新 last_used_at
```

两条实现约束（2026-11-03 修）：

- **按 kind 批量取数**：命中的 id 先分组，各用一条 `WHERE id IN (...)` 取回；
  旧实现每条命中单独查库，一次检索实测上百条 SQL。
- **归一化按 kind 分组**：事件用 `cnt`、l3 用 `use_count`，量纲不同，
  混在一张量尺上归一会让一类变化压缩另一类区间。

## 2. 记忆强度（人类记忆映射）

| 机制 | 公式 / 字段 |
|---|---|
| ACT-R 基础激活（频率×新近） | `B = ln(1+use_count) − 0.5·ln(1+days_since_last_use)` |
| 重要性（显著性） | `(importance−1)/4`；事件无此列，取中性 0.5 |
| 确信度 / 可靠性 | `confidence`；事件无此列，取中性 0.5（不再用 0.8 默认值白送分） |
| 情境匹配（编码特异性） | 同项目 1、通用 0.5、跨项目 0 |
| 联想来源（扩散激活） | 直接命中 1、扩展 0.5 |

**使用次数的字段名不同**：`event` 表是 `cnt`，`experience`/`cognition` 是 `use_count`，
统一经 `_use_count(record, kind)` 读取。新近项统一读 `last_used_at`；三张表都已具备该列，
事件在 `bump_events` / `bump_event_cnt` 时一并刷新（老库为 NULL 时退化到 `created_at`）。

```
Strength = 0.45·B̂ + 0.25·importance + 0.15·confidence
         + 0.10·project_match + 0.05·assoc
```

权重在 `config.search.w_*`，`alpha` 控制相关性与记忆强度的占比（默认 0.6/0.4），
所以**相关性权重始终高于热度**：cnt 高只会在同等相关性下占优，不会盖过更相关的条目。

## 3. 过滤（SQL 硬条件）

```sql
status = 'active'
AND (valid_to IS NULL OR valid_to > now)
AND (:scope = 'all' OR origin_project IS NULL OR origin_project = :current_project)
```

- 默认 `memory_search(scope="all")`：跨项目可见，条目带上 `origin_project`；
- `scope="project"` 时只返回本项目 + 通用（`origin_project IS NULL`），且该过滤**下推到
  FTS 召回**（`memory_fts.origin_project`，UNINDEXED 列），不再取回后再筛；
- 跨项目召回可加额外衰减：`config.search.cross_project_penalty`（0–1，默认 **0 = 关闭**）。
  它不是硬过滤——`scope=all` 时跨项目条目仍可被搜到，只是排序更靠后；开启前需用固定
  query 集做 A/B，避免直接改变打分分布；
- `status` / `valid_to` 永不参与打分——过期就是排除；
- 失效/被取代的 l3 会在**同一事务**内从 `memory_fts` 删除，不再占用召回名额
  （检索层仍保留 `_eligible` 兜底，防止历史脏数据漏删）。

## 4. 三个工具

| 工具 | 行为 |
|---|---|
| `memory_search(query, limit, kind?, scope?)` | 上面的检索，返回 `{items, count}` |
| `memory_get(id, kind)` | 再认：event 返回事件 + 全部消息；l3 返回整行；计一次使用 |
| `memory_recent(session_id, from_at, to_at, limit)` | 时间区间：事件按区间相交，l3 按 `created_at`（入库时间）；两类**独立配额**，l3 最多 3 条 |

返回条目：`{kind, id, title, text, score, meta{importance, confidence, origin_project, use_count, ...}}`。

### `memory_recent` 的口径（定稿）

- **事件**：发生区间（`MIN/MAX(occur_at)`）与查询区间相交即返回，按结束时间倒序；
- **l3**：按 `created_at`（**入库时间**，不是事情发生时间）落在区间内返回——
  回答的是「那段时间沉淀了什么」；补录老条目会显示为入库那天，这是刻意接受的语义；
- 配额：`l3_quota = min(3, limit)`，事件占 `limit - l3_quota`；旧实现混排后按固定分
  排序再截断（事件恒 1.0 > l3 恒 0.5），事件一多 l3 就被全灭；
- 时间比较走 SQLite `datetime()`，`+08:00` 与 `+00:00` 混存不会错 8 小时；
- 参数非法（无法解析 / from > to）返回 `-32602`，不再是 `-32603` 内部错误。

## 5. v1 / v2

| 版本 | 召回 | 说明 |
|---|---|---|
| v1（当前） | FTS + 元数据联想 | 无新依赖，143 事件规模足够 |
| v2 | 加向量通道（外壳代理 embedding） | FTS/向量各 top-50，RRF 融合后再进精排 |

## 6. 已知限制

- FTS5 `trigram` 要求查询 ≥3 字符，短查询走 `LIKE` 全表扫描（当前数据量下可接受）；
  短查询的 `%`/`_` 已转义，并按「标题命中优先、正文更短优先」排序，
  否则 top-k 会退化成表的插入顺序；
- `related_events` 目前没有写入方，联想扩展先用「同 subject / 同 cues」替代；
- 探索位是确定性选取（按分数取冷条目），不做随机扰动，便于测试复现。
