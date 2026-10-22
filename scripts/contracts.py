#!/usr/bin/env python3
"""流程编排公共契约模块 — 统一常量、校验和工具函数

所有脚本必须从此模块导入常量，禁止硬编码字符串。

【本文件在整体机制中的位置】
本模块是「AI 产品经理数字员工」工作区的 **唯一契约来源（single source of truth）**。
整个工作区遵循「不确定交给 LLM，确定交给脚本」的设计原则：
- 主 Agent（LLM）是唯一的工作流写入者，负责输入分流、派生隔离子 Agent、检索、分析等
  不确定环节；
- scripts/ 下的脚本是确定性边界：证据字段校验、加权评分、Gate 判定、状态迁移、幂等、
  退出码；
- 子 Agent 只往 runtime/tasks/ 写暂存分片，不得调用 Gate/状态/报告脚本、不得写正式台账。
本模块正是这条确定性边界中最底层、最共享的一段：提供阶段/状态/Gate 动作常量、
状态迁移白名单、原子写入工具、证据字段校验与双源时效校验。

为什么要求「所有脚本必须从此模块导入常量」而不是各自硬编码字符串：
    一旦阶段名、状态名、Gate 动作散落在各脚本里各写一份，两条业务线（工作流 A 评估 /
    工作流 B 需求驱动提案）的语义就会逐渐漂移，Gate 判定与状态机将失去统一口径，
    最终无法保证「确定性」——所以此处收敛为单一来源，任何脚本都不得绕开。

【两条业务线】
工作流 A 产品机会评估：
    INIT → TEARDOWN(可选) → SIX_LENS → SIX_LENS_GATE → FIT_ASSESSMENT
    → FIT_GATE → GENERATE → INNOVATION_REVIEW → DECISION_SUMMARY → PRODUCT_PROPOSAL
工作流 B 需求驱动提案：
    DEMAND_INSIGHT → BP_GENERATE → PRD_MVP → PRD_MVP_GATE → MODAO_DEMO

【统一入口】
所有脚本经 scripts/ops.py 统一命令入口暴露，命令形如 innovation:state.read、
innovation:gate.check，便于主 Agent 以稳定命名调用确定性能力。

【本模块导出的契约】
1. 阶段常量：STAGES、GATE_STAGES、WORKFLOW_B_STAGES、STAGE_ORDER、
   WORKFLOW_B_STAGE_ORDER、STAGE_PREDECESSORS；
2. 六看与证据：SIX_LENS_DIMENSIONS、SIX_LENS_CORE_DIMENSIONS、EVIDENCE_GRADES；
3. 状态常量：STATUSES、LEGACY_STATUS_MAP；
4. Gate 动作：GATE_ACTIONS、ACTION_TO_STATUS；
5. 状态迁移规则：VALID_TRANSITIONS、TERMINAL_STATES；
6. Schema 版本：WORKFLOW_STATE_SCHEMA_VERSION、EVIDENCE_LEDGER_SCHEMA_VERSION；
7. 工具函数：now_iso、atomic_write_json、migrate_status、validate_transition、
   is_terminal_state、is_gate_stage、get_gate_stage_for_analysis、
   validate_stage_progression、ensure_state_structure、ensure_ledger_structure、
   validate_evidence、verified_support、normalize_evidence。

【文件落盘位置约定】
- 状态文件：runtime/workflow_state/{id}.json（由 workflow_state.py 读写）
- 台账文件：runtime/evidence_ledger/{id}.json
- 暂存分片：runtime/tasks/（子 Agent 产出，主 Agent 汇总）
"""
import json
import os
import tempfile
from datetime import datetime, date
from typing import Optional, Dict, Any

