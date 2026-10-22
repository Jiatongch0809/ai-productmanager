# 主 Agent / 子 Agent 协议

## 所有权模型

主 Agent 是唯一的工作流写入者。子 Agent 是隔离的分析器。

```text
主 Agent: 登记分片 → 派生子 Agent → 校验分片 → 合并 → 提交台账
         → 执行 Gate → 继续或等待
子 Agent: 接收有界任务 → 分析 → 分片落盘
```

子 Agent 不得调用 `gate_check`、`workflow_state`、`generate_report`，也不得写入正式台账或状态文件；只允许写入 `runtime/tasks/` 下的分片暂存区。

## 任务信封

每个子 Agent 任务应包含：

```json
{
  "task_id": "task-...",
  "parent_run_id": "run-...",
  "role": "six_lens_researcher",
  "stage": "SIX_LENS",
  "opportunity_id": "...",
  "parts": ["market", "user"],
  "objective": "...",
  "tool_budget": 20,
  "environment": {
    "retrieval": "web_fetch + so.com / cn.bing",
    "web_search": "unavailable",
    "evidence_policy": "docs/agent/retrieval-policy.md"
  },
  "checkpoint": "每完成一维立即调用 innovation:task part 落盘，不得攒到最后一次性返回",
  "allowed_outputs": ["conclusion", "evidence", "evidence_tasks"],
  "required_schema": "six_lens_part_v1",
  "forbidden_actions": [
    "modify workflow state",
    "run Gate",
    "generate formal artifacts"
  ]
}
```

- `parts`：本 worker 负责的分片，六看默认 2 维一片。
- `tool_budget`：**工具调用硬上限**。达到上限即中断当前维度、落盘已完成分片后正常返回，不允许耗尽上下文。
- `environment`：必须显式携带检索通道现状，否则子 Agent 会重复撞已知失效的通道。
- `checkpoint`：落盘时机写进信封，不依赖 worker 自行判断。

## 交接流程

1. 主 Agent 用 `innovation:task init` 登记分片清单，再派生子 Agent 执行有界任务。
2. 子 Agent 每完成一个分片立即调用 `innovation:task part` 落盘；它不修改共享状态。
3. 全部完成后，主 Agent 用 `innovation:task status` 核对分片完整性。
4. 主 Agent 用 `innovation:task merge` 合并分片（合并在脚本内完成，大块证据不经过模型上下文）。
5. 主 Agent 校验 schema 与 evidence 字段，再用 `innovation:ledger write-*` 提交结果；该命令内部自动完成分析阶段的 RUNNING → COMPLETED。
6. 主 Agent 执行 Gate 并遵循其动作。

## 分片与检查点

六看等大体量分析阶段必须分片执行，落盘布局：

```text
runtime/tasks/{opportunity_id}/{stage}/
    manifest.json            分片清单与状态
    parts/{dimension}.json   每个分片完成即落盘
    merged*.json             merge 产物，可直接喂给 --*-file
```

分片是暂存区，子 Agent 可以写；正式台账仍只能由主 Agent 写。这样上下文丢失最多损失一个分片，已完成分片可直接复用——`innovation:task init` 不会重置已 DONE 的分片。

## 并行执行

独立的证据收集可以扇出到多个子 Agent，按分片切分（六看默认 3 个 worker × 2 维）。扇入必须通过 `innovation:task merge` 产生一个验证后的结果。子 Agent 不得并发写同一个分片文件，也不得并发写同一台账。
