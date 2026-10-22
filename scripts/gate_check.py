#!/usr/bin/env python3
"""Gate 硬检查脚本 — 所有业务线复用

【本文件在机制中的位置与职责】
    这是整条工作流 A 的"裁判"脚本。在本工作区里有一条铁律：
    「不确定交给 LLM，确定交给脚本」——模型只负责产出分析结论，
    而"这道门到底过不过、下一步该往哪走"永远由本脚本按确定性规则判定。
    因此本文件承载三类确定性边界：
      1. 证据校验：借助 contracts.verified_support 判断"可核查证据"是否齐备；
      2. Gate 判定：把分析结论/分数翻译成 CONTINUE / WAIT_USER / STOP / ERROR；
      3. 状态落地：把 Gate 结果原子写入状态文件的独立 Gate 阶段
         （SIX_LENS_GATE / FIT_GATE），并维护 HITL 与 pending_output。
    注意：本脚本只做"结构化字段与来源字符串"的比对，只能判断证据是否"齐备/达标"，
    无法证明外部证据本身为真；证据真实性仍需人工核验。

【为什么把 Gate 抽成独立脚本】
    - 总分与 Gate 只能由脚本给出，模型不得自判，避免"模型说过了就过了"。
    - Gate 结果写入独立的 Gate 阶段，而不是覆盖分析阶段结果：
      这样"分析结论"与"门禁判定"两笔账分开，事后可审计到底是谁改了哪一步。
    - Gate 结果绑定 ledger_version（台账/证据版本）：台账一改，旧结论立即失效，
      防止"拿旧证据得出的 Gate 结论去应付新版本台账"。

【用法】
    OPPORTUNITY_ID=xxx python3 scripts/gate_check.py SIX_LENS
    OPPORTUNITY_ID=xxx python3 scripts/gate_check.py FIT_ASSESSMENT

【环境变量】
    OPPORTUNITY_ID: 机会唯一标识（缺失时直接 ERROR，退出码 1）

【输出】
    stdout 输出 JSON，包含 action (CONTINUE/WAIT_USER/STOP/ERROR) 等字段。

【功能】
    - 执行 Gate 检查
    - 自动更新 workflow_state.json（原子操作，经 contracts.atomic_write_json）
    - Gate 结果写入对应的 Gate 阶段（SIX_LENS_GATE / FIT_GATE）

【退出码语义】（给上层编排器用，切勿与 action 混淆）
    0 = 正常业务结论（CONTINUE 放行、WAIT_USER 转人工、STOP 终止，都算"跑通了"）
    1 = 调用方式/环境错误（缺 stage 参数、缺 OPPORTUNITY_ID、输入 JSON 非法）
    2 = 台账缺失/损坏，或本次 Gate 未成功落状态（state_updated=False，含 ERROR）
"""
import json
import sys
import os

# 把 scripts 目录加入模块搜索路径：保证无论从哪个工作目录调用本脚本，
# 都能 import 到同目录下的 contracts 契约模块（常量与工具函数的唯一来源）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 统一从 contracts 导入常量与工具：禁止在业务脚本里硬编码阶段名/状态名/动作名，
# 否则改契约时各处会各自漂移，出现"每个脚本一套常量"的隐性 bug。
from contracts import (
    STAGES, STATUSES, GATE_ACTIONS, ACTION_TO_STATUS,
    get_gate_stage_for_analysis, atomic_write_json, now_iso,
    ensure_state_structure, migrate_status, is_gate_stage,
    SIX_LENS_CORE_DIMENSIONS, SIX_LENS_DIMENSIONS, verified_support
)

# 日志目录：相对当前工作目录。Gate 是判定点，失败必须留痕，便于交接排查。
LOG_DIR = "runtime/logs"