# ============================================================
# 阶段常量（全大写）
# ============================================================
# 为什么用「键=值」的字典而不是枚举（Enum）或字符串常量列表：
#   脚本对外一律以 JSON 输出，而 JSON 无法安全序列化枚举对象；用值等于键的字典，
#   既能作为白名单做校验（`stage in STAGES.values()`），又能直接参与 JSON 序列化，
#   同时保留按名访问（STAGES["INIT"]）的可读性，避免脚本里散落裸字符串。
STAGES = {
    # 工作流 A：评估阶段
    "INIT": "INIT",  # 起点：受理机会、建立状态文件
    "TEARDOWN": "TEARDOWN",  # 可选：拆解既有方案/竞品，为六看做准备
    "SIX_LENS": "SIX_LENS",  # 六看检索与分析（market/capital/industry/policy/user/competitor）
    "SIX_LENS_GATE": "SIX_LENS_GATE",  # 六看门禁：判定是否继续进入适配度评估
    "FIT_ASSESSMENT": "FIT_ASSESSMENT",  # 适配度八维加权评分
    "FIT_GATE": "FIT_GATE",  # 适配度门禁：决定 S51/S52/S53/S54 分支
    "GENERATE": "GENERATE",  # 生成提案初稿
    "INNOVATION_REVIEW": "INNOVATION_REVIEW",  # 创新性复核
    "DECISION_SUMMARY": "DECISION_SUMMARY",  # 决策摘要汇总
    "PRODUCT_PROPOSAL": "PRODUCT_PROPOSAL",  # 产品提案定稿（工作流 A 终点）
    # 工作流 B：执行阶段
    "DEMAND_INSIGHT": "DEMAND_INSIGHT",  # 需求洞察（依赖工作流 A 的 FIT_GATE 通过）
    "BP_GENERATE": "BP_GENERATE",  # 可选：生成商业计划
    "PRD_MVP": "PRD_MVP",  # 产出 PRD/MVP 定义
    "PRD_MVP_GATE": "PRD_MVP_GATE",  # HITL Gate（人工确认门禁）
    "MODAO_DEMO": "MODAO_DEMO",  # 可选：墨刀原型演示（工作流 B 终点）
}

# Gate 阶段集合
# 为什么单独抽出：Gate 阶段需要「人工/HITL 判定」而非纯脚本判定。
# 后续 pending_output 的 STOP 防重放（workflow_state.update_state）、resume 恢复校验、
# 报告门禁识别，都只对 Gate 阶段生效；用集合而不是散落地写 if 判断，
# 可保证「哪些阶段是门禁」的口径唯一，避免各脚本各写一套。
GATE_STAGES = {"SIX_LENS_GATE", "FIT_GATE", "PRD_MVP_GATE"}

# 工作流 B 阶段集合
# 用于把「执行阶段」与「评估阶段」区分开：两条业务线顺序独立，
# 工作流 B 的起点 DEMAND_INSIGHT 需要工作流 A 的 FIT_GATE 作为前置。
WORKFLOW_B_STAGES = {"DEMAND_INSIGHT", "BP_GENERATE", "PRD_MVP", "PRD_MVP_GATE", "MODAO_DEMO"}

# 合法阶段顺序（用于校验）
# 工作流 A：评估阶段
# 注意这是「顺序参考」而非强制硬约束：像 TEARDOWN 这样的可选分支无法用「相邻关系」表达，
# 真正的先后依赖由 STAGE_PREDECESSORS 承担，此处主要供文档化与报告排序使用。
STAGE_ORDER = [
    "INIT",
    "TEARDOWN",  # 可选
    "SIX_LENS",
    "SIX_LENS_GATE",
    "FIT_ASSESSMENT",
    "FIT_GATE",
    "GENERATE",
    "INNOVATION_REVIEW",
    "DECISION_SUMMARY",
    "PRODUCT_PROPOSAL",
]

# 工作流 B：执行阶段（独立顺序）
# 与工作流 A 分开维护：两条业务线不共享阶段序号，避免 A 侧新增阶段意外影响 B 的推进。
WORKFLOW_B_STAGE_ORDER = [
    "DEMAND_INSIGHT",
    "BP_GENERATE",  # 可选
    "PRD_MVP",
    "PRD_MVP_GATE",  # HITL Gate
    "MODAO_DEMO",  # 可选
]

