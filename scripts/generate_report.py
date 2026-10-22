#!/usr/bin/env python3
"""产物生成脚本 — 内含 Gate 门禁（最后防线）

【本文件在整套机制里的位置与职责】
    本脚本是「正式产物只能由脚本生成」这条硬约束的落点。
    - 主 Agent（LLM）负责推进工作流、写台账、调用 Gate；
    - 但报告文件（正式产物）的生成必须由本脚本完成，并在生成前重新校验
      「前置阶段已完成 + 相关 Gate 已通过 + 台账版本与 Gate 绑定一致」。
    - 换言之，它是流程的确定性边界（deterministic boundary）：
      LLM 的判断可以"不确定"，但产物落地必须是"确定"的——
      即使模型想提前出报告，也会在这里被拦下并返回退出码 1，而不是被放行或跳过。

用法：
    OPPORTUNITY_ID=xxx REPORT_TYPE=full_evaluation python3 scripts/generate_report.py
    OPPORTUNITY_ID=xxx REPORT_TYPE=full_evaluation OUTPUT_DIR=/path/to/output python3 scripts/generate_report.py

环境变量：
    OPPORTUNITY_ID: 机会唯一标识
    REPORT_TYPE: 报告类型（full_evaluation / prd / six_lens / fit_assessment）
    OUTPUT_DIR: 输出目录（可选，默认 runtime/outputs）

输出：
    JSON 格式，包含生成状态和文件路径

门禁检查（详见 check_all_gates，共六道关）：
    1. 状态文件必须存在（不存在则拒绝，不跳过）
    2. 检查前置阶段是否完成（使用大写阶段名）
    3. 检查 Gate 是否通过
    4. 检查 HITL 状态（WAITING_USER/STOPPED/REJECTED/FAILED 一律拒绝）
    5. 只有 S51 或经用户确认的 S52 才允许生成完整报告
    6. 台账必须存在，且关键 Gate 的证据版本与台账当前版本一致

【为什么只输出 Markdown】
    当前实现只落地 .md，DOCX/XLSX/PPTX 等正式格式尚未实现。
    按「不确定交给 LLM，确定交给脚本」的原则，未实现的格式宁可不出
    （generate_markdown 返回 None → 主流程报"不支持的报告类型"并 sys.exit(1)），
    也不生成半成品去冒充正式产物。
"""
import json
import sys
import os
import tempfile

# 把本脚本所在目录（scripts）加入模块搜索路径。
# 目的：无论从哪个工作目录、以何种方式启动，都能按"裸模块名"导入同目录的 contracts，
# 避免直接运行脚本时相对导入失败。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# contracts 是流程编排的公共契约：阶段名/状态名/Gate 阶段集合与工具函数统一在此定义。
# 业务脚本一律从此导入，禁止硬编码字符串，避免多处定义漂移。
from contracts import (
    STAGES, STATUSES, GATE_STAGES,
    now_iso, atomic_write_json, ensure_state_structure, ensure_ledger_structure,
    migrate_status
)

# 错误日志目录（与 workflow_state.py / gate_check.py 共用同一份 <opportunity_id>.log）
LOG_DIR = "runtime/logs"
# 产物默认输出目录（可被环境变量 OUTPUT_DIR 覆盖）
DEFAULT_OUTPUT_DIR = "runtime/outputs"

