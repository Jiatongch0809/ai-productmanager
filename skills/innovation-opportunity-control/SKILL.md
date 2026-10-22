---
name: "innovation-opportunity-control"
description: "总控统一输入分流、证据台账、Gate、汇总决策与后置产品提案成案；优秀案例库用于提案校准。"
---

# 创新机会总控（v1.4）

本文件只负责**输入分流与路由编排**。命令、状态机、子 Agent 任务书一律按第三节指针按需读取，不要预加载。

## 一、输入分流与路由

分流表同时承载路由链与阶段级停止点，判断入口只需读这一节。

| 输入类别 | 触发词 | 判定特征 | 路由链 → 停止点 |
| --- | --- | --- | --- |
| frontier | 扫描、一批、新品、众筹、爆款、热销 | 无明确单品链接，要求扫描多个产品 | frontier-opportunity-discovery → 输出漏斗 + A/B/C 后停止，等用户选候选（本阶段无脚本 Gate） |
| single_product | 分析这个、看看这个、[链接]、[图片] | 有明确的产品链接/图片/视频/规格书 | product-intelligence-teardown → 拆解完成后停止，询问用户是否继续六看 |
| clear_opportunity | 评估、机会、市场、用户、场景 | 已明确用户/场景/核心问题，不需要拆解产品 | six-lens-opportunity-scan → fit-assessment → report（Gate 允许时）；遇停止动作即停，见第二节 |
| full_evaluation | 完整评估、按机制跑、全流程 | 明确要求跑完整流程 | teardown → six-lens → fit-assessment → report；遇停止动作即停，见第二节 |
| 仅要 PRD/竞品/周报/PPT/Word | 写 PRD、竞品分析、周报、做 PPT | 无机会评估诉求 | pm-workflow（PRD/竞品/需求池/周报/路线图）、innovation-opportunity-packaging（原生 PPT/Word）；不进入本链路 |
| 工作流 B：需求驱动提案 | 需求洞察、BP 生成、PRD 输出、Demo 演示 | 明确要求从需求出发生成产品提案 | `skills/innovation-opportunity-control/SKILL.md` 第五节「工作流 B」；不进入工作流 A |

## 消歧优先级

一句话同时命中多个类别时，按以下顺序取**第一条命中**，不得并列执行两条链：

1. **排除优先**：命中分流表最后一行（纯文件诉求）时，本链路整体不启动。
2. **显式全链路优先**：命中 full_evaluation 时即走全链路；若同时给出单品链接或图片，把它作为 teardown 阶段的输入，不要再退回 single_product。
3. **其余按分流表"判定特征"就近判定**，顺序为：单品载体（链接/图片/规格书，或指向单一具体产品）> 批量扫描信号 > 已明确机会命题。

关键区分：**"评估"单独出现走 clear_opportunity；只有明确说出"完整评估 / 按机制跑 / 全流程"才走全链路**，不要因为出现"评估"二字就启动拆解。

仍无法判定（两类都像、或都不像）时，按下一句反问用户，不要猜一个分支直接开跑。

无法归类时询问用户："您是想扫描一批新品，还是分析某个具体产品？需要完整评估还是只做单品拆解？"

链路顺序固定为 INIT → TEARDOWN（可选）→ SIX_LENS → SIX_LENS_GATE → FIT_ASSESSMENT → FIT_GATE → GENERATE，不得跳阶段。

## 二、Gate 语义与停止动作

第一节的停止点由本节的动作判定触发：

- **六看**：YES → 继续适配度；CONDITIONAL → 生成补证任务后复评；WATCH / NO → 停止归档。
- **核心维度守卫**：`market`/`user`/`competitor` 任一无结论或为 WATCH/NO 时，六看不得以 YES 通过 Gate；脚本会降级为 WAIT_USER，要求补证或改判，理由与缺口列在 `core_dimension_gaps`。
- **适配度**：S51 → 继续，可提交创新评审与产物；S52 → 停止并等待用户确认；S53 → 观察池；S54 → 暂停。
- Gate 结论与总分只能由脚本给出，模型不得自行判定 Gate，也不得自行计算总分。
- 分数阈值与 Gate 等级以 `scripts/fit_scorer.py` 为准；阶段常量与合法迁移以 `scripts/contracts.py` 为准。
- 补证任务可后台更新，但不得静默改变结论；评分、Gate、决策与提案版本必须保留版本原因。
- 未获明确授权，不得进入 PRD / BOM / Demo / MVP / 开发 / 量产 / 立项。