# 阶段前置依赖：{阶段: [必须已完成的直接前置阶段]}
# 为什么只记录「直接前置」而不是「全部先祖」：
#   每个阶段的完成状态都记录在状态文件的 stage_results 里，只校验直接前置即可形成链式约束；
#   减少无关阶段与分支的耦合。GENERATE 与 INNOVATION_REVIEW 都只依赖 FIT_GATE，
#   说明 FIT_GATE 通过后可并行分叉。
# 未出现在本表中的阶段（INIT、TEARDOWN）即无前置：INIT 是根，TEARDOWN 为可选，
#   因此在 validate_stage_progression 里「取空列表 → 直接通过」。
STAGE_PREDECESSORS = {
    # 工作流 A
    "SIX_LENS": ["INIT"],
    "SIX_LENS_GATE": ["SIX_LENS"],
    "FIT_ASSESSMENT": ["SIX_LENS_GATE"],
    "FIT_GATE": ["FIT_ASSESSMENT"],
    "GENERATE": ["FIT_GATE"],
    "INNOVATION_REVIEW": ["FIT_GATE"],
    "DECISION_SUMMARY": ["INNOVATION_REVIEW"],
    "PRODUCT_PROPOSAL": ["DECISION_SUMMARY"],
    # 工作流 B
    "DEMAND_INSIGHT": ["FIT_GATE"],  # 需要工作流 A 的 FIT_GATE 通过
    "BP_GENERATE": ["DEMAND_INSIGHT"],
    "PRD_MVP": ["DEMAND_INSIGHT"],  # BP 可选，PRD 只依赖需求洞察
    "PRD_MVP_GATE": ["PRD_MVP"],
    "MODAO_DEMO": ["PRD_MVP_GATE"],
}

# ============================================================
# 六看维度与证据分级
# ============================================================
# 六看固定维度，分片任务与任务书均以此为准
# 为什么固定成这六个、且必须与派生子 Agent 的分片任务一一对应：
#   主 Agent 会按维度派生隔离子 Agent 并行检索，若维度口径不统一，
#   汇总时就会出现缺口或重复，六看门禁也无法据「全部维度是否都有结论」做判定。
SIX_LENS_DIMENSIONS = ["market", "capital", "industry", "policy", "user", "competitor"]

# 核心维度：任一无结论或不成立时，六看不得给出 YES
# 为什么核心三维要单独拎出、判定比其余三维更严格：
#   market/user/competitor 直接决定「机会是否真实存在、是否有人要」；
#   capital/industry/policy 更多是环境约束（锦上添花），
#   环境再好也无法弥补「市场或用户需求不成立」这一根本缺口。
SIX_LENS_CORE_DIMENSIONS = ["market", "user", "competitor"]

# 证据等级：A 官方/一手，B 媒体/研报，C 社区/推测
# 分级的意义：A/B 级可进入「已核实支持」通道（见 verified_support），
# C 级只能作为背景信息，不能单独支撑门禁放行。
EVIDENCE_GRADES = ["A", "B", "C"]

# ============================================================
# 状态常量
# ============================================================
# 状态表示「当前阶段推进到哪一步」，与阶段（STAGES）正交：
#   阶段回答「在哪个环节」，状态回答「这个环节处于什么进度」。
# 为什么需要这么多状态：Gate 动作（CONTINUE/WAIT_USER/STOP/ERROR）必须落成可判定的状态，
#   才能让主 Agent 的下一步调度由状态驱动，而不是靠 LLM 猜测。
STATUSES = {
    "PENDING": "PENDING",  # 待运行
    "RUNNING": "RUNNING",  # 运行中
    "COMPLETED": "COMPLETED",  # 已完成（Gate CONTINUE 落此态）
    "WAITING_USER": "WAITING_USER",  # 等待用户确认（Gate WAIT_USER 落此态）
    "CONFIRMED": "CONFIRMED",  # 用户已确认（WAITING_USER 的正向出口）
    "STOPPED": "STOPPED",  # 已终止（Gate STOP 落此态，终态）
    "FAILED": "FAILED",  # 失败（Gate ERROR 落此态）
    "REJECTED": "REJECTED",  # 用户驳回（WAITING_USER 的负向出口，终态）
}

