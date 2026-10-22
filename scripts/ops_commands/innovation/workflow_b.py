#!/usr/bin/env python3
"""工作流 B 命令封装

命令：
    innovation:workflow-b start --id ID [--skip-demand-insight]
    innovation:workflow-b demand-insight --id ID
    innovation:workflow-b bp --id ID
    innovation:workflow-b prd --id ID
    innovation:workflow-b demo --id ID
    innovation:workflow-b rollback --id ID --target STAGE
    innovation:workflow-b status --id ID

本文件在整体机制里的位置（交接重点）：
    innovation 命名空间下的“工作流 B”命令层，对应工作流 B 的阶段链：
    DEMAND_INSIGHT → BP_GENERATE → PRD_MVP → PRD_MVP_GATE → MODAO_DEMO。
    与工作流 A 不同的是，工作流 B 的状态由一个独立文件持久化
    （runtime/evidence_ledger/<id>/workflow_b_state.json），而不是靠底层子进程脚本，
    因此本文件直接读写该 JSON（用 contracts 的原子写），更像“状态机 + 命令封装”的混合体。

阶段语义与可选性：
    - DEMAND_INSIGHT：需求洞察，start 时可用 --skip-demand-insight 跳过；
    - BP_GENERATE   ：BP 生成，可选，支持 SKIPPED 状态；完成后自动进入 PRD；
    - PRD_MVP       ：PRD 定义，完成后自动进入 HITL Gate（PRD_MVP_GATE 置 WAITING_USER）；
    - PRD_MVP_GATE  ：人工确认闸门（HITL），confirm=false 会回退到 PRD_MVP 重做；
    - MODAO_DEMO    ：墨刀 Demo，可选，支持 SKIPPED；完成后工作流 B 整体 COMPLETED。

为什么把状态集中在一个 JSON 文件里直接读写：
    - 工作流 B 是线性的、“单机会一份状态”，无需像工作流 A 那样与证据台账/评分脚本
      深度耦合，状态自持更简单；
    - 每个阶段命令只更新对应阶段的 status/artifacts/completed_at，并推进 current_stage；
    - rollback 会记录 rollback_history 并把目标及后续阶段重置，实现可追溯的回退。

退出码约定：
    本文件不使用 sys.exit(result.returncode) 那套透传模式，而是按业务校验直接
    sys.exit(1)（如未 start、Gate 未过、artifacts 非法 JSON），成功则正常结束（0）。
"""
import argparse     # 各子命令参数解析
import json         # 读写状态文件、解析 artifacts
import os           # 路径拼接、目录创建、存在性判断
import sys          # 控制退出码、写 stderr

# 上溯三层（innovation -> ops_commands -> scripts）加入 sys.path，便于 import contracts
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, SCRIPTS_DIR)

from contracts import (
    WORKFLOW_B_STAGES,
    WORKFLOW_B_STAGE_ORDER,
    STATUSES,
    now_iso,
    atomic_write_json,
)


def get_workflow_b_state_path(opportunity_id: str) -> str:
    """获取工作流 B 状态文件路径。

    参数：
        opportunity_id —— 机会 ID。

    返回值：
        str —— scripts/../runtime/evidence_ledger/<id>/workflow_b_state.json
               （即在工作区根目录的 runtime/evidence_ledger/<id>/ 下）。

    为什么这么设计：
        用「scripts/.. 相对定位」而不是硬编码绝对路径，保证工作区整体搬迁后仍可用；
        每个机会一个独立子目录，天然隔离不同机会的状态。
    """
    return os.path.join(
        SCRIPTS_DIR,
        '..',
        'runtime',
        'evidence_ledger',
        opportunity_id,
        'workflow_b_state.json'
    )


