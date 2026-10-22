#!/usr/bin/env python3
"""证据台账命令封装

命令：
    innovation:ledger write-teardown --id ID --evidence-file PATH
    innovation:ledger write-six-lens --id ID --conclusion CONCLUSION [--evidence-file PATH] [--tasks-file PATH] [--dimensions-file PATH]
    innovation:ledger write-fit --id ID --scores-file PATH
    innovation:ledger read --id ID

核心设计：
    - write-fit 自动调用 fit_scorer.py 计算总分和 Gate
    - 模型只需传原始评分，不需要手动计算
    - 所有 stdin 传递由本模块处理，模型不需要关心管道
    - 大 payload 用 --*-file 传文件路径，不经模型上下文、不需命令行转义

本文件在整体机制里的位置（交接重点）：
    这是创新工作流 A 的“台账写入命令层”，是主 Agent 往事实源（evidence_ledger）落数的
    唯一通道。它自身不存储数据，而是把模型给的原始输入整理成底层脚本需要的形式，再
    subprocess 调用：
        - evidence_ledger.py  —— 真正写台账（init/update/get）；
        - workflow_state.py   —— 把对应阶段置为 RUNNING/COMPLETED；
        - fit_scorer.py       —— 由 8 维原始评分计算加权总分与 Gate（脚本内计算，避免
                                 模型手算出错或篡改）。

为什么命令层要承担这些编排（而不是让模型自己串脚本）：
    - 「不确定交给 LLM，确定交给脚本」：模型只产原始评分/结论/证据，加权、Gate 判定、
      写盘路径、版本递增全部由脚本确定性完成；
    - 防篡改：cmd_write_fit 会回读子任务 manifest 校验模型传的评分必须与子任务结果一致，
      阻断“主 Agent 随手编个分数就落库”；
    - 大 payload 走文件：证据/评分等大 JSON 用 --*-file 传路径，避免塞进命令行（转义、
      长度上限问题）或经过模型上下文（省 token、降污染）。

退出码约定（重要）：
    - 写台账路径（run_evidence_ledger_update / run_fit_scorer / cmd_init）失败时统一
      sys.exit(1)——对主 Agent 而言，写入失败就是必须停下的硬错误；
    - 透传退出码的只有 run_state_update 与 cmd_read（前者保留底层码，后者读操作透传）；
      交接时注意：run_evidence_ledger_update 失败会丢掉底层具体退出码、统一退 1，
      这是为了把 FAILED 语义收敛为单一信号，不是 bug。
"""
import argparse     # 各子命令参数解析
import subprocess   # 子进程调用底层脚本
import sys          # 透传/控制退出码、写 stderr
import os           # 路径拼接、文件存在性判断、环境变量
import json         # 序列化 stdin payload、解析 stdout 结果
import tempfile     # 注意：已 import，但当前代码块未见直接使用，交接留意是否遗留

# 由本文件所在目录上溯三层（innovation -> ops_commands -> scripts）定位 scripts/ 目录
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_evidence_ledger_update(opportunity_id: str, stage: str, data: dict):
    """调用 evidence_ledger.py update，处理 stdin 传递。

    参数：
        opportunity_id —— 机会 ID，写入子进程环境变量 OPPORTUNITY_ID；
        stage          —— 阶段名，写入子进程环境变量 STAGE；
        data           —— 要写入台账的阶段结果字典，序列化成 JSON 经 stdin 传进去。

    返回值：
        dict —— 解析底层 stdout 得到的 JSON（通常含 status/version/stage）；
                stdout 为空时返回 {}。

    异常与退出码：
        底层非零退出 -> 打印 ERROR + STDERR/STDOUT 后 sys.exit(1)。
        （注意：此处把底层具体退出码收敛为 1，语义是“台账写入失败”。）

    为什么这么设计：
        - stdin 传 JSON 而不是命令行：避免大 JSON 在命令行转义/长度上的坑；
        - 模型无需关心管道细节：这一层替模型把它和底层脚本粘好；
        - 失败即退 1：写台账是主 Agent 的关键动作，失败必须中断流程而不是被忽略。
    """
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'evidence_ledger.py'), 'update']
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = opportunity_id
    env['STAGE'] = stage
    
    # 序列化阶段结果，经 stdin 投喂给 evidence_ledger.py update
    input_json = json.dumps(data, ensure_ascii=False)
    result = subprocess.run(cmd, env=env, input=input_json, capture_output=True, text=True)
    
    if result.returncode != 0:
        # 把底层 stdout/stderr 一并打到 stderr，保留现场；退出码统一为 1
        print(f"ERROR: evidence_ledger.py update 失败", file=sys.stderr)
        print(f"STDERR: {result.stderr}", file=sys.stderr)
        if result.stdout:
            print(f"STDOUT: {result.stdout}", file=sys.stderr)
        sys.exit(1)
    
    # 成功：把底层回执 JSON 解析后返回，供调用方读取 version 等字段
    return json.loads(result.stdout) if result.stdout else {}