# 旧状态值兼容映射
# 为什么需要它：早期版本曾使用 WAITING_USER_INPUT，历史状态文件不能作废；
#   读取/更新时先经 migrate_status 归一化，避免新旧值并存导致判定口径分裂。
LEGACY_STATUS_MAP = {
    "WAITING_USER_INPUT": "WAITING_USER",
}

# ============================================================
# Gate 动作常量
# ============================================================
# Gate 动作表示「门禁结论的语义」，并不直接等于状态。
# 为什么要在动作与状态之间加一层映射，而不是让 Gate 脚本直接写状态：
#   这样「判定逻辑（不确定，可能由 LLM/规则产生）」与「状态机（确定）」解耦；
#   将来若新增动作或调整落态，只需改映射表，不必逐个改动 Gate 脚本的判定分支。
GATE_ACTIONS = {
    "CONTINUE": "CONTINUE",  # 放行进入下一阶段
    "WAIT_USER": "WAIT_USER",  # 挂起等待用户确认
    "STOP": "STOP",  # 终止工作流
    "ERROR": "ERROR",  # 判定过程异常
}

# Gate 动作到状态的映射
# 这是「不确定（判定动作）→ 确定（落地状态）」的衔接点：
#   动作由 LLM/规则判定产生，落状态由状态机确定性执行。
ACTION_TO_STATUS = {
    "CONTINUE": "COMPLETED",
    "WAIT_USER": "WAITING_USER",
    "STOP": "STOPPED",
    "ERROR": "FAILED",
}

# ============================================================
# 状态迁移规则
# ============================================================
# 格式: {from_status: [allowed_to_statuses]}
# 为什么必须用显式白名单，而不是允许自由赋值：
#   状态是主 Agent 调度的唯一依据。一旦允许任意迁移，就可能出现
#   「伪造 COMPLETED」「从 STOPPED/REJECTED 复活」等破坏台账一致性的路径。
#   把全部合法迁移集中到契约层，任何脚本都绕不过。
VALID_TRANSITIONS = {
    "PENDING": ["RUNNING", "STOPPED", "FAILED"],  # 待运行：开始 / 直接终止 / 失败
    "RUNNING": ["COMPLETED", "WAITING_USER", "STOPPED", "FAILED"],  # 运行中：正常结束 / 挂起 / 终止 / 失败
    "WAITING_USER": ["CONFIRMED", "REJECTED", "STOPPED", "FAILED"],  # 等待用户：确认 / 驳回 / 终止 / 失败
    "CONFIRMED": ["RUNNING", "COMPLETED", "STOPPED", "FAILED"],  # 已确认：回到运行或直接完成
    "COMPLETED": ["RUNNING"],  # 允许重新运行（同阶段重跑，例如证据补充后重评）
    "STOPPED": [],  # 终态，不可恢复：终止是最终决定，防止被后续流程「复活」
    "REJECTED": [],  # 终态，不可恢复：用户驳回即终局，避免绕过用户意志继续推进
    "FAILED": ["PENDING"],  # 允许重试：失败可回到待运行重来，但不允许直接跳到完成
}

# 禁止恢复的终态
# 与 VALID_TRANSITIONS 中这两项的空列表相对应：抽成集合便于各脚本快速判断
# （如 workflow_state.update_state 对终态恢复的提前拦截）。
TERMINAL_STATES = {"STOPPED", "REJECTED"}

# ============================================================
# Schema 版本
# ============================================================
# 为什么状态与台账各自维护版本号，而不共用一个：
#   两者演进节奏不同——状态文件结构可能因新增字段而升级，台账结构则随证据模型变化。
#   各自独立版本号，便于读取时按需做向后兼容迁移，而不会相互误判。
WORKFLOW_STATE_SCHEMA_VERSION = 1
EVIDENCE_LEDGER_SCHEMA_VERSION = 1

