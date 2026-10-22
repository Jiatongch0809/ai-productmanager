---
name: "prd-mvp"
description: "MVP PRD 生成：基于商业计划书，生成最小可行产品的产品需求文档。"
---

# MVP PRD 生成（PRD MVP）

工作流 B 第三阶段，将商业计划转化为可执行的产品需求文档。

## 输入

- 商业计划书（`business_plan.md`）
- 需求洞察报告（`demand_insight.json`）

## 输出

产物文件：`runtime/evidence_ledger/{opportunity_id}/workflow_b/prd/prd_mvp.md`

Markdown 结构：

```markdown
# 产品需求文档（PRD）：{产品名称} MVP

## 1. 文档信息
- 版本号：v1.0
- 创建日期：YYYY-MM-DD
- 作者：{agent_name}
- 状态：草稿/评审中/已批准

## 2. 背景与目标
### 2.1 背景
- 市场机会
- 用户痛点
- 商业价值

### 2.2 目标
- 产品目标（业务指标）
- 用户目标（用户体验）
- 技术目标（性能指标）

### 2.3 成功标准
- 可量化的成功指标
- 验收标准

## 3. 用户与场景
### 3.1 目标用户
- 用户画像（Persona）
- 用户特征
- 使用频率

### 3.2 核心场景
- 场景 1：{场景名称}
  - 用户故事：作为{角色}，我希望{功能}，以便{价值}
  - 前置条件
  - 主流程
  - 异常流程

## 4. 功能需求
### 4.1 功能列表
| 功能 ID | 功能名称 | 优先级 | 描述 | 验收标准 |
|---------|----------|--------|------|----------|
| F-001 | {功能} | P0 | {描述} | {标准} |

### 4.2 功能详情
#### F-001: {功能名称}
- **优先级**：P0
- **描述**：详细功能描述
- **用户故事**：作为...我希望...以便...
- **验收标准**：
  - [ ] 标准 1
  - [ ] 标准 2
- **UI/UX 要求**：界面描述或线框图引用
- **业务规则**：业务逻辑规则
- **依赖**：依赖的其他功能或系统

## 5. 非功能需求
### 5.1 性能需求
- 响应时间：< X 秒
- 并发用户：支持 X 用户
- 数据量：处理 X 条记录

### 5.2 安全需求
- 认证方式
- 数据加密
- 权限控制

### 5.3 兼容性需求
- 浏览器支持
- 设备支持
- 操作系统支持

### 5.4 可用性需求
- 系统可用性：> 99%
- 数据备份策略
- 灾难恢复

## 6. 技术架构
### 6.1 系统架构图
- 前端架构
- 后端架构
- 数据库设计
- 第三方服务

### 6.2 技术栈
- 前端：{框架}
- 后端：{框架}
- 数据库：{数据库}
- 云服务：{云服务商}

### 6.3 接口设计
- API 列表
- 数据格式
- 错误处理

## 7. 数据需求
### 7.1 数据模型
- 核心实体
- 实体关系
- 数据字典

### 7.2 数据迁移
- 历史数据处理
- 迁移策略

## 8. 发布计划
### 8.1 MVP 范围
- 包含功能（P0）
- 排除功能（P1/P2）

### 8.2 发布里程碑
- 开发完成：YYYY-MM-DD
- 测试完成：YYYY-MM-DD
- 上线日期：YYYY-MM-DD

### 8.3 回滚计划
- 回滚条件
- 回滚步骤

## 9. 验收标准
### 9.1 功能验收
- 所有 P0 功能通过测试
- 无 P0/P1 级别 Bug

### 9.2 性能验收
- 响应时间达标
- 并发测试通过

### 9.3 用户验收
- 用户测试通过率 > 80%
- 用户满意度 > 4/5

## 10. 附录
### 10.1 术语表
- 术语定义

### 10.2 参考文档
- 相关文档链接

### 10.3 变更历史
| 版本 | 日期 | 变更内容 | 作者 |
|------|------|----------|------|
| v1.0 | YYYY-MM-DD | 初始版本 | {agent_name} |
```

## 执行步骤

1. **读取输入**：加载 BP 和需求洞察
2. **定义 MVP 范围**：从 BP 中提取 P0 功能
3. **用户故事**：为每个 P0 功能编写用户故事
4. **功能详情**：详细描述每个功能的验收标准
5. **非功能需求**：定义性能、安全、兼容性要求
6. **技术架构**：设计系统架构和技术栈
7. **数据模型**：设计核心数据实体
8. **发布计划**：定义里程碑和验收标准

## 子 Agent 任务书

```markdown
## 任务
基于商业计划书生成 MVP PRD。

## 输入
- 机会 ID：{opportunity_id}
- BP 路径：`runtime/evidence_ledger/{opportunity_id}/workflow_b/bp/business_plan.md`
- 需求洞察路径：`runtime/evidence_ledger/{opportunity_id}/workflow_b/demand_insight/demand_insight.json`

## 输出要求
1. 产物路径：`runtime/evidence_ledger/{opportunity_id}/workflow_b/prd/prd_mvp.md`
2. Markdown 结构必须包含上述 10 个章节
3. 每个 P0 功能必须有详细的验收标准
4. 完成后调用：`python scripts/ops.py innovation:workflow-b:prd --id {opportunity_id} --file <产物路径>`

## 禁止
- 不得修改 BP 或需求洞察
- 不得执行 Gate 检查
- 不得生成 Demo
- 不得自动进入下一阶段（需等待用户确认）
```

## 质量检查

- 10 个章节完整
- 至少 5 个 P0 功能
- 每个功能有明确的验收标准
- 非功能需求有量化指标
- 技术架构清晰
- 发布计划有明确日期

## HITL Gate

PRD 生成后触发 HITL Gate，等待用户确认：

1. 向用户展示 PRD 摘要
2. 等待用户确认或修改意见
3. 用户确认后调用：`python scripts/ops.py innovation:workflow-b:prd-gate --id ID --approve true`
4. 用户拒绝则进入修改循环

## 命令

```bash
python scripts/ops.py innovation:workflow-b:prd --id ID --status COMPLETED --artifacts '["path/to/prd_mvp.md"]'
python scripts/ops.py innovation:workflow-b:prd-gate --id ID --confirm true|false
```

参数说明：
- `--id`：机会 ID（必填）
- `--status`：阶段状态，可选 RUNNING/COMPLETED/FAILED（默认 RUNNING）
- `--artifacts`：产物路径列表，JSON 数组格式（可选）
- `--confirm`：PRD Gate 确认，true 表示通过，false 表示拒绝（prd-gate 必填）
- `--reason`：拒绝原因（prd-gate 可选，confirm=false 时建议填写）