# 报告类型 → 生成前必须满足的前置条件映射表。
# 设计意图：把"要出这份报告，流程必须走到哪一步"写成声明式规则数据，
# check_all_gates 只做通用的机械校验；将来调整规则主要改这张表，不改校验逻辑。
#   required_stages      : 这些阶段必须出现在 stage_results 中且状态达标
#                          （普通阶段要求 COMPLETED，Gate 阶段允许 COMPLETED 或 CONFIRMED）
#   required_gates       : 这些 Gate 阶段必须在台账中绑定到有效证据版本
#   allow_waiting        : 是否允许在 WAITING_USER（等待用户）状态下生成（此处均不允许）
#   require_confirmation : 是否额外要求 HITL 已确认（S52 打分的完整评估/PRD 必须用户确认）
REPORT_REQUIREMENTS = {
    # 六看扫描报告：六看分析阶段本身完成即可出阶段性报告（不依赖任何 Gate）
    "six_lens": {
        "required_stages": [STAGES["SIX_LENS"]],
        "required_gates": [],
        "allow_waiting": False,
    },
    # 适配度评估报告：六看 + 六看 Gate + 适配度三个阶段都必须走完
    "fit_assessment": {
        "required_stages": [STAGES["SIX_LENS"], STAGES["SIX_LENS_GATE"], STAGES["FIT_ASSESSMENT"]],
        "required_gates": [STAGES["SIX_LENS_GATE"]],
        "allow_waiting": False,
    },
    # 完整评估报告：六看→六看 Gate→适配度→适配度 Gate 全链条完成，且需用户确认
    "full_evaluation": {
        "required_stages": [
            STAGES["SIX_LENS"],
            STAGES["SIX_LENS_GATE"],
            STAGES["FIT_ASSESSMENT"],
            STAGES["FIT_GATE"],
        ],
        "required_gates": [STAGES["SIX_LENS_GATE"], STAGES["FIT_GATE"]],
        "allow_waiting": False,
        "require_confirmation": True,  # S52 需要用户确认
    },
    # PRD：与完整评估同样的前置链，也要求用户确认（避免在未确认的 S52 上出正式文档）
    "prd": {
        "required_stages": [
            STAGES["SIX_LENS"],
            STAGES["SIX_LENS_GATE"],
            STAGES["FIT_ASSESSMENT"],
            STAGES["FIT_GATE"],
        ],
        "required_gates": [STAGES["SIX_LENS_GATE"], STAGES["FIT_GATE"]],
        "allow_waiting": False,
        "require_confirmation": True,
    },
    # 产品提案：位于工作流更后段，需适配度 Gate + 创新评审 + 决策摘要 + 提案阶段
    "product_proposal": {
        "required_stages": [STAGES["FIT_GATE"], STAGES["INNOVATION_REVIEW"],
                            STAGES["DECISION_SUMMARY"], STAGES["PRODUCT_PROPOSAL"]],
        "required_gates": [STAGES["FIT_GATE"]],
        "allow_waiting": False,
    },
}


