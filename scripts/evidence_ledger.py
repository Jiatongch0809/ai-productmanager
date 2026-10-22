#!/usr/bin/env python3
"""证据台账管理脚本 — 所有业务线复用

用法：
    OPPORTUNITY_ID=xxx python3 scripts/evidence_ledger.py init
    OPPORTUNITY_ID=xxx STAGE=SIX_LENS python3 scripts/evidence_ledger.py update
    OPPORTUNITY_ID=xxx python3 scripts/evidence_ledger.py get

环境变量：
    OPPORTUNITY_ID: 机会唯一标识
    STAGE: 阶段名称（update 时需要）

输入：
    init: 从 stdin 读取机会定义 JSON
    update: 从 stdin 读取阶段结果 JSON
    get: 无输入

输出：
    JSON 格式

本文件在整体机制里的位置（交接重点）：
    这是「证据台账」的底层确定性脚本，是整条创新工作流的事实源（single source of
    truth）。台账只允许主 Agent 通过命令层触发写入，子 Agent 只能往 runtime/tasks/
    写分片暂存，不能直接改台账。本脚本负责三件事：
      init   —— 建台账，把身份信息（产品名/定义/用户等）固定下来；
      update —— 按 stage（TEARDOWN/SIX_LENS/FIT_ASSESSMENT）把阶段结果写进固定字段，
                每次写入 version+1 并追加 version_history（含变更前后摘要）；
      get    —— 读出台账并补齐结构。

为什么把写入做成“固定路径 + 版本留痕”的脚本而不是让模型自由发挥：
    - 固定写入路径（如 fit_assessment.total_score、six_lens.conclusion）不给模型
      自由选择字段名的空间，避免同一语义被写到不同位置、下游读不到；
    - 每次 update 必 version+1 且追加 before/after 摘要，实现可追溯、可审计、可回滚；
    - 校验（stage 合法、opportunity_id 匹配、证据必填字段）都在脚本内完成，
      是「确定交给脚本」原则的体现——不确定的（比如结论怎么下）才交给 LLM。

注意：本脚本从 stdin 读取 JSON、以 stdout 输出 JSON，由命令层（ops_commands/innovation/
      ledger.py）负责管道与参数拼装，模型无需关心 stdin/stdout 细节。
"""
import json       # 读写台账 JSON、输出结果
import sys        # 读 stdin、控制退出码、向 stdout 输出
import os         # 目录创建、路径拼接、读取环境变量
import tempfile   # 注意：已 import 但当前代码未见直接使用（contracts 的原子写可能间接依赖）；交接时留意

# 添加 scripts 目录到路径
# 目的：保证能 import scripts/ 下的契约模块（contracts），不依赖调用方 cwd。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contracts import (
    STAGES, EVIDENCE_LEDGER_SCHEMA_VERSION,
    atomic_write_json, now_iso, ensure_ledger_structure,
    validate_evidence, normalize_evidence
)

# 台账落盘目录：runtime/evidence_ledger/<opportunity_id>.json（相对工作区根目录）
LEDGER_DIR = "runtime/evidence_ledger"
# 错误日志目录：runtime/logs/<opportunity_id>.log，便于事后排查
LOG_DIR = "runtime/logs"


