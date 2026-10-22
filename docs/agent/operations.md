# 操作命令

使用 `python scripts/ops.py` 作为唯一命令入口。直接调用 `scripts/` 下的底层脚本属于实现细节，不要写进流程。

## 台账与流程

```text
python scripts/ops.py innovation:ledger init --id ID --product NAME [--definition D] [--user U]
python scripts/ops.py innovation:ledger write-teardown --id ID --evidence-file PATH
python scripts/ops.py innovation:ledger write-six-lens --id ID --conclusion YES --evidence-file PATH [--dimensions-file PATH] [--tasks-file PATH]
python scripts/ops.py innovation:ledger write-fit --id ID --scores-file PATH [--evidence-file PATH]
python scripts/ops.py innovation:ledger read --id ID
python scripts/ops.py innovation:gate check --id ID --stage SIX_LENS
python scripts/ops.py innovation:gate check --id ID --stage FIT_ASSESSMENT
python scripts/ops.py innovation:state read --id ID
python scripts/ops.py innovation:state list-pending
python scripts/ops.py innovation:state list-pending-outputs
python scripts/ops.py innovation:state mark-output-sending --id ID
python scripts/ops.py innovation:state mark-output-sent --id ID [--receipt RECEIPT]
python scripts/ops.py innovation:state resume --id ID --approve true
python scripts/ops.py innovation:report generate --id ID --type full_evaluation
python scripts/ops.py innovation:review record --id ID --file review.json
python scripts/ops.py innovation:decision record --id ID --file decision.json
python scripts/ops.py innovation:proposal record --id ID --file proposal.json
python scripts/ops.py innovation:report generate --id ID --type product_proposal
```

## 分片任务（大体量阶段）

```text
python scripts/ops.py innovation:task init --id ID --stage SIX_LENS [--parts market,user,...]
python scripts/ops.py innovation:task part --id ID --stage SIX_LENS --part market --file PATH
python scripts/ops.py innovation:task status --id ID [--stage SIX_LENS]
python scripts/ops.py innovation:task merge --id ID --stage SIX_LENS [--allow-partial]
```

- 六看默认六维，按 **3 个 worker × 2 维**分工；
- `task part` 由子 Agent 在**每完成一维时立即调用**——分片是暂存区，允许 worker 写；正式台账仍只由主 Agent 写；
- `task merge` 在脚本内完成合并，缺片会被拒绝（除显式 `--allow-partial`，仅用于明确降级为观察）；产物直接喂给 `write-six-lens --*-file`，证据不经过模型上下文；
- `task init` 可重复执行且**不会重置已 DONE 的分片**——上下文丢失后靠这个续跑，只补缺失分片。

## 命令语义

- `ledger init`：初始化台账，并自动完成 `INIT` 的 RUNNING → COMPLETED。
- `ledger write-teardown`：写入单品拆解证据（至少一条），自动完成 `TEARDOWN` 的 RUNNING → COMPLETED。
- `ledger write-six-lens`：`--conclusion` 取值 `YES`/`CONDITIONAL`/`WATCH`/`NO`；自动完成 `SIX_LENS` 的 RUNNING → COMPLETED。
- `ledger write-fit`：传八维原始分（每维 0-10），自动调用 `fit_scorer.py` 计算加权总分与 Gate，并完成 `FIT_ASSESSMENT` 的 RUNNING → COMPLETED。禁止手工计算总分。
- `gate check`：仅支持 `SIX_LENS` 与 `FIT_ASSESSMENT` 两个分析阶段的 Gate，结果写入对应的 Gate 阶段。
- `report generate`：`--type` 取值 `full_evaluation`/`six_lens`/`fit_assessment`/`product_proposal`，当前只生成 Markdown；阶段报告允许展示停止或待补证结论。
- `review/decision/proposal record`：依次记录人工评审、汇总决策和明确授权的提案底稿；JSON 字段见各命令及 README。
- `state update --id ID --stage S --status ST`：仅用于恢复等特殊场景。Gate 结果由 `gate check` 自动写入 Gate 阶段，禁止用它覆盖 Gate 结果。
- `state list-pending-outputs`：返回所有待发送的 Gate 结果（status=pending 或 sending），含 delivery_key、attempt、started_at。
- `state mark-output-sending --id ID`：标记 pending_output 为发送中，记录 attempt 和 started_at，防止并发发送。
- `state mark-output-sent --id ID [--receipt RECEIPT]`：标记 pending_output 为已发送，写入渠道回执（钉钉 openMessageId 或 session message id）。已是 sent 时直接返回。