def log_error(opportunity_id: str, error: str):
    """记录错误日志（追加写，按机会 ID 分文件）

    参数：
        opportunity_id: 机会唯一标识，用于定位日志文件 runtime/logs/<id>.log
        error: 错误文本，会自动带上 ISO 时间戳与脚本名前缀
    返回：
        无返回值（副作用：向日志文件追加一行）
    为什么这么设计：
        门禁失败属于"确定性拒绝"，必须留下可追责的痕迹；
        用追加模式（'a'）而非覆盖，保证历史错误不被冲掉，便于交接排查。
        exist_ok=True 使重复调用不会因目录已存在而报错。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{opportunity_id}.log")
    with open(log_path, 'a', encoding='utf-8') as f:
        # 统一日志行格式，方便 grep / 人工核对失败原因与时间点
        f.write(f"[{now_iso()}] ERROR generate_report: {error}\n")


def check_all_gates(opportunity_id: str, report_type: str) -> dict:
    """产物生成前检查所有 Gate（生成动作的最后一道确定性防线）

    参数：
        opportunity_id: 机会唯一标识，用于定位状态文件与台账文件
        report_type: 报告类型，决定要校验哪些前置阶段/Gate（见 REPORT_REQUIREMENTS）
    返回：
        {"allowed": True}                          —— 通过，可以生成
        {"allowed": False, "reason": "..."}         —— 拒绝，reason 说明卡在哪一关
    断言/异常/退出码：
        本函数本身不抛异常、不退出；由 __main__ 依据返回结果决定 sys.exit(1)。
    为什么这么设计：
        1) 必须在"生成前"校验全部前置阶段与 Gate——产物一旦写出即成为对外可见的正式结果，
           事后补校验没有意义；只信脚本当场读到的状态与台账，不信任任何调用方传入的"已完成"声明。
        2) 状态文件缺失时"拒绝"而非"跳过"——状态文件是流程推进的唯一权威记录；
           没有它意味着 INIT/Gate 从未真正跑过，不能因为"没有记录"就默认放行（fail-closed）。
        3) 台账必须存在且版本绑定校验——防止有人先过 Gate、再改台账证据后沿用旧结论出报告。
    """
    # 台账：证据与结论的权威来源；状态文件：流程推进记录。二者都在 runtime/ 下的固定相对路径。
    ledger_path = f"runtime/evidence_ledger/{opportunity_id}.json"
    state_path = f"runtime/workflow_state/{opportunity_id}.json"

    # 第 0 关：报告类型必须已登记在规则表中，否则直接拒绝（避免未定义类型绕过校验）。
    requirements = REPORT_REQUIREMENTS.get(report_type)
    if not requirements:
        return {"allowed": False, "reason": f"不支持的报告类型: {report_type}"}

    # 第 1 关：状态文件必须存在。
    # 不存在即拒绝（而不是当作"无前置条件"跳过）——这是 fail-closed 的核心。
    if not os.path.exists(state_path):
        return {"allowed": False, "reason": f"状态文件不存在: {state_path}"}

    # 第 2 关：读取状态文件；损坏时记日志并拒绝，不允许"读不动就当通过"。
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        log_error(opportunity_id, f"状态文件 JSON 解析失败: {e}")
        return {"allowed": False, "reason": f"状态文件损坏: {e}"}

    # 补齐缺失字段结构（stage_results / hitl / schema_version），
    # 使后续 get() 访问不会因 KeyError 中断；这是兼容旧状态文件的手段。
    state = ensure_state_structure(state)

    # 第 3 关：检查当前顶层状态是否属于"被阻塞"状态。
    # WAITING_USER/STOPPED/REJECTED/FAILED 一律拒绝——这些状态代表流程没走完、
    # 或已被用户否决/异常终止，此时生成正式产物会误导下游。
    current_status = migrate_status(state.get("status", ""))
    blocked_statuses = [
        STATUSES["WAITING_USER"],
        STATUSES["STOPPED"],
        STATUSES["REJECTED"],
        STATUSES["FAILED"],
    ]
    # 例外：六看/适配度属于"阶段性报告"（过程产物，不是最终结论），
    # 允许在 WAITING_USER 或 STOPPED 时生成——用户正需要看到当前证据以决定是否补证/继续。
    # 但 REJECTED/FAILED 仍不豁免（流程已明确否决或异常）。
    interim = report_type in ("six_lens", "fit_assessment")
    if current_status in blocked_statuses and not (
        interim and current_status in (STATUSES["WAITING_USER"], STATUSES["STOPPED"])
    ):
        return {"allowed": False, "reason": f"当前状态 {current_status} 不允许生成报告"}

    # 第 4 关：逐个校验前置阶段是否完成（阶段名统一使用大写常量）。
    stage_results = state.get("stage_results", {})
    for required_stage in requirements["required_stages"]:
        # 阶段根本没出现在记录里 → 视为未完成
        if required_stage not in stage_results:
            return {"allowed": False, "reason": f"前置阶段 {required_stage} 未完成"}

        stage_status = migrate_status(stage_results[required_stage].get("status", ""))
        # Gate 阶段允许 COMPLETED 或 CONFIRMED：
        # 用户确认路径会把 Gate 阶段状态置为 CONFIRMED（而非 COMPLETED），
        # 若只认 COMPLETED 会把"已确认"误判为未通过。
        if required_stage in GATE_STAGES:
            if stage_status not in [STATUSES["COMPLETED"], STATUSES["CONFIRMED"]]:
                return {"allowed": False, "reason": f"Gate 阶段 {required_stage} 状态为 {stage_status}，未通过"}
        else:
            # 普通分析阶段必须是 COMPLETED
            if stage_status != STATUSES["COMPLETED"]:
                return {"allowed": False, "reason": f"前置阶段 {required_stage} 状态为 {stage_status}，未完成"}

    # 第 5 关：HITL（人工确认）状态检查——仅对声明了 require_confirmation 的报告类型生效。
    # 目的：S52（中适配）等需要用户拍板的场景，必须确认后才能出正式产物。
    if requirements.get("require_confirmation"):
        hitl = state.get("hitl", {})
        # 只要曾经处于"等待用户"（hitl.status=WAITING 或顶层 WAITING_USER），就必须已确认。
        if hitl.get("status") == "WAITING" or current_status == STATUSES["WAITING_USER"]:
            if not hitl.get("confirmed", False):
                return {"allowed": False, "reason": "等待用户确认中，不可生成产物"}

    # 第 6 关：所有正式报告都必须有台账——不能只凭状态文件放行。
    # 因为结论（六看/适配度）实际存放在台账里，缺台账就无法核对证据，理应拒绝。
    if not os.path.exists(ledger_path):
        return {"allowed": False, "reason": f"台账不存在: {ledger_path}"}
    try:
        with open(ledger_path, 'r', encoding='utf-8') as f:
            ledger = json.load(f)
    except json.JSONDecodeError as e:
        log_error(opportunity_id, f"台账 JSON 解析失败: {e}")
        return {"allowed": False, "reason": f"台账文件损坏: {e}"}

    # 补齐台账默认结构（schema_version / evidence / six_lens / fit_assessment / version_history）
    ledger = ensure_ledger_structure(ledger)
    # 读取台账中的适配度 Gate 等级（S51/S52/S53/S54）
    fit_gate = ledger.get("fit_assessment", {}).get("gate", "")
    if report_type in ("full_evaluation", "prd", "product_proposal"):
        # 版本绑定校验：每个必需的 Gate 阶段必须记录了它当时所依据的台账版本 ledger_version，
        # 且该版本不能"落后"于台账当前版本。
        # 若 gate_version 缺失（None）或小于当前版本，说明 Gate 通过后台账证据又被改过，
        # 旧结论已失效，此时拒绝生成，防止"用新证据 + 旧 Gate 结论"拼凑报告。
        for gate_stage in requirements["required_gates"]:
            gate_version = stage_results[gate_stage].get("ledger_version")
            if gate_version is None or gate_version > ledger.get("version", 0):
                return {"allowed": False, "reason": f"{gate_stage} 缺少有效证据版本"}
        # 完整评估/PRD/提案只在 S51（高适配）下出，其他等级只能出阶段性报告。
        if fit_gate != "S51":
            return {"allowed": False, "reason": "完整评估正式报告仅在 S51 通过后生成；其他等级可生成阶段报告"}
    if report_type == "product_proposal":
        # 产品提案额外要求：台账中已有授权人，且提案记录的版本与台账当前版本一致。
        # 授权人缺失或版本不一致 → 拒绝（避免未授权或过期授权的提案被当成正式产物）。
        proposal = ledger.get("product_proposal", {})
        if not proposal.get("authorized_by") or stage_results["PRODUCT_PROPOSAL"].get("ledger_version") != ledger.get("version"):
            return {"allowed": False, "reason": "提案授权或版本不匹配"}

    # 六看总体结论的兼容取值：不同历史版本把结论放在不同字段名上，
    # 这里按优先级依次回退，尽量兼容旧台账（references 其它脚本同样的兼容写法）。
    six_lens_conclusion = (
        ledger.get("conclusion", {}).get("overall") or
        ledger.get("six_lens", {}).get("overall") or
        ledger.get("updated_conclusion", {}).get("overall") or
        ledger.get("six_lens", {}).get("conclusion") or
        ""
    )
    # 六看结论为 NO / WATCH 时，完整评估/PRD 一律拒绝（结论已否定机会）。
    if report_type in ("full_evaluation", "prd") and str(six_lens_conclusion).upper() in ["NO", "WATCH"]:
        return {"allowed": False, "reason": f"six-lens conclusion rejected: {six_lens_conclusion}"}
    # 适配度 Gate 为 S53/S54（低适配/不适配）时，完整评估/PRD 一律拒绝。
    if report_type in ("full_evaluation", "prd") and fit_gate in ["S53", "S54"]:
        return {"allowed": False, "reason": f"fit gate rejected: {fit_gate}"}

    # 所有关卡通过
    return {"allowed": True}


def generate_markdown(opportunity_id: str, ledger: dict, report_type: str, output_dir: str) -> str:
    """按报告类型分派到具体的 Markdown 生成函数

    参数：
        opportunity_id: 机会唯一标识（用于拼产物文件名）
        ledger: 已补齐结构的台账字典（证据与结论来源）
        report_type: 报告类型
        output_dir: 输出目录
    返回：
        成功时返回产物文件路径；报告类型不在支持列表时返回 None
    为什么这么设计：
        只做"分派"，不做具体渲染，保持单一职责；
        未实现/不识别的类型返回 None，由调用方统一报错退出，避免静默产出空文件。
        注意：ops 层（ops_commands/innovation/report.py）当前只暴露
        full_evaluation / six_lens / fit_assessment 三种类型，
        prd / product_proposal 的生成逻辑已存在于本脚本，但尚未在 CLI 侧开放。
    """
    # 确保输出目录存在（不存在则创建），否则后续 mkstemp 会失败
    os.makedirs(output_dir, exist_ok=True)

    if report_type == "six_lens":
        return generate_six_lens_report(opportunity_id, ledger, output_dir)
    elif report_type == "fit_assessment":
        return generate_fit_assessment_report(opportunity_id, ledger, output_dir)
    elif report_type == "full_evaluation":
        return generate_full_evaluation_report(opportunity_id, ledger, output_dir)
    elif report_type == "product_proposal":
        proposal = ledger["product_proposal"]
        # 提案各字段 → 章节标题的中文映射（决定产物结构，改这里即改输出排版）
        labels = {
            "authorization_reason": "成案授权依据", "product_definition": "产品定义",
            "user_scenario": "用户与场景", "evidence_basis": "机会证据",
            "technical_boundary": "技术边界", "supply_chain_bom": "供应链与 BOM 粗判",
            "commercial_path": "商业路径", "compliance_risks": "合规与风险",
            "validation_plan": "验证计划",
        }
        # 标题头：产品名 + 授权人 + 证据版本（版本号体现"结论绑定到哪一版证据"）
        content = f"# {ledger['identity']['product_name']}｜产品提案\n\n授权人：{proposal['authorized_by']}\n证据版本：{proposal['evidence_version']}\n\n"
        for key, title in labels.items():
            content += f"## {title}\n\n{proposal[key]}\n\n"
        path = os.path.join(output_dir, f"{opportunity_id}-产品提案.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path
    elif report_type == "prd":
        return generate_prd_report(opportunity_id, ledger, output_dir)
    else:
        # 不支持的类型：返回 None，交由上层报错（不产出任何文件）
        return None


def generate_six_lens_report(opportunity_id: str, ledger: dict, output_dir: str) -> str:
    # 六看数据源的兼容取值（此处赋值的结果随后会被下方同名赋值覆盖，保留原写法）。
    six_lens = (
        ledger.get("six_lens", {}) or
        ledger.get("conclusion", {}) or
        ledger.get("updated_conclusion", {}) or
        {}
    )
    # 注意：下面这行 """生成六看报告""" 位于函数体中部，并非真正的 docstring
    # （Python 只把函数体第一条语句视为 docstring），它实际是一段无副作用的字符串常量表达式。
    # 交接说明：本文件只加注释、不改代码，故保留原样，仅提示此处不是有效文档字符串。
    """生成六看报告"""
    # 产品名缺失时给默认值，保证标题不出现空字段
    product_name = ledger.get("identity", {}).get("product_name", "未知产品")

    # 再次兼容取值（与函数开头重复，实际生效的是这一次）
    six_lens = (
        ledger.get("six_lens", {}) or
        ledger.get("conclusion", {}) or
        ledger.get("updated_conclusion", {}) or
        {}
    )

    # 六看总体结论，缺省为 "N/A"
    six_lens_conclusion = six_lens.get("overall", "N/A")

    # 报告头部：机会定义来自台账 identity 字段
    content = f"""# {product_name} - 六看压力测试报告

