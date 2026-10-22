# 工作流 B 契约：需求驱动产品提案

## 概述

工作流 B 适用于用户明确提出需求洞察、BP 生成、PRD 输出或 Demo 演示的场景。与工作流 A（产品机会评估）不同，工作流 B 从需求出发，最终输出可演示的产品原型。

## 阶段流程

```text
DEMAND_INSIGHT → BP_GENERATE → PRD_MVP → PRD_MVP_GATE → MODAO_DEMO
```

阶段常量和状态迁移规则以 `scripts/contracts.py` 为准。

## 阶段定义

| 阶段 | 所有者 | 必需输出 | Gate 类型 |
|------|--------|----------|-----------|
| DEMAND_INSIGHT | 子 Agent | 需求分析报告（JSON） | 轻量 Gate |
| BP_GENERATE | 子 Agent | 商业计划书（Markdown） | 轻量 Gate |
| PRD_MVP | 子 Agent | PRD 文档（Markdown） | HITL Gate |
| PRD_MVP_GATE | 脚本 | 用户确认 | - |
| MODAO_DEMO | 子 Agent | 墨刀原型链接 | 轻量 Gate |

## Gate 检查

### 轻量 Gate（需求洞察、BP、Demo）

检查条件：
1. 产物文件存在
2. 产物非空
3. 基本结构完整（JSON 可解析 / Markdown 包含必要章节）

通过条件：所有检查项通过 → CONTINUE

### HITL Gate（PRD 阶段）

检查条件：
1. PRD 文档已生成
2. PRD 包含必要章节（背景、目标、功能列表、非功能需求、验收标准）
3. 等待用户确认

通过条件：用户明确确认 → CONTINUE

## 阶段跳过

支持跳过非关键阶段：

```bash
python scripts/ops.py workflow-b start --id ID --skip DEMAND_INSIGHT
python scripts/ops.py workflow-b start --id ID --skip DEMAND_INSIGHT,BP_GENERATE
```

跳过规则：
- PRD_MVP 不可跳过（核心产物）
- MODAO_DEMO 不可跳过（最终交付物）
- 跳过的阶段不执行 Gate 检查

## 回退

支持回退到上一阶段：

```bash
python scripts/ops.py workflow-b rollback --id ID
```

回退规则：
- 只能回退到上一个已完成阶段
- 回退后当前阶段状态变为 PENDING
- 回退不删除产物文件，只重置状态

## 命令入口

```bash
python scripts/ops.py workflow-b start --id ID [--skip STAGE1,STAGE2]
python scripts/ops.py workflow-b demand-insight --id ID --file PATH
python scripts/ops.py workflow-b bp --id ID --file PATH
python scripts/ops.py workflow-b prd --id ID --file PATH
python scripts/ops.py workflow-b demo --id ID --proto-url URL
python scripts/ops.py workflow-b rollback --id ID
python scripts/ops.py workflow-b status --id ID
```

## 产物存储

所有产物存储在 `runtime/evidence_ledger/{opportunity_id}/workflow_b/` 下：

```text
workflow_b/
├── demand_insight/
│   └── demand_insight.json
├── bp/
│   └── business_plan.md
├── prd/
│   └── prd_mvp.md
└── demo/
    └── demo_manifest.json
```

## 与工作流 A 的关系

- 工作流 A 和 B 独立运行，互不干扰
- 同一 opportunity_id 可以同时运行两个工作流
- 工作流 A 的输出（如六看分析）可以作为工作流 B 的输入参考
- 工作流 B 的产物不写入工作流 A 的台账

## 失败处理

与工作流 A 相同：
- 瞬时错误：原样重试，最多 2 次
- 结构性错误：降级处理（拆小分片、换通道、缩范围）
- 业务性失败：HITL Gate 拒绝，等待用户修改

## 退出码

- `0`：成功，含 CONTINUE
- `1`：业务 STOP（HITL Gate 拒绝）
- `2`：ERROR 或状态落盘失败
