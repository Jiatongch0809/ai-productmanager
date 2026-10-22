#!/usr/bin/env python3
"""流程状态管理脚本 — 所有业务线复用

用法：
    OPPORTUNITY_ID=xxx python3 scripts/workflow_state.py read
    OPPORTUNITY_ID=xxx python3 scripts/workflow_state.py update --stage SIX_LENS --status COMPLETED
    python3 scripts/workflow_state.py list-pending
    OPPORTUNITY_ID=xxx python3 scripts/workflow_state.py resume --approve true

环境变量：
    OPPORTUNITY_ID: 机会唯一标识

输出：
    JSON 格式

【本文件在整体机制中的位置】
本脚本是「状态机执行者」，是确定性边界中负责「状态迁移」的那一段。
主 Agent（LLM）是唯一的工作流写入者与调度者，但它不直接改状态文件——
所有状态变更都必须经由本脚本，由本脚本依据 contracts.py 的契约完成：
    - 阶段名/状态名白名单校验；
    - 状态迁移白名单校验（VALID_TRANSITIONS）；
    - 终态不可恢复拦截（TERMINAL_STATES）；
    - 阶段前置条件校验（STAGE_PREDECESSORS）；
    - 原子写入（atomic_write_json）落盘。
这样做的目的，是让「调度意图（LLM）」与「状态真相（脚本）」分离：
主 Agent 只能表达「我想推进到 SIX_LENS 且已完成」，能不能落盘由本脚本确定性判定。

【状态文件位置】
runtime/workflow_state/{opportunity_id}.json

【pending_output 三态与幂等】
Gate 判定结果需要可靠地送达用户渠道（钉钉/session）。为防重放与丢发，
本脚本维护待发送输出的三态推进：
    pending → sending → sent
- delivery_key 是幂等键（同一机会 + 版本 + Gate 阶段 + 动作 + 渠道唯一）；
- 恢复/重试时只补发「未送达」的结果，绝不重放「已送达」的结果。
注意：update_state 在 Gate STOP 分支写入的待发送结构使用的是早期字段（sent 布尔），
    与 list_pending_outputs / mark_output_sent 期望的 status 字段口径不一致，
    详见该分支处的「注意」注释（仅提示，本次交接不改动代码）。

【退出码约定】
    0：成功（打印 JSON 结果）
    1：任何校验失败、文件缺失/损坏、写入失败等（打印 {"error": ...} 并 sys.exit(1)）
统一以非零退出码表达「失败」，便于主 Agent 依据退出码决定下一步，而不必解析文本。
"""
import json
import sys
import os

# 添加 scripts 目录到路径
# 目的：让本脚本无论从哪个工作目录被调用，都能以 `from contracts import ...` 直接导入
# 同目录的契约模块，无需依赖 PYTHONPATH 或把 scripts 打包成 Python 包。
# 注意：此写法要求 contracts.py 与本脚本位于同一目录（scripts/）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contracts import (
    STAGES, STATUSES, GATE_STAGES, VALID_TRANSITIONS, TERMINAL_STATES,
    atomic_write_json, now_iso, ensure_state_structure, migrate_status,
    validate_transition, is_terminal_state, is_gate_stage,
    validate_stage_progression
)

# 状态文件目录：每个机会一个 {opportunity_id}.json。
STATE_DIR = "runtime/workflow_state"
# 日志目录：按机会追加写 {opportunity_id}.log，记录校验/IO 失败原因，供事后审计。
LOG_DIR = "runtime/logs"


def log_error(opportunity_id: str, error: str):
    """记录错误日志

    参数：
        opportunity_id: 机会 ID，用作日志文件名。
        error: 错误描述文本。

    为什么既打印 JSON 又写日志文件：
        打印 JSON 是给调用方（主 Agent）的结构化反馈，便于程序判定；
        写文件是给运维/交接人员的事后审计，二者受众不同、互不替代。
        日志按机会分文件（append 模式），便于定位「某一个机会」的完整失败轨迹。
    为什么用 now_iso 统一时间戳：与契约层时间口径一致，避免日志与状态文件时间不可比。
    """
    # 日志目录可能尚未创建，先兜底；append 模式保证多次失败逐条累积不覆盖。
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{opportunity_id}.log")
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"[{now_iso()}] ERROR workflow_state: {error}\n")


