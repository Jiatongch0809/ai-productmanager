#!/usr/bin/env python3
"""状态管理命令封装

命令：
    innovation:state update --id ID --stage STAGE --status STATUS
    innovation:state read --id ID
    innovation:state resume --id ID --approve true|false [--reason REASON]
    innovation:state list-pending

本文件在整体机制里的位置：
    innovation 命名空间下的“工作流状态”命令层（薄封装）。工作流 A 的阶段推进
    （INIT → TEARDOWN → SIX_LENS → SIX_LENS_GATE → FIT_ASSESSMENT → FIT_GATE →
    GENERATE → INNOVATION_REVIEW → DECISION_SUMMARY → PRODUCT_PROPOSAL）以及 HITL
    等待/恢复，都由底层脚本 scripts/workflow_state.py 落盘管理。本文件只做参数解析 +
    环境变量准备 + 子进程转发。

核心设计（交接重点）：
    - 所有子命令都保留底层 workflow_state.py 的退出码（sys.exit(result.returncode)），
      这是“确定交给脚本”的体现：状态迁移是否合法、是否满足前置条件，由脚本判定，
      退出码作为主 Agent 唯一的确定性信号，绝不能被吞掉；
    - 机会 ID 走环境变量 OPPORTUNITY_ID；list-pending / list-pending-outputs 为全局查询，
      不需要 --id。

命令分组说明：
    - update / resume：写入类，推进或恢复状态（可能因非法迁移而失败，故必须保留退出码）；
    - read / list-pending / list-pending-outputs：读取类，只输出当前状态；
    - mark-output-sent / mark-output-sending：维护 pending_output（Gate 结果待发送/已发送）
      的两段式状态，配合外部渠道回执做幂等标记。
"""
import argparse     # 解析各子命令参数
import subprocess   # 以子进程调用底层脚本 workflow_state.py
import sys          # 透传退出码、写 stderr
import os           # 拼 SCRIPTS_DIR、复制并注入环境变量

# 由本文件所在目录上溯三层（innovation -> ops_commands -> scripts）定位 scripts/ 目录
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cmd_update(args):
    """更新工作流状态（对应命令 innovation:state update）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id     : 机会 ID（必填）→ 环境变量 OPPORTUNITY_ID；
                    --stage  : 阶段名（必填），如 INIT/TEARDOWN/SIX_LENS/SIX_LENS_GATE/
                               FIT_ASSESSMENT/FIT_GATE/GENERATE 等；
                    --status : 状态名（必填），如 PENDING/RUNNING/COMPLETED/
                               WAITING_USER/CONFIRMED/STOPPED/FAILED/REJECTED；
                    --output : 可选，输出数据（JSON 字符串），有值才追加到子进程命令；
                    --reason : 可选，原因说明，有值才追加到子进程命令。

    返回值：无。

    异常与退出码：
        argparse 参数缺失/非法 -> argparse 自身 exit(2)；
        底层非零退出 -> 透传其退出码；成功 -> 0。

    为什么这么设计：
        - --output / --reason 采用“有值才 extend”的方式拼命令，未提供时不产生多余参数；
        - stage / status 不在命令层做枚举校验（此处 help 只做说明），交给底层脚本判定，
          因为合法状态集合与迁移规则属于业务规则，应由确定性脚本统一维护；
        - 保留退出码：非法状态迁移必须让主 Agent 停下来，不能静默成功。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:state update',
        description='更新工作流状态'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--stage', required=True, 
                        help='阶段名: INIT, TEARDOWN, SIX_LENS, SIX_LENS_GATE, FIT_ASSESSMENT, FIT_GATE, GENERATE')
    parser.add_argument('--status', required=True,
                        help='状态名: PENDING, RUNNING, COMPLETED, WAITING_USER, CONFIRMED, STOPPED, FAILED, REJECTED')
    parser.add_argument('--output', help='输出数据（JSON 字符串）')
    parser.add_argument('--reason', help='原因说明')
    
    parsed = parser.parse_args(args)
    
    # 基础命令：update 子命令 + stage/status 两个必传项
    cmd = [
        sys.executable, 
        os.path.join(SCRIPTS_DIR, 'workflow_state.py'),
        'update',
        '--stage', parsed.stage,
        '--status', parsed.status
    ]
    # 仅当用户提供了这些可选参数时才追加，保持子进程命令行最小化
    if parsed.output:
        cmd.extend(['--output', parsed.output])
    if parsed.reason:
        cmd.extend(['--reason', parsed.reason])
    
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        # 失败才转发 stderr，并把退出码原样透传（关键：不可吞码）
        print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)


def cmd_read(args):
    """读取工作流状态（对应命令 innovation:state read）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id : 机会 ID（必填）→ 环境变量 OPPORTUNITY_ID。

    返回值：无（把底层 workflow_state.py read 的 JSON 打到 stdout）。

    异常与退出码：
        argparse 参数缺失/非法 -> execute(2)；底层非零退出 -> 透传其退出码。

    为什么这么设计：
        只读命令也保留退出码：若台账/状态文件缺失或损坏，底层会非零退出，
        主 Agent 需要据此判断能不能继续，而不是拿到空输出却当成功。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:state read',
        description='读取工作流状态'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    
    parsed = parser.parse_args(args)
    
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'workflow_state.py'), 'read']
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)