def run_state_update(opportunity_id: str, stage: str, status: str):
    """通过统一状态脚本提交阶段状态。

    参数：
        opportunity_id —— 机会 ID，写入子进程环境变量 OPPORTUNITY_ID；
        stage          —— 阶段名（如 TEARDOWN / SIX_LENS / FIT_ASSESSMENT / INIT）；
        status         —— 目标状态（如 RUNNING / COMPLETED）。

    返回值：
        dict —— 解析底层 workflow_state.py stdout 得到的 JSON；stdout 为空时返回 {}。

    异常与退出码：
        底层非零退出 -> 打印 ERROR + stdout/stderr 后 sys.exit(result.returncode)。
        （与 run_evidence_ledger_update 不同：此处透传底层退出码，保留状态机语义。）

    为什么这么设计：
        统一走 workflow_state.py 这一个状态入口，保证状态迁移规则（合法性、幂等）只有
        一处实现；用 RUNNING→COMPLETED 包裹真正的台账写入，使阶段状态与数据落盘对齐。
    """
    cmd = [
        sys.executable,
        os.path.join(SCRIPTS_DIR, 'workflow_state.py'),
        'update', '--stage', stage, '--status', status
    ]
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = opportunity_id
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        # 原样透传底层退出码，让状态迁移失败能向上传播
        print(f"ERROR: workflow_state.py {stage}/{status} 失败", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout) if result.stdout else {}


