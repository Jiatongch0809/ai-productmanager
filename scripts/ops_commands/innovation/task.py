#!/usr/bin/env python3
"""分片任务命令封装 — 支持大阶段任务拆片与增量落盘

命令：
    innovation:task init   --id ID --stage SIX_LENS [--parts market,user,...]
    innovation:task part   --id ID --stage SIX_LENS --part market --file PATH
    innovation:task status --id ID [--stage SIX_LENS]
    innovation:task merge  --id ID --stage SIX_LENS [--allow-partial]

核心设计：
    - 子 Agent 每完成一个分片立即落盘，上下文丢失最多损失一个分片
    - merge 在脚本内完成合并，大块证据不经过模型上下文
    - parts/ 是暂存区，正式台账仍只能由主 Agent 写入

本文件在整体机制里的位置（交接重点）：
    这是与「分片（part）机制」直接相关的命令层，支撑大阶段（如 SIX_LENS）的并行/分段
    执行。核心思想：
        - 主 Agent 是唯一的工作流写入者，子 Agent 只往 runtime/tasks/<id>/<STAGE>/ 这个
          暂存区写分片（parts/*.json + manifest.json），不直接改台账；
        - 子 Agent 每完成一维就立刻 task part 落盘，因此“上下文丢失最多损失一个分片”；
        - task merge 在脚本内把分片合并成可落库的 payload（按 (fact, source) 去重），
          大块证据全程不过模型上下文，既省 token 又避免模型改动证据。

四个子命令职责：
    init   —— 登记 manifest（幂等：已存在的分片状态、尤其 DONE，不被重置）；
    part   —— 校验并写入单个分片，更新 manifest 中该分片为 DONE；
    status —— 查看分片进度（done / missing），单阶段或全阶段；
    merge  —— 汇总已 DONE 分片，缺片默认拒绝，去重后产出 merged*.json 供上层落库。

为什么 merge 要按 (fact, source) 去重：
    多维分片可能各自收录同一个来源的同一事实，直接拼接会产生重复证据、放大计数，
    影响后续“可核查证据”判定与评分。以 (fact, source) 作为去重键，保留首次出现，
    实现可复现、稳定的合并结果（同一批分片无论合并顺序如何，结果一致）。
"""
import argparse     # 各子命令参数解析
import json         # 读分片/清单 JSON、输出结果
import os           # 路径拼接、目录创建、存在性判断
import sys          # 控制退出码、写 stderr

# 由本文件所在目录上溯三层（innovation -> ops_commands -> scripts）后 insert，
# 以便 import scripts/ 下的契约模块（contracts）——并集常量与原子写工具都在那里
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from contracts import (
    SIX_LENS_DIMENSIONS, EVIDENCE_GRADES,
    atomic_write_json, now_iso, validate_evidence,
)

# 分片暂存根目录：runtime/tasks/<opportunity_id>/<STAGE>/
TASK_DIR = "runtime/tasks"


def fail(message: str):
    """统一的失败出口：把错误信息以 JSON 打到 stdout，再以退出码 1 结束。

    参数：
        message —— 错误描述文本。

    返回值：无（永不返回）。

    异常与退出码：固定 sys.exit(1)。

    为什么这么设计：
        本模块输出统一为 JSON（stdout），错误也用同构的 {"error": ...} 打印，方便上层
        稳定解析；退出码 1 是给主 Agent 的确定性失败信号（分片/合并出错必须停下）。
    """
    print(json.dumps({"error": message}, ensure_ascii=False))
    sys.exit(1)


def load_json_file(path: str):
    """读取一个 JSON 文件，失败即 fail。

    参数：
        path —— 待读取文件的路径。

    返回值：解析出的 Python 对象。

    异常与退出码：
        文件不存在 -> fail（退出 1）；
        JSON 解析失败 -> fail（退出 1）。

    为什么这么设计：
        用 utf-8-sig 打开，容忍 Windows 工具写入的 BOM（否则 json.load 会因 BOM 报错），
        与 ledger.py 的 load_payload 保持一致；不存在/损坏一律走 fail 收敛为退出码 1。
    """
    if not os.path.exists(path):
        fail(f"文件不存在: {path}")
    try:
        # utf-8-sig 容忍 Windows 工具写入的 BOM
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except json.JSONDecodeError as error:
        fail(f"JSON 解析失败: {path}: {error}")


