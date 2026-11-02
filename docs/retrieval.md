# 记忆检索（retrieval）

> 实现：`src/retrieval.py`；工具入口：`src/tools.py`（`memory_search` / `memory_get` / `memory_recent`）。

## 1. 流水线

```
① 召回：FTS5 trigram 取 top-50（<3 字符退回 LIKE）
         + 联想扩展：同 subject / 同 cues 的 l3 以 0.5 分进候选
② 融合：单路时 rel = 1/(1+rank)；多路（v2 向量）时 RRF(k=60)
③ 精排：final = α·rel_norm + (1-α)·Strength      α = 0.6
④ 选择：同事件簇最多 2 条；留 10% 探索位给 use_count=0 的条目
⑤ 计数：只对最终返回的条目 use_count+1、刷新 last_used_at
```

## 2. 记忆强度（人类记忆映射）

| 机制 | 公式 / 字段 |
|---|---|
| ACT-R 基础激活（频率×新近） | `B = ln(1+use_count) − 0.5·ln(1+days_since_last_use)` |
| 重要性（显著性） | `(importance−1)/4` |
| 确信度 / 可靠性 | `confidence` |
| 情境匹配（编码特异性） | 同项目 1、通用 0.5、跨项目 0 |
| 联想来源（扩散激活） | 直接命中 1、扩展 0.5 |

```
Strength = 0.45·B̂ + 0.25·importance + 0.15·confidence
         + 0.10·project_match + 0.05·assoc
```

权重在 `config.search.w_*`，`alpha` 控制相关性与记忆强度的占比（默认 0.6/0.4）。

## 3. 过滤（SQL 硬条件）

```sql
status = 'active'
AND (valid_to IS NULL OR valid_to > now)
AND (:scope = 'all' OR origin_project IS NULL OR origin_project = :current_project)
```

- 默认 `memory_search(scope="all")`：跨项目可见，条目带上 `origin_project`；
- `scope="project"` 时只返回本项目 + 通用（`origin_project IS NULL`）；
- `status` / `valid_to` 永不参与打分——过期就是排除。

## 4. 三个工具

| 工具 | 行为 |
|---|---|
| `memory_search(query, limit, kind?, scope?)` | 上面的检索，返回 `{items, count}` |
| `memory_get(id, kind)` | 再认：event 返回事件 + 全部消息；l3 返回整行；计一次使用 |
| `memory_recent(session_id, from_at, to_at, limit)` | 时间区间：事件按区间相交，l3 按 `created_at` |

返回条目：`{kind, id, title, text, score, meta{importance, confidence, origin_project, use_count, ...}}`。

## 5. v1 / v2

| 版本 | 召回 | 说明 |
|---|---|---|
| v1（当前） | FTS + 元数据联想 | 无新依赖，143 事件规模足够 |
| v2 | 加向量通道（外壳代理 embedding） | FTS/向量各 top-50，RRF 融合后再进精排 |

## 6. 已知限制

- FTS5 `trigram` 要求查询 ≥3 字符，短查询走 `LIKE` 全表扫描（当前数据量下可接受）；
- `related_events` 目前没有写入方，联想扩展先用「同 subject / 同 cues」替代；
- 探索位是确定性选取（按分数取冷条目），不做随机扰动，便于测试复现。
