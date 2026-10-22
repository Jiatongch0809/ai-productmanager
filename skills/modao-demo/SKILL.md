---
name: "modao-demo"
description: "墨刀 Demo 生成：基于 PRD 调用墨刀 MCP 生成可交互原型。"
---

# 墨刀 Demo 生成（Modao Demo）

工作流 B 第四阶段，将 PRD 转化为可交互的原型演示。

## 输入

- PRD 文档（`prd_mvp.md`）
- 用户确认（PRD_MVP_GATE 通过）

## 输出

产物文件：`runtime/evidence_ledger/{opportunity_id}/workflow_b/demo/demo_manifest.json`

JSON 结构：

```json
{
  "opportunity_id": "xxx",
  "stage": "MODAO_DEMO",
  "completed_at": "2026-10-15T10:00:00Z",
  "modao_task_id": "task_xxx",
  "modao_task_url": "https://modao.cc/proto/xxx",
  "modao_preview_url": "https://modao.cc/preview/xxx",
  "pages": [
    {
      "page_name": "首页",
      "page_description": "产品首页，展示核心功能入口",
      "components": ["导航栏", "功能卡片", "底部菜单"]
    },
    {
      "page_name": "功能页",
      "page_description": "核心功能页面",
      "components": ["表单", "列表", "操作按钮"]
    }
  ],
  "interaction_flows": [
    {
      "flow_name": "主流程",
      "steps": ["首页 → 功能页 → 结果页"]
    }
  ],
  "demo_status": "completed"
}
```

## 执行步骤

1. **读取 PRD**：加载 `prd_mvp.md`，提取核心页面和交互
2. **页面规划**：定义 3-5 个核心页面
3. **调用墨刀 MCP**：使用 `modao-generator` Skill 生成原型
4. **等待生成**：轮询任务状态直到完成
5. **记录结果**：保存 task_id、预览链接到 manifest

## 子 Agent 任务书

```markdown
## 任务
基于 PRD 生成墨刀原型。

## 输入
- 机会 ID：{opportunity_id}
- PRD 路径：`runtime/evidence_ledger/{opportunity_id}/workflow_b/prd/prd_mvp.md`

## 输出要求
1. 产物路径：`runtime/evidence_ledger/{opportunity_id}/workflow_b/demo/demo_manifest.json`
2. 必须包含 modao_task_id、modao_task_url、modao_preview_url
3. 至少 3 个核心页面
4. 完成后调用：`python scripts/ops.py innovation:workflow-b:demo --id {opportunity_id} --proto-url <预览链接>`

## 执行流程
1. 读取 PRD，提取核心页面和交互
2. 读取 `skills/modao-generator/SKILL.md`
3. 调用墨刀 MCP 生成原型
4. 等待生成完成（最多 100 秒）
5. 保存结果到 manifest

## 禁止
- 不得修改 PRD
- 不得执行 Gate 检查
- 不得跳过墨刀 MCP 直接编造链接
```

## 墨刀 MCP 调用

使用 `modao__generate_html` 或 `modao__generate_react`：

```python
# 示例：生成 HTML 原型
modao__generate_html(
    query="基于以下 PRD 生成可交互原型：\n\n{prd_content}",
    user_input="产品名：{product_name}\n核心功能：{core_features}"
)
```

等待任务完成：

```python
modao__get_task_result(task_id="task_xxx")
```

## 质量检查

- modao_task_id 非空
- modao_preview_url 可访问
- 至少 3 个核心页面
- 交互流程完整

## 命令

```bash
python scripts/ops.py innovation:workflow-b:demo --id ID --status COMPLETED --artifacts '["path/to/demo_manifest.json"]'
```

参数说明：
- `--id`：机会 ID（必填）
- `--status`：阶段状态，可选 RUNNING/COMPLETED/FAILED/SKIPPED（默认 RUNNING）
- `--artifacts`：产物路径列表，JSON 数组格式（可选）

## 依赖

- `skills/modao-generator/SKILL.md`：墨刀 MCP 调用指南
- 墨刀 MCP 服务可用
