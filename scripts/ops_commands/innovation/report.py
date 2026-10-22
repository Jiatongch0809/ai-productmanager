#!/usr/bin/env python3
"""报告生成命令封装

命令：
    innovation:report generate --id ID --type TYPE

本文件在整体机制里的位置：
    innovation 命名空间下的“报告生成”命令层（薄封装），位于工作流 A 的
    GENERATE / DECISION_SUMMARY 环节附近。它自身不生成报告内容，只负责参数校验
    （--id、--type）+ 环境变量准备，再 subprocess 调用底层脚本 scripts/generate_report.py。

核心设计（交接重点）：
    - 报告类型通过环境变量 REPORT_TYPE 传给底层脚本（机会 ID 走 OPPORTUNITY_ID），
      与其它底层脚本的“环境变量传参”约定一致；
    - 失败时保留底层退出码（sys.exit(result.returncode)），不吞码，避免主 Agent
      把失败当成功继续跑。
"""
import argparse     # 解析 --id / --type，并用 choices 限定报告类型
import subprocess   # 以子进程调用底层脚本 generate_report.py
import sys          # 透传退出码、写 stderr
import os           # 拼 SCRIPTS_DIR、复制并注入环境变量

# 由本文件所在目录上溯三层（innovation -> ops_commands -> scripts）定位 scripts/ 目录
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cmd_generate(args):
    """生成报告（对应命令 innovation:report generate）。

    参数：
        args —— 由 ops.py 透传的剩余参数列表，经 argparse 解析为：
                    --id   : 机会 ID（必填），写入环境变量 OPPORTUNITY_ID；
                    --type : 报告类型（必填），仅允许
                             full_evaluation / six_lens / fit_assessment，
                             写入环境变量 REPORT_TYPE（dest 为 report_type）。

    返回值：无。

    异常与退出码：
        argparse 参数缺失/非法 -> argparse 自身 exit(2)；
        底层脚本非零退出 -> 透传其退出码（sys.exit(result.returncode)）；
        底层成功 -> 正常结束（退出码 0）。

    为什么这么设计：
        - choices 白名单把报告类型锁死为三种，非法类型在调用前被拦；
        - dest='report_type' 把命令行名 --type 映射到变量 report_type，避免与内建
          type 混淆；随后作为 REPORT_TYPE 环境变量传给底层；
        - 保留退出码：报告生成失败必须让主 Agent 感知，而非静默成功。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:report generate',
        description='生成评估报告',
        epilog='''
报告类型:
    full_evaluation     完整评估报告
    six_lens            六看报告
    fit_assessment      适配度报告

示例:
    python3 scripts/ops.py innovation:report generate --id xxx --type full_evaluation
        '''
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--type', required=True, dest='report_type',
                        choices=['full_evaluation', 'six_lens', 'fit_assessment'],
                        help='报告类型')
    
    parsed = parser.parse_args(args)
    
    # 子进程命令：当前解释器 + scripts/generate_report.py（参数全部走环境变量传递）
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'generate_report.py')]
    
    # 复制环境并注入机会 ID 与报告类型，底层脚本据此读取
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    env['REPORT_TYPE'] = parsed.report_type
    
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        # 注意：这里在 returncode!=0 时把 stderr 同时打到 stdout 与 stderr
        # （stdout 分支未做空串判断，即使 stderr 为空也会执行一次 print），
        # 使错误文本在两处重复出现。疑似重复输出，但属既有行为，未做改动，交接留意。
        if result.stderr:
            print(result.stderr, end='')
        print(result.stderr, end='', file=sys.stderr)
        # 保留底层退出码，防止主 Agent 把失败当成功
        sys.exit(result.returncode)


def register_commands(commands: dict):
    """注册报告命令。

    参数：
        commands —— ops.py 传入的命名空间局部 registry；
                    此处登记一条：'report.generate' -> cmd_generate（前缀化后为 innovation:report.generate）。

    返回值：无。
    """
    commands['report.generate'] = cmd_generate