def task_dir(opportunity_id: str, stage: str) -> str:
    """返回某机会某阶段的分片目录路径。

    参数：
        opportunity_id —— 机会 ID；
        stage          —— 阶段名（会统一大写，保证目录名规范）。

    返回值：str，形如 runtime/tasks/<id>/<STAGE>。
    """
    return os.path.join(TASK_DIR, opportunity_id, stage.upper())


def manifest_path(opportunity_id: str, stage: str) -> str:
    """返回该机会该阶段的分片清单文件路径（<stage_dir>/manifest.json）。"""
    return os.path.join(task_dir(opportunity_id, stage), "manifest.json")


def load_manifest(opportunity_id: str, stage: str):
    """读取分片清单，不存在则给出“先 init”的提示并失败。

    参数：
        opportunity_id —— 机会 ID；
        stage          —— 阶段名。

    返回值：manifest 字典。

    异常与退出码：清单不存在 -> fail（退出 1）。

    为什么这么设计：
        分片机制要求先 task init 登记，再逐个 task part；没登记直接操作会让人摸不着头脑，
        因此这里明确提示正确的下一步命令，而不是抛一个原始的文件不存在错误。
    """
    path = manifest_path(opportunity_id, stage)
    if not os.path.exists(path):
        fail(f"分片清单不存在，请先执行: innovation:task init --id {opportunity_id} --stage {stage}")
    return load_json_file(path)


def save_manifest(opportunity_id: str, stage: str, manifest: dict):
    """保存分片清单（刷新 updated_at 后原子写入）。

    参数：
        opportunity_id —— 机会 ID；
        stage          —— 阶段名；
        manifest       —— 待保存的清单字典（就地更新其 updated_at）。

    返回值：无。

    为什么这么设计：
        每次保存前刷新 updated_at，便于判断清单新鲜度；用 atomic_write_json 避免
        写一半崩溃留下损坏清单（清单是分片进度的依据）。
    """
    manifest["updated_at"] = now_iso()
    atomic_write_json(manifest_path(opportunity_id, stage), manifest)


def parse_parts(raw: str, stage: str):
    """解析分片名列表，并对 SIX_LENS 阶段做维度白名单校验。

    参数：
        raw   —— 逗号分隔的分片名字符串（来自 --parts），可为空串；
        stage —— 阶段名（用于决定默认分片与是否做维度校验）。

    返回值：
        list[str] —— 分片名列表。若 raw 为空且阶段是 SIX_LENS，返回 SIX_LENS_DIMENSIONS
                     全部六维作为默认分片。

    异常与退出码：
        raw 为空且阶段不是 SIX_LENS（该阶段无默认分片）-> fail；
        SIX_LENS 阶段出现非法维度 -> fail。

    为什么这么设计：
        - 六看有固定六维，允许 --parts 缺省即取全六维，减少手工输入；
        - 其它阶段没有天然默认分片，必须显式指定，避免默默产出一个空分片集合；
        - 对 SIX_LENS 做白名单校验，防止出现拼错/未定义的维度名导致后续 merge 混乱。
    """
    # 去空白、丢弃空项，得到规范化分片名列表
    parts = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not parts:
        # 未指定分片：六看阶段默认取全部六维
        if stage.upper() == "SIX_LENS":
            return list(SIX_LENS_DIMENSIONS)
        fail(f"阶段 {stage} 没有默认分片，请用 --parts 指定")
    if stage.upper() == "SIX_LENS":
        # 六看维度必须落在 SIX_LENS_DIMENSIONS 白名单内
        invalid = [part for part in parts if part not in SIX_LENS_DIMENSIONS]
        if invalid:
            fail(f"无效的六看维度: {invalid}，允许: {SIX_LENS_DIMENSIONS}")
    return parts