def run_fit_scorer(scores: dict) -> dict:
    """调用 fit_scorer.py 计算总分和 Gate。

    参数：
        scores —— dict，8 个维度（market_certainty/user_demand/category_validation/
                  ai_value/org_capability/quick_launch/business_path/cost_risk）
                  的原始 0-10 分，经 stdin 传给底层脚本。

    返回值：
        dict —— fit_scorer.py 的计算结果，含 total_score、gate、gate_description、
                calculation_detail 等。

    异常与退出码：
        底层非零退出 -> 打印 ERROR + STDERR 后 sys.exit(1)。
        解析失败会抛 json 异常（未捕获，交由上层直接失败）。

    为什么这么设计：
        加权总分与 Gate 判定是确定性算法，必须由脚本算而非模型拍；
        把原始评分喂进去、拿回算好的总分与 Gate，模型只负责给分，杜绝手算差异与篡改。
    """
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'fit_scorer.py')]
    input_json = json.dumps(scores, ensure_ascii=False)
    
    result = subprocess.run(cmd, input=input_json, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"ERROR: fit_scorer.py 失败", file=sys.stderr)
        print(f"STDERR: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    
    return json.loads(result.stdout)


def load_payload(inline, file_path, name: str):
    """读取 JSON 参数：优先文件，其次内联。

    参数：
        inline    —— 内联 JSON 字符串（来自 --evidence / --scores 等），可为 None；
        file_path —— 文件路径（来自 --*-file），若提供则优先；
        name      —— 参数名，仅用于错误信息（如 "evidence" / "scores"）。

    返回值：
        object —— 解析出的 Python 对象（list/dict/None 等，取决于 JSON 内容）。

    异常与退出码：
        指定文件不存在 -> 打印 ERROR 后 sys.exit(1)；
        文件/内联 JSON 解析失败 -> 打印 ERROR 后 sys.exit(1)。

    为什么这么设计：
        大 payload 一律走文件，避免经过模型上下文与命令行转义：
        - 用 utf-8-sig 读文件，容忍 Windows 工具写入的 BOM（否则 json.load 会报错）；
        - inline 缺省按 "null" 解析，保证“什么都不传”也能得到一个合法 JSON 值，
          而不是直接崩溃。
    """
    if file_path:
        if not os.path.exists(file_path):
            print(f"ERROR: {name} 文件不存在: {file_path}", file=sys.stderr)
            sys.exit(1)
        try:
            # utf-8-sig 容忍 Windows 工具写入的 BOM
            with open(file_path, "r", encoding="utf-8-sig") as handle:
                return json.load(handle)
        except json.JSONDecodeError as e:
            print(f"ERROR: {name} 文件 JSON 解析失败: {file_path}: {e}", file=sys.stderr)
            sys.exit(1)
    # 无文件时回退内联；inline 为 None 时按 "null" 解析，得到 None
    try:
        return json.loads(inline if inline is not None else "null")
    except json.JSONDecodeError as e:
        print(f"ERROR: {name} JSON 解析失败: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_write_teardown(args):
    """写入单品拆解阶段的证据（对应命令 innovation:ledger write-teardown）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id            : 机会 ID（必填）→ 各子进程环境变量 OPPORTUNITY_ID；
                    --evidence      : 拆解证据列表 JSON 数组（默认 '[]'）；
                    --evidence-file : 证据文件路径，存在时优先于 --evidence（大 payload 推荐）。

    返回值：无（成功时把 {"status":"ok","stage":"TEARDOWN","evidence_count":N} 打到 stdout）。

    异常与退出码：
        evidence 非数组 / 空数组 -> 打印 ERROR 后 sys.exit(1)；
        load_payload 失败（文件缺失/JSON 非法）-> sys.exit(1)；
        底层台账/状态脚本失败 -> 由被调函数各自退出码（多为 1 或透传）终止。

    为什么这么设计：
        - TEARDOWN 至少要有一条证据，否则“拆解”无意义，故空数组直接拒绝；
        - 用 RUNNING → 写台账 → COMPLETED 三步包裹，保证状态机与数据落盘同步推进；
        - 只写证据、不写结论：TEARDOWN 阶段产出的是素材，结论留给 SIX_LENS 阶段。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:ledger write-teardown',
        description='写入单品情报拆解阶段的结构化证据',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
    python3 scripts/ops.py innovation:ledger write-teardown --id demo --evidence-file evidence.json
        '''
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--evidence', default='[]', help='拆解证据列表 JSON 数组')
    parser.add_argument('--evidence-file', dest='evidence_file', help='证据文件路径（大 payload 推荐，优先于 --evidence）')

    parsed = parser.parse_args(args)

    # 优先读文件（大 payload 不经模型上下文/命令行转义）
    evidence = load_payload(parsed.evidence, parsed.evidence_file, "evidence")

    # 结构校验：必须是数组，否则底层无法逐条归一化
    if not isinstance(evidence, list):
        print("ERROR: evidence 必须是 JSON 数组", file=sys.stderr)
        sys.exit(1)

    # 业务校验：拆解阶段空证据无意义，直接拒绝
    if not evidence:
        print("ERROR: 拆解阶段至少需要提供一条证据", file=sys.stderr)
        sys.exit(1)

    # 状态机包裹：进入 RUNNING → 写台账 → 置 COMPLETED
    run_state_update(parsed.id, 'TEARDOWN', 'RUNNING')
    run_evidence_ledger_update(parsed.id, 'TEARDOWN', {"evidence": evidence})
    run_state_update(parsed.id, 'TEARDOWN', 'COMPLETED')

    # 回执：告知写入条数，方便主 Agent 确认
    print(json.dumps({
        "status": "ok",
        "stage": "TEARDOWN",
        "evidence_count": len(evidence)
    }, ensure_ascii=False, indent=2))


def cmd_write_six_lens(args):
    """写入六看结论（对应命令 innovation:ledger write-six-lens）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id             : 机会 ID（必填）；
                    --conclusion     : 六看总体结论（必填），仅 YES/CONDITIONAL/WATCH/NO；
                    --evidence       : 证据列表 JSON 数组（默认 '[]'）；
                    --tasks          : 补证任务列表 JSON 数组（默认 '[]'）；
                    --dimensions     : 各维度详情 JSON（默认 '{}'）；
                    --evidence-file  : 证据文件路径，优先于 --evidence；
                    --tasks-file     : 补证任务文件路径，优先于 --tasks；
                    --dimensions-file: 维度详情文件路径，优先于 --dimensions。

    返回值：无（成功时输出 {"status":"ok","stage":"SIX_LENS","conclusion":...,"evidence_count":N,"tasks_count":M}）。

    异常与退出码：
        argparse 缺 --conclusion 或结论不在 choices 内 -> exit(2)；
        load_payload 失败 -> sys.exit(1)；
        底层脚本失败 -> 各自退出码终止。

    为什么这么设计：
        - conclusion 用 choices 锁死四种枚举，避免模型自由生成结论措辞导致下游不可判定；
        - 把 dimensions 打散后并入 conclusion（{overall: conclusion, **dimensions}）再交给
          evidence_ledger.py，是让“结论 + 六维详情”落到六看固定写入路径的统一格式；
        - 三个大 payload（证据/补证任务/维度详情）都支持 --*-file，典型用法是把
          task merge 產出的 merged-evidence/dimensions/tasks 文件直接喂进来，全程不过模型上下文。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:ledger write-six-lens',
        description='写入六看压力测试结论',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
    python3 scripts/ops.py innovation:ledger write-six-lens --id demo --conclusion CONDITIONAL \\
        --evidence-file runtime/tasks/demo/SIX_LENS/merged-evidence.json \\
        --dimensions-file runtime/tasks/demo/SIX_LENS/merged-dimensions.json \\
        --tasks-file runtime/tasks/demo/SIX_LENS/merged-tasks.json
        '''
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--conclusion', required=True,
                        choices=['YES', 'CONDITIONAL', 'WATCH', 'NO'],
                        help='六看总体结论')
    parser.add_argument('--evidence', default='[]',
                        help='证据列表 JSON 数组')
    parser.add_argument('--tasks', default='[]',
                        help='补证任务列表 JSON 数组')
    parser.add_argument('--dimensions', default='{}',
                        help='各维度详情 JSON（可选）')
    parser.add_argument('--evidence-file', dest='evidence_file', help='证据文件路径（优先于 --evidence）')
    parser.add_argument('--tasks-file', dest='tasks_file', help='补证任务文件路径（优先于 --tasks）')
    parser.add_argument('--dimensions-file', dest='dimensions_file', help='维度详情文件路径（优先于 --dimensions）')

    parsed = parser.parse_args(args)

    # 三个 payload 各自“文件优先、内联兜底”
    evidence = load_payload(parsed.evidence, parsed.evidence_file, "evidence")
    tasks = load_payload(parsed.tasks, parsed.tasks_file, "tasks")
    dimensions = load_payload(parsed.dimensions, parsed.dimensions_file, "dimensions")
    
    # 构造 evidence_ledger.py 需要的数据格式
    # overall 固定放结论，维度详情用 **dimensions 展开合并；evidence/evidence_tasks 分别落位
    data = {
        "conclusion": {
            "overall": parsed.conclusion,
            **dimensions
        },
        "evidence": evidence,
        "evidence_tasks": tasks
    }
    
    # 状态机包裹：SIX_LENS 置 RUNNING → 写台账 → 置 COMPLETED
    run_state_update(parsed.id, 'SIX_LENS', 'RUNNING')
    result = run_evidence_ledger_update(parsed.id, 'SIX_LENS', data)
    run_state_update(parsed.id, 'SIX_LENS', 'COMPLETED')
    
    # 回执：回显结论与证据/补证任务条数，便于核对
    print(json.dumps({
        "status": "ok",
        "stage": "SIX_LENS",
        "conclusion": parsed.conclusion,
        "evidence_count": len(evidence),
        "tasks_count": len(tasks)
    }, ensure_ascii=False, indent=2))


def cmd_write_fit(args):
    """写入适配度评分（自动计算总分和 Gate）（对应命令 innovation:ledger write-fit）。

    核心流程：
    1. 接收 8 维原始评分
    2. 调用 fit_scorer.py 计算加权总分和 Gate
    3. 将完整结果写入 evidence_ledger

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id           : 机会 ID（必填）；
                    --scores       : 8 维原始评分 JSON（与 --scores-file 二选一）；
                    --scores-file  : 评分文件路径，优先于 --scores；
                    --evidence     : 补充证据 JSON 数组（默认 '[]'）；
                    --evidence-file: 证据文件路径，优先于 --evidence。

    返回值：无（成功时输出含 total_score/gate/gate_description/dimension_scores 的 JSON）。

    异常与退出码：
        既无 --scores 也无 --scores-file -> sys.exit(1)；
        缺必填字段 / 分数越界 / 与子任务结果不一致 -> sys.exit(1)；
        load_payload 失败 / fit_scorer.py 失败 / 台账或状态脚本失败 -> sys.exit(1)。

    为什么这么设计（本函数的防篡改是交接重点）：
        - 分数只允许命令层做“结构性校验”（字段齐全、范围 0-10），真正的加权与 Gate 判定
          交给 fit_scorer.py，模型无法自行“算”出结论；
        - 强制校验 FIT_ASSESSMENT 子任务 manifest 存在且 completed、且其 result.scores 与本次
          传入评分一致（容差 0.01），阻断主 Agent 绕过子任务自行编造评分；
        - 用 RUNNING → 写台账 → COMPLETED 包裹，保持状态与数据同步。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:ledger write-fit',
        description='写入适配度评分（自动计算总分和 Gate）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
必填字段（8 维，每维 0-10 分）:
    market_certainty      市场/趋势确定性
    user_demand           用户需求强度
    category_validation   品类/竞品验证程度
    ai_value              AI 价值必要性
    org_capability     组织能力匹配度
    quick_launch          快速落地可行性
    business_path         商业转化路径
    cost_risk             成本与风险可控性

示例:
    python3 scripts/ops.py innovation:ledger write-fit \\
        --id ai-pet-translator-20260922 \\
        --scores '{"market_certainty":4,"user_demand":5,"category_validation":5,"ai_value":4,"org_capability":3,"quick_launch":5,"business_path":4,"cost_risk":4}'
        '''
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--scores',
                        help='8 维原始评分 JSON，格式: {"market_certainty":N,...}；与 --scores-file 二选一')
    parser.add_argument('--scores-file', dest='scores_file', help='评分文件路径（优先于 --scores）')
    parser.add_argument('--evidence', default='[]',
                        help='补充证据 JSON 数组（可选）')
    parser.add_argument('--evidence-file', dest='evidence_file', help='证据文件路径（优先于 --evidence）')

    parsed = parser.parse_args(args)

    # 评分来源至少给一个：文件或内联，否则无法评估
    if not parsed.scores and not parsed.scores_file:
        print("ERROR: 必须提供 --scores 或 --scores-file", file=sys.stderr)
        sys.exit(1)

    scores = load_payload(parsed.scores, parsed.scores_file, "scores")
    
    # 校验必填字段
    # 固定 8 维清单：缺任何一维都无法算出可比的总分，直接拒绝
    required_fields = [
        'market_certainty', 'user_demand', 'category_validation', 'ai_value',
        'org_capability', 'quick_launch', 'business_path', 'cost_risk'
    ]
    missing = [f for f in required_fields if f not in scores]
    if missing:
        print(f"ERROR: scores 缺少必填字段: {', '.join(missing)}", file=sys.stderr)
        print(f"完整字段: {', '.join(required_fields)}", file=sys.stderr)
        print(f"用法: python3 scripts/ops.py innovation:ledger write-fit --help", file=sys.stderr)
        sys.exit(1)
    
    # 校验分数范围
    # 每维必须是数字且落在 [0,10]，范围外一律拒绝（防止 100 制/负分混入）
    for field, value in scores.items():
        if not isinstance(value, (int, float)) or value < 0 or value > 10:
            print(f"ERROR: {field} 分数必须在 0-10 之间，当前值: {value}", file=sys.stderr)
            sys.exit(1)
    
    # 验证子任务完成状态（防止主 Agent 编造评分）
    # 该阶段必须先派发 fit-assessment 子任务并落盘 manifest，否则拒绝写入
    task_manifest_path = f"runtime/tasks/{parsed.id}/FIT_ASSESSMENT/manifest.json"
    if not os.path.exists(task_manifest_path):
        print(f"ERROR: 未找到适配度评估任务记录: {task_manifest_path}", file=sys.stderr)
        print("必须先派发 fit-assessment 子任务并等待完成，才能调用 write-fit", file=sys.stderr)
        sys.exit(1)
    
    with open(task_manifest_path, encoding='utf-8') as f:
        task_manifest = json.load(f)
    
    # 检查子任务状态
    # 子任务必须已 completed；未完成说明结果还没回齐，禁止提前落库
    if task_manifest.get('status') != 'completed':
        print(f"ERROR: 适配度评估子任务未完成，当前状态: {task_manifest.get('status')}", file=sys.stderr)
        print("必须等待子任务完成事件到达后，才能调用 write-fit", file=sys.stderr)
        sys.exit(1)
    
    # 检查子任务结果是否包含评分
    # 子任务结果里必须带 scores，否则没有可比对的基准
    task_result = task_manifest.get('result', {})
    if not task_result.get('scores'):
        print(f"ERROR: 适配度评估子任务结果缺少评分数据", file=sys.stderr)
        print("子任务必须返回包含 scores 字段的结果", file=sys.stderr)
        sys.exit(1)
    
    # 验证传入的评分与子任务结果一致（防止主 Agent 篡改）
    # 以子任务结果为唯一基准：逐维比对，缺失或偏差 >0.01 即判定被篡改并拒绝
    expected_scores = task_result['scores']
    for field in required_fields:
        if field not in expected_scores:
            print(f"ERROR: 子任务结果缺少 {field} 评分", file=sys.stderr)
            sys.exit(1)
        if abs(scores[field] - expected_scores[field]) > 0.01:
            print(f"ERROR: {field} 评分与子任务结果不一致", file=sys.stderr)
            print(f"  传入值: {scores[field]}, 子任务结果: {expected_scores[field]}", file=sys.stderr)
            print("必须使用子任务返回的评分，不得篡改", file=sys.stderr)
            sys.exit(1)
    
    # 调用 fit_scorer.py 计算总分
    # 提示信息走 stderr，不污染 stdout 的结构化结果
    print("正在计算总分和 Gate...", file=sys.stderr)
    scorer_result = run_fit_scorer(scores)
    
    # 构造 evidence_ledger.py 需要的数据
    # total_score/gate 直接采用脚本算出的结果；dimension_scores 回填原始分以留痕
    ledger_data = {
        "total_score": scorer_result["total_score"],
        "gate": scorer_result["gate"],
        "gate_description": scorer_result.get("gate_description", ""),
        "dimension_scores": scores,
        "calculation_detail": scorer_result.get("calculation_detail", {}),
        "evidence": load_payload(parsed.evidence, parsed.evidence_file, "evidence")
    }
    
    # 写入台账
    # 状态机包裹：FIT_ASSESSMENT 置 RUNNING → 写台账 → 置 COMPLETED
    run_state_update(parsed.id, 'FIT_ASSESSMENT', 'RUNNING')
    result = run_evidence_ledger_update(parsed.id, 'FIT_ASSESSMENT', ledger_data)
    run_state_update(parsed.id, 'FIT_ASSESSMENT', 'COMPLETED')
    
    # 输出结果
    # 回执里把总分、Gate 一并给出，主 Agent 无需再读台账即可判断走向
    output = {
        "status": "ok",
        "stage": "FIT_ASSESSMENT",
        "total_score": scorer_result["total_score"],
        "gate": scorer_result["gate"],
        "gate_description": scorer_result.get("gate_description", ""),
        "dimension_scores": scores
    }
    
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_init(args):
    """初始化证据台账（对应命令 innovation:ledger init）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id         : 机会 ID（必填）→ 环境变量 OPPORTUNITY_ID；
                    --product    : 产品名称（必填）→ 机会定义里的 product_name；
                    --definition : 产品定义（可选，默认空串）；
                    --user       : 目标用户（可选，默认空串）。

    返回值：无（成功时转发底层 evidence_ledger.py init 的 stdout，再置 INIT 状态）。

    异常与退出码：
        底层 init 非零退出 -> 打印 ERROR + STDERR 后 sys.exit(1)。
        （注意：此处统一退 1，不比底层更细。）

    为什么这么设计：
        - init 只暴露最必要的身份字段（产品名/定义/用户），其余身份项由底层补默认值，
          保持命令面简洁；
        - 建台账（init）后紧接着把 INIT 阶段置 RUNNING→COMPLETED，使“台账已建”与
          “INIT 阶段完成”两个事实一致对外可见；
        - 机会 ID 依然走环境变量，与其它底层脚本调用约定统一。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:ledger init',
        description='初始化证据台账'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--product', required=True, help='产品名称')
    parser.add_argument('--definition', default='', help='产品定义')
    parser.add_argument('--user', default='', help='目标用户')
    
    parsed = parser.parse_args(args)
    
    # 组装底层需要的 intake 数据（键名与 evidence_ledger.init_ledger 读取的字段对齐）
    init_data = {
        "product_name": parsed.product,
        "opportunity_definition": parsed.definition,
        "target_user": parsed.user
    }
    
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'evidence_ledger.py'), 'init']
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    
    # 经 stdin 把机会定义投喂给底层 init
    input_json = json.dumps(init_data, ensure_ascii=False)
    result = subprocess.run(cmd, env=env, input=input_json, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"ERROR: 初始化台账失败", file=sys.stderr)
        print(f"STDERR: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    
    # 先把底层成功回执原样转出，再推进 INIT 阶段状态
    print(result.stdout, end='')
    run_state_update(parsed.id, 'INIT', 'RUNNING')
    run_state_update(parsed.id, 'INIT', 'COMPLETED')


def cmd_read(args):
    """读取证据台账（对应命令 innovation:ledger read）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id : 机会 ID（必填）→ 环境变量 OPPORTUNITY_ID。

    返回值：无（把底层 evidence_ledger.py get 的完整台账 JSON 打到 stdout）。

    异常与退出码：
        底层非零退出 -> 转发 stderr 并 sys.exit(result.returncode)（透传，不吞码）。

    为什么这么设计：
        只读操作也透传底层退出码：台账不存在/损坏时底层非零退出，主 Agent 需要据此
        判断是“没有这个台账”还是“拿到了（可能为空的）结果”。
    """
    parser = argparse.ArgumentParser(
        prog='ops.py innovation:ledger read',
        description='读取证据台账'
    )
    parser.add_argument('--id', required=True, help='机会 ID')
    
    parsed = parser.parse_args(args)
    
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, 'evidence_ledger.py'), 'get']
    env = os.environ.copy()
    env['OPPORTUNITY_ID'] = parsed.id
    
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(result.stdout, end='')
    if result.returncode != 0:
        # 读操作透传底层退出码（与写入路径统一退 1 的约定不同）
        print(result.stderr, end='', file=sys.stderr)
        sys.exit(result.returncode)


def register_commands(commands: dict):
    """注册台账命令。

    参数：
        commands —— ops.py 传入的命名空间局部 registry；此处登记 5 条本地 key，
                    前缀化后为 innovation:ledger.init / write-teardown / write-six-lens /
                    write-fit / read。

    返回值：无。
    """
    commands['ledger.init'] = cmd_init
    commands['ledger.write-teardown'] = cmd_write_teardown
    commands['ledger.write-six-lens'] = cmd_write_six_lens
    commands['ledger.write-fit'] = cmd_write_fit
    commands['ledger.read'] = cmd_read
