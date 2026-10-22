---
name: "bp-generator"
description: "商业计划书生成：基于需求洞察输出，生成结构化商业计划书（BP）。"
---

# 商业计划书生成（BP Generator）

工作流 B 第二阶段，将需求洞察转化为可执行的商业计划。

## 输入

- 需求洞察报告（`demand_insight.json`）
- 可选：市场数据、财务模型假设

## 输出

产物文件：`runtime/evidence_ledger/{opportunity_id}/workflow_b/bp/business_plan.md`

Markdown 结构：

```markdown
# 商业计划书：{产品名称}

## 1. 执行摘要
- 产品定位（一句话）
- 目标市场
- 核心价值主张
- 预期成果

## 2. 市场分析
### 2.1 目标市场规模
- TAM（总可及市场）
- SAM（可服务市场）
- SOM（可获得市场）

### 2.2 竞争格局
- 主要竞品（3-5 个）
- 竞品优劣势对比
- 差异化定位

### 2.3 市场趋势
- 增长驱动因素
- 潜在风险

## 3. 产品策略
### 3.1 产品定位
- 目标用户
- 核心功能（P0/P1/P2）
- MVP 范围

### 3.2 技术架构
- 核心技术栈
- 关键技术挑战
- 第三方依赖

### 3.3 产品路线图
- Phase 1（0-3 个月）：MVP
- Phase 2（3-6 个月）：功能扩展
- Phase 3（6-12 个月）：规模化

## 4. 商业模式
### 4.1 收入模式
- 定价策略
- 收入来源（订阅/一次性/分成）
- 预期 ARPU

### 4.2 成本结构
- 研发成本
- 运营成本
- 获客成本（CAC）

### 4.3 盈利预测
- 月度/年度收入预测（12 个月）
- 盈亏平衡点
- 关键假设

## 5. 营销策略
### 5.1 获客渠道
- 线上渠道（SEO/SEM/社媒）
- 线下渠道（活动/合作）
- 预期转化率

### 5.2 品牌定位
- 品牌调性
- 核心信息
- 视觉识别

## 6. 团队与资源
### 6.1 核心团队
- 关键角色
- 能力缺口

### 6.2 资源需求
- 人力需求
- 资金需求
- 合作伙伴

## 7. 风险与应对
### 7.1 技术风险
- 风险描述
- 影响程度
- 应对策略

### 7.2 市场风险
- 风险描述
- 影响程度
- 应对策略

### 7.3 运营风险
- 风险描述
- 影响程度
- 应对策略

## 8. 里程碑与 KPI
### 8.1 关键里程碑
- 产品上线
- 首批用户
- 盈亏平衡

### 8.2 核心 KPI
- 用户增长
- 收入增长
- 用户留存
```

## 执行步骤

1. **读取需求洞察**：加载 `demand_insight.json`
2. **市场分析**：补充市场规模、竞品、趋势数据
3. **产品策略**：定义 MVP 范围和技术架构
4. **商业模式**：设计定价、收入、成本结构
5. **营销策略**：规划获客渠道和品牌定位
6. **风险评估**：识别技术/市场/运营风险
7. **里程碑**：定义关键节点和 KPI

## 子 Agent 任务书

```markdown
## 任务
基于需求洞察生成商业计划书。

## 输入
- 机会 ID：{opportunity_id}
- 需求洞察路径：`runtime/evidence_ledger/{opportunity_id}/workflow_b/demand_insight/demand_insight.json`

## 输出要求
1. 产物路径：`runtime/evidence_ledger/{opportunity_id}/workflow_b/bp/business_plan.md`
2. Markdown 结构必须包含上述 8 个章节
3. 财务预测必须包含关键假设
4. 完成后调用：`python scripts/ops.py innovation:workflow-b:bp --id {opportunity_id} --file <产物路径>`

## 禁止
- 不得修改需求洞察报告
- 不得执行 Gate 检查
- 不得生成 PRD 或 Demo
```

## 质量检查

- 8 个章节完整
- 市场规模有数据支撑（TAM/SAM/SOM）
- 至少 3 个竞品分析
- 财务预测至少 12 个月
- 至少 3 个风险及应对策略
- 至少 3 个关键里程碑

## 命令

```bash
python scripts/ops.py innovation:workflow-b:bp --id ID --status COMPLETED --artifacts '["path/to/business_plan.md"]'
```

参数说明：
- `--id`：机会 ID（必填）
- `--status`：阶段状态，可选 RUNNING/COMPLETED/FAILED/SKIPPED（默认 RUNNING）
- `--artifacts`：产物路径列表，JSON 数组格式（可选）