**生成时间**：{now_iso()}

## 机会定义

- **产品定义**：{ledger.get('identity', {}).get('product_definition', '')}
- **目标用户**：{ledger.get('identity', {}).get('target_user', '')}
- **核心场景**：{ledger.get('identity', {}).get('core_scenario', '')}
- **核心问题**：{ledger.get('identity', {}).get('core_problem', '')}

## 六看结论

**总体结论**：{six_lens_conclusion}

### 各维度分析

"""

    # 六看固定六维（顺序即输出顺序）
    dimensions = ['market', 'capital', 'industry', 'policy', 'user', 'competitor']
    # 维度英文键 → 中文显示名
    dimension_names = {
        'market': '市场/趋势',
        'capital': '资本/融资',
        'industry': '行业/供应链',
        'policy': '政策/合规',
        'user': '用户/需求',
        'competitor': '品类/竞品'
    }

    for dim in dimensions:
        dim_data = six_lens.get(dim, {})
        # 每个维度输出：标题 + 结论（缺省 N/A）
        content += f"""### {dimension_names.get(dim, dim)}

**结论**：{dim_data.get('conclusion', 'N/A')}

"""

    # 添加证据清单（最多列前 20 条，避免报告过长——这是一个展示层的截断，不是证据被丢弃）
    content += "\n## 证据清单\n\n"
    evidence_list = ledger.get("evidence", [])
    for i, evidence in enumerate(evidence_list[:20], 1):
        content += f"{i}. [{evidence.get('category', '')}] {evidence.get('fact', '')}\n"
        content += f"   - 来源：{evidence.get('source', '')} ({evidence.get('date', '')})\n"
        content += f"   - 证据等级：{evidence.get('grade', '')} | 信号：{evidence.get('signal', '')}\n\n"

    # 添加补证任务（六看下一步要补的证据项）
    if six_lens.get('evidence_tasks'):
        content += "\n## 补证任务\n\n"
        for task in six_lens['evidence_tasks']:
            content += f"- {task}\n"

    # 原子写入：先写临时文件，再 os.replace 到目标路径。
    # 这样即使写入中途崩溃，也不会留下半个文件——要么是旧版本，要么是完整新版本。
    output_path = os.path.join(output_dir, f"{opportunity_id}-六看扫描报告.md")
    fd, tmp_path = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, output_path)
    except Exception:
        # 失败时清理临时文件，避免残留 .tmp 垃圾文件
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    return output_path


def generate_fit_assessment_report(opportunity_id: str, ledger: dict, output_dir: str) -> str:
    """生成适配度评估报告（八维加权评分明细）

    参数：
        opportunity_id: 机会唯一标识
        ledger: 台账字典，评分主体在 ledger["fit_assessment"]
        output_dir: 输出目录
    返回：
        产物文件路径
    为什么这么设计：
        适配度是"确定交给脚本"的部分：总分/Gate 由 fit_scorer.py 计算后写入台账，
        本函数只做展示（读取台账字段渲染），不再重算，避免展示与判定不一致。
    """
    product_name = ledger.get("identity", {}).get("product_name", "未知产品")
    fit_assessment = ledger.get("fit_assessment", {})

    # 总分展示用 /100（内部加权满分即 100）；Gate 与描述直接取台账值
    content = f"""# {product_name} - 适配度评估报告