## 阶段名

合法阶段以 `scripts/contracts.py` 为准：`INIT`、`TEARDOWN`、`SIX_LENS`、`SIX_LENS_GATE`、`FIT_ASSESSMENT`、`FIT_GATE`、`GENERATE`、`INNOVATION_REVIEW`、`DECISION_SUMMARY`、`PRODUCT_PROPOSAL`。

**没有 `FRONTIER` 和 `INTAKE` 阶段。** 前沿扫描在输出漏斗与 A/B/C 分流后即停止，不写台账、不执行 Gate。台账 `update` 只接受 `TEARDOWN`、`SIX_LENS`、`FIT_ASSESSMENT`。

## 退出码

命令包装器必须保留底层退出代码。`WAIT_USER` 是带有等待动作的成功命令结果；`STOP` 和 `ERROR` 仍然是可区分的退出结果。

- `0`：成功，含 `CONTINUE` 与 `WAIT_USER`；
- `1`：业务 `STOP`（例如六看结论为 NO）；
- `2`：`ERROR` 或状态落盘失败（例如结论缺失、前置阶段未完成）。

## JSON 参数

**首选 `--*-file` 传文件路径**：大 payload 不经模型上下文，也完全避开 shell 转义问题。

内联 JSON 仅在 payload 很小时使用，且必须按当前 shell 转义：

- bash / sh：单引号 `--scores '{"market_certainty":8}'`；
- Windows PowerShell 5.1：双引号并对内层引号加反引号 `--scores "{\`"market_certainty\`":8}"`；单引号写法会被剥掉内层引号，导致 `JSON 解析失败`；
- 报出 `Expecting property name enclosed in double quotes` 即说明内层引号被吞，改用 `--*-file`。

## 工作流 B：需求驱动产品提案

```text
python scripts/ops.py workflow-b start --id ID [--skip STAGE1,STAGE2]
python scripts/ops.py workflow-b demand-insight --id ID --file PATH
python scripts/ops.py workflow-b bp --id ID --file PATH
python scripts/ops.py workflow-b prd --id ID --file PATH
python scripts/ops.py workflow-b demo --id ID --proto-url URL
python scripts/ops.py workflow-b rollback --id ID
python scripts/ops.py workflow-b status --id ID
```

- `workflow-b start`：初始化工作流 B，可选跳过阶段（PRD_MVP 和 MODAO_DEMO 不可跳过）。
- `workflow-b demand-insight`：写入需求洞察产物，自动通过轻量 Gate。
- `workflow-b bp`：写入商业计划书产物，自动通过轻量 Gate。
- `workflow-b prd`：写入 PRD 文档产物，触发 HITL Gate 等待用户确认。
- `workflow-b demo`：写入墨刀原型链接，自动通过轻量 Gate。
- `workflow-b rollback`：回退到上一个已完成阶段。
- `workflow-b status`：查看工作流 B 当前状态。

详细契约读取 `docs/agent/workflow-b-contract.md`。

## 添加业务线

创建 `scripts/ops_commands/<namespace>/__init__.py` 并暴露 `register_commands(registry)` 函数。注册本地键如 `state.read` 或 `report.generate`；`ops.py` 在发现期间将它们限定为 `<namespace>:<key>`。因此，业务线不需要修改路由器或覆盖另一条线的命令名。它应该保留自己的 schema 和 Skill，只在工作流语义相同的地方重用共享状态和 Gate 契约。