# ============================================================
# 工具函数
# ============================================================
# 以下函数是本模块对外提供的确定性能力，覆盖：时间戳、原子写入、状态归一化与迁移校验、
# 结构补全、证据校验与双源时效校验、证据规范化。所有脚本都应复用这些函数，
# 以保证两条业务线在同一套契约下运行。

def now_iso() -> str:
    """返回当前时间的 ISO 格式字符串

    返回值：
        str：datetime.now().isoformat() 的输出（本地时间，ISO 8601 形式）。

    为什么所有脚本统一走这一个函数、而不各自调用 datetime：
        时间戳是审计与时效校验的基础字段，集中到一处，便于将来需要统一时区/格式时
        只改这一个出口；也避免不同脚本生成格式不一致的时间串，导致比较或展示出错。
    """
    return datetime.now().isoformat()


def atomic_write_json(path: str, data: Dict[str, Any], ensure_ascii: bool = False) -> None:
    """原子写入 JSON 文件

    参数：
        path: 目标文件路径；父目录不存在时会自动创建。
        data: 待写入的、可 JSON 序列化的对象。
        ensure_ascii: 透传给 json.dump 的编码开关；默认 False 以保留中文原文。

    异常：
        IOError/OSError：目录创建或文件写入失败时抛出。
        TypeError：data 无法 JSON 序列化时抛出。
        任意异常：都会先清理临时文件再向上抛出，避免残留 .tmp 残骸。

    为什么必须原子写入（而不是直接 open(path,'w') 覆写）：
        状态文件与台账是主 Agent 调度的唯一依据。若写到一半进程中断/崩溃，
        直接覆写会留下半截 JSON，后续读取将 JSONDecodeError、状态丢失。
        采用「临时文件 + os.replace」：os.replace 在同目录下是原子操作，
        读者要么读到旧的完整文件，要么读到新的完整文件，绝不会读到半成品。
    """
    # 父目录可能尚未创建（首次写入某机会时），这里兜底创建；"." 处理路径无目录名的情形。
    dir_path = os.path.dirname(path) or "."
    os.makedirs(dir_path, exist_ok=True)
    
    # 创建临时文件
    # 关键：临时文件必须与目标文件「同目录」，因为 os.replace 只在同一文件系统内保证原子；
    # 若放到系统默认临时目录（可能跨盘），os.replace 会退化为非原子的「复制+删除」。
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            # ensure_ascii=False：让中文（错误信息、输出文案等）以原文落盘，而非 \uXXXX 转义。
            json.dump(data, f, indent=2, ensure_ascii=ensure_ascii)
        # 原子替换
        # 执行到这一行时临时文件已完整写完并关闭，替换瞬间完成，读者不可能看到半成品。
        os.replace(tmp_path, path)
    except Exception:
        # 清理临时文件
        # 任何失败都要删掉临时文件，否则会积累 .tmp 残骸并干扰排查。
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def migrate_status(status: str) -> str:
    """迁移旧状态值到新状态值

    参数：
        status: 原始状态字符串，可能为历史遗留值。

    返回值：
        str：归一化后的状态；未知值原样返回（不抛异常），保证向后兼容且不误伤新值。

    为什么把归一化放在契约层：
        历史状态文件散落在 runtime/ 下，无法一次性重写；统一在读取出口做映射，
        能让新旧值在同一判定口径下工作，避免 WAITING_USER_INPUT 与 WAITING_USER
        被当作两个不同状态而漏判。
    """
    return LEGACY_STATUS_MAP.get(status, status)


def validate_transition(from_status: str, to_status: str) -> bool:
    """校验状态迁移是否合法

    参数：
        from_status: 源状态。
        to_status: 目标状态。

    返回值：
        bool：仅当源状态存在且目标状态在其白名单内时返回 True。

    为什么未知 from_status 直接判为 False：
        白名单同时兼作「合法状态」校验。源状态不在表中，说明它本身就不是契约定义的
        状态，任何迁移都应拒绝，防止伪造状态绕过状态机。
    """
    if from_status not in VALID_TRANSITIONS:
        return False
    return to_status in VALID_TRANSITIONS[from_status]