**生成时间**：{now_iso()}

## 评估结果

- **总分**：{fit_assessment.get('total_score', 0)}/100
- **Gate**：{fit_assessment.get('gate', 'N/A')}
- **结论**：{fit_assessment.get('gate_description', 'N/A')}

## 八维评分明细

"""

    # 八维英文键 → 中文显示名
    dimension_names = {
        'market_certainty': '市场/趋势确定性',
        'user_demand': '用户需求强度',
        'category_validation': '品类/竞品验证程度',
        'ai_value': 'AI 价值必要性',
        'org_capability': '组织能力匹配度',
        'quick_launch': '快速落地可行性',
        'business_path': '商业转化路径',
        'cost_risk': '成本与风险可控性'
    }

    # 优先使用 dimension_scores，兼容旧的 scores 字段（历史台账兼容）
    scores = fit_assessment.get('dimension_scores', fit_assessment.get('scores', {}))
    for dim, score in scores.items():
        content += f"- **{dimension_names.get(dim, dim)}**：{score}/10\n"

    # 添加计算明细（便于核对"原始分 × 权重 = 加权分"的算式）
    if fit_assessment.get('calculation_detail'):
        content += "\n## 计算明细\n\n"
        for dim, detail in fit_assessment['calculation_detail'].items():
            content += f"- {dimension_names.get(dim, dim)}：{detail.get('raw_score', 0)}/10 × {detail.get('weight', 0)}% = {detail.get('weighted_score', 0)}\n"

    # 原子写入（同六看报告：临时文件 + os.replace）
    output_path = os.path.join(output_dir, f"{opportunity_id}-适配度评估报告.md")
    fd, tmp_path = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    return output_path


def generate_full_evaluation_report(opportunity_id: str, ledger: dict, output_dir: str) -> str:
    """生成完整评估报告（六看 + 适配度合并）

    参数：
        opportunity_id: 机会唯一标识
        ledger: 台账字典
        output_dir: 输出目录
    返回：
        合并后产物文件路径
    为什么这么设计：
        先分别生成两份分报告文件，再读取拼接成一份完整报告，最后删除两份临时分报告，
        这样"分报告"与"完整报告"复用同一套渲染逻辑，避免重复维护。
    """
    product_name = ledger.get("identity", {}).get("product_name", "未知产品")

    # 生成六看部分（落到中间文件）
    six_lens_path = generate_six_lens_report(opportunity_id, ledger, output_dir)

    # 生成适配度部分（落到中间文件）
    fit_path = generate_fit_assessment_report(opportunity_id, ledger, output_dir)

    # 合并报告标题头
    content = f"""# {product_name} - 完整评估报告

