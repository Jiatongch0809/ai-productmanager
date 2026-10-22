---
name: modao-generator
description: "生成产品原型、页面、PRD时使用。通过墨刀 MCP 生成 HTML/React/Vue/PRD/图片，自动导入墨刀编辑器。"
---

# 墨刀生成器

通过墨刀 MCP 生成产品原型、应用、PRD 或图片，HTML 产物自动导入墨刀编辑器。

## 前置条件

- 墨刀 MCP 已配置并连接（`mcp.servers.modao` 存在且 enabled）
- 若工具列表中无 `modao__*` 工具，提示用户检查 MCP 配置

## client 参数说明

所有工具调用均使用 `client` 参数标识调用来源。默认值为 `"openclaw"`。若用户明确指定其他 client 值，则使用用户指定的值。

## 步骤

### 1. 确认需求

从用户输入中提取以下信息，缺失项主动询问：

- **做什么**：产品类型（页面/应用/文档/图片）
- **给谁用**：目标用户
- **核心内容**：页面范围、功能模块、关键流程
- **约束**：技术栈偏好、不要什么、风格要求

若用户未指定产物类型，按以下规则询问：

| 用户意图 | 推荐产物 | 对应工具 | 是否可导入墨刀编辑 |
|----------|----------|----------|-------------------|
| 看页面结构、布局、流程 | HTML 原型 | `generate_html` | ✅ 支持 |
| 要可交互 Demo，明确要 React | React 应用 | `generate_react` | ❌ 暂不支持导入 |
| 要可交互 Demo，明确要 Vue | Vue 应用 | `generate_vue` | ❌ 暂不支持导入 |
| 梳理需求、功能说明、验收标准 | PRD 文档 | `generate_prd` | 不需要导入 |
| 海报、插图、配图等视觉素材 | 图片 | `generate_image` | 不需要导入 |
| 无法归类 | 通用生成 | `generate` | 视产物类型而定 |

询问示例：「你想要哪种产物？1) HTML 原型（快速看页面结构，可直接导入墨刀编辑）2) React 应用（可交互 Demo，暂不支持导入墨刀）3) Vue 应用（同 React）4) PRD 文档（需求梳理）5) 图片（海报/插图）」

### 2. 检查积分余额

生成操作（`generate_*`）为付费操作，开始前先检查积分：

```
modao__get_account_status(client="openclaw")
```

向用户回复当前积分剩余情况，例如：「当前墨刀积分剩余：xxx。确认继续生成吗？」

若用户确认，进入下一步。若积分不足，提示用户充值后再试。

### 3. 构造需求描述

将用户输入整理为结构化 query，包含：

- 产品类型和目标用户
- 页面范围和核心功能
- 必须覆盖的状态和异常场景
- 明确不要什么（排除无关功能）
- 期望的产物类型

query 示例：
```
生成一个移动端用户访谈预约原型。
目标用户：收到研究邀请的注册用户。
页面范围：场次选择、信息填写、预约确认。
必须覆盖：场次已满、手机号校验失败、重复预约、提交失败和成功结果。
输出：可预览的 HTML 原型。
不要添加支付、社区或与预约无关的功能。
```

### 4. 调用生成工具

根据产物类型调用对应工具：

- HTML → `modao__generate_html(query=..., client="openclaw")`
- React → `modao__generate_react(query=..., client="openclaw")`
- Vue → `modao__generate_vue(query=..., client="openclaw")`
- PRD → `modao__generate_prd(query=..., client="openclaw")`
- 图片 → `modao__generate_image(query=..., client="openclaw")`
- 通用 → `modao__generate(query=..., client="openclaw")`

工具最多等待 100 秒。返回结果中记录 `task_id`。

### 5. 超时恢复（硬约束）

**背景**：MCP 客户端超时约 60 秒，但墨刀服务端可能仍在正常生成（2-3 分钟）。超时返回的错误信息中**没有 task_id**，但任务已在墨刀后台创建。

**超时后的处理流程**：

1. **禁止直接重试 `generate_*`**：重复调用会创建新任务，浪费积分并占满并发槽位（最多 3 个）
2. 调用 `modao__list_recent_tasks(limit=5, client="openclaw")`
3. 从返回的任务列表中，按 `updated_at` 找到**最新的、名称匹配的、status 为 running 的任务**
4. 用该 `task_id` 调用 `modao__get_task_result(task_id=..., client="openclaw")` 查询状态
5. 若仍为 running，等待 15 秒后再次查询（最多 3 次）
6. 3 次后仍 running，停止轮询，向用户返回 task_id 和任务链接，提示稍后查询

**判断「最新匹配任务」的规则**：
- `updated_at` 在最近 5 分钟内
- 任务名称与本次 query 的关键词匹配
- status 为 `running` 或 `completed`
- 若找不到匹配任务，向用户说明情况，等用户指示

### 6. 轮询任务状态

若返回状态为 `running`（正常返回 task_id 但任务未完成），采用指数退避策略轮询：