def log_error(opportunity_id: str, script: str, error: str, retry_count: int = 0):
    """把一次 Gate 运行中的错误追加写入按机会 ID 分文件的日志。

    参数：
        opportunity_id: 机会唯一标识；用作日志文件名（<id>.log），按机会聚类便于排查。
        script:        出错脚本名（本文件固定传 "gate_check"），便于多脚本共用一个目录。
        error:         错误描述文本。
        retry_count:   重试次数，默认 0。用于区分"首次失败"与"反复失败"。

    返回：无返回值；副作用是向 runtime/logs/<id>.log 追加一行带时间戳的记录。

    为什么这么设计：
        Gate 判定一旦出错，事后必须能追溯，所以所有异常分支都调用它留痕。
        日志按机会分文件，避免所有机会的错误挤在一个文件；'a' 追加模式 + ISO 时间戳，
        保证多次运行的时间线完整可回溯。
        这里刻意不对文件写入做 try/except：日志写失败属于环境问题，
        不应反过来把正常的 Gate 判定流程打断。
    """
    # 确保日志目录存在（exist_ok=True 让重复/并发调用不报错）
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{opportunity_id}.log")
    # 'a' 追加模式：不覆盖历史，形成完整的错误时间线
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"[{now_iso()}] ERROR {script}: {error} (retry={retry_count})\n")


def check_six_lens(ledger):
    """六看（SIX_LENS）Gate 检查：决定"六看结论能否放行到适配度评估"。

    参数：
        ledger: 已解析的台账 dict（runtime/evidence_ledger/<id>.json）。

    返回：
        dict，至少包含 "action"（GATE_ACTIONS 之一）；视分支附带 next_stage /
        reason / core_dimension_gaps / dimension_gaps / evidence_gaps / evidence_tasks，
        用于告诉上层"该让用户补什么证"。

    可能的异常/退出码：本函数自身不抛异常；其返回的 action 由 __main__ 转成退出码。

    为什么这么设计：
        - 六看结论是"要不要继续投入"的第一道确定性闸门，而结论字段在不同时期
          台账里的位置不一致，因此先做多路兼容读取，避免旧台账被误判成"无结论"。
        - 本函数只比对台账里的结构化字段与来源字符串，不判断证据真假；
          真假仍需人工核验——脚本只保证"够不够门槛"。
    """
    # 兼容多种字段名：不同时期的台账把六看总结放在 conclusion / six_lens /
    # updated_conclusion / six_lens.conclusion 等位置，按序回退读取，任一命中即可，
    # 最后回退空串表示缺失。这样迁移期的台账不会被判成"无结论"而误伤。
    conclusion = (
        ledger.get("conclusion", {}).get("overall") or
        ledger.get("six_lens", {}).get("overall") or
        ledger.get("updated_conclusion", {}).get("overall") or
        ledger.get("six_lens", {}).get("conclusion") or
        ""
    )
    
    # 结论缺失属于"无法判定"，必须 ERROR 而不是默认放行：
    # 缺结论就放行等于把闸门敞开，直接违反"确定交给脚本"的原则。
    if not conclusion:
        return {
            "action": GATE_ACTIONS["ERROR"],
            "reason": "六看结论缺失，无法判定 Gate"
        }
    
    # 统一转大写，容忍台账里写成 yes / Yes / conditional 等情况；后续比较一律用大写。
    conclusion = str(conclusion).upper()
    
    # 核心维度完整性守卫：缺证不得当作"外部证据足够"
    # 核心三维 market/user/competitor 是六看的承重墙：任一维度没有结论、
    # 或结论是 WATCH/NO，则整体六看"不得判 YES"。这里先一次性收集缺口 core_gaps，
    # 一旦外部结论为 YES 就据此拦截；core_gaps 同时作为返回理由，指出具体缺哪一维。
    core_gaps = []
    for dimension in SIX_LENS_CORE_DIMENSIONS:
        part = ledger.get("six_lens", {}).get(dimension)
        if not isinstance(part, dict) or not part.get("conclusion"):
            # 维度结构缺失或该维度没有结论 → 记为"缺失"
            core_gaps.append(f"{dimension}(缺失)")
        elif str(part["conclusion"]).upper() in ("WATCH", "NO"):
            # 该维度结论为 WATCH（观望）或 NO（不成立）→ 记为不通过
            core_gaps.append(f"{dimension}({str(part['conclusion']).upper()})")

    # 结论说 YES，但核心维度有缺口 → 拒绝放行，转人工（WAIT_USER）：
    # 这里不"降级"成自动改判 CONDITIONAL，是因为改判属于模型/人的判断权；
    # 脚本只负责指出矛盾并要求补证或由人改判，绝不替模型下结论。
    if conclusion == "YES" and core_gaps:
        return {
            "action": GATE_ACTIONS["WAIT_USER"],
            "reason": f"六看结论为 YES，但核心维度证据不足: {', '.join(core_gaps)}；需补证或改判 CONDITIONAL/WATCH",
            "core_dimension_gaps": core_gaps
        }

    # 到这里说明外部结论为 YES 且核心三维无缺口；继续校验"六维是否都已给出结论"。
    # 六看要求 6 个维度都看全，缺任一维的结论就不算完整结论，仍转人工补全。
    if conclusion == "YES":
        missing = [dim for dim in SIX_LENS_DIMENSIONS
                   if not isinstance(ledger.get("six_lens", {}).get(dim), dict)
                   or not ledger["six_lens"][dim].get("conclusion")]
        if missing:
            return {"action": GATE_ACTIONS["WAIT_USER"],
                    "reason": f"六看维度结论未齐: {', '.join(missing)}",
                    "dimension_gaps": missing}
        # 更深一层：核心维度不仅要有结论，还必须挂有"可核查证据"。
        # verified_support 的口径 = A/B 级 + 正向 + 维度匹配 + 时效内 + ≥2 个不同来源字符串；
        # 少任一条件都不算"可核查"，故这里对核心三维逐一核验。
        evidence = ledger.get("evidence", [])
        unsupported = [dim for dim in SIX_LENS_CORE_DIMENSIONS
                       if not any(verified_support(item, dim) for item in evidence)]
        if unsupported:
            return {
                "action": GATE_ACTIONS["WAIT_USER"],
                "reason": f"核心维度缺少经双源标注的 A/B 正向证据: {', '.join(unsupported)}",
                "evidence_gaps": unsupported
            }
        # 三道校验全过：外部结论 YES + 六维齐 + 核心三维有可核查证据 → 放行到适配度评估。
        return {
            "action": GATE_ACTIONS["CONTINUE"],
            "next_stage": STAGES["FIT_ASSESSMENT"],
            "reason": "六看结论为 YES，继续适配度评估"
        }
    # CONDITIONAL（有条件成立）：既不能自动放行，也不该直接终止，
    # 需要用户决定是否补充证据——故 WAIT_USER，并把待办证据任务原样透传给用户。
    elif conclusion == "CONDITIONAL":
        return {
            "action": GATE_ACTIONS["WAIT_USER"],
            "reason": "六看结论为 CONDITIONAL，需要用户决定是否补证",
            "evidence_tasks": ledger.get("six_lens", {}).get("evidence_tasks", [])
        }
    # WATCH（观望）/ NO（不成立）：判定不成立，业务上就是"不做"，直接 STOP。
    # 这是正常业务结论而非错误，上层因此按退出码 0 处理。
    elif conclusion in ["WATCH", "NO"]:
        return {
            "action": GATE_ACTIONS["STOP"],
            "reason": f"六看结论为 {conclusion}，流程停止"
        }
    # 既非 YES/CONDITIONAL/WATCH/NO 的取值（例如拼写错误、旧枚举、脏数据）：
    # 无法安全推断意图，只能 ERROR 交人工处理，绝不猜测放行。
    else:
        return {
            "action": GATE_ACTIONS["ERROR"],
            "reason": f"未知的六看结论: {conclusion}"
        }