def cmd_init(args):
    """初始化分片清单（已存在时保留已完成分片）（对应命令 innovation:task init）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id    : 机会 ID（必填）；
                    --stage : 阶段名（必填），例如 SIX_LENS；
                    --parts : 分片列表，逗号分隔；六看阶段缺省取六维。

    返回值：无（输出 {"status":"initialized",...,"parts":{名:状态},"preserved_done":[...]}）。

    异常与退出码：parse_parts / load_json_file 失败 -> 退出 1。

    为什么这么设计（init 的幂等是交接重点）：
        - 重复执行 init 不重置已完成分片：已存在的分片默认不动，DONE 状态会被“保留”
          （记录进 preserved_done），只补登记缺失的分片为 PENDING；
        - 这样才能支持“任务中断后重跑 init 继续做”，不会被一次重复调用清空进度；
        - 首次创建时同时建好 parts/ 子目录与 manifest 骨架，后续 task part 逐步填充。
    """
    parser = argparse.ArgumentParser(prog='ops.py innovation:task init', description='初始化分片清单')
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--stage', required=True, help='阶段名，例如 SIX_LENS')
    parser.add_argument('--parts', default='', help='分片列表，逗号分隔；六看默认六维')
    parsed = parser.parse_args(args)

    parts = parse_parts(parsed.parts, parsed.stage)
    path = manifest_path(parsed.id, parsed.stage)
    preserved = []

    if os.path.exists(path):
        # 清单已存在：幂等更新——只补登记缺失分片，已 DONE 的分片记为保留、不重置
        manifest = load_json_file(path)
        for part in parts:
            if part not in manifest.get("parts", {}):
                manifest["parts"][part] = {"status": "PENDING", "file": None, "evidence_count": 0, "updated_at": None}
            elif manifest["parts"][part].get("status") == "DONE":
                # 已完成分片保留，避免重复 init 把进度清零
                preserved.append(part)
    else:
        # 首次创建：建 parts/ 子目录，并生成含全部分片的 PENDING 骨架
        os.makedirs(os.path.join(task_dir(parsed.id, parsed.stage), "parts"), exist_ok=True)
        manifest = {
            "schema_version": 1,
            "opportunity_id": parsed.id,
            "stage": parsed.stage.upper(),
            "created_at": now_iso(),
            "parts": {part: {"status": "PENDING", "file": None, "evidence_count": 0, "updated_at": None} for part in parts},
        }

    save_manifest(parsed.id, parsed.stage, manifest)
    # 回执：列出各分片当前状态与本次被保留的已完成分片，便于确认幂等效果
    print(json.dumps({
        "status": "initialized",
        "opportunity_id": parsed.id,
        "stage": parsed.stage.upper(),
        "parts": {name: info["status"] for name, info in manifest["parts"].items()},
        "preserved_done": preserved,
    }, ensure_ascii=False, indent=2))