def load_workflow_b_state(opportunity_id: str) -> dict:
    """加载工作流 B 状态（不存在则返回一份全新的初始状态骨架，不落盘）。

    参数：
        opportunity_id —— 机会 ID。

    返回值：
        dict —— 已存在则返回读到的状态；不存在则返回初始骨架：
                started_at=None、五个阶段全 PENDING、current_stage=None、rollback_history=[]。

    异常与退出码：
        文件存在但 JSON 损坏 -> 直接抛 json 异常（未捕获，进程以非零码崩溃）。

    为什么这么设计：
        - 用“不存在就返回初始骨架”的读法，使各阶段命令不必先判断文件是否存在；
        - 初始骨架把五个阶段（含可选阶段的 skipped 标记、Gate 的 user_confirmed）都预置好，
          后续命令只改字段、不新建结构，避免结构漂移；
        - 真正落盘发生在 save_workflow_b_state，读操作本身不产生副作用。
    """
    state_path = get_workflow_b_state_path(opportunity_id)
    if not os.path.exists(state_path):
        # 返回内存初值；调用方在 cmd_* 里可能据此判定“未启动”
        return {
            'opportunity_id': opportunity_id,
            'workflow': 'B',
            'started_at': None,
            'stages': {
                'DEMAND_INSIGHT': {'status': 'PENDING', 'completed_at': None, 'artifacts': []},
                'BP_GENERATE': {'status': 'PENDING', 'completed_at': None, 'artifacts': [], 'skipped': False},
                'PRD_MVP': {'status': 'PENDING', 'completed_at': None, 'artifacts': []},
                'PRD_MVP_GATE': {'status': 'PENDING', 'completed_at': None, 'user_confirmed': False},
                'MODAO_DEMO': {'status': 'PENDING', 'completed_at': None, 'artifacts': [], 'skipped': False},
            },
            'current_stage': None,
            'rollback_history': [],
        }
    with open(state_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_workflow_b_state(opportunity_id: str, state: dict):
    """保存工作流 B 状态（先确保目录存在，再原子写入）。

    参数：
        opportunity_id —— 机会 ID；
        state          —— 待保存的状态字典。

    返回值：无。

    为什么这么设计：
        - 先 makedirs 保证 <id>/ 目录存在（首次启动时该目录可能还没有）；
        - 用 atomic_write_json 原子写，避免写一半崩溃导致状态文件损坏（状态是工作流 B 的
          唯一事实源，损坏会让后续命令全乱）。
    """
    state_path = get_workflow_b_state_path(opportunity_id)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    atomic_write_json(state_path, state)


def cmd_start(args):
    """启动工作流 B（对应命令 innovation:workflow-b start）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id                  : 机会 ID（必填）；
                    --skip-demand-insight : 布尔开关，置为真则跳过需求洞察阶段。

    返回值：无（输出 {"success":True,...,"current_stage":...}）。

    异常与退出码：
        工作流 B 已启动（started_at 非空）-> 打印 ERROR 后 sys.exit(1)。

    为什么这么设计：
        - 幂等/防重：已启动则拒绝再次 start，避免覆盖已有进度；
        - 跳过需求洞察时把该阶段直接置 SKIPPED 并写 completed_at，同时把 current_stage
          推到 BP_GENERATE，保持“阶段状态”与“指针位置”一致；
        - 用 now_iso() 打启动时间戳，作为“是否已启动”的判定依据与审计信息。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:workflow-b start',
        description='启动工作流 B（执行阶段）'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--skip-demand-insight', action='store_true',
                        help='跳过需求洞察阶段')
    
    parsed = parser.parse_args(args)
    opportunity_id = parsed.id
    
    # 加载状态
    state = load_workflow_b_state(opportunity_id)
    
    # 已启动则拒绝：防止重复 start 覆盖进度
    if state['started_at']:
        print(f"ERROR: 工作流 B 已启动（{state['started_at']}）", file=sys.stderr)
        sys.exit(1)
    
    # 初始化状态
    state['started_at'] = now_iso()
    state['current_stage'] = 'DEMAND_INSIGHT'
    
    if parsed.skip_demand_insight:
        # 跳过需求洞察：该阶段直接 SKIPPED，指针前移到 BP_GENERATE
        state['stages']['DEMAND_INSIGHT']['status'] = 'SKIPPED'
        state['stages']['DEMAND_INSIGHT']['completed_at'] = now_iso()
        state['current_stage'] = 'BP_GENERATE'
    
    save_workflow_b_state(opportunity_id, state)
    
    # 回执：回显启动时间、当前阶段、是否跳过了需求洞察
    print(json.dumps({
        'success': True,
        'opportunity_id': opportunity_id,
        'workflow': 'B',
        'started_at': state['started_at'],
        'current_stage': state['current_stage'],
        'skipped_demand_insight': parsed.skip_demand_insight,
    }, indent=2, ensure_ascii=False))


def cmd_demand_insight(args):
    """执行需求洞察阶段（对应命令 innovation:workflow-b demand-insight）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id        : 机会 ID（必填）；
                    --status    : 阶段状态，仅 RUNNING/COMPLETED/FAILED（默认 RUNNING）；
                    --artifacts : 产物列表（JSON 数组字符串，可选）。

    返回值：无（输出 {"success":True,"stage":"DEMAND_INSIGHT","status":...,"current_stage":...}）。

    异常与退出码：
        工作流 B 未启动 -> 打印 ERROR 后 sys.exit(1)；
        artifacts 不是合法 JSON -> 打印 ERROR 后 sys.exit(1)。

    为什么这么设计：
        - 必须先 start（started_at 非空）才允许推进阶段，避免未初始化就写状态；
        - 只有 COMPLETED 才写 completed_at 并把 current_stage 推进到 BP_GENERATE，
          RUNNING/FAILED 只更新自身状态、不推进指针；
        - artifacts 用 JSON 字符串传入并当场解析，非法输入立即失败，避免脏数据落盘。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:workflow-b demand-insight',
        description='执行需求洞察阶段'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--status', choices=['RUNNING', 'COMPLETED', 'FAILED'],
                        default='RUNNING', help='阶段状态')
    parser.add_argument('--artifacts', help='产物列表（JSON 数组）')
    
    parsed = parser.parse_args(args)
    opportunity_id = parsed.id
    
    state = load_workflow_b_state(opportunity_id)
    
    # 未启动（started_at 为空）不允许推进阶段
    if not state['started_at']:
        print("ERROR: 工作流 B 未启动，请先执行 start", file=sys.stderr)
        sys.exit(1)
    
    # 更新阶段状态
    state['stages']['DEMAND_INSIGHT']['status'] = parsed.status
    state['current_stage'] = 'DEMAND_INSIGHT'
    
    if parsed.status == 'COMPLETED':
        # 完成才写完成时间，并把指针推进到 BP_GENERATE
        state['stages']['DEMAND_INSIGHT']['completed_at'] = now_iso()
        if parsed.artifacts:
            # artifacts 以 JSON 字符串形式传入，当场解析；非法即失败
            try:
                artifacts = json.loads(parsed.artifacts)
                state['stages']['DEMAND_INSIGHT']['artifacts'] = artifacts
            except json.JSONDecodeError:
                print("ERROR: artifacts 必须是有效的 JSON 数组", file=sys.stderr)
                sys.exit(1)
        # 自动进入下一阶段
        state['current_stage'] = 'BP_GENERATE'
    
    save_workflow_b_state(opportunity_id, state)
    
    # 回执：回显阶段状态与推进后的当前阶段
    print(json.dumps({
        'success': True,
        'opportunity_id': opportunity_id,
        'stage': 'DEMAND_INSIGHT',
        'status': parsed.status,
        'current_stage': state['current_stage'],
    }, indent=2, ensure_ascii=False))