def check_fit_assessment(ledger):
    """适配度（FIT_ASSESSMENT）Gate 检查：把八维总分翻译成 S51/S52/S53/S54。

    参数：
        ledger: 已解析的台账 dict，需含 fit_assessment.total_score。

    返回：
        dict，含 "action"，S51 分支还带 "gate" 与 next_stage；各级都带 reason。

    为什么这么设计：
        - 总分必须"事先算好"（由 fit_scorer.py 产出并写入台账），本函数只读不算。
          这样把"加权计算"与"门禁判定"两件事解耦：计算口径只有一处，判定也只有一处。
        - S51（≥80）真放行前还要再核一次关键内部适配维度的可核查证据；
          其余档位只给出 action，不自动放行。
        注意：本脚本只核"是否存在达标的来源字符串"，证据真实性仍需人工核验。
    """
    fit_data = ledger.get("fit_assessment", {})
    
    # 必须读取台账里已算好的 total_score；不能在这里从原始 scores 现算，
    # 否则会出现"两处各自算分、口径不一"的问题。
    total_score = fit_data.get("total_score")
    
    # 总分缺失 → ERROR：说明上游没跑 fit_scorer.py，此时任何判定都无依据，必须拒绝。
    if total_score is None:
        return {
            "action": GATE_ACTIONS["ERROR"],
            "reason": "适配度总分缺失，必须先调用 fit_scorer.py 计算"
        }
    
    # 校验分数有效性：类型必须是数值。若是字符串等非数值类型，比较会抛异常/产生错误比较，
    # 因此宁可 ERROR 也不强行转换（强行 int() 会掩盖上游数据问题）。
    if not isinstance(total_score, (int, float)):
        return {
            "action": GATE_ACTIONS["ERROR"],
            "reason": f"适配度总分类型错误: {type(total_score)}"
        }
    
    # Gate 判定：阈值 80/65/50 分别对应 S51 高适配 / S52 中适配 / S53 观察池 / S54 暂停。
    # 这些阈值是产品侧商定的业务分档线，必须与 fit_scorer.py 中的分档保持完全一致，
    # 否则会出现"评分脚本判 S51、Gate 脚本判 S52"的口径冲突。
    if total_score >= 80:
        # S51 看似高适配，但关键"内部适配维度"（组织能力/快速落地/商业路径）若缺
        # 可核查证据，则高分可能建立在自我评估之上 → 不能直接放行，转人工补证。
        evidence = ledger.get("evidence", [])
        critical = ("org_capability", "quick_launch", "business_path")
        unsupported = [dim for dim in critical
                       if not any(verified_support(item, dim) for item in evidence)]
        if unsupported:
            return {
                "action": GATE_ACTIONS["WAIT_USER"],
                "gate": "S51",
                "reason": f"S51 初评分数成立，但关键内部适配维度缺可核查证据: {', '.join(unsupported)}",
                "evidence_gaps": unsupported
            }
        # 分数达标 + 关键维度证据齐备 → 放行，进入创新评审产出阶段（GENERATE）。
        return {
            "action": GATE_ACTIONS["CONTINUE"],
            "next_stage": STAGES["GENERATE"],
            "gate": "S51",
            "reason": f"S51 高适配（{total_score}分），可提交创新评审"
        }
    # 65-79 分：中适配，需要用户决定是否补证后复评 → WAIT_USER。
    elif total_score >= 65:
        return {
            "action": GATE_ACTIONS["WAIT_USER"],
            "reason": f"S52 中适配（{total_score}分），需要用户决定是否补证",
            "gate": "S52"
        }
    # 50-64 分：低适配，进入观察池 → STOP（不进主流程，但保留归档观察）。
    elif total_score >= 50:
        return {
            "action": GATE_ACTIONS["STOP"],
            "reason": f"S53 低适配（{total_score}分），进入观察池",
            "gate": "S53"
        }
    # <50 分：不适配，流程暂停 → STOP。STOP 是正常业务结论（不是错误）。
    else:
        return {
            "action": GATE_ACTIONS["STOP"],
            "reason": f"S54 不适配（{total_score}分），流程暂停",
            "gate": "S54"
        }


