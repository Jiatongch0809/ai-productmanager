#!/usr/bin/env python3
"""Gate 检查命令封装

命令：
    innovation:gate check --id ID --stage STAGE

本文件在整体机制里的位置：
    这是 innovation 命名空间下的 Gate 命令层（薄封装），对应工作流 A 的两个卡点：
    SIX_LENS_GATE（六看压力测试后）与 FIT_GATE（适配度评估后）。它本身不做 Gate 判定，
    只负责把命令行参数整理好、设置环境变量，然后 subprocess 调用底层脚本
    scripts/gate_check.py 执行真正的确定性判定。

核心设计（交接重点）：
    - 命令层刻意保留底层脚本的退出码（sys.exit(result.returncode)）。Gate 有
      WAIT_USER / STOP / ERROR 等多态，退出码是主 Agent 判断“下一步该不该继续”的
      唯一确定性信号；一旦吞掉（比如统一 exit 0），主 Agent 会把失败/需等待当成成功
      继续往下跑，破坏整条工作流的正确性。
    - 机会 ID 通过环境变量 OPPORTUNITY_ID 传给底层脚本，而不是命令行位置参数，
      与其它底层脚本（evidence_ledger.py / workflow_state.py）保持一致约定。
"""
import argparse     # 解析 --id / --stage，并借它做 choices 白名单校验
import subprocess   # 以子进程调用底层脚本 gate_check.py
import sys          # 透传退出码、写 stderr
import os           # 拼 SCRIPTS_DIR、复制并注入环境变量

# 由本文件所在目录（.../ops_commands/innovation）上溯三层得到 scripts/ 目录：
# innovation -> ops_commands -> scripts，用于定位底层脚本 gate_check.py
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cmd_check(args):
    """执行 Gate 检查（对应命令 innovation:gate check）。

    参数：
        args —— 由 ops.py 透传的剩余参数列表（如 ['--id','xxx','--stage','SIX_LENS']）。
                经 argparse 解析为：
                    --id    : 机会 ID（必填），透传给底层脚本的环境变量 OPPORTUNITY_ID；
                    --stage : Gate 阶段名（必填），仅允许 SIX_LENS / FIT_ASSESSMENT，
                              以位置参数形式传给 gate_check.py。

    返回值：无（成功与否由退出码体现）。

    异常与退出码：
        argparse 参数缺失/非法 -> argparse 自身 exit(2)，进程终止；
        Gate 结果与落盘状态 -> 透传 gate_check.py 的退出码：
            WAIT_USER 返回 0；STOP 返回 1；ERROR 或状态落盘失败返回 2。

    为什么这么设计：
        - 用 argparse 的 choices 把 --stage 限定为两个枚举值，非法阶段在进入底层脚本前
          就被挡下，避免底层处理未知阶段；
        - 把退出码原样 sys.exit：让主 Agent 依据确定性退出码决定停/走，而不是靠解析文本。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:gate check',
        description='执行 Gate 检查',
        epilog='''
阶段名:
    SIX_LENS        六看压力测试 Gate
    FIT_ASSESSMENT  适配度评估 Gate

示例:
    python3 scripts/ops.py innovation:gate check --id xxx --stage SIX_LENS
        '''
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--stage', required=True,
                        choices=['SIX_LENS', 'FIT_ASSESSMENT'],
                        help='Gate 阶段名')
    
    parsed = parser.parse_args(args)
    
    # 组装子进程命令：用当前解释器 + scripts/gate_check.py + 阶段名（位置参数）
    # 注意 stage 已由 choices 限定合法值，不会引入注入风险
    cmd = [
        sys.executable,
        os.path.join(SCRIPTS_DIR, 'gate_check.py'),
        parsed.stage
    ]
    
    # 复制当前环境再注入 OPPORTUNITY_ID：底层脚本按环境变量取机会 ID
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    
    # capture_output 捕获 stdout/stderr 后自行转发，便于统一控制输出通道与顺序
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout, end='')
    # 仅在“非零退出且确实有 stderr”时才把 stderr 转出去，避免噪声
    if result.returncode != 0 and result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    # WAIT_USER 返回 0；STOP 返回 1；ERROR 或状态落盘失败返回 2。
    # 不能吞掉退出码，否则主 Agent 会把失败当作成功继续执行。
    sys.exit(result.returncode)


def register_commands(commands: dict):
    """注册 Gate 命令。

    参数：
        commands —— ops.py 传入的命名空间局部 registry；
                    此处登记一条：'gate.check' -> cmd_check（前缀化后为 innovation:gate.check）。

    返回值：无。
    """
    commands['gate.check'] = cmd_check