def is_terminal_state(status: str) -> bool:
    """判断是否为终态

    参数：
        status: 待判断的状态字符串。

    返回值：
        bool：是否属于 TERMINAL_STATES（STOPPED / REJECTED）。

    为什么需要这个便捷判断：
        终态不可恢复是硬约束，多个脚本（如 update_state）都要提前拦截；
        抽出函数可避免各处重复写 `status in {"STOPPED","REJECTED"}` 而造成口径不一。
    """
    return status in TERMINAL_STATES


def is_gate_stage(stage: str) -> bool:
    """判断是否为 Gate 阶段

    参数：
        stage: 阶段名。

    返回值：
        bool：是否属于 GATE_STAGES。

    为什么不能简单用后缀判断：
        Gate 阶段在 pending_output 防重放、resume 恢复校验、报告门禁中都是特殊分支；
        集中判断可保证「哪些阶段是门禁」的口径唯一，避免各脚本各写一套。
    """
    return stage in GATE_STAGES


def get_gate_stage_for_analysis(analysis_stage: str) -> Optional[str]:
    """获取分析阶段对应的 Gate 阶段

    参数：
        analysis_stage: 分析类阶段名（如 SIX_LENS、FIT_ASSESSMENT）。

    返回值：
        Optional[str]：对应的 Gate 阶段名；无对应关系时返回 None。

    为什么用显式映射而非按规律推导：
        分析阶段与 Gate 阶段并非简单加后缀（FIT_ASSESSMENT 对应 FIT_GATE，
        而不是 FIT_ASSESSMENT_GATE），显式映射可读性更强、也不易因改名被牵连出错。
        注意：工作流 B 的 PRD_MVP → PRD_MVP_GATE 未收录在本映射中，由工作流 B 自身逻辑处理。
    """
    mapping = {
        "SIX_LENS": "SIX_LENS_GATE",
        "FIT_ASSESSMENT": "FIT_GATE",
    }
    return mapping.get(analysis_stage)


def validate_stage_progression(state: Dict[str, Any], stage: str) -> tuple[bool, str]:
    """校验阶段是否具备最小前置条件。

    参数：
        state: 完整状态对象，需含 stage_results 字段。
        stage: 准备切换到的目标阶段。

    返回值：
        (bool, str)：校验是否通过；不通过时第二项为中文原因（可直接作为错误信息返回）。

    为什么只校验「直接前置」：
        每个阶段记录自己的完成状态，只查直接前置即可形成链式校验；
        而 INIT 无前置、TEARDOWN 可选无前置，二者未出现在 STAGE_PREDECESSORS 中，
        因此天然通过（取到空列表 → 循环不执行 → 返回 True）。

    为什么接受 COMPLETED 和 CONFIRMED 两种前置状态：
        非 Gate 阶段正常结束落 COMPLETED；Gate 阶段经人工确认后落 CONFIRMED。
        二者都表示「这一步实质已放行」，故均算通过；同时经 migrate_status 归一化，兼容历史值。
    """
    # 可视为「已放行」的前置状态集合。
    accepted = {"COMPLETED", "CONFIRMED"}
    for predecessor in STAGE_PREDECESSORS.get(stage, []):
        # 取前置阶段结果；缺失时用空字典兜底，进而其状态为空串（必然不通过）。
        result = state.get("stage_results", {}).get(predecessor, {})
        if migrate_status(result.get("status", "")) not in accepted:
            return False, f"前置阶段 {predecessor} 未完成"
    return True, ""