def check_priority(ledger):
    """优先级 Gate 检查（历史遗留占位，业务线 B 未走此入口）。

    参数：
        ledger: 台账 dict（本分支不使用）。

    返回：
        恒为 {"action": ERROR, ...}。

    为什么这么设计：
        业务线 B 的轻量 Gate/HITL 检查已由 workflow_b_gate.py 承担，
        本分支历史遗留、未实现，为保证"未实现即显式报错"，返回 ERROR 而非放行 CONTINUE
        （宁可报错，也不要静默放行一条未经验证的路径）。
    """
    # 业务线 B 未实现，返回 ERROR 而非 CONTINUE
    return {
        "action": GATE_ACTIONS["ERROR"],
        "reason": "业务线 B（优先级评估）尚未实现，请联系管理员"
    }


def update_workflow_state(opportunity_id: str, analysis_stage: str, gate_result: dict) -> dict:
    """根据 Gate 结果自动更新状态文件（原子写入）。

    参数：
        opportunity_id: 机会唯一标识，用于定位 runtime/workflow_state/<id>.json。
        analysis_stage: 本次检查对应的"分析阶段"名（如 SIX_LENS / FIT_ASSESSMENT），
                        本函数据此推导出对应的 Gate 阶段名。
        gate_result:    check_* 返回的 Gate 结果 dict，至少含 "action"。

    返回：
        成功：{"state_updated": bool, "state_status": ..., "state_stage": ...}
              幂等命中时额外含 "idempotent": True 与 "reason"。
        失败：{"error": "..."}（由上层转为 state_error，并计入退出码 2）。

    为什么这么设计：
        - Gate 结果写入独立的 Gate 阶段（SIX_LENS_GATE / FIT_GATE），
          而不是覆盖分析阶段结果，把"分析"与"判定"两笔账分开。
        - 幂等护栏：同一 ledger_version 的 Gate 已经是 WAIT_USER 或 STOP 时，
          再次调用直接返回原结果，不再写状态、不再跑检查——防止重复跑同一道门
          导致状态被反复改写或产生多份 pending_output。
        - 先读 ledger_version 再写 Gate 结果，实现"Gate 结论绑定证据版本"。
    """
    state_path = f"runtime/workflow_state/{opportunity_id}.json"
    
    # 获取对应的 Gate 阶段：分析阶段 → Gate 阶段（SIX_LENS→SIX_LENS_GATE，其余同理）
    gate_stage = get_gate_stage_for_analysis(analysis_stage)
    if not gate_stage:
        # 若传入的本身就是 Gate 阶段名，直接复用；否则无法确定门禁去向 → 报错不写。
        if is_gate_stage(analysis_stage):
            gate_stage = analysis_stage
        else:
            return {"error": f"无法确定 Gate 阶段: {analysis_stage}"}
    
    # 读取或创建状态文件
    if os.path.exists(state_path):
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except json.JSONDecodeError as e:
            # 状态文件损坏时先留日志，再返回错误（不尝试"修复"半损坏的状态文件，
            # 避免把不确定的脏状态写回去）。
            log_error(opportunity_id, "gate_check", f"状态文件 JSON 解析失败: {e}")
            return {"error": f"状态文件损坏: {e}"}
    else:
        # 首次运行时按最小 schema 初始化状态文件（后续 ensure_state_structure 补全字段）。
        state = {
            "schema_version": 1,
            "opportunity_id": opportunity_id,
            "workflow_name": "opportunity-evaluation",
            "created_at": now_iso(),
        }
    
    # 确保结构完整：补齐 stage_results / hitl / schema_version 等字段，避免 KeyError。
    state = ensure_state_structure(state)
    
    # 迁移旧状态值：把历史遗留的状态名（如 WAITING_USER_INPUT）映射为当前枚举。
    if "status" in state:
        state["status"] = migrate_status(state["status"])
    
    # 根据 Gate 结果确定状态：动作 → 状态（CONTINUE→COMPLETED, WAIT_USER→WAITING_USER,
    # STOP→STOPPED, ERROR→FAILED）。未登记的动作说明契约被破坏 → 报错不写。
    action = gate_result.get("action")
    if action not in ACTION_TO_STATUS:
        log_error(opportunity_id, "gate_check", f"未知的 Gate 动作: {action}")
        return {"error": f"未知的 Gate 动作: {action}"}
    
    status = ACTION_TO_STATUS[action]

    # 前置守卫：只有该"分析阶段"已完成（COMPLETED）才允许为其跑/写 Gate。
    # 未完成就跑 Gate 属于越权判定，直接拒绝（防止模型跳过分析直接探门禁）。
    analysis_result = state.get("stage_results", {}).get(analysis_stage, {})
    if migrate_status(analysis_result.get("status", "")) != STATUSES["COMPLETED"]:
        return {"error": f"分析阶段 {analysis_stage} 未标记为 COMPLETED"}
    
    # 将 Gate 绑定到本次证据版本，防止后续改台账沿用旧结论。
    ledger_path = f"runtime/evidence_ledger/{opportunity_id}.json"
    with open(ledger_path, encoding="utf-8") as handle:
        ledger_version = json.load(handle).get("version")
    
    # 幂等护栏：同一 ledger_version 的 Gate 已经是 WAIT_USER 或 STOP 时，直接返回原结果。
    # 之所以只护栏 WAIT_USER/STOP 这两种"终局等待/终止"状态：它们需要用户介入或已终止，
    # 重复调用不应改写；CONTINUE/ERROR 则可被新一轮运行覆盖（视作可重跑）。
    existing_gate_result = state.get("stage_results", {}).get(gate_stage, {})
    if (existing_gate_result.get("ledger_version") == ledger_version and
        existing_gate_result.get("status") in [STATUSES["WAITING_USER"], STATUSES["STOPPED"]]):
        # 已存在相同版本的 Gate 结果，直接返回（注意此时 state_updated=False，
        # 上层据此可能判为"未落状态"——这是有意为之的"没写=没变化"语义）。
        return {
            "state_updated": False,
            "state_status": existing_gate_result["status"],
            "state_stage": gate_stage,
            "idempotent": True,
            "reason": f"Gate {gate_stage} 已存在相同版本 {ledger_version} 的结果"
        }
    
    # 更新状态：当前阶段推进到 Gate 阶段，工作流整体状态取本门禁的判定结果。
    state["current_stage"] = gate_stage
    state["status"] = status
    state["updated_at"] = now_iso()
    
    # 更新 Gate 阶段结果（写入独立 Gate 阶段，而不是分析阶段，二者分开记账）
    state["stage_results"][gate_stage] = {
        "status": status,
        "output": gate_result,
        "ledger_version": ledger_version,
        "completed_at": now_iso()
    }
    
    # 更新 HITL（Human-in-the-Loop）状态：只有"需要人"或"人已确认"才动它
    if action == "WAIT_USER":
        # 等待用户：记录提示语与等待起点，便于前端展示与超时统计。
        state["hitl"]["status"] = "WAITING"
        state["hitl"]["confirmed"] = False
        state["hitl"]["prompt"] = gate_result.get("reason", "")
        state["hitl"]["waiting_since"] = now_iso()
        state["hitl"]["user_decision"] = None
    elif action == "CONTINUE":
        # 从"等待"恢复为放行时，视为用户已确认（confirmed=True）。
        # 仅当此前确实处于 WAITING 才改写，避免无谓变更其它状态。
        if state["hitl"].get("status") == "WAITING":
            state["hitl"]["status"] = "CONFIRMED"
            state["hitl"]["confirmed"] = True
    elif action == "STOP":
        # 终止：本门禁不再需要人机交互，HITL 置 N/A。
        state["hitl"]["status"] = "N/A"
    
    # WAIT_USER 和 STOP 都写 pending_output：这两类结果需要"向外播报"
    # （等待用户的那一条要发给用户，终止的那一条要通知结果），因此登记一条待发送记录。
    if action in ["WAIT_USER", "STOP"]:
        # 构造 delivery_key：机会 ID + ledger_version + gate_stage + action + 目标会话。
        # 该 key 用于"同一件事只发一次"的幂等去重；目标会话暂用 "default"，
        # 实际发送时由上层替换为真实会话标识。
        delivery_key = f"{opportunity_id}:v{ledger_version}:{gate_stage}:{action}:default"
        
        # 检查是否已存在相同 delivery_key 的未完成记录
        existing_pending = state.get("pending_output", {})
        if (existing_pending.get("delivery_key") == delivery_key and
            existing_pending.get("status") in ["pending", "sending"]):
            # 已存在相同 delivery_key 的未完成记录，不新建（跳过，保持幂等）
            pass
        else:
            # 新建或替换 pending_output。
            # 注意：此处 pending_output 为"单个 dict"的旧版结构；若新版契约要求
            # pending_output 为列表/按 key 索引的字典，本段写入结构可能与之不一致，
            # 交接时需核对上层读取端能否兼容。
            state["pending_output"] = {
                "delivery_key": delivery_key,
                "status": "pending",
                "gate_stage": gate_stage,
                "action": action,
                "output": gate_result,
                "ledger_version": ledger_version,
                "created_at": now_iso()
            }
    
    # 原子写入：先用临时文件再 os.replace，避免"写一半"产生半损坏状态文件。
    try:
        atomic_write_json(state_path, state)
    except Exception as e:
        # 写盘失败属于环境问题：记日志并返回 error，由上层计入退出码 2。
        log_error(opportunity_id, "gate_check", f"状态写入失败: {e}")
        return {"error": f"状态写入失败: {e}"}
    
    return {
        "state_updated": True,
        "state_status": status,
        "state_stage": gate_stage
    }


