#!/usr/bin/env python3
"""适配度评分计算脚本 — 业务线 A 专用

【本文件在机制中的位置与职责】
    本脚本是工作流 A 里"适配度打分"的唯一权威计算器，属于确定性边界的一部分：
    模型（LLM）只负责按维度打"原始分（0-10）"，而"加权总分、S51/S52/S53/S54 分级、
    计算明细"必须由本脚本产出。这样做的目的是——
      - 加权公式只有一处实现，避免多处各自算分、口径漂移；
      - 模型不能自判总分与分级（不许模型"说我 85 分"）；
      - 计算过程可复算、可审计（calculation_detail 逐维列出原始分/权重/加权分）。
    产出的 total_score 会被写进证据台账，随后由 gate_check.py 的 FIT_GATE 读取判定。

【用法】
    echo '{"market_certainty": 7, "user_demand": 9, ...}' | python3 scripts/fit_scorer.py

【输入】
    从 stdin 读取 JSON，必须包含全部 8 个维度的原始分（每个 0-10 的数值）。

【输出】
    JSON 格式，包含：
    - total_score: 总分（满分 100）
    - gate: Gate 判定（S51/S52/S53/S54）
    - dimension_scores: 原始维度分数
    - calculation_detail: 逐维计算明细
    - weights: 本次使用的权重表

【评分公式】
    总分 = sum( (原始分 / 10) × 权重 )
    即先把每个 0-10 的原始分归一化成 0-1 的小数，再乘以该维权重，累加得 0-100 的总分。

【严格校验】（校验失败一律拒绝而非降级，理由见函数注释）
    - 拒绝未知维度
    - 拒绝缺失维度
    - 拒绝非数字、NaN、Infinity
    - 拒绝超出 0-10 范围的分数

【退出码语义】
    0 = 计算成功（stdout 为结果 JSON）
    1 = 输入非法（JSON 解析失败 / 输入不是对象 / 维度校验失败）
"""
import json
import sys
import math

# 八维权重（总和 100）
# 权重反映"该维度对适配结论的重要性"，是本项目产品侧的既定权重，属于业务口径：
#   org_capability 最高（25）——因为这是"本组织能不能接得住"的内部能力匹配，最关键；
#   user_demand / quick_launch 各 15——需求真实性与能否快速落地并列次高；
#   其余市场确定性/品类验证/AI 价值/商业路径各 10，成本风险最低 5。
# 权重之和固定为 100，与"原始分/10 × 权重"的归一化公式配合，使满分恰好 100。
# 注意：此处权重必须与 gate_check.py 的分档阈值（80/65/50）配套理解。
WEIGHTS = {
    "market_certainty": 10,      # 市场/趋势确定性
    "user_demand": 15,           # 用户需求强度
    "category_validation": 10,   # 品类/竞品验证程度
    "ai_value": 10,              # AI 价值必要性
    "org_capability": 25,     # 组织能力匹配度
    "quick_launch": 15,          # 快速落地可行性
    "business_path": 10,         # 商业转化路径
    "cost_risk": 5               # 成本与风险可控性
}

# 维度描述：仅供输出明细时展示中文名，不参与任何计算。
# 注意：这里与 WEIGHTS 是两份并行的映射，新增维度时两处都要改，否则会出现描述对不上的情况。
DIMENSION_DESCRIPTIONS = {
    "market_certainty": "市场/趋势确定性",
    "user_demand": "用户需求强度",
    "category_validation": "品类/竞品验证程度",
    "ai_value": "AI 价值必要性",
    "org_capability": "组织能力匹配度",
    "quick_launch": "快速落地可行性",
    "business_path": "商业转化路径",
    "cost_risk": "成本与风险可控性"
}


def validate_score(dim: str, score) -> tuple[bool, str]:
    """校验单个维度的原始分是否合法。

    参数：
        dim:   维度名（仅用于拼错误信息，便于定位是哪个维度出了问题）。
        score: 待校验的分数，应为 0-10 的 int/float。

    返回：
        (is_valid, error_message)：合法时为 (True, "")；非法时为 (False, 原因)。

    为什么这么设计：
        采用"逐条早退 + 返回原因"的风格，把每类非法输入分开报错，
        便于上游/人工一眼看出是类型错、NaN、Infinity 还是越界。
        校验失败必须拒绝而不是裁剪（clamp）或四舍五入：
        静默修正会掩盖上游（模型）的输入错误，让"脏分"混进权威总分。
    """
    # 检查是否为数字：bool 在 Python 中是 int 子类，但 bool 不属于合法分数语义，
    # 这里用 isinstance(score, (int, float)) 排除字符串/None/列表等明显非法类型。
    if not isinstance(score, (int, float)):
        return False, f"维度 {dim} 的分数不是数字: {type(score)}"
    
    # 检查 NaN：NaN 参与算术会污染总分（结果也是 NaN），必须显式拒绝。
    if math.isnan(score):
        return False, f"维度 {dim} 的分数为 NaN"
    
    # 检查 Infinity：无穷大乘权重后总分变成 inf，无法分档，必须显式拒绝。
    if math.isinf(score):
        return False, f"维度 {dim} 的分数为 Infinity"
    
    # 检查范围：原始分约定为 0-10。越界分不裁剪，直接拒绝——
    # 因为越界往往意味着模型误解了打分口径，裁剪会把"系统性错误"伪装成"正常分数"。
    if not (0 <= score <= 10):
        return False, f"维度 {dim} 的分数 {score} 超出范围 [0, 10]"
    
    return True, ""


