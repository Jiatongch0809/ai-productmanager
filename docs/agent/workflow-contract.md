# 工作流契约

## 标准流程

```text
INIT → TEARDOWN(可选) → SIX_LENS → SIX_LENS_GATE
     → FIT_ASSESSMENT → FIT_GATE → GENERATE（完整评估）
     → INNOVATION_REVIEW → DECISION_SUMMARY → PRODUCT_PROPOSAL（明确授权后）
```

阶段常量和状态迁移规则以 `scripts/contracts.py` 为准。

## 阶段所有权

| 阶段 | 所有者 | 必需输出 |
|------|--------|----------|
| INIT | 主 Agent | 机会身份和 RUNNING 状态 |
| TEARDOWN | 子 Agent | 结构化拆解结果 |
| SIX_LENS | 子 Agent | 结论、维度、证据、未知项 |
| SIX_LENS_GATE | 脚本 | CONTINUE / WAIT_USER / STOP / ERROR |
| FIT_ASSESSMENT | 子 Agent + 评分器 | 原始分数加评分器结果 |
| FIT_GATE | 脚本 | S51/S52/S53/S54 决策 |
| GENERATE | 脚本 | 受控 Markdown 产物 |
| INNOVATION_REVIEW | 人工评审，主 Agent 记录 | GO/REVISE/STOP、理由、风险与行动 |
| DECISION_SUMMARY | 负责人决策，主 Agent 记录 | PROPOSE/HOLD、理由与行动 |
| PRODUCT_PROPOSAL | 明确成案授权，主 Agent 记录 | 授权、产品定义和验证计划 |

分析阶段完成后，主 Agent 必须先提交 `COMPLETED`，再调用对应 Gate。Gate 结果写入独立 Gate 阶段，不能覆盖分析结果。

## 人机交互

- `WAIT_USER` 映射到状态 `WAITING_USER`。
- `resume --approve true` 只记录同意补证；Gate 保持 `WAITING_USER`。再次提交分析和重新运行 Gate 后才能推进。
- `resume --approve false` 将运行改为 `REJECTED`。
- 等待中或已停止仍可生成已完成阶段的六看/适配度 Markdown 判断；完整评估与提案必须通过所需 Gate。

## pending_output 契约

Gate 返回 WAIT_USER 或 STOP 时，必须写入 `pending_output`，字段如下：

| 字段 | 说明 |
| --- | --- |
| delivery_key | `{opportunity_id}:v{ledger_version}:{gate_stage}:{action}:{target}`，同一结果永远同一个键 |
| status | `pending` → `sending` → `sent`，三态 |
| gate_stage | Gate 阶段名 |
| action | WAIT_USER 或 STOP |
| output | Gate 结果（含 reason、evidence_tasks 等） |
| ledger_version | 台账版本号 |
| created_at | 创建时间 |
| attempt | 发送尝试次数（仅 sending/sent） |
| started_at | 开始发送时间（仅 sending） |
| sent_at | 发送成功时间（仅 sent） |
| receipt | 渠道回执（钉钉 openMessageId 或 session message id） |

幂等规则：
- 同一 delivery_key 已有未完成记录时，不新建。
- 版本变了才替换未发送记录。
- 同一 ledger_version 的 Gate 已是 WAIT_USER/STOP 时，再次调用直接返回原结果（`idempotent: true`）。

恢复规则：
- `list-pending-outputs` 返回 pending 和 sending。
- pending → 发送前改成 sending → 成功后改成 sent 并写入回执。
- sending + 已有回执：只补标记 sent，不再发送。
- sending + 没有回执：先核对；核对不到再发，attempt 加 1。
- sent：只跳过。
- replaySafe=no 时禁止重跑六看、重写台账、重跑 Gate。

## 工作流 B

需求驱动产品提案流程，详见 `docs/agent/workflow-b-contract.md`。

阶段：`DEMAND_INSIGHT → BP_GENERATE → PRD_MVP → PRD_MVP_GATE → MODAO_DEMO`

命令入口：`scripts/ops.py innovation:workflow-b:<command>`

## 失败处理

失败必须分类，因为处理方式完全不同：

| 类型 | 例子 | 处理 |
| --- | --- | --- |
| 瞬时 | I/O 错误、网络超时、单次脚本报错 | 原样重试，最多 2 次 |
| 结构性 | 子 Agent 上下文耗尽、返回值不符合 schema、同类失败重复出现 | **禁止原样重试**；必须降级：拆小分片、换检索通道、缩小范围 |
| 业务性 | Gate 返回 `STOP` | 不是错误，正常停止归档 |

- 业务 Gate 失败不是脚本错误；它是故意的 `STOP`。
- 不可恢复的执行错误变为 `FAILED` 并保留上一个已完成的阶段。
- 失败记录写入 `runtime/logs/{opportunity_id}.log`。

## 熔断

同一 stage、同一失败签名累计 **≥2 次**即判定为结构性失败：

1. 停止原样重派，改走降级路径（拆片 / 换通道 / 缩范围）；
2. 把失败签名、已尝试路径与降级动作写入台账版本历史；
3. 向用户说明降级原因与影响，不得继续无声重试。

用同样方式重跑一个已经因上下文耗尽而失败的任务，属于本规则明确禁止的行为。
