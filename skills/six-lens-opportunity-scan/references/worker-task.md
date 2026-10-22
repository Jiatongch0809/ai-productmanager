# 六看分片子 Agent 任务书

主 Agent 派生六看子 Agent 时使用本模板。**六看必须分片执行**：每个 worker 只负责 2 个维度，每完成一维立即落盘，不允许把六个维度攒在一个上下文里跑完。

## 分片划分

| worker | parts |
| --- | --- |
| W1 | `market`, `user` |
| W2 | `capital`, `policy` |
| W3 | `industry`, `competitor` |

其中 `market`/`user`/`competitor` 是**核心维度**：任一缺结论或判定为 WATCH/NO，六看整体就不得给 YES（Gate 会拦截）。这三个维度必须优先做扎实。

## 任务书模板

```python
task = f"""
你是六看压力测试执行者，本次只负责 {parts} 两个维度。

机会 ID：{opportunity_id}
分片：{parts}
产品定义：{ledger['identity']['product_definition']}
目标用户：{ledger['identity']['target_user']}

【检索环境（重要，不要撞已知失效通道）】
- web_search(searxng) 已知不可用，禁止反复重试；
- 可用通道：web_fetch + https://so.com/s?q=<关键词>，备选 https://cn.bing.com/search?q=<关键词>；
- 证据分级、信源清单与双源验证规则见 docs/agent/retrieval-policy.md。

【工具预算】
本任务工具调用上限 {tool_budget} 次。达到上限时立即中断当前维度、落盘已完成的分片后返回，
不允许耗尽上下文。宁可少一个维度并标注 UNKNOWN，也不要把上下文跑爆。

【落盘要求（硬约束）】
每完成一个维度，立即执行落盘，不要攒到最后：
    python scripts/ops.py innovation:task part --id {opportunity_id} --stage SIX_LENS \\
        --part <维度> --file <你写好的分片 JSON 路径>

【分片 JSON 格式】
{{
  "dimension": "market",
  "conclusion": "YES/CONDITIONAL/WATCH/NO",
  "key_facts": ["关键事实/数据"],
  "case_samples": ["代表案例"],
  "positive_signals": ["支持信号"],
  "negative_signals": ["反向信号"],
  "unknowns": ["信息缺口"],
  "need_more": true,
  "evidence": [
    {{"category": "市场", "fact": "...", "source": "...", "date": "YYYY-MM-DD", "grade": "A/B/C", "signal": "正向/反向"}}
  ],
  "evidence_tasks": ["补证任务1"]
}}

【质量要求】
- 维度结论必须给出 YES/CONDITIONAL/WATCH/NO；
- 每条证据必须带 fact/source/date/grade/signal，B 级及以上必须 ≥2 个独立来源，否则降为 C；
- 必须同时列出正向与反向信号；
- 缺口如实写成 UNKNOWN，不得用推测补齐。

【禁止行为】
- 不得写入 runtime/evidence_ledger/ 或 runtime/workflow_state/；
- 不得调用 gate_check / workflow_state / generate_report；
- 不得自行给出六看整体结论（整体结论由主 Agent 在合并后判定）。
"""
```

## 主 Agent 合并与落库

```text
# 1. 登记分片（上下文丢失后重跑前先执行，已 DONE 的分片会被保留）
python scripts/ops.py innovation:task init --id <ID> --stage SIX_LENS --parts market,user,capital,policy,industry,competitor

# 2. 核对进度：缺哪个分片只补哪个
python scripts/ops.py innovation:task status --id <ID> --stage SIX_LENS

# 3. 合并（缺片会被拒绝；合并在脚本内完成，证据不经过模型上下文）
python scripts/ops.py innovation:task merge --id <ID> --stage SIX_LENS

# 4. 主 Agent 依据合并摘要判定整体结论后落库
python scripts/ops.py innovation:ledger write-six-lens --id <ID> --conclusion <YES|CONDITIONAL|WATCH|NO> \
    --evidence-file   runtime/tasks/<ID>/SIX_LENS/merged-evidence.json \
    --dimensions-file runtime/tasks/<ID>/SIX_LENS/merged-dimensions.json \
    --tasks-file      runtime/tasks/<ID>/SIX_LENS/merged-tasks.json

# 5. Gate
python scripts/ops.py innovation:gate check --id <ID> --stage SIX_LENS
```

`merge` 会在 stdout 打印紧凑摘要（分片完成情况、证据数与等级分布、各维结论、未知项数）与产物路径，主 Agent 据此判定整体结论即可，**不需要读取全部证据正文**。