### Gate 命令返回后的第一步（硬约束）

`innovation:gate check` 返回后，**先看 action 再决定能不能调工具**，不得继续执行后续阶段：

| action | 允许的操作 | 禁止的操作 |
| --- | --- | --- |
| CONTINUE | 进入下一阶段（如 SIX_LENS → FIT_ASSESSMENT） | 不得重复跑同一个 Gate |
| WAIT_USER | 把结论、证据数、等待事项发给用户，然后**停**。不得评分，不得再跑同一个 Gate | 不得进入下一阶段，不得派发子 Agent |
| STOP | 发送停止原因和已完成内容，然后结束 | 不得进入下一阶段，不得派发子 Agent |
| ERROR | 报告失败位置；瞬时失败最多重试 2 次 | 结构性失败不得原样重试 |

**幂等护栏**：同一 `ledger_version` 的 Gate 已经是 WAIT_USER 或 STOP 时，再次调用 `gate check` 会直接返回原结果（`idempotent: true`），不会重写状态、不会重跑检查。不要试图绕过。

### Memory flush 写入限制规则（硬约束）

- 当 `write` 工具返回错误信息包含 `Memory flush writes are restricted to memory/` 时，必须**立即停止当前流程**。
- 不得重试业务路径（如 `ops.py innovation:ledger write-teardown`）。
- 不得改用 `exec` 提交台账或执行其他写入操作。
- 不得派发六看分析或其他子 Agent 任务。
- 当天 memory 里没有这条进度时，只追加一行到 `memory/YYYY-MM-DD.md`。

### 恢复只补发，不重放（硬约束）

会话启动时执行 `innovation:state list-pending-outputs`，返回 `pending` 和 `sending` 状态的记录：

| 状态 | 处理方式 |
| --- | --- |
| pending | 发送前改成 sending（`mark-output-sending`），成功后改成 sent 并写入回执（`mark-output-sent --receipt`） |
| sending + 已有回执 | 只补标记 sent，不再发送 |
| sending + 没有回执 | 先核对；核对不到再发，attempt 加 1。正文带同一个 delivery_key |
| sent | 只跳过 |

**replaySafe=no 时禁止重跑六看、重写台账、重跑 Gate。** 钉钉发送没有幂等键，不能声称绝不重复。有回执就不再发。

## 三、执行指针（按需读取）

| 场景 | 读取 |
| --- | --- |
| 需要执行命令 | `docs/agent/operations.md`（统一入口 `scripts/ops.py`） |
| 单品拆解 | `skills/product-intelligence-teardown/SKILL.md` |
| 前沿扫描 | `skills/frontier-opportunity-discovery/SKILL.md` |
| 六看子 Agent 任务书 | `skills/six-lens-opportunity-scan/references/worker-task.md` |
| 适配度子 Agent 任务书 | `skills/fit-assessment/references/worker-task.md` |

流程与恢复、主子 Agent 交接、上下文边界、安全与输出策略的指针见 `AGENTS.md`，本文件不重复。

台账是所有阶段共享的唯一事实来源，**只有主 Agent 可以写入**；各阶段只从台账读取并回写，新增证据保留版本。

## 四、流程连续性（硬约束）

- **本阶段首次**派生子 Agent 前，先回复用户："正在执行，预计需要 X 分钟，请稍候..."（同一阶段只说一次，不得每个子 Agent 派发前都播报）。
- completion 事件到达后，必须在当前 turn 立即续跑内部步骤（核对分片、合并、写台账、执行 Gate）；不得延迟到下一个 turn，不得等用户催促。**可见回复只留给停止点**（Gate 返回 WAIT_USER/STOP、流程正常结束、续跑失败、memory flush 受限）。

### 子 Agent 启动顺序（硬约束）

- 之后只做必要的台账初始化、分片登记和轻量检索通道检查，然后立即启动当前阶段所需的子 Agent。
- 在 `sessions_spawn` 返回 `accepted` 和 `runId` 前，主 Agent 不得代替子 Agent 做产品、市场、竞品或政策研究，也不得为了补充任务书而先做这些研究。
- `sessions_spawn` 已接受，才算子任务真正启动；未接受或启动失败时，及时告知用户实际状态，不得只说"正在执行"。
- 按既定流程启动正确的第一阶段：需要先拆解产品时，先派发拆解任务；满足六看前置条件时，再派发六看分片。不得为了抢先派发而跳过阶段。

