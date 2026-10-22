---
name: modao-downloader
description: "下载墨刀 AI 生成的产物（HTML/React/Vue/PRD/图片）到本地。触发词：下载墨刀、下载原型、下载产物、保存原型、导出原型。"
---

# 墨刀产物下载器

从墨刀 AI 下载已生成的产物（HTML/React/Vue/PRD/图片）到本地文件。

## 前置条件

- 墨刀 MCP 已配置并连接（`mcp.servers.modao` 存在且 enabled）
- 若工具列表中无 `modao__*` 工具，提示用户检查 MCP 配置

## 步骤

### 1. 确认下载目标

从用户输入中提取：

- **任务标识**：任务名称、关键词、或 task_id
- **产物类型**：HTML/React/Vue/PRD/图片（可选，用于筛选）

若用户未指定具体任务，询问：
- 「下载哪个任务？可以说任务名称或关键词」
- 或「下载最近的任务」

### 2. 查找任务

调用 `modao__list_recent_tasks`：

```
modao__list_recent_tasks(
  keyword=<用户提供的关键词>,  // 可选
  limit=10,
  client="openclaw"
)
```

**结果处理：**
- 若找到多个任务，列出任务名称和更新时间，让用户选择
- 若只找到一个，直接使用
- 若未找到，告知用户「未找到匹配的任务」

记录选中的 `task_id` 和 `name`（任务名称，用于后续文件命名）。

### 3. 获取产物内容

调用 `modao__get_task_result`：

```
modao__get_task_result(
  task_id=<上一步的task_id>,
  client="openclaw"
)
```

**结果处理：**
- 检查 `status`：必须是 `completed`
- 提取 `artifact_content`：产物的完整内容（HTML/代码/文本）
- 提取 `artifact_type`：产物类型（html/react/vue/prd/image）
- 任务名称：从步骤 2 的 `list_recent_tasks` 结果中获取（`name` 字段）
- 若步骤 2 未记录名称，使用 `unknown` 作为默认值

**若 status 不是 completed：**
- `running`：告知用户「任务正在生成中，请稍后再试」
- `failed`：告知用户「任务生成失败」
- `paused`：询问用户是否要继续任务

### 4. 内容完整性检测

**截断检测规则：**

| 产物类型 | 检测方式 |
|----------|----------|
| HTML | 检查是否以 `</html>` 结尾，或包含 `<html` 和 `</html>` 配对 |
| React/Vue | 检查是否有完整的 export 语句或组件闭合 |
| PRD | 检查是否有完整的章节结构 |
| 图片 | 检查是否为有效 URL 或 base64 格式 |

**若检测到截断：**
1. 告知用户「产物内容可能不完整（检测到截断）」
2. 仍然保存已有内容
3. 建议用户通过墨刀网页端下载完整版本

### 5. 写入本地文件

**sessionKey 获取：**
- 从当前会话上下文获取 sessionKey
- 格式如 `agent:u445:dashboard:49741c66-50a3-4df2-b83a-3e26197acc61`
- 最后一段是 UUID，如 `49741c66-50a3-4df2-b83a-3e26197acc61`

**文件命名规则：**
```
{任务名称}_{YYYYMMDD_HHmmss}.{扩展名}
```

**扩展名映射规则：**

| artifact_type | 扩展名 | 判断条件 |
|---------------|--------|----------|
| html | `.html` | 固定 |
| react | `.tsx` | 内容包含 `import React` 或 `from 'react'` 或 TypeScript 语法 |
| react | `.jsx` | 其他 React 代码 |
| vue | `.vue` | 固定 |
| prd | `.md` | 固定 |
| image | `.png` | URL 以 `.png` 结尾或 content-type 为 image/png |
| image | `.jpg` | URL 以 `.jpg`/`.jpeg` 结尾 |
| image | `.webp` | URL 以 `.webp` 结尾 |
| image | `.png` | 默认（无法判断时） |

**图片产物特殊处理：**
- 若 `artifact_content` 是 URL（以 `http://` 或 `https://` 开头），使用 `web_fetch` 或 `curl` 下载二进制内容
- 若 `artifact_content` 是 base64（以 `data:image/` 开头），解码后写入
- 若 `artifact_content` 是其他格式，原样保存为 `.txt`

**文件存放位置：**
```
media/local-file/{sessionKey最后一段}/{文件名}
```

**写入逻辑：**
```python
# 伪代码
session_key = "<从会话上下文获取>"
last_seg = session_key.split(":")[-1]  # 取最后一段
file_path = f"media/local-file/{last_seg}/{task_name}_{timestamp}.{ext}"
write(file_path, artifact_content)
```

### 6. 交付结果

向用户展示：

- **文件名**：纯文本文件名
- **下载位置**：「请到「生成文件」面板下载」
- **产物摘要**：简要说明产物内容（如「固件管理平台 HTML 原型」）
- **完整性提示**：若检测到截断，明确告知

**示例回复（正常）：**
```
已下载：固件管理平台_20261013_155700.html

请到「生成文件」面板下载。

产物类型：HTML 原型
任务名称：固件管理平台
文件大小：19,962 字节
```

**示例回复（截断）：**
```
已下载：复杂原型_20261013_155700.html

请到「生成文件」面板下载。

⚠️ 注意：产物内容可能不完整（检测到 HTML 未闭合）。
建议通过墨刀网页端下载完整版本：
https://modao.cc/ai/design/...

产物类型：HTML 原型
任务名称：复杂原型
```

## 错误处理

| 错误场景 | 处理方式 |
|----------|----------|
| 未找到任务 | 告知用户「未找到匹配的任务」，建议检查关键词 |
| 任务未完成 | 告知用户状态，建议稍后重试 |
| 产物内容为空 | 告知用户「产物内容为空」，可能是任务异常 |
| 内容截断 | 保存已有内容，提示用户通过网页端下载完整版 |
| 图片下载失败 | 展示错误信息，提供原始 URL 让用户手动下载 |
| 文件写入失败 | 展示错误信息，建议检查磁盘空间或权限 |

## 红线

- **禁止自动重试失败的下载**：展示错误信息，等用户指示
- **禁止修改产物内容**：原样保存，不做任何处理（截断检测除外）
- **禁止覆盖已有文件**：文件名带时间戳，避免覆盖
- **令牌安全**：不在对话中展示、记录或传输用户的墨刀令牌
- **禁止自动调用 generate_* 创建新任务**：generate_* 是付费操作

## 工具速查

| 工具 | 用途 | 关键参数 |
|------|------|----------|
| `modao__list_recent_tasks` | 查找任务 | keyword, limit, client |
| `modao__get_task_result` | 获取产物内容 | task_id (必填), client |

## 参考

- [墨刀 AI MCP 配置教程](https://modao.cc/ad/blog/modao-ai-mcp-guide.html)
- [墨刀 AI MCP 官方接入页](https://modao.cc/feature/ai-mcp.html)