1. 告知用户「生成中，请稍候...」
2. 等待 **15 秒**后调用 `modao__get_task_result(task_id=..., client="openclaw")`
3. 若仍为 running，等待 **15 秒**后再次查询（第 2 次）
4. 若仍为 running，等待 **15 秒**后再次查询（第 3 次）
5. 3 次查询后仍为 running，停止轮询，向用户返回：
   - 生成任务仍在进行中
   - task_id 值
   - 提示用户稍后可用 `get_task_result` 查询结果，或用 `list_recent_tasks` 查看

不要重复创建相同任务。

### 6. 自动导入墨刀（仅 HTML 产物）

生成完成后，若产物为 HTML 类型，自动调用导入：

```
modao__import_to_proto(task_id=<上一步的task_id>, client="openclaw")
```

导入成功后向用户展示 `prototype_url`，说明可在墨刀中继续编辑。

**限制**：`import_to_proto` 仅支持 HTML。若产物为 React/Vue，告知用户：
- 导入功能暂不支持 React/Vue
- 可通过 `download_generated_content` 下载代码
- 如需导入，需用户明确同意后重新用 `generate_html` 生成 HTML 版本

PRD 和图片不需要导入，直接展示结果。

### 7. 展示结果

向用户展示：

- 预览链接（preview_url）— 用户可直接查看
- 任务链接（task_url）— 用户可查看任务详情
- 原型链接（prototype_url）— 导入成功后，可在墨刀编辑器中继续设计
- 本次积分消耗（从任务结果中获取）
- 简要说明产物内容和下一步建议

### 8. 下载产物到本地

当用户要求保存产物到本地时，调用：

```
modao__download_generated_content(task_id=..., client="openclaw")
```

返回下载链接，告知用户在已登录墨刀的浏览器中打开下载。

### 9. 后续迭代

用户要求修改时，使用 `continue_task` 而非重新生成：

```
modao__continue_task(task_id=..., query="修改需求...", client="openclaw")
```

用户要求暂停/取消时：

```
modao__paused_task(task_id=..., client="openclaw")
```

## 红线

- **禁止自动重试失败的生成工具**：失败时展示错误信息，等用户指示
- **禁止在用户未同意时调用 generate_* 创建新任务**：generate_* 是付费操作
- **禁止把 React/Vue 自动转为 HTML**：必须用户明确同意
- **禁止自动重试 import_to_proto 失败**：展示错误，等用户指示
- **令牌安全**：不在对话中展示、记录或传输用户的墨刀令牌

## 工具速查

| 工具 | 用途 | 关键参数 |
|------|------|----------|
| `modao__generate_html` | 生成 HTML 原型 | query, client |
| `modao__generate_react` | 生成 React 应用 | query, client |
| `modao__generate_vue` | 生成 Vue 应用 | query, client |
| `modao__generate_prd` | 生成 PRD 文档 | query, client |
| `modao__generate_image` | 生成图片 | query, client |
| `modao__generate` | 通用生成 | query, client |
| `modao__get_task_result` | 查询任务状态 | task_id (必填), client |
| `modao__continue_task` | 继续/修改任务 | task_id (必填), query, client |
| `modao__paused_task` | 暂停/取消任务 | task_id (必填), client |
| `modao__import_to_proto` | 导入墨刀编辑 | task_id 或 html, client |
| `modao__list_recent_tasks` | 查看最近任务 | limit, keyword, client |
| `modao__get_account_status` | 查询账号积分 | client |
| `modao__download_generated_content` | 下载产物 | task_id, client |

## 完整示例

**用户**：帮我做个预约页面

**Agent**：
好的，你想要哪种产物？
1) HTML 原型 — 快速看页面结构，可直接导入墨刀编辑
2) React 应用 — 可交互 Demo，暂不支持导入墨刀
3) Vue 应用 — 同 React
4) PRD 文档 — 需求梳理
5) 图片 — 海报/插图

**用户**：1

**Agent**：
了解。请补充几个信息：
- 给谁用的？（目标用户）
- 需要哪些页面？（如：场次选择、信息填写、确认页）
- 有没有必须覆盖的异常场景？（如：场次已满、重复预约）
- 有没有不要的功能？

**用户**：给收到邀请的注册用户用，需要选场次、填信息、确认，要处理场次已满和重复预约的情况，不要支付功能。

**Agent**：
先帮你查一下积分余额...
当前墨刀积分剩余：500。确认继续生成吗？

**用户**：确认

**Agent**：
好的，正在生成...

（调用 `modao__generate_html`，返回 running）

生成中，请稍候...

（5秒后轮询第1次，仍 running）
（10秒后轮询第2次，仍 running）
（20秒后轮询第3次，完成）

生成完成！已自动导入墨刀编辑器。

📎 预览：https://xxx/preview/xxx
📎 墨刀编辑：https://xxx/proto/xxx
💰 本次消耗积分：15

这是一个移动端用户访谈预约原型，包含场次选择、信息填写、预约确认三个页面，已覆盖场次已满和重复预约的异常场景。你可以在墨刀中继续编辑调整。

需要修改什么吗？或者需要下载产物到本地？

## 参考

- [墨刀 AI MCP 配置教程](https://modao.cc/ad/blog/modao-ai-mcp-guide.html)
- [墨刀 AI MCP 官方接入页](https://modao.cc/feature/ai-mcp.html)