def validate_part(part_name: str, payload: dict):
    """校验分片内容：维度匹配、证据字段完整、证据等级合法。

    参数：
        part_name —— 命令行指定的分片名（如 market）；
        payload   —— 分片文件的 JSON 内容（应为对象）。

    返回值：
        dict —— 校验通过（必要时补写 dimension）后的 payload。

    异常与退出码：
        payload 非对象 -> fail；
        文件内 dimension 与 --part 不一致 -> fail；
        evidence 非数组 / 单条证据缺必填字段 / 等级非法 -> fail；
        既无 evidence 又无 conclusion -> fail。
        （均退出 1。）

    为什么这么设计：
        - 维度一致性校验：分片名与文件内 dimension 必须对得上，防止把 user 的内容塞进
          market 分片，造成后续 merge 的维度错位；
        - 证据必填字段校验（validate_evidence）：保证每条证据有 fact/source 等，merge 的
          (fact, source) 去重才有意义；
        - 证据等级白名单（EVIDENCE_GRADES）：统一 A/B/C 分级口径，为后续“可核查证据”判定打底；
        - 要求 conclusion 或 evidence 至少有一个：空分片毫无价值，直接拒绝。
    """
    if not isinstance(payload, dict):
        fail("分片内容必须是 JSON 对象")

    dimension = payload.get("dimension")
    if dimension is None:
        # 文件未自带维度：以命令行 --part 补写（方便子 Agent 少填一个字段）
        payload["dimension"] = part_name
    elif dimension != part_name:
        # 自带维度与命令行不一致：视为错位或误放，拒绝
        fail(f"分片维度不匹配: 文件内 dimension={dimension}，命令行 --part={part_name}")

    evidence = payload.get("evidence", [])
    if not isinstance(evidence, list):
        fail("evidence 必须是数组")
    for item in evidence:
        # grade 允许缺省；一旦提供就必须是白名单内的 A/B/C。
        # 先单独判等级，避免 validate_evidence 的通用必填校验把"等级非法"误报成"缺字段"。
        grade = item.get("grade")
        if grade is not None and grade not in EVIDENCE_GRADES:
            fail(f"证据等级非法: {grade}，允许: {EVIDENCE_GRADES}")
        # 每条证据都必须能通过契约层校验（含 fact/source 等必填字段）
        if not validate_evidence(item):
            fail(f"证据缺少必填字段(fact/source/date/grade/signal): {item}")

    # 分片至少要有结论或证据之一，否则对 merge 无贡献
    if not evidence and not payload.get("conclusion"):
        fail("分片至少需要 conclusion 或 evidence 之一")
    return payload


def cmd_part(args):
    """写入单个已完成分片（对应命令 innovation:task part）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id    : 机会 ID（必填）；
                    --stage : 阶段名（必填）；
                    --part  : 分片名（必填），例如 market；
                    --file  : 分片 JSON 文件路径（必填）。

    返回值：无（输出该分片结果与 done/missing 列表）。

    异常与退出码：
        未登记的分片 / 文件缺失或坏 / validate_part 校验失败 -> fail（退出 1）。

    为什么这么设计：
        - 必须在 manifest 里登记过才允许写入，防止写出“孤儿分片”让进度统计失真；
        - 写入即把该分片标为 DONE 并记录相对路径与证据条数，实现“完成一维立即落盘”，
          这样即便子 Agent 上下文丢失，最坏也只损失当前未落盘的那一维；
        - 回执同时列出 done 与 missing，让调用方一眼看到还差哪些分片。
    """
    parser = argparse.ArgumentParser(prog='ops.py innovation:task part', description='写入单个分片结果')
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--stage', required=True, help='阶段名')
    parser.add_argument('--part', required=True, help='分片名，例如 market')
    parser.add_argument('--file', required=True, help='分片 JSON 文件路径')
    parsed = parser.parse_args(args)

    manifest = load_manifest(parsed.id, parsed.stage)
    # 未入库的分片不允许写入：保证分片集合与清单严格一致
    if parsed.part not in manifest.get("parts", {}):
        fail(f"未登记的分片 {parsed.part}，清单内为: {list(manifest['parts'].keys())}")

    # 读取并校验分片内容（维度匹配、证据字段、等级等）
    payload = validate_part(parsed.part, load_json_file(parsed.file))

    # 分片固定落到 <stage_dir>/parts/<part>.json，相对路径记进 manifest
    relative = os.path.join("parts", f"{parsed.part}.json")
    atomic_write_json(os.path.join(task_dir(parsed.id, parsed.stage), relative), payload)

    # 更新该分片条目：状态 DONE + 相对路径 + 证据条数 + 时间戳
    entry = manifest["parts"][parsed.part]
    entry.update({
        "status": "DONE",
        "file": relative,
        "evidence_count": len(payload.get("evidence", [])),
        "updated_at": now_iso(),
    })
    save_manifest(parsed.id, parsed.stage, manifest)

    # 回执：算出已完成/缺失分片，反馈当前进度
    done = [name for name, info in manifest["parts"].items() if info["status"] == "DONE"]
    print(json.dumps({
        "status": "ok",
        "part": parsed.part,
        "evidence_count": entry["evidence_count"],
        "done": done,
        "missing": [name for name in manifest["parts"] if name not in done],
    }, ensure_ascii=False, indent=2))