**生成时间**：{now_iso()}

---

"""

    # 读取六看报告（存在才读，避免路径异常导致崩溃）
    if os.path.exists(six_lens_path):
        with open(six_lens_path, 'r', encoding='utf-8') as f:
            content += f.read()

    # 两段报告之间加分隔线
    content += "\n---\n\n"

    # 读取适配度报告
    if os.path.exists(fit_path):
        with open(fit_path, 'r', encoding='utf-8') as f:
            content += f.read()

    # 使用临时文件原子写入完整报告
    output_path = os.path.join(output_dir, f"{opportunity_id}-完整评估报告.md")
    fd, tmp_path = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    # 清理中间分报告：完整报告已包含其内容，保留会造成同机会多份冗余产物。
    if os.path.exists(six_lens_path):
        os.remove(six_lens_path)
    if os.path.exists(fit_path):
        os.remove(fit_path)

    return output_path


def generate_prd_report(opportunity_id: str, ledger: dict, output_dir: str) -> str:
    """生成 PRD 报告（当前为骨架版，多数小节标注"待补充"）

    参数：
        opportunity_id: 机会唯一标识
        ledger: 台账字典
        output_dir: 输出目录
    返回：
        产物文件路径
    为什么这么设计：
        1~3 节（产品概述/市场机会/适配度）由已有台账确定，直接填充；
        4~6 节（功能/非功能需求、里程碑）属于需要更多调研的"不确定"部分，
        刻意留"（待补充）"占位，而不是用模型臆测内容冒充需求——
        这与"确定交给脚本、不确定交给 LLM"的分工一致。
    """
    product_name = ledger.get("identity", {}).get("product_name", "未知产品")

    # PRD 骨架：产品概述四要素取自台账 identity
    content = f"""# {product_name} - 产品需求文档（PRD）