def ensure_state_structure(state: Dict[str, Any]) -> Dict[str, Any]:
    """确保状态文件具有完整结构

    参数：
        state: 已解析的状态字典（可能字段缺失）。

    返回值：
        Dict：补全后的同一对象（就地修改并返回，便于链式使用）。

    为什么用「缺则补默认」而不是严格 schema 校验：
        历史状态文件与新字段并存，读取阶段应尽量「可读、可修」而非直接失败；
        缺失字段补上安全默认值后，所有后续判定都能安全访问，避免 KeyError 中断整个工作流。
        hitl 的默认值即「未挂起」：N/A 状态、未确认、无用户决策、无提示、无等待起始时间。
    """
    if "stage_results" not in state:
        state["stage_results"] = {}
    if "hitl" not in state:
        # 默认视为「无人机交互在进行」：状态 N/A、未确认、无用户决策、无提示、无等待起始时间。
        state["hitl"] = {
            "status": "N/A",
            "confirmed": False,
            "user_decision": None,
            "prompt": None,
            "waiting_since": None,
        }
    if "schema_version" not in state:
        state["schema_version"] = WORKFLOW_STATE_SCHEMA_VERSION
    return state


def ensure_ledger_structure(ledger: Dict[str, Any]) -> Dict[str, Any]:
    """确保台账文件具有完整结构

    参数：
        ledger: 已解析的台账字典（可能字段缺失）。

    返回值：
        Dict：补全后的同一对象（就地修改并返回）。

    为什么台账要有固定骨架：
        台账按「证据流水 evidence + 六看汇总 six_lens + 适配度汇总 fit_assessment +
        变更历史 version_history」组织。补齐这些顶层键，能让报告脚本在尚未产出对应内容的
        阶段也安全读取，而不必到处判空；version_history 用于记录证据增删改的版本轨迹。
    """
    if "schema_version" not in ledger:
        ledger["schema_version"] = EVIDENCE_LEDGER_SCHEMA_VERSION
    if "evidence" not in ledger:
        ledger["evidence"] = []
    if "six_lens" not in ledger:
        ledger["six_lens"] = {}
    if "fit_assessment" not in ledger:
        ledger["fit_assessment"] = {}
    if "version_history" not in ledger:
        ledger["version_history"] = []
    return ledger


def validate_evidence(evidence: Dict[str, Any]) -> bool:
    """校验证据基本结构。来源独立性仍需人工核验。

    参数：
        evidence: 单条证据字典。

    返回值：
        bool：结构合法返回 True，否则 False（不抛异常，便于批量过滤）。

    为什么只校验五个必填字段（fact/source/date/grade/signal）：
        这五个字段是证据「可被引用」的最小完整集——没有 fact 无法陈述结论、
        没有 source 无法追溯、没有 date 无法做时效判断、没有 grade 无法分级、
        没有 signal 无法判断正/反向。缺任意一项，该证据在评分与门禁中就无法参与确定性计算；
        其余字段（dimension、corroborating_sources 等）属语义补充，交由 verified_support 进一步把关。

    为什么在这里（而不是在写入前）校验：
        写入由不同脚本/主 Agent 触发，无法保证调用方都做校验；把校验收敛到契约层，
        读取与评分时统一调用，形成单一防线。

    为什么「来源独立性仍需人工核验」：
        本函数只能确认 source 非空、corroborating_sources 是非空字符串列表，
        无法判断多个来源是否真的互相独立（例如同一篇通稿被多家转载）。
        真实性/独立性属于「不确定」范畴，按设计原则交回人工核验。
    """
    if not isinstance(evidence, dict):
        return False
    # 第一组校验：五个必填字段必须是非空字符串。
    required = ("fact", "source", "date", "grade", "signal")
    if any(not isinstance(evidence.get(key), str) or not evidence[key].strip()
           for key in required):
        return False
    # grade 必须在 A/B/C 之内，越界即视为无效分级。
    if evidence["grade"] not in EVIDENCE_GRADES:
        return False
    # signal 只接受正向/反向（中英文两种写法都兼容，便于兼容历史数据）。
    if evidence["signal"] not in ("POSITIVE", "NEGATIVE", "正向", "反向"):
        return False
    # date 必须能被 ISO 解析，否则无法参与后续时效（366/183 天）判断。
    try:
        datetime.fromisoformat(evidence["date"])
    except ValueError:
        return False
    # corroborating_sources 若存在，必须是「非空字符串的列表」，供后续双源判定使用。
    sources = evidence.get("corroborating_sources", [])
    return isinstance(sources, list) and all(isinstance(s, str) and s.strip() for s in sources)