def log_error(opportunity_id: str, error: str):
    """记录错误日志（追加写入，不进 stdout，避免污染给上层解析的 JSON 输出）。

    参数：
        opportunity_id —— 决定日志文件名 runtime/logs/<opportunity_id>.log；
        error          —— 要记录的原始错误信息（会被拼上 ISO 时间戳与前缀）。

    返回值：无。

    异常与退出码：无异常、不改退出码。日志写失败会直接冒出（此处不做兜底），
        因为日志是辅助手段，不影响主流程判定。

    为什么这么设计：
        本脚本用 stdout 输出结构化 JSON 给上层，错误对象不能混进 stdout；
        把诊断信息落进独立日志文件，既保留现场又不干扰机器解析。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{opportunity_id}.log")
    # 追加模式 + utf-8：多次调用累积成一行行带时间戳的日志
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"[{now_iso()}] ERROR evidence_ledger: {error}\n")


def init_ledger(opportunity_id: str, intake_data: dict):
    """初始化证据台账（动作=init，输入=从 stdin 读到的机会定义 JSON）。

    参数：
        opportunity_id —— 台账标识，同时决定文件名 runtime/evidence_ledger/<id>.json；
        intake_data    —— 机会定义字典，至少含 product_name；其余字段缺省填空串/默认值。

    返回值：无（成功时向 stdout 打印 {"status":"initialized","path":...}）。

    异常与退出码：
        缺少 product_name → 打印 error JSON 后 sys.exit(1)；
        原子写失败        → 记日志、打印 error JSON 后 sys.exit(1)。

    为什么这么设计：
        - product_name 是台账最核心的身份字段，缺失则整份台账无意义，直接拒绝；
        - 其余字段用 get(..., "") 兜底：台账结构一次建全，后续阶段只需按固定路径补写；
        - version 初始化为 1，并写第一条 version_history（"初始化台账"），
          保证版本链从第一天就可追溯；
        - 使用 atomic_write_json 而非直接 open(...,'w')：避免写到一半崩溃留下半截
          损坏台账（台账是唯一事实源，损坏代价极高）。
    """
    # 校验必填字段
    if not intake_data.get("product_name"):
        print(json.dumps({"error": "缺少必填字段: product_name"}, ensure_ascii=False))
        sys.exit(1)
    
    # 组装初始台账：身份信息 / 空的证据与阶段容器 / 版本历史首条
    ledger = {
        "schema_version": EVIDENCE_LEDGER_SCHEMA_VERSION,
        "opportunity_id": opportunity_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "version": 1,
        "identity": {
            # identity 字段都从 intake 映射而来，缺省以空串占位，保证结构完整
            "product_name": intake_data.get("product_name", ""),
            "product_definition": intake_data.get("opportunity_definition", ""),
            "target_user": intake_data.get("target_user", ""),
            "target_customer": intake_data.get("target_customer", ""),
            "core_scenario": intake_data.get("core_scenario", ""),
            "core_problem": intake_data.get("core_problem", ""),
            "target_price_range": intake_data.get("target_price_range", ""),
            "region": intake_data.get("region", "中国大陆"),
            "time_scope": intake_data.get("time_scope", "")
        },
        # 证据总库：所有阶段写入的证据都会归一化后追加到这里
        "evidence": [],
        # 六看阶段容器（conclusion / evidence_tasks 占位，等 SIX_LENS 阶段填充）
        "six_lens": {
            "conclusion": None,
            "evidence_tasks": [],
            "updated_at": None
        },
        # 适配度评估容器（等 FIT_ASSESSMENT 阶段按固定路径填充）
        "fit_assessment": {
            "total_score": None,
            "gate": None,
            "dimension_scores": {},
            "calculation_detail": {},
            "updated_at": None
        },
        "artifacts": [],
        # 版本历史：每条含 version/时间/变更说明/操作者，init 先写第一条
        "version_history": [
            {
                "version": 1,
                "timestamp": now_iso(),
                "change": "初始化台账",
                "changed_by": "init"
            }
        ]
    }
    
    # 文件名固定为 <opportunity_id>.json，ID 已在入口层校验过，无路径片段风险
    path = os.path.join(LEDGER_DIR, f"{opportunity_id}.json")
    os.makedirs(LEDGER_DIR, exist_ok=True)
    
    try:
        atomic_write_json(path, ledger)
    except Exception as e:
        # 写盘失败：既落日志也报错给上层，退出码 1 让主 Agent 必须停下来处理
        log_error(opportunity_id, f"台账初始化失败: {e}")
        print(json.dumps({"error": f"台账初始化失败: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 成功：回执里带上落盘路径，便于上层定位
    print(json.dumps({"status": "initialized", "path": path}, ensure_ascii=False))


def update_ledger(opportunity_id: str, stage: str, data: dict):
    """更新证据台账（动作=update，输入=从 stdin 读到的阶段结果 JSON）。

    参数：
        opportunity_id —— 台账标识；必须与已存在台账里的 opportunity_id 一致；
        stage          —— 阶段名（大小写不敏感，内部统一 upper()），
                          合法值仅 TEARDOWN / SIX_LENS / FIT_ASSESSMENT 三者；
        data           —— 阶段结果字典，内容随 stage 不同：
                          TEARDOWN      : {"evidence": [...]}
                          SIX_LENS      : {"conclusion": {...}, "evidence": [...], "evidence_tasks": [...]}
                          FIT_ASSESSMENT: {"total_score":N, "gate":"...", "dimension_scores":{}, ...}

    返回值：无（成功时 stdout 打印 {"status":"updated","version":..,"stage":..}）。

    异常与退出码：
        无效 stage / 台账不存在 / 台账损坏 / opportunity_id 不匹配 / 证据字段不全 /
        FIT_ASSESSMENT 缺 total_score 或 gate / 写盘失败 → 均 sys.exit(1)。

    严格校验：
        - opportunity_id 必须匹配；
        - stage 必须有效；
        - 证据必须有必填字段（走 validate_evidence）。

    为什么这么设计：
        - 每次写入都 version+1 并追加 version_history（含 before/after 摘要），
          形成不可篡改的变更链，便于审计和回溯；
        - 写入路径按 stage 固定（不接受模型指定路径/字段名），保证下游读取口径一致；
        - 校验全部在脚本内完成，模型无法绕过；退出码 1 逼主 Agent 停下处理失败。
    """
    # 校验 stage
    # 统一大写，容忍调用方传小写；只放行这三个能写台账的阶段
    stage = stage.upper()
    valid_stages = [STAGES["TEARDOWN"], STAGES["SIX_LENS"], STAGES["FIT_ASSESSMENT"]]
    if stage not in valid_stages:
        print(json.dumps({"error": f"无效的阶段: {stage}，允许: {valid_stages}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 台账必须已 init；不存在则拒绝，避免凭空创建一个不完整台账
    path = os.path.join(LEDGER_DIR, f"{opportunity_id}.json")
    if not os.path.exists(path):
        print(json.dumps({"error": f"台账不存在: {opportunity_id}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 读取台账
    try:
        with open(path, 'r', encoding='utf-8') as f:
            ledger = json.load(f)
    except json.JSONDecodeError as e:
        # 台账是事实源，损坏必须显式失败而不是自动重建
        log_error(opportunity_id, f"台账 JSON 解析失败: {e}")
        print(json.dumps({"error": f"台账文件损坏: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 确保结构完整
    # 兼容旧版本/缺字段的台账：补齐结构后再写，避免 KeyError
    ledger = ensure_ledger_structure(ledger)
    
    # 校验 opportunity_id
    # 双保险：防止把 A 机会的阶段结果误写到 B 机会的台账里
    if ledger.get("opportunity_id") != opportunity_id:
        log_error(opportunity_id, f"台账 opportunity_id 不匹配: {ledger.get('opportunity_id')}")
        print(json.dumps({"error": "台账 opportunity_id 不匹配"}, ensure_ascii=False))
        sys.exit(1)
    
    # 记录变更前后摘要
    # 写入前先校验本次携带的证据是否全部合格（列表、且每条通过 validate_evidence），
    # 不合格直接拒绝，避免脏数据污染事实源
    incoming = data.get("evidence", [])
    if not isinstance(incoming, list) or any(not validate_evidence(item) for item in incoming):
        print(json.dumps({"error": "证据须包含 fact/source/date(ISO)/grade(A|B|C)/signal(正向|反向)；corroborating_sources 如提供须为来源数组"}, ensure_ascii=False))
        sys.exit(1)
    # before 摘要：记录写入前的关键状态，供事后对比“这次改了什么”
    before_summary = {
        "version": ledger.get("version"),
        "six_lens_conclusion": ledger.get("six_lens", {}).get("conclusion"),
        "fit_total_score": ledger.get("fit_assessment", {}).get("total_score"),
        "evidence_count": len(ledger.get("evidence", []))
    }
    
    # 版本号自增 + 刷新 updated_at：任何一次成功写入都推进版本
    ledger["updated_at"] = now_iso()
    ledger["version"] = ledger.get("version", 0) + 1
    
    # 根据阶段更新
    if stage == STAGES["TEARDOWN"]:
        # 添加单品拆解证据
        # TEARDOWN 只追加证据；idx 用“当前证据总数+1”保证全局序号连续
        for i, evidence in enumerate(data.get("evidence", [])):
            idx = len(ledger["evidence"]) + 1
            normalized = normalize_evidence(evidence, idx, stage)
            ledger["evidence"].append(normalized)
    
    elif stage == STAGES["SIX_LENS"]:
        # 更新六看结论（固定写入路径）
        # conclusion 固定写进 six_lens.conclusion（取 overall，缺省直接用整个对象），
        # 不接受模型另选字段名，保证下游 generate_report 读取口径一致
        conclusion_data = data.get("conclusion", {})
        if conclusion_data:
            ledger["six_lens"]["conclusion"] = conclusion_data.get("overall", conclusion_data)
            ledger["six_lens"]["updated_at"] = now_iso()
            
            # 各维度详情
            # 六个维度固定写在 six_lens 下同名键里（market/capital/... ），
            # 只写实际传入的维度，未传的保持原样
            for dim in ['market', 'capital', 'industry', 'policy', 'user', 'competitor']:
                if dim in conclusion_data:
                    ledger["six_lens"][dim] = conclusion_data[dim]
        
        # 添加证据
        # 同 TEARDOWN：追加并归一化，保持全局序号连续
        for evidence in data.get("evidence", []):
            idx = len(ledger["evidence"]) + 1
            normalized = normalize_evidence(evidence, idx, stage)
            ledger["evidence"].append(normalized)
        
        # 添加补证任务
        # 补证任务（evidence_tasks）整体覆盖写入，而非追加，避免重复累积
        if "evidence_tasks" in data:
            ledger["six_lens"]["evidence_tasks"] = data["evidence_tasks"]
    
    elif stage == STAGES["FIT_ASSESSMENT"]:
        # 更新适配度评估（必须包含 total_score 和 gate）
        # 这两个字段是适配度评估的核心产出，缺失即视为无效更新，直接拒绝
        if "total_score" not in data:
            log_error(opportunity_id, "适配度更新缺少 total_score")
            print(json.dumps({"error": "适配度更新缺少 total_score"}, ensure_ascii=False))
            sys.exit(1)
        
        if "gate" not in data:
            log_error(opportunity_id, "适配度更新缺少 gate")
            print(json.dumps({"error": "适配度更新缺少 gate"}, ensure_ascii=False))
            sys.exit(1)
        
        # 固定写入路径
        # 全部写入 fit_assessment 下的固定键；可选字段用 get(..., 默认) 兜底
        ledger["fit_assessment"]["total_score"] = data["total_score"]
        ledger["fit_assessment"]["gate"] = data["gate"]
        ledger["fit_assessment"]["gate_description"] = data.get("gate_description", "")
        ledger["fit_assessment"]["dimension_scores"] = data.get("dimension_scores", {})
        ledger["fit_assessment"]["calculation_detail"] = data.get("calculation_detail", {})
        ledger["fit_assessment"]["updated_at"] = now_iso()
        
        # 添加证据
        for evidence in data.get("evidence", []):
            idx = len(ledger["evidence"]) + 1
            normalized = normalize_evidence(evidence, idx, stage)
            ledger["evidence"].append(normalized)
    
    # 记录版本历史（包含变更前后摘要）
    # after 摘要与 before 同构，便于直接 diff 出“本次变更”
    after_summary = {
        "version": ledger["version"],
        "six_lens_conclusion": ledger.get("six_lens", {}).get("conclusion"),
        "fit_total_score": ledger.get("fit_assessment", {}).get("total_score"),
        "evidence_count": len(ledger.get("evidence", []))
    }
    
    # 追加一条版本历史：拿不到旧记录就地失败，说明结构异常
    ledger["version_history"].append({
        "version": ledger["version"],
        "timestamp": now_iso(),
        "change": f"{stage} 阶段更新",
        "changed_by": stage,
        "before": before_summary,
        "after": after_summary
    })
    
    # 原子写入
    try:
        atomic_write_json(path, ledger)
    except Exception as e:
        # 落盘失败：日志 + 报错 + 退出码 1；此时内存改动未生效，台账保持旧版本
        log_error(opportunity_id, f"台账写入失败: {e}")
        print(json.dumps({"error": f"台账写入失败: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 成功回执：带回新的 version，上层据此确认写入生效
    print(json.dumps({
        "status": "updated",
        "version": ledger["version"],
        "stage": stage
    }, ensure_ascii=False))


def get_ledger(opportunity_id: str):
    """获取证据台账（动作=get，只读，无输入）。

    参数：
        opportunity_id —— 台账标识，决定读取 runtime/evidence_ledger/<id>.json。

    返回值：无（成功时把整份台账 JSON 打到 stdout）。

    异常与退出码：
        台账不存在 / JSON 损坏 → 记日志、打印 error JSON 后 sys.exit(1)。

    为什么这么设计：
        读操作也要先补齐结构（ensure_ledger_structure）再输出，保证上层拿到的
        台账字段齐全、可直接按固定路径取值，无需在调用方做兼容判断。
    """
    path = os.path.join(LEDGER_DIR, f"{opportunity_id}.json")
    if not os.path.exists(path):
        print(json.dumps({"error": f"台账不存在: {opportunity_id}"}, ensure_ascii=False))
        sys.exit(1)
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            ledger = json.load(f)
    except json.JSONDecodeError as e:
        # 只读路径上的损坏同样落日志，方便运维定位
        log_error(opportunity_id, f"台账 JSON 解析失败: {e}")
        print(json.dumps({"error": f"台账文件损坏: {e}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 确保结构完整
    ledger = ensure_ledger_structure(ledger)
    
    # 输出完整台账 JSON（ensure_ascii=False 保留中文可读性）
    print(json.dumps(ledger, ensure_ascii=False))


if __name__ == '__main__':
    # —— CLI 分派：argv[1] 决定动作（init/update/get）——
    # 动作缺失：打印用法 JSON 后退出 1
    if len(sys.argv) < 2:
        print(json.dumps({"error": "用法: evidence_ledger.py <init|update|get> [args]"}, ensure_ascii=False))
        sys.exit(1)
    
    action = sys.argv[1]
    # 机会 ID 统一走环境变量传入（命令层负责设置），缺失则拒绝执行
    opportunity_id = os.environ.get("OPPORTUNITY_ID")
    
    if not opportunity_id:
        print(json.dumps({"error": "缺少环境变量 OPPORTUNITY_ID"}, ensure_ascii=False))
        sys.exit(1)
    
    if action == "init":
        # init 的机会定义 JSON 从 stdin 读入；解析失败视为输入错误
        try:
            intake_data = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"输入 JSON 解析失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
        init_ledger(opportunity_id, intake_data)
    
    elif action == "update":
        # stage 优先取环境变量 STAGE；没有则回退到 --stage 命令行参数（兼容旧调用方式）
        stage = os.environ.get("STAGE")
        if not stage:
            # 尝试从命令行参数读取
            if len(sys.argv) >= 4 and sys.argv[2] == "--stage":
                stage = sys.argv[3]
            else:
                print(json.dumps({"error": "缺少环境变量 STAGE 或 --stage 参数"}, ensure_ascii=False))
                sys.exit(1)
        
        # update 的阶段结果 JSON 同样从 stdin 读入
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"输入 JSON 解析失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
        
        update_ledger(opportunity_id, stage, data)
    
    elif action == "get":
        # 只读：无需 stdin
        get_ledger(opportunity_id)
    
    else:
        # 未知动作：明确报错并退出 1
        print(json.dumps({"error": f"未知操作: {action}"}, ensure_ascii=False))
        sys.exit(1)