def cmd_bp(args):
    """执行 BP 生成阶段（对应命令 innovation:workflow-b bp，可选阶段）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id        : 机会 ID（必填）；
                    --status    : 阶段状态，RUNNING/COMPLETED/FAILED/SKIPPED（默认 RUNNING）；
                    --artifacts : 产物列表（JSON 数组字符串，可选）。

    返回值：无（输出 {"success":True,"stage":"BP_GENERATE",...}）。

    异常与退出码：
        未启动 -> sys.exit(1)；artifacts 非法 JSON -> sys.exit(1)。

    为什么这么设计：
        - BP 是可选阶段：COMPLETED 与 SKIPPED 都会推进到 PRD_MVP，区别是 SKIPPED 会额外
          置 skipped=True 以留痕“本阶段被跳过”（便于事后审计为什么不产 BP）；
        - RUNNING/FAILED 只更新自身状态，不推进指针。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:workflow-b bp',
        description='执行 BP 生成阶段（可选）'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--status', choices=['RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED'],
                        default='RUNNING', help='阶段状态')
    parser.add_argument('--artifacts', help='产物列表（JSON 数组）')
    
    parsed = parser.parse_args(args)
    opportunity_id = parsed.id
    
    state = load_workflow_b_state(opportunity_id)
    
    if not state['started_at']:
        print("ERROR: 工作流 B 未启动，请先执行 start", file=sys.stderr)
        sys.exit(1)
    
    # 更新阶段状态
    state['stages']['BP_GENERATE']['status'] = parsed.status
    state['current_stage'] = 'BP_GENERATE'
    
    if parsed.status == 'COMPLETED':
        # 完成：记完成时间 + 产物（如有），进入 PRD_MVP
        state['stages']['BP_GENERATE']['completed_at'] = now_iso()
        if parsed.artifacts:
            try:
                artifacts = json.loads(parsed.artifacts)
                state['stages']['BP_GENERATE']['artifacts'] = artifacts
            except json.JSONDecodeError:
                print("ERROR: artifacts 必须是有效的 JSON 数组", file=sys.stderr)
                sys.exit(1)
        # 自动进入下一阶段
        state['current_stage'] = 'PRD_MVP'
    elif parsed.status == 'SKIPPED':
        # 跳过 BP：置 skipped 标记留痕，同样进入 PRD_MVP（跳过不等于未完成）
        state['stages']['BP_GENERATE']['skipped'] = True
        state['stages']['BP_GENERATE']['completed_at'] = now_iso()
        # 跳过 BP，直接进入 PRD
        state['current_stage'] = 'PRD_MVP'
    
    save_workflow_b_state(opportunity_id, state)
    
    # 回执
    print(json.dumps({
        'success': True,
        'opportunity_id': opportunity_id,
        'stage': 'BP_GENERATE',
        'status': parsed.status,
        'current_stage': state['current_stage'],
    }, indent=2, ensure_ascii=False))


def cmd_prd(args):
    """执行 PRD 定义阶段（对应命令 innovation:workflow-b prd）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id        : 机会 ID（必填）；
                    --status    : 阶段状态，RUNNING/COMPLETED/FAILED（默认 RUNNING）；
                    --artifacts : 产物列表（JSON 数组字符串，可选）。

    返回值：无（输出 {"success":True,"stage":"PRD_MVP",...,"hitl_gate":bool}）。

    异常与退出码：未启动 -> sys.exit(1)；artifacts 非法 JSON -> sys.exit(1)。

    为什么这么设计：
        - PRD 完成后不能直接进 Demo，必须先过人工确认：COMPLETED 时把 current_stage 推进
          到 PRD_MVP_GATE，并把该 Gate 阶段置 WAITING_USER，形成 HITL 卡点；
        - 回执里的 hitl_gate 字段直接告诉上层“是否刚进入等待用户确认状态”。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:workflow-b prd',
        description='执行 PRD 定义阶段'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--status', choices=['RUNNING', 'COMPLETED', 'FAILED'],
                        default='RUNNING', help='阶段状态')
    parser.add_argument('--artifacts', help='产物列表（JSON 数组）')
    
    parsed = parser.parse_args(args)
    opportunity_id = parsed.id
    
    state = load_workflow_b_state(opportunity_id)
    
    if not state['started_at']:
        print("ERROR: 工作流 B 未启动，请先执行 start", file=sys.stderr)
        sys.exit(1)
    
    # 更新阶段状态
    state['stages']['PRD_MVP']['status'] = parsed.status
    state['current_stage'] = 'PRD_MVP'
    
    if parsed.status == 'COMPLETED':
        # 完成 PRD：记完成时间 + 产物，并进入 HITL Gate（等待用户确认）
        state['stages']['PRD_MVP']['completed_at'] = now_iso()
        if parsed.artifacts:
            try:
                artifacts = json.loads(parsed.artifacts)
                state['stages']['PRD_MVP']['artifacts'] = artifacts
            except json.JSONDecodeError:
                print("ERROR: artifacts 必须是有效的 JSON 数组", file=sys.stderr)
                sys.exit(1)
        # 进入 HITL Gate
        state['current_stage'] = 'PRD_MVP_GATE'
        state['stages']['PRD_MVP_GATE']['status'] = 'WAITING_USER'
    
    save_workflow_b_state(opportunity_id, state)
    
    # 回执：hitl_gate 表明是否已进入等待用户确认
    print(json.dumps({
        'success': True,
        'opportunity_id': opportunity_id,
        'stage': 'PRD_MVP',
        'status': parsed.status,
        'current_stage': state['current_stage'],
        'hitl_gate': parsed.status == 'COMPLETED',
    }, indent=2, ensure_ascii=False))