def cmd_resume(args):
    """恢复等待用户确认的流程（对应命令 innovation:state resume，即 HITL 恢复）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id      : 机会 ID（必填）→ 环境变量 OPPORTUNITY_ID；
                    --approve : 必填，'true' 或 'false'（字符串枚举，非布尔），
                                表示用户是否批准继续；
                    --reason  : 可选，拒绝原因（approve=false 时提供）。

    返回值：无。

    异常与退出码：
        argparse 参数缺失/非法 -> exit(2)；底层非零退出 -> 透传其退出码。

    为什么这么设计：
        - approve 用 choices=['true','false'] 字符串而非布尔：与底层参数形态保持一致；
        - reason 可有可无、只在提供时传递，避免给子进程塞空参数；
        - 恢复动作会改变工作流走向，必须保留退出码让主 Agent 依据结果决定后续。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:state resume',
        description='恢复等待用户确认的流程'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--approve', required=True, choices=['true', 'false'],
                        help='是否批准继续: true 或 false')
    parser.add_argument('--reason', help='拒绝原因（approve=false 时）')
    
    parsed = parser.parse_args(args)
    
    cmd = [
        sys.executable,
        os.path.join(SCRIPTS_DIR, 'workflow_state.py'),
        'resume',
        '--approve', parsed.approve
    ]
    if parsed.reason:
        cmd.extend(['--reason', parsed.reason])
    
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)


def cmd_list_pending(args):
    """列出待处理的任务（对应命令 innovation:state list-pending）。

    参数：
        args —— 未使用（本子命令不需要 --id；为与其它 handler 签名一致仍保留形参）。

    返回值：无（把底层 workflow_state.py list-pending 的 JSON 打到 stdout）。

    异常与退出码：底层非零退出 -> 透传其退出码。

    为什么这么设计：
        这是全局巡检命令，用于主 Agent 发现“有没有卡在 WAITING_USER 的机会”，
        因此不需要 --id，不带环境变量调用。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:state list-pending',
        description='列出所有待处理的任务'
    )
    # list-pending 不需要参数
    # 注意：这里只为 --help 提供说明，实际不读取 parsed，且不存在 --id
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'workflow_state.py'), 'list-pending']
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)


def cmd_list_pending_outputs(args):
    """列出所有待发送的 Gate 结果（对应命令 innovation:state list-pending-outputs）。

    参数：
        args —— 未使用（无需 --id；保留形参以对齐 handler 约定）。

    返回值：无（把底层 list-pending-outputs 的 JSON 打到 stdout）。

    异常与退出码：底层非零退出 -> 透传其退出码。

    为什么这么设计：
        与 mark-output-sent/mark-output-sending 配合，构成“待发送 → 发送中 → 已发送”的
        两段式投递状态机；本命令负责把“还有哪些 Gate 结果没发出去”列清楚。
    """
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'workflow_state.py'), 'list-pending-outputs']
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)


def cmd_mark_output_sent(args):
    """标记 pending_output 为已发送（对应命令 innovation:state mark-output-sent）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id      : 机会 ID（必填）→ 环境变量 OPPORTUNITY_ID；
                    --receipt : 可选，渠道回执（钉钉 openMessageId 或 session message id），
                                有值才追加到子进程命令。

    返回值：无。

    异常与退出码：argparse 出错 exit(2)；底层非零退出 -> 透传其退出码。

    为什么这么设计：
        用回执（receipt）记录外部渠道的投递凭据，使“已发送”可被审计与幂等核对；
        保留退出码让主 Agent 知道标记是否成功，避免重复投递。
    """
    parser = argparse.ArgumentParser(prog='ops.py innovation:state mark-output-sent')
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--receipt', help='渠道回执（钉钉 openMessageId 或 session message id）')
    parsed = parser.parse_args(args)
    
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'workflow_state.py'), 'mark-output-sent']
    if parsed.receipt:
        cmd.extend(['--receipt', parsed.receipt])
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)


def cmd_mark_output_sending(args):
    """标记 pending_output 为发送中（对应命令 innovation:state mark-output-sending）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id : 机会 ID（必填）→ 环境变量 OPPORTUNITY_ID。

    返回值：无。

    异常与退出码：argparse 出错 exit(2)；底层非零退出 -> 透传其退出码。

    为什么这么设计：
        两段式状态：先置“发送中”再置“已发送”，避免在真正投递过程中被并发重复触发；
        中途崩溃会停在“发送中”，可由上层巡检发现并进行人工/自动补救。
    """
    parser = argparse.ArgumentParser(prog='ops.py innovation:state mark-output-sending')
    parser.add_argument('--id', required=True, help='机会 ID')
    parsed = parser.parse_args(args)
    
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'workflow_state.py'), 'mark-output-sending']
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)


def register_commands(commands: dict):
    """注册状态管理命令。

    参数：
        commands —— ops.py 传入的命名空间局部 registry；此处登记 7 条本地 key，
                    前缀化后分别为 innovation:state.update / read / resume /
                    list-pending / list-pending-outputs / mark-output-sent / mark-output-sending。

    返回值：无。
    """
    commands['state.update'] = cmd_update
    commands['state.read'] = cmd_read
    commands['state.resume'] = cmd_resume
    commands['state.list-pending'] = cmd_list_pending
    commands['state.list-pending-outputs'] = cmd_list_pending_outputs
    commands['state.mark-output-sent'] = cmd_mark_output_sent
    commands['state.mark-output-sending'] = cmd_mark_output_sending