### 子 Agent 等待规则（硬约束）

- 派发本阶段所有子 Agent 后，若下一步依赖它们的结果（如合并分片、写台账、执行 Gate），必须立即调用 `sessions_yield` 等待完成事件。
- 收到并核验所有完成结果前，不得调用 `write-fit`、`gate check` 或后续阶段的任何写入命令。
- 不得在 spawn 后继续执行后续步骤（如直接调用 write-six-lens、write-fit），必须等子 Agent 完成事件到达。
- 如果 spawn 后未调用 yield 就继续执行，属于流程错误，必须回滚并重新等待。

### 子 Agent 完成事件续跑规则（硬约束）

- 收到本次流程所派生的子 Agent 完成事件（如 `A background task completed`）时，必须把它当作继续当前流程的触发信号，不得当作可忽略的后台通知。
- 先核对事件中的 runId、taskId、机会 ID 和阶段，确认它属于当前流程。
- 检查本次阶段的所有预期分片：
  - **仍有分片未完成**：核对事件中的 runId 和机会 ID，保留已完成结果，立即调用 `sessions_yield` 等待剩余分片；**本轮用户可见内容只能是 `NO_REPLY`**，不得写"W2 已完成，继续等待"等进度句。
  - **所有分片均已完成**：在同一轮内静默完成合并、写台账、执行 Gate，**不得播报"开始合并""正在写入台账"等进度句**；只有到达停止点时才发可见回复。
- **分片未齐套时**，允许返回 `NO_REPLY` 后继续 `sessions_yield` 等待。
- 到达流程停止点或需要用户确认时，必须向发起本次流程的对话发送可见回复，说明已完成内容、当前状态和需要用户做什么。
- 以下两种情况允许返回 `NO_REPLY`：
  1. 分片未齐套，继续 `sessions_yield` 等待剩余分片；
  2. 同一流程的结果已经实际发送给用户，且收到重复或迟到的完成事件。
- 如果续跑失败，必须说明卡在哪一步、已完成什么以及需要如何处理；不得静默结束。

### Memory flush 写入限制规则（硬约束）

- 当 `write` 工具返回错误信息包含 `Memory flush writes are restricted to memory/` 时，必须**立即停止当前流程**。
- 不得重试业务路径（如 `ops.py innovation:ledger write-teardown`）。
- 不得改用 `exec` 提交台账或执行其他写入操作。
- 不得派发六看分析或其他子 Agent 任务。
- 必须向用户说明：当前处于 memory flush 状态，写入受限，流程暂停，需要等待 flush 完成或在新会话中继续。
- 不得尝试绕过限制，不得把业务数据写入 `memory/` 目录作为临时方案。
- 恢复流程的前提：`write` 工具恢复正常，或在新会话中重新加载上下文后继续。

### Gate 结果发送规则（硬约束）

- 主 Agent 启动时（包括 heartbeat 唤醒），先执行 `innovation:state list-pending-outputs` 检查是否有待发送的 Gate 结果。
- 如果有 pending_output，必须根据**当前会话的 channel** 决定投递路由：
  - **钉钉群会话**（channel = `dingtalk-connector`）：使用 `message(action=send)` 向当前钉钉群发送可见回复，内容包括：Gate 阶段、总分、Gate 等级、核心阻断因素、决策建议。发送成功后，立即执行 `innovation:state mark-output-sent --id ID` 标记为已发送。
  - **OpenClaw 网页端会话**（channel ≠ `dingtalk-connector`）：直接在当前会话回复用户，**不得**调用 `message(action=send)` 投递到钉钉。回复完成后，同样执行 `innovation:state mark-output-sent --id ID` 标记为已发送。
- 判断当前会话 channel 的方法：检查运行时上下文中的 `channel` 字段，或检查 `inbound_meta.v2` 中的 `channel` 值。
- 不得跳过发送步骤，不得让 pending_output 长期滞留。
- 不得将网页端会话的结果投递到钉钉群，除非用户明确要求"发送到群里"。