def read_state(opportunity_id):
    """读取状态文件并以 JSON 打印

    参数：
        opportunity_id: 机会 ID。

    退出码：
        1：状态文件不存在或 JSON 损坏（均打印 {"error": ...}）；成功则打印状态对象后返回。

    为什么读取时也要补结构、迁旧值（本次只在内存补全，不写回文件）：
        read 是主 Agent 获取「当前真相」的入口。若返回对象缺字段，调用方就得到处判空；
        若返回旧状态值，调用方判定口径又会与写入口不一致。
        因此在出口处统一 ensure_state_structure + migrate_status，
        让所有调用方拿到的状态结构一致、状态值口径统一。
    """
    path = os.path.join(STATE_DIR, f"{opportunity_id}.json")
    # 文件缺失是最常见的「机会不存在」情形，直接以退出码 1 明确失败。
    if not os.path.exists(path):
        print(json.dumps({"error": f"状态文件不存在: {opportunity_id}"}, ensure_ascii=False))
        sys.exit(1)
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        # JSON 损坏无法继续，记日志并明确失败，避免调用方拿到残缺对象。
        log_error(opportunity_id, f"状态文件 JSON 解析失败: {e}")
        print(json.dumps({"error": f"状态文件损坏: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 确保结构完整
    state = ensure_state_structure(state)
    
    # 迁移旧状态值
    # 顶层状态与各阶段状态都要迁移，否则会出现「顶层已是 WAITING_USER、
    # 阶段里仍是 WAITING_USER_INPUT」的双口径，导致报告门禁判定漏判。
    state["status"] = migrate_status(state.get("status", "PENDING"))
    for stage_key, stage_data in state.get("stage_results", {}).items():
        if "status" in stage_data:
            stage_data["status"] = migrate_status(stage_data["status"])
    
    print(json.dumps(state, ensure_ascii=False))


def update_state(opportunity_id, stage, status, output=None, reason=None):
    """更新状态文件（状态机的写入入口）
    
    包含状态迁移校验：
    - 禁止从终态（STOPPED/REJECTED）直接恢复为 COMPLETED
    - 必须按合法路径迁移

    参数：
        opportunity_id: 机会 ID。
        stage: 目标阶段名（大小写不敏感，内部会 upper 归一）。
        status: 目标状态名（大小写不敏感，内部会 upper 归一）。
        output: 可选，阶段产物 / Gate 输出；Gate STOP 时会作为待发送结果保存。
        reason: 可选，结果说明文本（仅透传回执，不写入状态文件）。

    退出码：
        1：阶段名/状态名非法、新文件非 INIT 起点、非法状态迁移、从终态恢复、
           阶段前置未满足、写入失败。0：成功并打印更新回执。

    为什么所有写入都必须走这里（不接受别处直接改状态文件）：
        状态是调度的唯一真相，集中的写入入口才能保证：
        (1) 每个变更都经过白名单与前置校验；
        (2) 每个变更都原子落盘（不会留下半截文件）；
        (3) hitl / pending_output 等派生字段随主状态同步，不会各自漂移。
    """
    path = os.path.join(STATE_DIR, f"{opportunity_id}.json")
    
    # 校验阶段名
    # 为什么先归一化再校验：命令行的 --stage 可能大小写混写，
    # 统一 upper 后与契约常量比对，避免大小写造成误判。
    stage = stage.upper()
    if stage not in STAGES.values() and not is_gate_stage(stage):
        print(json.dumps({"error": f"无效的阶段名: {stage}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 校验状态名
    status = status.upper()
    if status not in STATUSES.values():
        print(json.dumps({"error": f"无效的状态名: {status}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 如果文件不存在，创建新文件
    if not os.path.exists(path):
        # 新工作流只能从 INIT 起步：禁止凭空把某个中间阶段设成已完成——
        # 否则可以绕过前置依赖伪造进度，状态机的效力会被架空。
        if stage != STAGES["INIT"]:
            print(json.dumps({"error": "新的工作流必须从 INIT 阶段开始"}, ensure_ascii=False))
            sys.exit(1)
        # 新文件状态只能是 PENDING（待运行）或 RUNNING（已开始），
        # 不允许「新建即 COMPLETED」等跳步状态。
        if status not in ["PENDING", "RUNNING"]:
            print(json.dumps({"error": f"新状态文件只能以 PENDING/RUNNING 开始，当前: {status}"}, ensure_ascii=False))
            sys.exit(1)
        
        # 初始骨架：字段与 ensure_state_structure 的默认结构保持一致；
        # schema_version 固定为 1，hitl 初值为「未挂起」。
        state = {
            "schema_version": 1,
            "opportunity_id": opportunity_id,
            "workflow_name": "opportunity-evaluation",
            "current_stage": stage,
            "status": status,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "stage_results": {},
            "hitl": {
                "status": "N/A",
                "confirmed": False,
                "user_decision": None,
                "prompt": None,
                "waiting_since": None,
            }
        }
    else:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except json.JSONDecodeError as e:
            log_error(opportunity_id, f"状态文件 JSON 解析失败: {e}")
            print(json.dumps({"error": f"状态文件损坏: {e}"}, ensure_ascii=False))
            sys.exit(1)
        
        # 确保结构完整
        state = ensure_state_structure(state)
        
        # 迁移旧状态值
        old_status = migrate_status(state.get("status", "PENDING"))
        
        # 状态迁移校验
        # 只在「状态发生变化」时校验：同状态重复写入（例如重复标记完成）应幂等放行，
        # 否则重试/补写会被误判为非法迁移而失败。
        if old_status != status:
            # 禁止从终态恢复
            # 为什么在 VALID_TRANSITIONS 之外再拦一道：这里能给出更明确的中文错误，
            # 并且只允许终态回到 PENDING（等价于显式重置），其余目标状态一律拒绝，
            # 防止「误用普通迁移路径复活终态」。
            if is_terminal_state(old_status) and status not in ["PENDING"]:
                error_msg = f"禁止从终态 {old_status} 迁移到 {status}"
                log_error(opportunity_id, error_msg)
                print(json.dumps({"error": error_msg}, ensure_ascii=False))
                sys.exit(1)
            
            # 校验迁移路径
            if not validate_transition(old_status, status):
                error_msg = f"非法状态迁移: {old_status} → {status}"
                log_error(opportunity_id, error_msg)
                print(json.dumps({"error": error_msg}, ensure_ascii=False))
                sys.exit(1)

    # 阶段切换必须满足前置条件；同一阶段的状态更新不重复校验。
    # 为什么只在「新旧阶段不同」时校验前置：同一阶段内的状态反复变化（RUNNING→COMPLETED）
    # 不应重复检查前置阶段，避免把「同阶段重跑」误判为非法推进。
    if state.get("current_stage") != stage:
        valid_stage, stage_reason = validate_stage_progression(state, stage)
        if not valid_stage:
            print(json.dumps({"error": stage_reason}, ensure_ascii=False))
            sys.exit(1)
    
    # 更新状态
    state["current_stage"] = stage
    state["status"] = status
    state["updated_at"] = now_iso()
    
    # 更新阶段结果
    # 阶段结果按阶段名建索引，记录该阶段的状态与产物，供前置校验与报告读取。
    if stage not in state["stage_results"]:
        state["stage_results"][stage] = {}
    
    state["stage_results"][stage]["status"] = status
    if output:
        # output 仅在非空时写入，避免用 None 覆盖该阶段已有的产物。
        state["stage_results"][stage]["output"] = output
    # 注意：无论目标状态是否为 COMPLETED，这里都会写 completed_at。
    # 对 RUNNING / WAITING_USER 等中间状态而言，completed_at 的语义并不准确
    # （疑似与字段名不符）；按交接要求不改代码，仅在此标注。
    state["stage_results"][stage]["completed_at"] = now_iso()
    
    # 更新 HITL 状态
    # 为什么要把 hitl 与主状态同步：报告门禁与 list-pending 都会读取 hitl；
    # 若只改顶层 status 而不改 hitl，会出现「顶层说等待、hitl 说没等待」的分裂，
    # 从而使等待中的任务在 list-pending 里漏报。
    if status == STATUSES["WAITING_USER"]:
        state["hitl"]["status"] = "WAITING"
        state["hitl"]["confirmed"] = False
        state["hitl"]["waiting_since"] = now_iso()
        state["hitl"]["user_decision"] = None
        # 把 Gate 询问理由作为给用户的提示语（若 output 中带有 reason）。
        if output and isinstance(output, dict) and "reason" in output:
            state["hitl"]["prompt"] = output["reason"]
    elif status == STATUSES["CONFIRMED"]:
        state["hitl"]["status"] = "CONFIRMED"
        state["hitl"]["confirmed"] = True
        state["hitl"]["user_decision"] = "continue"
    elif status == STATUSES["REJECTED"]:
        state["hitl"]["status"] = "REJECTED"
        state["hitl"]["confirmed"] = False
        state["hitl"]["user_decision"] = "stop"
    
    # Gate STOP 时记录待发送结果
    # 目的：Gate 判定出的终止结果（可能含结论文本）需要可靠送达渠道，
    # 先落到 pending_output，再由发送方按三态（pending→sending→sent）补发，
    # 避免「脚本已判 Gate 但用户没收到」的悬空。
    # 注意：此处写入的是早期结构（sent 布尔字段，无 status/delivery_key/action/ledger_version），
    # 而 list_pending_outputs / mark_output_sent / mark_output_sending 期望的是 status 三态字段。
    # 因 status 为 None，STOP 产生的记录不会被 list_pending_outputs 识别到，
    # 可能导致这类待发送结果无法进入补发流程（疑似缺陷；按要求不改代码，仅标注）。
    if is_gate_stage(stage) and status == STATUSES["STOPPED"] and output:
        state["pending_output"] = {
            "gate_stage": stage,
            "output": output,
            "created_at": now_iso(),
            "sent": False
        }
    
    # 原子写入
    # 失败即明确退出码 1，绝不留半截文件；成功后才打印回执。
    try:
        atomic_write_json(path, state)
    except Exception as e:
        log_error(opportunity_id, f"状态写入失败: {e}")
        print(json.dumps({"error": f"状态写入失败: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 成功回执：回带 stage/current_status 便于调用方确认落盘结果。
    print(json.dumps({
        "status": "updated",
        "stage": stage,
        "current_status": status,
        "reason": reason
    }, ensure_ascii=False))


def list_pending():
    """获取所有等待审批的任务
    
    统一检查 WAITING_USER 状态，兼容旧值 WAITING_USER_INPUT。

    为什么同时看顶层 status 与 hitl.status：
        历史数据里「等待」可能只体现在 hitl.status（WAITING），也可能体现在顶层
        status（WAITING_USER）。两者取「或」，可避免漏报等待中的任务。
    为什么损坏文件直接跳过：本函数是只读勘查，容错优先——
        一个坏文件不应拖垮整份待办列表，导致其他等待任务也看不到。
    """
    # 目录不存在视为「暂无任何机会」，返回空列表而非报错，降低调用方处理负担。
    if not os.path.exists(STATE_DIR):
        print(json.dumps([], ensure_ascii=False))
        return
    
    pending = []
    for filename in os.listdir(STATE_DIR):
        if filename.endswith(".json"):
            path = os.path.join(STATE_DIR, filename)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
            except json.JSONDecodeError:
                # 单个文件损坏不影响整体列表，跳过该文件继续遍历。
                continue
            
            # 确保结构完整
            state = ensure_state_structure(state)
            
            # 统一检查 WAITING_USER（兼容旧值）
            current_status = migrate_status(state.get("status", ""))
            hitl_status = migrate_status(state.get("hitl", {}).get("status", ""))
            
            if current_status == STATUSES["WAITING_USER"] or hitl_status == "WAITING":
                # 输出字段面向「审批列表」：机会 ID、当前阶段、等待理由、等待起始时间。
                # gate_stage 直接复用 current_stage，因为等待只发生在 Gate 阶段。
                pending.append({
                    "opportunity_id": state.get("opportunity_id", filename.replace(".json", "")),
                    "current_stage": state.get("current_stage", ""),
                    "reason": state.get("hitl", {}).get("prompt", ""),
                    "waiting_since": state.get("hitl", {}).get("waiting_since", ""),
                    "gate_stage": state.get("current_stage", "")
                })
    
    print(json.dumps(pending, ensure_ascii=False))


def list_pending_outputs():
    """获取所有待发送的 Gate 结果（status=pending 或 sending）
    
    返回格式：
    [
        {
            "opportunity_id": "xxx",
            "delivery_key": "xxx:v1:SIX_LENS_GATE:WAIT_USER:default",
            "status": "pending|sending",
            "gate_stage": "SIX_LENS_GATE",
            "action": "WAIT_USER",
            "output": {...},
            "ledger_version": 1,
            "created_at": "...",
            "attempt": 1,  # 仅 sending 状态
            "started_at": "..."  # 仅 sending 状态
        }
    ]

    为什么只返回 pending / sending：
        sent 表示已送达，重试/恢复时不得重放（幂等键 delivery_key 汇总了
        「机会 + 版本 + Gate 阶段 + 动作 + 渠道」，确保同一结果只发一次）。
        因此列表只暴露「待处理」的记录，由发送方负责把 pending 推进为 sending、再推进为 sent。
    为什么 sending 额外带 attempt / started_at：
        用于识别「发送中断/卡死」的记录，便于恢复时判断是否需要重新尝试。
    """
    if not os.path.exists(STATE_DIR):
        print(json.dumps([], ensure_ascii=False))
        return
    
    pending = []
    for filename in os.listdir(STATE_DIR):
        if filename.endswith(".json"):
            path = os.path.join(STATE_DIR, filename)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
            except json.JSONDecodeError:
                # 与 list_pending 一致：坏文件跳过，不影响整体列表。
                continue
            
            state = ensure_state_structure(state)
            pending_output = state.get("pending_output", {})
            
            # 只返回 pending 或 sending 状态的记录
            # 注意：此处依赖 status 字段；而 update_state 在 Gate STOP 分支写入的是
            # 早期结构（sent 布尔，无 status/delivery_key/action/ledger_version），
            # 其 status 为 None 不会命中本条件，故那类记录不会被本列表识别
            # （疑似缺陷，仅标注不改代码）。
            if pending_output and pending_output.get("status") in ["pending", "sending"]:
                item = {
                    "opportunity_id": state.get("opportunity_id", filename.replace(".json", "")),
                    "delivery_key": pending_output.get("delivery_key", ""),
                    "status": pending_output.get("status", "pending"),
                    "gate_stage": pending_output.get("gate_stage", ""),
                    "action": pending_output.get("action", ""),
                    "output": pending_output.get("output", {}),
                    "ledger_version": pending_output.get("ledger_version"),
                    "created_at": pending_output.get("created_at", "")
                }
                # sending 状态额外返回 attempt 和 started_at
                if pending_output.get("status") == "sending":
                    item["attempt"] = pending_output.get("attempt", 1)
                    item["started_at"] = pending_output.get("started_at", "")
                pending.append(item)
    
    print(json.dumps(pending, ensure_ascii=False))


def mark_output_sent(opportunity_id, receipt=None):
    """标记 pending_output 为已发送
    
    参数：
        opportunity_id: 机会 ID
        receipt: 渠道回执（钉钉 openMessageId 或 session message id）

    退出码：
        1：状态文件不存在/损坏、无 pending_output、写入失败。0：标记成功或已发送（幂等）。

    为什么必须幂等：发送流程常因超时重试，同一结果可能被标记多次；
        若已是 sent，直接返回 already_sent（不覆盖 sent_at/receipt），
        保证「已送达」这一事实不被重放路径改写。
    为什么记录 receipt：回执是「用户确实收到」的渠道凭证，
        是对账/排查丢发时的关键证据，因此与 sent_at 一并落盘。
    """
    path = os.path.join(STATE_DIR, f"{opportunity_id}.json")
    if not os.path.exists(path):
        print(json.dumps({"error": f"状态文件不存在: {opportunity_id}"}, ensure_ascii=False))
        sys.exit(1)
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        log_error(opportunity_id, f"状态文件 JSON 解析失败: {e}")
        print(json.dumps({"error": f"状态文件损坏: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    state = ensure_state_structure(state)
    
    # 没有待发送输出就报错，避免标记一个不存在的对象。
    if not state.get("pending_output"):
        print(json.dumps({"error": f"机会 {opportunity_id} 没有待发送的输出"}, ensure_ascii=False))
        sys.exit(1)
    
    # 已经是 sent 状态，直接返回
    # 幂等出口：不修改任何字段，只回读结果，避免重复标记改写送达事实。
    if state["pending_output"].get("status") == "sent":
        print(json.dumps({
            "status": "already_sent",
            "opportunity_id": opportunity_id,
            "sent_at": state["pending_output"].get("sent_at"),
            "receipt": state["pending_output"].get("receipt")
        }, ensure_ascii=False))
        return
    
    state["pending_output"]["status"] = "sent"
    state["pending_output"]["sent_at"] = now_iso()
    # receipt 可选：发送方可能拿不到回执，此时不写该字段。
    if receipt:
        state["pending_output"]["receipt"] = receipt
    state["updated_at"] = now_iso()
    
    try:
        atomic_write_json(path, state)
    except Exception as e:
        log_error(opportunity_id, f"状态写入失败: {e}")
        print(json.dumps({"error": f"状态写入失败: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    print(json.dumps({
        "status": "marked_sent",
        "opportunity_id": opportunity_id,
        "sent_at": state["pending_output"]["sent_at"],
        "receipt": state["pending_output"].get("receipt")
    }, ensure_ascii=False))


def mark_output_sending(opportunity_id):
    """标记 pending_output 为发送中
    
    用于防止并发发送，记录 attempt 和 started_at

    退出码：
        1：状态文件不存在/损坏、无 pending_output、写入失败。0：标记成功或已发送（幂等）。

    为什么需要 sending 中间态（而不是 pending 直接到 sent）：
        发送是「外部渠道」的副作用，可能超时/失败。若直接 pending→sent，
        一旦发送中断，外部可能已发出而本地仍显示 pending，恢复时会误重发（用户收到两条）。
        先置 sending 并记录 attempt/started_at：
        - 表明「有一次发送正在进行」，可据此判定是否卡死、是否需要人工介入；
        - attempt 自增便于追踪重试次数，而不是无限重发。
    """
    path = os.path.join(STATE_DIR, f"{opportunity_id}.json")
    if not os.path.exists(path):
        print(json.dumps({"error": f"状态文件不存在: {opportunity_id}"}, ensure_ascii=False))
        sys.exit(1)
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        log_error(opportunity_id, f"状态文件 JSON 解析失败: {e}")
        print(json.dumps({"error": f"状态文件损坏: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    state = ensure_state_structure(state)
    
    if not state.get("pending_output"):
        print(json.dumps({"error": f"机会 {opportunity_id} 没有待发送的输出"}, ensure_ascii=False))
        sys.exit(1)
    
    pending = state["pending_output"]
    
    # 已经是 sent 状态，不再发送
    # 幂等出口：已送达的结果绝不允许被重新置为 sending，防止重放。
    if pending.get("status") == "sent":
        print(json.dumps({
            "status": "already_sent",
            "opportunity_id": opportunity_id,
            "sent_at": pending.get("sent_at"),
            "receipt": pending.get("receipt")
        }, ensure_ascii=False))
        return
    
    # 更新 attempt 计数
    # 首次发送时无 attempt 字段，默认 0 再加 1 → 1，语义即「第 1 次尝试」。
    current_attempt = pending.get("attempt", 0) + 1
    
    pending["status"] = "sending"
    pending["attempt"] = current_attempt
    pending["started_at"] = now_iso()
    state["updated_at"] = now_iso()
    
    try:
        atomic_write_json(path, state)
    except Exception as e:
        log_error(opportunity_id, f"状态写入失败: {e}")
        print(json.dumps({"error": f"状态写入失败: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    print(json.dumps({
        "status": "marked_sending",
        "opportunity_id": opportunity_id,
        "attempt": current_attempt,
        "started_at": pending["started_at"]
    }, ensure_ascii=False))


def resume_workflow(opportunity_id, approve=True, user_reason=None):
    """恢复工作流（人工确认 Gate 后推进）
    
    校验：
    - 当前阶段必须是 Gate 阶段
    - 当前状态必须是 WAITING_USER
    - 用户确认后只能进入 CONFIRMED，不能直接伪造后续阶段为 COMPLETED

    参数：
        opportunity_id: 机会 ID。
        approve: True 表示用户确认（落 CONFIRMED），False 表示用户驳回（落 REJECTED）。
        user_reason: 可选，用户给出的理由，写入 hitl 与阶段结果供审计。

    退出码：
        1：文件不存在/损坏、当前状态非 WAITING_USER、当前阶段非 Gate、写入失败。0：成功。

    为什么只能进 CONFIRMED/REJECTED，不能直接写下一阶段的 COMPLETED：
        人工确认表达的是「同意放行这一步」，而不是「后续阶段已做完」。
        后续阶段仍须由主 Agent 实际执行并经 update_state 合法推进；
        否则可绕过执行直接伪造进度，Gate 就形同虚设。
    """
    path = os.path.join(STATE_DIR, f"{opportunity_id}.json")
    if not os.path.exists(path):
        print(json.dumps({"error": f"状态文件不存在: {opportunity_id}"}, ensure_ascii=False))
        sys.exit(1)
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        log_error(opportunity_id, f"状态文件 JSON 解析失败: {e}")
        print(json.dumps({"error": f"状态文件损坏: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 确保结构完整
    state = ensure_state_structure(state)
    
    # 迁移旧状态值
    current_status = migrate_status(state.get("status", ""))
    
    # 校验当前状态
    # 只有真正处于等待的任务才能被恢复，防止对运行中/已结束的机会误操作。
    if current_status != STATUSES["WAITING_USER"]:
        error_msg = f"机会 {opportunity_id} 不在等待状态，当前: {current_status}"
        log_error(opportunity_id, error_msg)
        print(json.dumps({"error": error_msg}, ensure_ascii=False))
        sys.exit(1)
    
    # 校验当前阶段是 Gate 阶段
    # 恢复只对 Gate 的人工确认有意义；非 Gate 阶段的「等待」不应走这条路径。
    current_stage = state.get("current_stage", "")
    if not is_gate_stage(current_stage):
        error_msg = f"当前阶段 {current_stage} 不是 Gate 阶段，无法恢复"
        log_error(opportunity_id, error_msg)
        print(json.dumps({"error": error_msg}, ensure_ascii=False))
        sys.exit(1)
    
    # 更新状态
    # approve 与 reject 两个分支都同时更新顶层 status 与 hitl，保持二者口径一致。
    if approve:
        state["status"] = STATUSES["CONFIRMED"]
        state["hitl"]["status"] = "CONFIRMED"
        state["hitl"]["confirmed"] = True
        state["hitl"]["user_decision"] = "continue"
        decision_status = STATUSES["CONFIRMED"]
    else:
        state["status"] = STATUSES["REJECTED"]
        state["hitl"]["status"] = "REJECTED"
        state["hitl"]["confirmed"] = False
        state["hitl"]["user_decision"] = "stop"
        decision_status = STATUSES["REJECTED"]
    
    if user_reason:
        state["hitl"]["user_reason"] = user_reason

    # 顶层状态和 Gate 阶段状态必须同步，否则报告门禁无法识别确认结果。
    # 只在阶段结果已存在时同步（新文件或该阶段尚未落结果时跳过），避免凭空造出阶段记录。
    if current_stage in state.get("stage_results", {}):
        state["stage_results"][current_stage]["status"] = decision_status
        state["stage_results"][current_stage]["user_decision_at"] = now_iso()
        if user_reason:
            state["stage_results"][current_stage]["user_reason"] = user_reason
    
    state["updated_at"] = now_iso()
    
    # 原子写入
    try:
        atomic_write_json(path, state)
    except Exception as e:
        log_error(opportunity_id, f"状态写入失败: {e}")
        print(json.dumps({"error": f"状态写入失败: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 回执状态字符串根据 approve 决定为 confirmed / rejected。
    result_status = "confirmed" if approve else "rejected"
    print(json.dumps({
        "status": result_status,
        "opportunity_id": opportunity_id,
        "gate_stage": current_stage,
        "user_reason": user_reason
    }, ensure_ascii=False))


if __name__ == '__main__':
    # 无参数直接报错并给出用法，避免无操作空跑。
    if len(sys.argv) < 2:
        # 注意：此用法串列出了 mark-output-sent，却未列出已实现的 mark-output-sending，
        # 属文档与实际分支不一致（仅标注，不改动字符串）。
        print(json.dumps({"error": "用法: workflow_state.py <read|update|list-pending|list-pending-outputs|mark-output-sent|resume> [args]"}, ensure_ascii=False))
        sys.exit(1)
    
    # action 是第一个位置参数，决定走哪个分支。
    action = sys.argv[1]
    
    if action == "read":
        # read 等「按机会」操作的动作都要求 OPPORTUNITY_ID 环境变量。
        opportunity_id = os.environ.get("OPPORTUNITY_ID")
        if not opportunity_id:
            print(json.dumps({"error": "缺少环境变量 OPPORTUNITY_ID"}, ensure_ascii=False))
            sys.exit(1)
        read_state(opportunity_id)
    
    elif action == "update":
        opportunity_id = os.environ.get("OPPORTUNITY_ID")
        if not opportunity_id:
            print(json.dumps({"error": "缺少环境变量 OPPORTUNITY_ID"}, ensure_ascii=False))
            sys.exit(1)
        
        # 解析命令行参数
        # 采用手写 while 遍历而非 argparse，是为了兼容主 Agent 拼接的「不定序、可缺省」
        # 参数（args 顺序不固定、部分参数可省），并保持极简依赖。
        stage = None
        status = None
        output = None
        reason = None
        
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--stage" and i + 1 < len(sys.argv):
                # 识别到选项后取值，指针跳 2（选项 + 值）。
                stage = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--status" and i + 1 < len(sys.argv):
                status = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--reason" and i + 1 < len(sys.argv):
                reason = sys.argv[i + 1]
                i += 2
            else:
                # 无法识别的参数（含缺少值的选项）跳过，不中断解析。
                i += 1
        
        # stage 与 status 是必需项，缺任一即无法确定目标状态。
        if not stage or not status:
            print(json.dumps({"error": "缺少 --stage 或 --status 参数"}, ensure_ascii=False))
            sys.exit(1)
        
        # 从 stdin 读取 output（如果有）
        # 仅在非交互式（管道）输入时尝试解析 JSON；
        # 解析失败则静默忽略（pass），让 output 保持 None，不因管道内容异常而中断更新。
        if not sys.stdin.isatty():
            try:
                output = json.load(sys.stdin)
            except:
                pass
        
        update_state(opportunity_id, stage, status, output, reason)
    
    elif action == "list-pending":
        # 只读列表，不需要 OPPORTUNITY_ID。
        list_pending()
    
    elif action == "list-pending-outputs":
        # 只读列表，不需要 OPPORTUNITY_ID。
        list_pending_outputs()
    
    elif action == "mark-output-sent":
        opportunity_id = os.environ.get("OPPORTUNITY_ID")
        if not opportunity_id:
            print(json.dumps({"error": "缺少环境变量 OPPORTUNITY_ID"}, ensure_ascii=False))
            sys.exit(1)
        
        # 解析 --receipt 参数
        # 与 update 相同的极简解析方式；receipt 可选（拿不到回执时不传）。
        receipt = None
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--receipt" and i + 1 < len(sys.argv):
                receipt = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        
        mark_output_sent(opportunity_id, receipt)
    
    elif action == "mark-output-sending":
        # 注意：该动作已实现，但未出现在上方 usage 提示串中（见该处的「注意」标注）。
        opportunity_id = os.environ.get("OPPORTUNITY_ID")
        if not opportunity_id:
            print(json.dumps({"error": "缺少环境变量 OPPORTUNITY_ID"}, ensure_ascii=False))
            sys.exit(1)
        mark_output_sending(opportunity_id)
    
    elif action == "resume":
        opportunity_id = os.environ.get("OPPORTUNITY_ID")
        if not opportunity_id:
            print(json.dumps({"error": "缺少环境变量 OPPORTUNITY_ID"}, ensure_ascii=False))
            sys.exit(1)
        
        # 解析参数
        # approve 默认 True（缺省即视为同意），仅当显式传入非 "true"（不区分大小写）时才视为驳回。
        approve = True
        user_reason = None
        
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--approve" and i + 1 < len(sys.argv):
                approve = sys.argv[i + 1].lower() == "true"
                i += 2
            elif sys.argv[i] == "--reason" and i + 1 < len(sys.argv):
                user_reason = sys.argv[i + 1]
                i += 2
            else:
                i += 1
        
        resume_workflow(opportunity_id, approve, user_reason)
    
    else:
        # 未知动作统一失败，便于调用方区分「参数错误」与「无操作」。
        print(json.dumps({"error": f"未知操作: {action}"}, ensure_ascii=False))
        sys.exit(1)
