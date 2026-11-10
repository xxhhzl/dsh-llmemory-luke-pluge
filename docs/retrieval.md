# 记忆检索（retrieval）

> 实现：`src/retrieval.py`；工具入口：`src/tools.py`（`memory_search` / `memory_get` / `memory_recent`）。

## 1. 流水线

```
① 召回：FTS5 trigram 取 top-50（<3 字符退回 LIKE）
         范围过滤（scope=project）在**召回前**用 memory_fts 的 origin_project 列完成，
         避免范围外条目占满 top-k
         + 向量通道：外壳把查询文本向量化后随工具调用带下来（query_vector），
         走 ANN（usearch/HNSW）取 top-k；不可用时退回 sqlite-vec 精确 KNN
         + HyDE（外壳侧）：词法+向量都零命中时，外壳用提取槽模型生成假想回答、
         重算向量后重试一次（call_id 带 -h）；命中才采用——内核零改动
         + 联想扩展：同 subject / 同 cues 的 l3 以 0.5 分进候选（总量上限 20 条）
② 融合：多路 RRF(k=60)，rel = Σ 1/(60+rank)；单路时自然退化为 1/(60+rank)
③ 精排：final = α·rel_norm + (1-α)·Strength      α = 0.6
④ 选择：同事件簇最多 2 条；留 10% 探索位给 use_count=0 的条目
⑤ 计数：只对最终返回的条目 use_count+1、刷新 last_used_at
```

向量通道是**可选**的：embedding 端点没配 / 不可用 / 没装 sqlite-vec 时
`query_vector` 为空，检索完全退化成 v1 的纯词法路径，接口行为不变。
查询向量由**外壳**算（`src/js/tools-bridge.js` 调 `embedTexts`），内核只在
`tools.calls` 里接收，避免主循环阻塞在 HTTP 上。

### 1.1 ANN 加速层（usearch / HNSW）

`vec0` 是**事实源**，ANN 是**进程内缓存**（`src/ann.py`）：

- 首次检索按 kind 全量装载 `vec_*` 表（当前 43 条经验 <10ms；万级条目 1s 量级），
  之后按 `Database.take_vec_changes()` 的差量（新增/覆盖/删除）增量同步；
- 不落盘：进程重启后从 `vec0` 重建，避免索引与库不一致；
- 缓存按**数据库实例**隔离（`WeakKeyDictionary`），多个库同时打开不会串；
- 项目过滤用「过量取数 + Python 过滤」：先取 `k*8`，不够就翻倍，
  直到够 k 条或取完整个索引（HNSW 没有原生过滤，本项目「本项目 + 通用」
  集合很小，这样最稳）；
- `usearch`/`numpy` 缺失、维度不符、检索异常 → 自动退回 `vec_search` 精确 KNN，
  只在日志留 `db.ann_search_failed`；
- 入库与查询都做**单位化**（`Database._vec_unit`）：`vec0` 的 L2 与 usearch 的
  cos 只有在向量等长时排序才一致，否则两条路径会给出不同名次。

实测（43 经验 / 55 认知，bge-m3 1024 维）：ANN 与精确 KNN 的 top-k 顺序逐条一致；
项目过滤结果一致；覆盖写与删除后下一次检索即生效。

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
| 人工反馈（0.5.1） | `feedback_score` 截断到 ±3 后除以 3 → [−1, 1]；`useful` +1、`useless` −1 |

**使用次数的字段名不同**：`event` 表是 `cnt`，`experience`/`cognition` 是 `use_count`，
统一经 `_use_count(record, kind)` 读取。新近项统一读 `last_used_at`；三张表都已具备该列，
事件在 `bump_events` / `bump_event_cnt` 时一并刷新（老库为 NULL 时退化到 `created_at`）。

```
Strength = 0.45·B̂ + 0.25·importance + 0.15·confidence
         + 0.10·project_match + 0.25·feedback + 0.05·assoc
```

`Strength` 最后截断到 [0, 1]（反馈项可能把总分顶出 1）。反馈项**有界**：
±3 次到饱和，避免长期使用后少数条目被反馈分彻底支配；`event` 没有
`feedback_score`，反馈项恒为 0。

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
- 事件表 0.5.1 起也有 `valid_to`：`outdated` / `wrong` 会置时间并摘掉全文索引，
  因此事件和 l3 一样会退出召回（`_eligible` 只做兜底，不靠它过滤）；
- 失效/被取代的 l3 会在**同一事务**内从 `memory_fts` 删除，并调用 `vec_unindex`
  摘掉向量，不再占用召回名额；`Database.invalidate_l3` / `mark_l3_outdated` /
  `retire_event` 是唯一入口，工具层不再自己拼 SQL。

## 4. 三个工具