def cmd_prd_gate(args):
    """PRD HITL Gate 确认（对应命令 innovation:workflow-b prd-gate）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id      : 机会 ID（必填）；
                    --confirm : 必填，'true' 或 'false'（字符串枚举），是否确认 PRD；
                    --reason  : 可选，拒绝原因（confirm=false 时）。

    返回值：无（输出 {"success":True,"stage":"PRD_MVP_GATE","confirmed":bool,"current_stage":...}）。

    异常与退出码：
        Gate 不在 WAITING_USER -> 打印 ERROR 后 sys.exit(1)。

    为什么这么设计：
        - 只有处于 WAITING_USER 时才允许确认，防止在错误时机重复确认/跳过；
        - confirm=true：Gate 置 COMPLETED、user_confirmed=True，进入 MODAO_DEMO；
        - confirm=false：Gate 置 REJECTED，记录 reject_reason（如有），并把 PRD_MVP 打回
          RUNNING、指针退回 PRD_MVP，实现“人工驳回→重做 PRD”的闭环。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:workflow-b prd-gate',
        description='PRD HITL Gate 确认'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--confirm', choices=['true', 'false'], required=True,
                        help='是否确认 PRD')
    parser.add_argument('--reason', help='拒绝原因（confirm=false 时）')
    
    parsed = parser.parse_args(args)
    opportunity_id = parsed.id
    
    state = load_workflow_b_state(opportunity_id)
    
    # 前置校验：Gate 必须处于等待用户状态，否则拒绝确认
    if state['stages']['PRD_MVP_GATE']['status'] != 'WAITING_USER':
        print("ERROR: PRD Gate 不在等待用户状态", file=sys.stderr)
        sys.exit(1)
    
    if parsed.confirm == 'true':
        # 通过：Gate 置 COMPLETED + 记录用户已确认，进入 Demo 阶段
        state['stages']['PRD_MVP_GATE']['status'] = 'COMPLETED'
        state['stages']['PRD_MVP_GATE']['user_confirmed'] = True
        state['stages']['PRD_MVP_GATE']['completed_at'] = now_iso()
        # 进入 Demo 阶段
        state['current_stage'] = 'MODAO_DEMO'
    else:
        # 驳回：Gate 置 REJECTED，记录原因（如有），PRD 打回 RUNNING 重做
        state['stages']['PRD_MVP_GATE']['status'] = 'REJECTED'
        state['stages']['PRD_MVP_GATE']['user_confirmed'] = False
        state['stages']['PRD_MVP_GATE']['completed_at'] = now_iso()
        if parsed.reason:
            state['stages']['PRD_MVP_GATE']['reject_reason'] = parsed.reason
        # 回退到 PRD 阶段重新修改
        state['current_stage'] = 'PRD_MVP'
        state['stages']['PRD_MVP']['status'] = 'RUNNING'
    
    save_workflow_b_state(opportunity_id, state)
    
    # 回执：确认结果 + 推进后的当前阶段
    print(json.dumps({
        'success': True,
        'opportunity_id': opportunity_id,
        'stage': 'PRD_MVP_GATE',
        'confirmed': parsed.confirm == 'true',
        'current_stage': state['current_stage'],
    }, indent=2, ensure_ascii=False))


def cmd_demo(args):
    """执行墨刀 Demo 阶段（对应命令 innovation:workflow-b demo，可选阶段）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id        : 机会 ID（必填）；
                    --status    : 阶段状态，RUNNING/COMPLETED/FAILED/SKIPPED（默认 RUNNING）；
                    --artifacts : 产物列表（JSON 数组字符串，可选）。

    返回值：无（输出 {"success":True,"stage":"MODAO_DEMO",...,"workflow_completed":bool}）。

    异常与退出码：
        未启动 -> sys.exit(1)；
        PRD_MVP_GATE 状态不是 COMPLETED -> 打印 ERROR 后 sys.exit(1)；
        artifacts 非法 JSON -> sys.exit(1)。

    为什么这么设计：
        - 强制执行前置校验：Demo 必须建立在 PRD Gate 已通过之上，否则不让做（保证不跳步）；
        - COMPLETED 或 SKIPPED 都把 current_stage 置 'COMPLETED'，代表工作流 B 收尾
          （SKIPPED 会额外置 skipped 标记留痕）；
        - 回执的 workflow_completed 便于上层识别整体是否结束。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:workflow-b demo',
        description='执行墨刀 Demo 阶段（可选）'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--status', choices=['RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED'],
                        default='RUNNING', help='阶段状态')
    parser.add_argument('--artifacts', help='产物列表（JSON 数组）')
    
    parsed = parser.parse_args(args)
    opportunity_id = parsed.id
    
    state = load_workflow_b_state(opportunity_id)
    
    if not state['started_at']:
        print("ERROR: 工作流 B 未启动，请先执行 start", file=sys.stderr)
        sys.exit(1)
    
    # 检查 PRD Gate 是否通过
    # 前置闸门：Gate 未 COMPLETED 不允许做 Demo，防止越过人工确认
    if state['stages']['PRD_MVP_GATE']['status'] != 'COMPLETED':
        print("ERROR: PRD Gate 未通过，无法执行 Demo", file=sys.stderr)
        sys.exit(1)
    
    # 更新阶段状态
    state['stages']['MODAO_DEMO']['status'] = parsed.status
    state['current_stage'] = 'MODAO_DEMO'
    
    if parsed.status == 'COMPLETED':
        # 完成 Demo：记完成时间 + 产物，工作流 B 整体完成
        state['stages']['MODAO_DEMO']['completed_at'] = now_iso()
        if parsed.artifacts:
            try:
                artifacts = json.loads(parsed.artifacts)
                state['stages']['MODAO_DEMO']['artifacts'] = artifacts
            except json.JSONDecodeError:
                print("ERROR: artifacts 必须是有效的 JSON 数组", file=sys.stderr)
                sys.exit(1)
        # 工作流 B 完成
        state['current_stage'] = 'COMPLETED'
    elif parsed.status == 'SKIPPED':
        # 跳过 Demo：置跳过标记，同样视为工作流 B 完成
        state['stages']['MODAO_DEMO']['skipped'] = True
        state['stages']['MODAO_DEMO']['completed_at'] = now_iso()
        # 工作流 B 完成（跳过 Demo）
        state['current_stage'] = 'COMPLETED'
    
    save_workflow_b_state(opportunity_id, state)
    
    # 回执：workflow_completed 反映整体是否收尾
    print(json.dumps({
        'success': True,
        'opportunity_id': opportunity_id,
        'stage': 'MODAO_DEMO',
        'status': parsed.status,
        'current_stage': state['current_stage'],
        'workflow_completed': state['current_stage'] == 'COMPLETED',
    }, indent=2, ensure_ascii=False))