- 不得只声明"继续执行"而不实际派生子 Agent；流程必须连续执行，直到遇到停止点或流程结束。
- 脚本失败先诊断（常见为字段名不匹配、前置阶段未完成），修复后重试，最多 2 次；不得把内部错误抛给用户后停下。
- **失败必须分类**（细则见 `docs/agent/workflow-contract.md`）：瞬时失败原样重试 ≤2 次；**结构性失败**（子 Agent 上下文耗尽、schema 不符、同类失败重复）**禁止原样重试**，必须降级（拆小分片 / 换检索通道 / 缩范围）；Gate `STOP` 属正常停止而非错误。同一失败签名 ≥2 次即触发熔断。
- 研究类阶段必须分片执行并逐片落盘，任务书与命令见 `skills/six-lens-opportunity-scan/references/worker-task.md`。
- 确需用户决策时，说明降级原因、影响与可选路径，不要把"原样重跑"列为选项。

## 五、产物路由

本表只覆盖机会链路各阶段的产物形态；最终交付 deck 的生图规则见第六节。

- 前沿机会：Markdown 漏斗 + 候选表；
- 单品拆解：Markdown，按需 DOCX、原生可编辑 PPTX、XLSX；
- 六看：Markdown + T-A02 XLSX，按需原生 PPTX；
- 适配度：Markdown/DOCX + T-A04 XLSX，按需原生 PPTX；
- 完整评估/决策汇总：Markdown + DOCX + 原生可编辑 PPTX，必要时附 XLSX；
- 产品提案：Markdown/Word 主稿 + PPT 页面脚本或原生可编辑 PPTX + 事实底稿/验证项 XLSX。

正式产物只能通过 `innovation:report generate` 生成，并须通过存在、可打开、非空、内容一致与可编辑性检查后标记 `ARTIFACT_STATUS=READY`。

## 六、生图与 PPT 边界

分两类产物，规则不同：

**分析页**（拆解、六看、适配度、评审、决策汇总、产品提案正文）

- 默认不调用 image；不得为结构图、场景图、信号地图、评分图或状态卡生成装饰性图片；
- PPT / Word / XLSX 必须使用原生文本、形状、SVG、图表与可编辑表格。

**最终交付 deck**（用户明确要求做 PPT 时）

- 按既定口径执行：**逐页前置完整视觉脚本 → 每页直接生成最终整页 PNG（`gpt-image-relay`）→ 按顺序拼接为 PPTX**；
- 禁止"底图 + PPT 文本覆盖层"式补救；
- 拼接前逐页核对脚本、图片与台账中的结论和分数一致。

唯一允许的单张生图例外是《产品机会一页卡》视觉卡，且不替代 Markdown/DOCX 正式底稿。

## 七、后置产品提案成案层

进入条件：前置评估完成、`DECISION_SUMMARY` 已形成、Gate 允许继续，且用户或评审结果明确要求成案。

该层负责：组织产品命题；补齐事实底稿、核心假设、关键未知与验证项；按提案 Gate 检查证据完整度、产品定义、技术边界、供应链/BOM、商业路径、AI 价值、IP/合规与风险；按 Page Map 组织提案并产出正式产物（产物形态见第五节）。

标准结构至少覆盖：项目摘要与决策结论、用户/场景/问题、机会依据、六看结论、用户需求、产品定义与核心机制、AI 必要性与边界、组织能力适配、竞品与差异化、技术与产品形态、供应链/BOM/成本、IP/合规/安全、商业路径、验证计划、Gate 与下一步。

提案完成后即停止；后续是否进入验证、PRD、BOM、Demo、MVP、开发、量产或立项，不在当前机制内定义，由用户另行决定。

## 八、优秀案例库

案例库仅作为产品提案层的执行参考与质量校准资产，不参与前沿扫描、六看评分、适配度评分、Gate 结论或相似产品检索。

调用范围：参考事实底稿、假设清单、调研提纲的字段完整度；参考六看、适配度、证据核验和 Gate 的组织方式；参考 Page Map、页面顺序、信息密度与表达质量；对照检查提案是否缺少关键模块或验证项。

使用边界：案例中的产品事实、市场数据、评分、结论、图片和页面内容不得直接迁移为新项目事实或结论，只能作为结构、方法与质量标准参考。