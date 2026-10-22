---
name: "demand-insight"
description: "需求洞察阶段：从用户痛点、场景、竞品缺口提炼核心需求，输出结构化需求分析报告。"
---

# 需求洞察（Demand Insight）

工作流 B 第一阶段，负责从模糊需求中提炼出可执行的产品方向。

## 输入

- 用户描述的问题/痛点/场景
- 可选：竞品链接、市场调研数据、用户反馈

## 输出

产物文件：`runtime/evidence_ledger/{opportunity_id}/workflow_b/demand_insight/demand_insight.json`

JSON 结构：

```json
{
  "opportunity_id": "xxx",
  "stage": "DEMAND_INSIGHT",
  "completed_at": "2026-10-15T09:00:00Z",
  "user_problem": "用户核心问题描述",
  "target_users": [
    {"persona": " persona 名称", "pain_point": "痛点", "frequency": "高频/中频/低频"}
  ],
  "use_cases": [
    {"scenario": "场景描述", "user_goal": "用户目标", "current_solution": "现有方案", "gap": "缺口"}
  ],
  "core_needs": [
    {"need": "需求描述", "priority": "P0/P1/P2", "evidence": "证据来源"}
  ],
  "market_gaps": [
    {"gap": "市场缺口", "opportunity_size": "大/中/小", "competitor_coverage": "无/部分/充分"}
  ],
  "recommended_direction": "推荐产品方向",
  "success_criteria": ["可量化的成功标准"]
}
```

## 执行步骤

1. **问题澄清**：如果用户描述模糊，先追问明确问题边界
2. **用户画像**：识别目标用户群体和核心痛点
3. **场景梳理**：列出 3-5 个核心使用场景
4. **需求提炼**：从场景中提炼 P0/P1/P2 需求
5. **竞品缺口**：分析现有竞品的不足之处
6. **方向推荐**：基于需求和缺口，推荐产品方向
7. **成功标准**：定义可量化的成功指标

## 子 Agent 任务书

派生需求洞察子 Agent 时，任务书必须包含：

```markdown
## 任务
执行需求洞察分析，输出结构化 JSON。

## 输入
- 机会 ID：{opportunity_id}
- 用户问题：{user_problem}
- 可选参考：{reference_data}

## 输出要求
1. 产物路径：`runtime/evidence_ledger/{opportunity_id}/workflow_b/demand_insight/demand_insight.json`
2. JSON 结构必须包含上述所有字段
3. 每个需求必须有证据来源（用户原话/调研数据/竞品分析）
4. 完成后调用：`python scripts/ops.py innovation:workflow-b:demand-insight --id {opportunity_id} --file <产物路径>`

## 禁止
- 不得修改其他机会的台账
- 不得执行 Gate 检查
- 不得生成 PRD 或 Demo
```

## 质量检查

- 至少 3 个用户画像
- 至少 3 个使用场景
- 至少 5 个核心需求（P0 至少 1 个）
- 至少 2 个市场缺口
- 成功标准必须可量化（如"用户留存率 > 60%"）

## 命令

```bash
python scripts/ops.py innovation:workflow-b:demand-insight --id ID --status COMPLETED --artifacts '["path/to/demand_insight.json"]'
```

参数说明：
- `--id`：机会 ID（必填）
- `--status`：阶段状态，可选 RUNNING/COMPLETED/FAILED（默认 RUNNING）
- `--artifacts`：产物路径列表，JSON 数组格式（可选）
