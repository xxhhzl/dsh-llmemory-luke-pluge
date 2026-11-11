# 技术架构与质量门禁

## 版本与依赖

`VERSION` 是当前版本事实源。`scripts/check_project_consistency.py` 校验 Python 内核、JavaScript 协议、发布清单、项目元数据及带有当前版本标记的文档。`pyproject.toml` 声明支持范围，`requirements.lock.txt` 记录实际验证环境。

运行环境分为三档：

| 档位 | 必需组件 | 行为 |
|---|---|---|
| Core | Python、Pydantic、SQLite FTS5 | 词法检索 |
| Vector | Core、sqlite-vec | 精确向量检索 |
| ANN | Vector、NumPy、USearch | 进程内 ANN 加速 |

可选组件缺失时预检结果为 `DEGRADED`；Python、Pydantic、FTS5 或项目文件不满足要求时为 `FAIL`。

## 质量评测

评测器直接调用 `retrieval.search(..., touch=False)`，避免评测过程改变使用次数。期望条目使用 `kind:id` 标识，指标计算与数据库执行分离，便于对固定结果做单元测试。

质量门槛集中保存在 `tests/eval/thresholds.json`。小型样例只验证评测器契约；正式发布应换用脱敏的真实会话快照和至少 200 条人工标注查询。
其中 Precision@5 使用固定分母 5；当每个查询只有一条正确记忆时，其理论最大值为 0.2，因此不能照搬多相关文档数据集的阈值。

## 性能基准

性能脚本只在临时目录生成合成数据库。每个操作先预热，再用 `perf_counter_ns` 测量多次，输出 JSON 和 Markdown。冷启动、Embedding HTTP 和模型调用属于独立链路，不与 SQLite 热态耗时混算。

## 门禁分层

- 提交门禁：一致性、预检、单元测试、小型质量集、快速性能烟测。
- 夜间门禁：完整质量集、1 万条热态性能、三档依赖环境。
- 发布门禁：真实 DSH 验收、升级回滚、故障恢复和 10 万条容量测试。