def collect_status(opportunity_id: str, stage: str):
    """汇总某机会某阶段的分片进度（供 status 与 merge 复用）。

    参数：
        opportunity_id —— 机会 ID；
        stage          —— 阶段名。

    返回值：
        dict —— {"stage": 阶段名, "parts": 各分片条目, "done": [已完成名], "missing": [缺失名]}。

    异常与退出码：manifest 不存在 -> 由 load_manifest 触发 fail（退出 1）。

    为什么这么设计：
        把“已完成/缺失”的判定抽成一个函数，保证 status 展示与 merge 的完整性判断口径一致，
        避免两处各算一遍产生分歧。
    """
    manifest = load_manifest(opportunity_id, stage)
    done = [name for name, info in manifest["parts"].items() if info["status"] == "DONE"]
    return {
        "stage": manifest["stage"],
        "parts": manifest["parts"],
        "done": done,
        "missing": [name for name in manifest["parts"] if name not in done],
    }


def cmd_status(args):
    """查看分片进度（对应命令 innovation:task status）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id    : 机会 ID（必填）；
                    --stage : 阶段名（可选）；缺省时列出该机会的全部阶段。

    返回值：无（输出单阶段状态，或一次输出该机会下所有有 manifest 的阶段）。

    异常与退出码：
        指定 stage 但清单不存在 -> fail；
        未指定 stage 且该机会没有任何分片任务 -> fail（退出 1）。

    为什么这么设计：
        - 指定阶段时直接给出该阶段进度，便于精确核对；
        - 不指定阶段时遍历该机会目录下所有阶段，只汇总“确实存在 manifest”的阶段，
          方便一眼看全某次机会的分片全景；
        - 目录不存在说明该机会压根没派发过分片，明确报错而非返回空。
    """
    parser = argparse.ArgumentParser(prog='ops.py innovation:task status', description='查看分片进度')
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--stage', help='阶段名，缺省时列出该机会的全部阶段')
    parsed = parser.parse_args(args)

    if parsed.stage:
        # 指定阶段：只输出该阶段
        print(json.dumps(collect_status(parsed.id, parsed.stage), ensure_ascii=False, indent=2))
        return

    # 未指定阶段：遍历该机会的分片根目录，收集所有含 manifest 的阶段
    root = os.path.join(TASK_DIR, parsed.id)
    if not os.path.isdir(root):
        fail(f"该机会没有任何分片任务: {parsed.id}")
    output = {}
    for stage in sorted(os.listdir(root)):
        # 只收录真的有 manifest 的阶段目录，忽略空的/无关目录
        if os.path.exists(manifest_path(parsed.id, stage)):
            output[stage] = collect_status(parsed.id, stage)
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_merge(args):
    """合并分片为可落库的 payload（大块证据不经模型上下文）（对应命令 innovation:task merge）。

    参数：
        args —— ops.py 透传的剩余参数，经 argparse 解析为：
                    --id            : 机会 ID（必填）；
                    --stage         : 阶段名（必填）；
                    --allow-partial : 允许缺片合并（仅用于明确降级为观察）。

    返回值：无（输出合并摘要，并写出 4 个 merged*.json 文件）。

    异常与退出码：
        缺片且未加 --allow-partial -> fail（退出 1）；
        读取任一已 DONE 分片文件失败 -> fail。

    为什么这么设计（merge 的两点关键）：
        1) 缺片默认拒绝：分片不全时的合并结果不可信（可能刚好缺了关键维度），因此默认
           失败并要求补齐；只有显式 --allow-partial 才允许，且语义上仅用于“降级为观察”，
           避免把不完整数据误当完成品落库。
        2) 按 (fact, source) 去重：不同分片可能重复收录同一来源的同一事实，直接拼接会
           重复计数并污染后续评分；以 (fact, source) 为键保留首次出现，使合并结果稳定、
           可复现（与分片合并顺序无关）。
        另外，整个合并与去重都在脚本内完成，证据不进入模型上下文，既省 token 又防止
        模型改动原始证据。
    """
    parser = argparse.ArgumentParser(prog='ops.py innovation:task merge', description='合并分片结果')
    parser.add_argument('--id', required=True, help='机会 ID')
    parser.add_argument('--stage', required=True, help='阶段名')
    parser.add_argument('--allow-partial', action='store_true', help='允许缺片合并（仅用于明确降级为观察）')
    parsed = parser.parse_args(args)

    state = collect_status(parsed.id, parsed.stage)
    # 完整性闸门：缺片且未显式放行，直接拒绝，避免合并出不完整结果
    if state["missing"] and not parsed.allow_partial:
        fail(f"分片不完整，缺失: {state['missing']}；补齐后再合并，或显式使用 --allow-partial")

    directory = task_dir(parsed.id, parsed.stage)
    # 四个累加容器：维度详情 / 证据 / 补证任务 / 未知项
    dimensions, evidence, tasks, unknowns = {}, [], [], []

    for name, info in state["parts"].items():
        # 只合并状态为 DONE 的分片；PENDING/缺片直接跳过
        if info["status"] != "DONE":
            continue
        payload = load_json_file(os.path.join(directory, info["file"]))
        # 维度详情：剔除 evidence/dimension/evidence_tasks 这些“非详情”键，保留其余字段
        dimensions[name] = {key: value for key, value in payload.items()
                            if key not in ("evidence", "dimension", "evidence_tasks")}
        # 证据、补证任务、未知项分别汇集
        evidence.extend(payload.get("evidence", []))
        tasks.extend(payload.get("evidence_tasks", []))
        unknowns.extend(payload.get("unknowns", []))

    # 按 (fact, source) 去重，保留首次出现
    # 去重键取“事实+来源”二元组，使合并结果与分片处理顺序无关、可复现
    seen, deduped = set(), []
    for item in evidence:
        key = (item.get("fact"), item.get("source"))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    # 组装最终合并结果；missing_parts 如实记录本次合并时的缺失分片（便于审计）
    merged = {
        "opportunity_id": parsed.id,
        "stage": state["stage"],
        "merged_at": now_iso(),
        "dimensions": dimensions,
        "evidence": deduped,
        "evidence_tasks": list(dict.fromkeys(tasks)),
        "unknowns": list(dict.fromkeys(unknowns)),
        "missing_parts": state["missing"],
    }

    # 4 个输出文件：整体 merged.json + 三个可直接喂给 ledger 命令的分文件
    # （merged-evidence / merged-dimensions / merged-tasks 正是 write-six-lens 的 --*-file 输入）
    outputs = {
        "merged": os.path.join(directory, "merged.json"),
        "evidence": os.path.join(directory, "merged-evidence.json"),
        "dimensions": os.path.join(directory, "merged-dimensions.json"),
        "tasks": os.path.join(directory, "merged-tasks.json"),
    }
    atomic_write_json(outputs["merged"], merged)
    atomic_write_json(outputs["evidence"], deduped)
    atomic_write_json(outputs["dimensions"], dimensions)
    atomic_write_json(outputs["tasks"], merged["evidence_tasks"])

    # 回执：报告分片、缺失、去重后证据数、各等级计数、各维度结论、未知项/补证任务数与产物路径
    print(json.dumps({
        "status": "merged",
        "stage": state["stage"],
        "parts": state["done"],
        "missing_parts": state["missing"],
        "evidence_count": len(deduped),
        "evidence_grades": {grade: sum(1 for item in deduped if item.get("grade") == grade)
                            for grade in EVIDENCE_GRADES},
        "dimension_conclusions": {name: value.get("conclusion") for name, value in dimensions.items()},
        "unknowns_count": len(merged["unknowns"]),
        "evidence_tasks_count": len(merged["evidence_tasks"]),
        "files": outputs,
    }, ensure_ascii=False, indent=2))


def register_commands(commands: dict):
    """注册分片任务命令。

    参数：
        commands —— ops.py 传入的命名空间局部 registry；此处登记 4 条本地 key，
                    前缀化后为 innovation:task.init / part / status / merge。

    返回值：无。
    """
    commands['task.init'] = cmd_init
    commands['task.part'] = cmd_part
    commands['task.status'] = cmd_status
    commands['task.merge'] = cmd_merge