if __name__ == '__main__':
    # 参数守卫 1：必须给定阶段名，否则无法确定跑哪道门 → ERROR + 退出码 1。
    if len(sys.argv) < 2:
        print(json.dumps({
            "action": GATE_ACTIONS["ERROR"],
            "reason": "用法: gate_check.py <stage>"
        }, ensure_ascii=False))
        sys.exit(1)
    
    # 阶段名统一大写，容忍用户传小写；机会 ID 从环境变量取（约定俗成的传参方式）。
    stage = sys.argv[1].upper()
    opportunity_id = os.environ.get("OPPORTUNITY_ID")
    
    # 参数守卫 2：缺机会 ID 无法定位台账/状态文件 → ERROR + 退出码 1。
    if not opportunity_id:
        print(json.dumps({
            "action": GATE_ACTIONS["ERROR"],
            "reason": "缺少环境变量 OPPORTUNITY_ID"
        }, ensure_ascii=False))
        sys.exit(1)
    
    # 读取台账
    ledger_path = f"runtime/evidence_ledger/{opportunity_id}.json"
    # 台账不存在：没有证据来源就无法判定 → ERROR + 退出码 2（区别于参数错误）。
    if not os.path.exists(ledger_path):
        print(json.dumps({
            "action": GATE_ACTIONS["ERROR"],
            "reason": f"台账不存在: {ledger_path}"
        }, ensure_ascii=False))
        sys.exit(2)
    
    # 解析台账 JSON；损坏则留日志并 ERROR + 退出码 2（不尝试部分解析脏数据）。
    try:
        with open(ledger_path, 'r', encoding='utf-8') as f:
            ledger = json.load(f)
    except json.JSONDecodeError as e:
        log_error(opportunity_id, "gate_check", f"台账 JSON 解析失败: {e}")
        print(json.dumps({
            "action": GATE_ACTIONS["ERROR"],
            "reason": f"台账文件损坏: {e}"
        }, ensure_ascii=False))
        sys.exit(2)
    
    # 执行检查：按阶段名分派到对应的检查函数
    if stage == STAGES["SIX_LENS"]:
        result = check_six_lens(ledger)
    elif stage == STAGES["FIT_ASSESSMENT"]:
        result = check_fit_assessment(ledger)
    elif stage == STAGES["PRIORITY"]:
        # 注意：STAGES 常量表里并无 "PRIORITY" 键，这行会抛 KeyError 导致脚本崩溃；
        # 这是历史遗留的分支，交接时需确认是否还需要它（不改代码，仅标注）。
        result = check_priority(ledger)
    else:
        # 未知阶段：无法判定 → ERROR（不作为正常业务结论）
        result = {
            "action": GATE_ACTIONS["ERROR"],
            "reason": f"未知阶段: {stage}"
        }
    
    # 所有可识别结果都落状态；ERROR 映射为 FAILED，避免运行记录停在旧阶段。
    # 落状态与判定结果解耦：即使判定为 ERROR，也要把 FAILED 写进状态，留下运行痕迹。
    update_result = update_workflow_state(opportunity_id, stage, result)
    if "error" in update_result:
        # 落状态失败：标记 state_updated=False 并把原因附到输出，同时记日志。
        result["state_updated"] = False
        result["state_error"] = update_result["error"]
        log_error(opportunity_id, "gate_check", update_result["error"])
    else:
        # 落状态成功（或幂等命中）：把状态结果回填到输出，供上层读取。
        result["state_updated"] = update_result["state_updated"]
        result["state_status"] = update_result["state_status"]
        result["state_stage"] = update_result["state_stage"]
    
    print(json.dumps(result, ensure_ascii=False))
    
    # STOP 是正常业务结论，退出码为 0
    # （STOPPED 属于流程正常终止，不该被上层当成脚本执行失败。）
    if result["action"] == GATE_ACTIONS["STOP"]:
        sys.exit(0)
    
    # ERROR 时退出码为 2
    # 注意：这里的条件还包含 "未成功落状态"（state_updated=False）。因此当幂等护栏命中
    # （返回 state_updated=False）时，即便 action 是 WAIT_USER，也会走到退出码 2；
    # 交接时需确认上层对"幂等重跑退 2"的容忍度。CONTINUE/WAIT_USER 且落状态成功时
    # 不会进入本分支，脚本自然结束即为退出码 0。
    if result["action"] == GATE_ACTIONS["ERROR"] or not result.get("state_updated", False):
        sys.exit(2)
