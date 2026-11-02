# 事件边界标注工具

用于给「事件切分」建立人工基准：把候选边界导出成逐条样本，人工标 `B / N / U`，再统计一致性。

## 标注规则（先统一口径）

> **如果「当前消息」属于另一个可独立命名的任务/目标，则标 `B`（边界）；否则标 `N`。**
> 拿不准标 `U`，不要猜。

## 用法

```powershell
# 1) 生成候选（默认离线规则；--live 用真实 StartLux，会产生调用）
python annotate/annotate.py prepare --session <SESSION_ID>
python annotate/annotate.py prepare --session <SESSION_ID> --live

# 2) 交互标注（两个人各跑一次，换 --field）
python annotate/annotate.py label --file annotate/data/<SESSION_ID>_candidates.jsonl --field label_a
python annotate/annotate.py label --file annotate/data/<SESSION_ID>_candidates.jsonl --field label_b

# 3) 统计进度 / 双人一致性
python annotate/annotate.py stats --file annotate/data/<SESSION_ID>_candidates.jsonl
```

## 键盘

| 键 | 含义 |
|---|---|
| `b` | 边界（会追问原因，可回车跳过） |
| `n` | 非边界 |
| `u` | 拿不准 |
| `s` | 跳过（保持未标注） |
| `q` | 保存退出 |
| `?` | 帮助 |

每标一条自动保存，中断不丢。

## 显示

- 每条消息渲染成一个块：标签、`│` 竖线、按终端宽度自动换行（CJK 也按字符断行）；
- 块与块之间空一行，块尾再用 `│` 收口，长文本不会糊在一起；
- 当前判定对象用 `>>` 标出，其余留缩进。

```text
   prev_2  │ （无）
           │

>> current │ user: 另外，评测脚本跑一次要多久？
           │
```

## 文件

- `annotate/data/<session>_candidates.jsonl`：程序读取的主文件；
- `annotate/data/<session>_candidates.csv`：同一份数据的 CSV（UTF-8 BOM，可直接用 Excel 标 `label_a/label_b/reason/note` 四列）；
- 两种格式字段一致，Excel 标完可以用 `stats` 读 CSV 吗？——**目前 `stats` 只读 JSONL**；用 Excel 标注时请另存为 CSV，再用下面的命令回灌（规划中）。

## 字段

| 字段 | 说明 |
|---|---|
| `sample_id` / `seq` / `role` | 样本编号、会话内序号、角色 |
| `prev_2` / `prev_1` / `current` / `next_1` / `next_2` | 判定所需上下文（各截断 200+200） |
| `model_decision` / `p_split` / `source` / `event_id` | 模型当时的判定与来源 |
| `label_a` / `label_b` | 两位标注者的结果（`B`/`N`/`U`） |
| `reason` | 边界原因：换任务 / 任务收尾 / 工具循环 / 长间隔 / 显式转折 / 其他 |
| `note` | 自由备注 |

## 候选是怎么选的

- 正样本：模型判为 `SPLIT` 的消息（全部保留）；
- 邻接负样本：每个边界前后各 1 条 `CONTINUE` 消息（防止只标正样本）；
- 随机负样本：默认再抽 20 条 `CONTINUE`；
- 上限 `--max-items`（默认 300），正样本优先。