**生成时间**：{now_iso()}

## 1. 产品概述

### 1.1 产品定义
{ledger.get('identity', {}).get('product_definition', '')}

### 1.2 目标用户
{ledger.get('identity', {}).get('target_user', '')}

### 1.3 核心场景
{ledger.get('identity', {}).get('core_scenario', '')}

### 1.4 核心问题
{ledger.get('identity', {}).get('core_problem', '')}

## 2. 市场机会

（基于六看分析）

## 3. 适配度评估

（基于适配度评估）

## 4. 功能需求

（待补充）

## 5. 非功能需求

（待补充）

## 6. 里程碑

（待补充）
"""

    # 原子写入
    output_path = os.path.join(output_dir, f"{opportunity_id}-PRD.md")
    fd, tmp_path = tempfile.mkstemp(dir=output_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    return output_path


if __name__ == '__main__':
    # 入口从环境变量读取三要素：机会 ID、报告类型、输出目录
    opportunity_id = os.environ.get("OPPORTUNITY_ID")
    report_type = os.environ.get("REPORT_TYPE", "full_evaluation")
    output_dir = os.environ.get("OUTPUT_DIR", DEFAULT_OUTPUT_DIR)

    # OPPORTUNITY_ID 缺失直接报错退出：没有机会 ID 就无法定位状态/台账，谈不上校验。
    if not opportunity_id:
        print(json.dumps({"error": "缺少环境变量 OPPORTUNITY_ID"}, ensure_ascii=False))
        sys.exit(1)

    # 硬门禁：生成前先跑全部 Gate 校验（本脚本最重要的防线）。
    gate_check = check_all_gates(opportunity_id, report_type)
    if not gate_check["allowed"]:
        # 记日志 + 输出错误 JSON + 退出码 1（退出码让主 Agent 明确知道"未生成"）
        log_error(opportunity_id, f"Gate 未通过: {gate_check['reason']}")
        print(json.dumps({"error": f"Gate 未通过: {gate_check['reason']}"}, ensure_ascii=False))
        sys.exit(1)

    # 通过检查后再读取台账（双重保险：即使 check_all_gates 已读过，这里仍重新读一次）
    ledger_path = f"runtime/evidence_ledger/{opportunity_id}.json"
    if not os.path.exists(ledger_path):
        print(json.dumps({"error": f"台账不存在: {ledger_path}"}, ensure_ascii=False))
        sys.exit(1)

    try:
        with open(ledger_path, 'r', encoding='utf-8') as f:
            ledger = json.load(f)
    except json.JSONDecodeError as e:
        log_error(opportunity_id, f"台账 JSON 解析失败: {e}")
        print(json.dumps({"error": f"台账文件损坏: {e}"}, ensure_ascii=False))
        sys.exit(1)

    ledger = ensure_ledger_structure(ledger)

    # 生成报告（内部分派到具体渲染函数）
    output_path = generate_markdown(opportunity_id, ledger, report_type, output_dir)

    if output_path:
        # 成功：输出状态 + 路径 + 类型，退出码 0（默认）
        print(json.dumps({
            "status": "generated",
            "path": output_path,
            "type": report_type
        }, ensure_ascii=False))
    else:
        # 类型不支持：输出错误并退出码 1
        print(json.dumps({"error": f"不支持的报告类型: {report_type}"}, ensure_ascii=False))
        sys.exit(1)