def cmd_rollback(args):
    """回退到指定阶段（对应命令 innovation:workflow-b rollback）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id     : 机会 ID（必填）；
                    --target : 回退目标阶段，仅 DEMAND_INSIGHT/BP_GENERATE/PRD_MVP/MODAO_DEMO；
                    --reason : 回退原因（必填）。

    返回值：无（输出 {"success":True,...,"rollback":记录,"current_stage":目标阶段}）。

    异常与退出码：未启动 -> 打印 ERROR 后 sys.exit(1)。

    为什么这么设计：
        - 必须带 reason：回退是有风险的变更，强制留下原因便于审计；
        - 先把“从哪来、到哪去、为什么、何时”记进 rollback_history，再重置状态，保证历史可追溯；
        - 重置范围是“目标阶段及其之后的所有阶段”：按 WORKFLOW_B_STAGE_ORDER 比较索引，
          index >= target 的阶段统一置回 PENDING、清空 completed_at/artifacts，
          PRD_MVP_GATE 额外清 user_confirmed——这样回退后从目标阶段往后都要重新走一遍；
        - 注意：回退不重置可选阶段的 skipped 标记，交接时若需连同 skipped 一起清除，需另行处理。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:workflow-b rollback',
        description='回退到指定阶段（需要用户授权）'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--target', required=True,
                        choices=['DEMAND_INSIGHT', 'BP_GENERATE', 'PRD_MVP', 'MODAO_DEMO'],
                        help='回退目标阶段')
    parser.add_argument('--reason', required=True, help='回退原因')
    
    parsed = parser.parse_args(args)
    opportunity_id = parsed.id
    
    state = load_workflow_b_state(opportunity_id)
    
    if not state['started_at']:
        print("ERROR: 工作流 B 未启动", file=sys.stderr)
        sys.exit(1)
    
    # 记录回退历史
    # 留下“from/to/reason/timestamp”四元组，作为审计凭据
    rollback_record = {
        'from_stage': state['current_stage'],
        'to_stage': parsed.target,
        'reason': parsed.reason,
        'timestamp': now_iso(),
    }
    state['rollback_history'].append(rollback_record)
    
    # 重置目标阶段及后续阶段
    # 以阶段顺序表的下标为准：>= 目标下标的所有阶段都要复位
    target_idx = WORKFLOW_B_STAGE_ORDER.index(parsed.target)
    for stage_name in WORKFLOW_B_STAGE_ORDER:
        stage_idx = WORKFLOW_B_STAGE_ORDER.index(stage_name)
        if stage_idx >= target_idx:
            # 统一复位为“从未执行”的干净形态
            state['stages'][stage_name]['status'] = 'PENDING'
            state['stages'][stage_name]['completed_at'] = None
            state['stages'][stage_name]['artifacts'] = []
            if stage_name == 'PRD_MVP_GATE':
                # Gate 需额外清除用户确认标记，回退后要重新人工确认
                state['stages'][stage_name]['user_confirmed'] = False
    
    state['current_stage'] = parsed.target
    
    save_workflow_b_state(opportunity_id, state)
    
    # 回执：回显回退记录与新的当前阶段
    print(json.dumps({
        'success': True,
        'opportunity_id': opportunity_id,
        'rollback': rollback_record,
        'current_stage': state['current_stage'],
    }, indent=2, ensure_ascii=False))


def cmd_status(args):
    """查看工作流 B 状态（对应命令 innovation:workflow-b status）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id : 机会 ID（必填）。

    返回值：无（输出 opportunity_id/workflow/started_at/current_stage/stages/rollback_count）。

    异常与退出码：无显式退出；状态文件不存在时 load_workflow_b_state 返回初始骨架，
        因此会输出一份“未启动”的状态（started_at=None），而不是报错。

    为什么这么设计：
        状态查询用于主 Agent/巡检了解“工作流 B 到哪一步、回退过几次”；
        rollback_count 用 rollback_history 长度表示，快速反映该机会被回退的频率。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:workflow-b status',
        description='查看工作流 B 状态'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    
    parsed = parser.parse_args(args)
    opportunity_id = parsed.id
    
    state = load_workflow_b_state(opportunity_id)
    
    print(json.dumps({
        'opportunity_id': opportunity_id,
        'workflow': 'B',
        'started_at': state['started_at'],
        'current_stage': state['current_stage'],
        'stages': state['stages'],
        'rollback_count': len(state['rollback_history']),
    }, indent=2, ensure_ascii=False))


def register_commands(commands: dict):
    """注册工作流 B 命令。

    参数：
        commands —— ops.py 传入的命名空间局部 registry；此处登记 8 条本地 key，
                    前缀化后为 innovation:workflow-b.start / demand-insight / bp / prd /
                    prd-gate / demo / rollback / status。

    返回值：无。
    """
    commands['workflow-b.start'] = cmd_start
    commands['workflow-b.demand-insight'] = cmd_demand_insight
    commands['workflow-b.bp'] = cmd_bp
    commands['workflow-b.prd'] = cmd_prd
    commands['workflow-b.prd-gate'] = cmd_prd_gate
    commands['workflow-b.demo'] = cmd_demo
    commands['workflow-b.rollback'] = cmd_rollback
    commands['workflow-b.status'] = cmd_status
