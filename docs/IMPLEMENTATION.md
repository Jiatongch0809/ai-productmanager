# AI 产品经理工作流｜实施说明

本文记录确定性命令的接入范围、仍需人工检查的部分，以及尚未实现为受控命令的能力。

## 已接入确定性命令

1. 机会 ID 在统一命令入口限制为字母、数字、下划线、连字符，避免路径片段进入运行文件名。证据须包含 `fact/source/date/grade/signal`；缺字段拒绝落库，不再跳过坏证据。YES 前检查六维结论齐备，并要求 market/user/competitor 各有近期 A/B 正向双源标注。S51 前对组织能力、快速落地、商业路径做同类检查。
2. CONDITIONAL、缺证 YES、S52 的 `resume --approve true` 仅表示同意补证。重新写入分析、重跑 Gate 才能继续；复评会标记后续状态失效。
3. 六看和适配度阶段 Markdown 可呈现 WATCH/NO/S52/S53/S54 等停止或补证结论；完整评估 Markdown 须 S51 Gate 通过。
4. S51 后依次执行 `innovation:review record`（GO/REVISE/STOP）、`innovation:decision record`（PROPOSE/HOLD）、`innovation:proposal record`（记录明确授权及底稿）；之后可生成 `product_proposal` Markdown。

## 仍需人工检查

- 脚本只能核对不同来源字符串、日期与等级，不能判定来源真实独立、事实可信或证据相互矛盾。内部能力和 BOM 仍须跨角色确认。
- `authorized_by` 是记录真实授权的字段，不是程序能够核实用户身份的认证机制；Agent 不得代填虚构授权。
- 提案命令检查字段存在与阶段顺序；提案的商业、技术、合规质量仍须人审。

## 尚未实现为受控生成命令

DOCX、XLSX、原生可编辑 PPTX、一页卡视觉 PNG、前沿扫描的脚本 Gate、3-7-30 的自动定时与追踪。目前不要承诺它们由这套命令自动交付。后续若补 PPTX，应遵守原方案的原生可编辑要求，禁止整页 PNG 拼接。

## 回归

`python -m unittest discover -s tests -q`。测试覆盖证据拒绝、双源/六维 Gate、补证复评、阶段报告、S51 完整报告，以及评审与授权提案顺序。示例来源均为测试夹具，不代表真实产品证据。
