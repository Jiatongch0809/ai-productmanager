# 适配度评估子 Agent 任务书模板

主 Agent 派生适配度子 Agent 时使用本模板。**子 Agent 只输出原始分与证据，不计算总分、不写台账、不执行 Gate**；总分与 Gate 由 `fit_scorer.py` 计算，落库由主 Agent 完成。

## 任务书模板

```python
task = f"""
你是适配度评估执行者。请对以下机会执行八维评分。

机会 ID：{opportunity_id}
产品定义：{ledger['identity']['product_definition']}
目标用户：{ledger['identity']['target_user']}

【输出格式要求】
必须按以下 JSON Schema 返回，不得遗漏任何字段：
{{
  "scores": {{
    "market_certainty": 7,
    "user_demand": 9,
    "category_validation": 7,
    "ai_value": 5,
    "org_capability": 6,
    "quick_launch": 8,
    "business_path": 5,
    "cost_risk": 6
  }},
  "evidence": [
    {{"category": "...", "fact": "...", "source": "...", "date": "...", "grade": "...", "signal": "..."}}
  ],
  "strengths": ["核心优势1", "核心优势2"],
  "gaps": ["关键缺口1", "关键缺口2"],
  "verification_tasks": ["验证任务1", "验证任务2"]
}}

【评分规则】
- 每维评分范围：0-10 分，8 个维度必须全部给出
- 评分必须基于证据，缺证保守打分，不得主观臆断

【禁止行为】
- 不得自行计算总分或判定 Gate（总分由 fit_scorer.py 计算）
- 不得写入 runtime/evidence_ledger/ 或 runtime/workflow_state/
- 不得调用 gate_check / workflow_state / generate_report
"""
```

## 主 Agent 落库

子 Agent 回传后，校验 schema 与 evidence 字段，再调用：

```text
python scripts/ops.py innovation:ledger write-fit \
    --id <ID> --scores '<八维评分 JSON 对象>' --evidence '<evidence JSON 数组>'
```

该命令内部自动调用 `fit_scorer.py` 计算加权总分与 Gate 等级，并完成 `FIT_ASSESSMENT` 的 RUNNING → 写台账 → COMPLETED。落库后再执行 `innovation:gate check --id <ID> --stage FIT_ASSESSMENT`。