| 工具 | 行为 |
|---|---|
| `memory_search(query, limit, kind?, scope?)` | 上面的检索，返回 `{items, count}`；FTS 命中的条目带 `meta.excerpt`，返回体带 `next_action_hint` |
| `memory_get(id, kind, max_chars?, msg_offset?)` | 再认：event 返回事件 + 正文（默认 8000 字符分页，`next_msg_offset` 续读）；l3 返回整行；计一次使用 |

事件正文分页先用 SQL 聚合获取条数/字符数，再按 32 条消息分批取正文；只有显式 `max_chars=0` 才完整加载。FTS 重建同时回灌洞见，并跳过过期 L3。
| `memory_recent(session_id?, from_at, to_at, limit)` | 时间区间：事件按区间相交，l3 按 `created_at`（入库时间）；两类**独立配额**，l3 最多 3 条；**session_id 省略 = 跨会话回放**（0.4.0 起） |

返回条目：`{kind, id, title, text, score, meta{importance, confidence, origin_project, use_count, ...}}`。

### `memory_recent` 的口径（定稿）

- **事件**：发生区间（`MIN/MAX(occur_at)`）与查询区间相交即返回，按结束时间倒序；
- **l3**：按 `created_at`（**入库时间**，不是事情发生时间）落在区间内返回——
  回答的是「那段时间沉淀了什么」；补录老条目会显示为入库那天，这是刻意接受的语义；
- 配额：`l3_quota = min(3, limit)`，事件占 `limit - l3_quota`；旧实现混排后按固定分
  排序再截断（事件恒 1.0 > l3 恒 0.5），事件一多 l3 就被全灭；
- 时间比较走 SQLite `datetime()`，`+08:00` 与 `+00:00` 混存不会错 8 小时；
- 参数非法（无法解析 / from > to）返回 `-32602`，不再是 `-32603` 内部错误。

## 5. 读取侧语义去重（0.5.1 起只读选中条目）

选完 top-k 后，对 l3（经验/认知/洞见）做一次余弦去重：同一件事被不同事件抽成
多条时只保留分高的一条，其余丢弃并把数量记进 `meta.merged`（事件不参与）。

旧实现每次检索都 `list_vec_blobs` 全量读三张向量表并解包（O(全部条数×维度)），
库长大后会抵消 ANN 的收益；现在只按 `selected` 里的 `(kind, id)` 批量
`WHERE ref_id IN (...)` 取需要的几条（`Database.get_vec_blobs`）。

去重会删条：删完腾出的名额由 `_backfill_after_dedup` 按原分数顺序从剩余候选
补回（补入项同样要与已保留条目比一次余弦），所以 `limit=2` 不会因为两条同义
只剩 1 条。补位不重新召回、不改变已选条目的分数。

## 6. v1 / v2 / v3 / v4

| 版本 | 召回 | 说明 |
|---|---|---|
| v1 | FTS + 元数据联想 | 无新依赖 |
| v2 | FTS + sqlite-vec 精确 KNN + 元数据联想，RRF 融合 | 依赖 `sqlite-vec` 与 embedding 端点 |
| v3 | 同 v2，向量通道走 usearch/HNSW（ANN），`vec0` 保留为事实源与降级路径 | 额外依赖 `usearch` + `numpy`；任一层缺失自动降级，接口不变 |
| v4（当前） | 同 v3 + 零命中 HyDE 重试（外壳侧假想文档向量）+ 摘要第二句保留具体产出值 | HyDE 只在零命中且向量通道可用时触发，正常命中不多花模型调用 |

实测（bge-m3 本地 Ollama，1024 维）：词面零重叠的查询（`borderWidth` vs
「边框粗细保持一致」）只能靠向量通道召回；`vec.reindex` 重建后
`vec.status` 显示各表条数与 ANN 后端，检索结果里两路都命中的条目 RRF 分更高。

## 7. 已知限制

- FTS5 `trigram` 要求查询 ≥3 字符，短查询走 `LIKE` 全表扫描（当前数据量下可接受）；
  短查询的 `%`/`_` 已转义，并按「标题命中优先、正文更短优先」排序，
  否则 top-k 会退化成表的插入顺序；
- ANN 索引是**进程内缓存**：重启后首次检索要全量装载（万级约 1s），
  换来的是不需要维护索引与库的同步；条目量再上一个量级时应改成落盘 + 版本校验；
- 查询向量由外壳现算（约 30ms 热态、首次加载模型 3s+），冷启动首次检索会明显变慢；
- `related_events` 目前没有写入方，联想扩展先用「同 subject / 同 cues」替代；
- 探索位是确定性选取（按分数取冷条目），不做随机扰动，便于测试复现。

被动召回在候选阶段排除核心块已注入的 ID，以及同会话、全部原文消息都在
宿主当前投影中的事件；显式 `memory_search` 保持完整查询能力。详见 `docs/core_memory.md` §6。