def calculate(scores: dict) -> dict:
    """加权评分计算（纯函数，不读写任何文件，便于复算与单测）。

    参数：
        scores: dict，键为维度名，值为该维原始分（0-10）。

    返回：
        成功：{"total_score", "gate", "gate_description", "dimension_scores",
               "calculation_detail", "weights"}
        失败：{"error": "..."}（附 allowed_dimensions / required_dimensions 提示）。

    为什么这么设计：
        - 纯函数便于对同一输入复现同一结果，是"确定性边界"的基础。
        - 三类校验（未知/缺失/非法）都在计算前完成，保证"要么全合法算出总分，
          要么直接报错不产出任何分数"，杜绝半成品总分流入台账。
        - 分档阈值（80/65/50）与 gate_check.py 保持完全一致：同一总分两处必须判出同一档。
    """
    # 检查未知维度：传入 WEIGHTS 之外的键说明上游传参有误（拼写错/多余维），
    # 直接拒绝而不是忽略——忽略会让"打错的分"悄悄消失，总分被虚高。
    unknown_dims = [k for k in scores if k not in WEIGHTS]
    if unknown_dims:
        return {
            "error": f"未知维度: {unknown_dims}",
            "allowed_dimensions": list(WEIGHTS.keys())
        }
    
    # 检查缺失维度：八维必须齐全。缺一维若按 0 分补齐会系统性压低总分，
    # 若按"剩几维归一化"又会篡改口径，两者都是隐性错误 → 一律拒绝，要求补全。
    missing_dims = [k for k in WEIGHTS if k not in scores]
    if missing_dims:
        return {
            "error": f"缺少维度评分: {missing_dims}",
            "required_dimensions": list(WEIGHTS.keys())
        }
    
    # 校验每个维度的分数：逐维调用 validate_score，首个非法即返回，
    # 保证不会用部分非法的分数继续累加。
    for dim, score in scores.items():
        is_valid, error_msg = validate_score(dim, score)
        if not is_valid:
            return {"error": error_msg}
    
    # 公式：总分 = sum( (原始分/10) × 权重 )
    # 遍历 WEIGHTS（而非 scores）以保证计算顺序稳定；权重和=100，故总分落于 0-100。
    total = sum((scores[k] / 10) * WEIGHTS[k] for k in WEIGHTS)
    
    # Gate 判定：与 gate_check.py 的阈值完全一致（80/65/50），
    # 保证"评分脚本给出的 gate"与"Gate 脚本判定"不会互相打架。
    if total >= 80:
        gate = "S51"
        gate_description = "高适配，可提交创新评审"
    elif total >= 65:
        gate = "S52"
        gate_description = "中适配，需补证后复评"
    elif total >= 50:
        gate = "S53"
        gate_description = "低适配，进入观察池"
    else:
        gate = "S54"
        gate_description = "不适配，暂停"
    
    # 计算明细：逐维输出原始分/权重/加权分，供人工复核与审计（可复算追溯）。
    calculation_detail = {}
    for k in WEIGHTS:
        raw_score = scores[k]
        weight = WEIGHTS[k]
        weighted_score = (raw_score / 10) * weight
        calculation_detail[k] = {
            "raw_score": raw_score,
            "weight": weight,
            # 保留 2 位小数：与 total_score 的精度展示保持一致
            "weighted_score": round(weighted_score, 2),
            "description": DIMENSION_DESCRIPTIONS.get(k, k)
        }
    
    return {
        # 总分同样保留 2 位小数，避免浮点长尾（如 84.999999）影响展示与阈值观感
        "total_score": round(total, 2),
        "gate": gate,
        "gate_description": gate_description,
        "dimension_scores": scores,
        "calculation_detail": calculation_detail,
        "weights": WEIGHTS
    }


if __name__ == "__main__":
    # 从 stdin 读取模型打的原始分（每维 0-10）
    # 读 stdin 而不是命令行参数：便于与管道/上层编排器对接。
    try:
        raw_scores = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        # 输入不是合法 JSON：无法继续 → 输出错误 JSON + 退出码 1
        print(json.dumps({"error": f"JSON 解析失败: {str(e)}"}, ensure_ascii=False))
        sys.exit(1)
    
    # 检查输入是否为字典：顶层必须是对象（维度名→分数）。若为数组/标量则无法按维取分 → 退出码 1
    if not isinstance(raw_scores, dict):
        print(json.dumps({"error": f"输入必须是 JSON 对象，当前类型: {type(raw_scores)}"}, ensure_ascii=False))
        sys.exit(1)
    
    result = calculate(raw_scores)
    
    # 校验失败：输出带 error 的 JSON 并退出码 1（不产出任何总分，避免脏分流入台账）
    if "error" in result:
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)
    
    # 成功：输出结果 JSON；正常结束即退出码 0
    print(json.dumps(result, ensure_ascii=False))