def verified_support(evidence: Dict[str, Any], dimension: str) -> bool:
    """A/B 级支持证据需注明维度与至少两个不同来源；不声称机器已证明独立性。

    参数：
        evidence: 单条证据字典。
        dimension: 该证据声明所属的维度（六看之一）。

    返回值：
        bool：满足「已核实支持」的全部条件才返回 True。

    判定条件（全部为 AND）：
        1. 通过 validate_evidence 的基础结构校验；
        2. grade 属于 A/B（C 级不能作为支持证据）；
        3. signal 为正向（POSITIVE/正向），反向证据不构成支持；
        4. 证据自报的 dimension 与期望维度一致（防止张冠李戴）；
        5. 落在时效窗口内：policy 366 天、其他维度 183 天，且 age_days>=0
           （取 date 前 10 位为「日」精度，避免带时区的时间串比较问题）；
        6. source 与 corroborating_sources 去重后至少 2 个不同来源（双源要求）。

    为什么关键证据要双源：
        六看门禁的放行会直接推动后续评估与提案，单源证据一旦有误，错误会被级联放大；
        双源是「确定性」的最低防线。

    为什么时效分「政策 366 天 / 其他 183 天」：
        政策类信息有效期更长（法规/规划通常按年更新）；市场/用户/竞品类变化更快，
        故收紧到半年，避免用陈年数据支撑当下结论。

    为什么仅靠本函数不足以证明来源独立：
        去重只能消除「字符串完全相同的来源」，无法识别「不同名称但同源」（转载、通稿）。
        因此 docstring 明确「不声称机器已证明独立性」，独立性最终仍需人工核验。
    """
    if not isinstance(evidence, dict):
        return False
    # 汇总全部来源（主来源 + 佐证来源），后续据此做去重计数。
    sources = [evidence.get("source", ""), *evidence.get("corroborating_sources", [])]
    if not validate_evidence(evidence):
        return False
    # 时效计算：只取日期部分（前 10 位）为「日」，以天数差衡量新旧。
    age_days = (date.today() - date.fromisoformat(evidence["date"][:10])).days
    # 政策维度放宽到 366 天，其余维度收紧到 183 天。
    max_age = 366 if dimension == "policy" else 183
    # 六条件全部满足才算「已核实支持」；去重用 strip+lower 归一化，避免大小写/空白造成假双源。
    return (validate_evidence(evidence) and evidence["grade"] in ("A", "B")
            and evidence["signal"] in ("POSITIVE", "正向")
            and evidence.get("dimension") == dimension
            and 0 <= age_days <= max_age
            and len({s.strip().lower() for s in sources if isinstance(s, str) and s.strip()}) >= 2)


def normalize_evidence(evidence: Dict[str, Any], index: int, stage: str) -> Dict[str, Any]:
    """规范化证据字段

    参数：
        evidence: 原始证据字典。
        index: 证据在台账中的序号，用于生成稳定的 id。
        stage: 证据被加入时所在的阶段名。

    返回值：
        Dict：补全 id/stage_added/added_at 后的**副本**（不修改入参）。

    为什么返回副本：主 Agent 可能仍持有原始对象做其他处理，
        就地修改会造成难以排查的副作用；copy 后补字段既安全又可预测。

    为什么 id 用 3 位零填充（evidence_001）：
        固定宽度便于按字符串排序、日志对齐与人工核对，避免「10」与「2」排序倒挂。

    为什么 stage_added/added_at 只在缺失时补：
        这两个字段一旦写入即作为「证据来源阶段与时间」的审计信息，不应被后续规范化覆盖；
        这里只保证「有」而非「重写」，以维持审计可信。
    """
    normalized = evidence.copy()
    if "id" not in normalized:
        normalized["id"] = f"evidence_{index:03d}"
    if "stage_added" not in normalized:
        normalized["stage_added"] = stage
    if "added_at" not in normalized:
        normalized["added_at"] = now_iso()
    return normalized
