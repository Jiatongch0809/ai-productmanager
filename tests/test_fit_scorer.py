#!/usr/bin/env python3
"""适配度评分脚本单元测试

【本文件在机制里的位置与职责】
    本文件是 scripts/fit_scorer.py 的单元测试。适配度是流程里"确定交给脚本"的核心一环：
    八维加权评分与 Gate 阈值判定（80/65/50 → S51/S52/S53/S54）全部由脚本计算，
    LLM 只提供每维原始分。因此这里重点验证：
    - 加权公式（总分 = Σ 原始分/10 × 权重）是否正确；
    - Gate 阈值分档是否落在正确边界；
    - 各类非法输入（缺维、未知维、越界、负数、NaN、Infinity、非数字）是否被严格拒绝。

【测试数据声明】
    本文件中的评分数值均为虚构样例，仅用于触发公式与边界分支，
    不对应任何真实产品的评估结论。
"""
import sys
import os
import json
import unittest

# 把 scripts 目录加入搜索路径，以便导入待测模块 fit_scorer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from fit_scorer import calculate, validate_score


class TestFitScorer(unittest.TestCase):
    """fit_scorer.py 测试（calculate 主流程）"""

    def test_valid_scores(self):
        """测试有效分数：一组合法八维分应产出总分/S51-S54 Gate/明细

        验证的业务规则：
            传入完整且合法的八维分时，结果字典必须含 total_score、gate、
            dimension_scores、calculation_detail 四个键，且按加权公式算出正确的档位。
        为什么这么构造：
            这组分数（含 org_capability=6）加权后总分正好 64.5，
            落在 S53（50–64 低适配）区间，用于同时验证"公式正确"与"分档正确"。
        """
        scores = {
            "market_certainty": 7,
            "user_demand": 9,
            "category_validation": 7,
            "ai_value": 5,
            "org_capability": 6,
            "quick_launch": 7,
            "business_path": 5,
            "cost_risk": 3
        }
        result = calculate(scores)
        # 结果必须包含关键字段
        self.assertIn("total_score", result)
        self.assertIn("gate", result)
        self.assertIn("dimension_scores", result)
        self.assertIn("calculation_detail", result)
        # 加权总分 = 7*0.1*10 + ... = 64.5，落入 S53 档
        self.assertEqual(result["gate"], "S53")  # 64.5 分

    def test_boundary_scores(self):
        """测试边界分数：全 0 与全 10 的分档边界

        验证的业务规则：
            评分区间下界（全 0）应得 0 分 → S54；上界（全 10）应得 100 分 → S51。
            这验证公式在端点处的行为以及两端档位映射。
        """
        # 0 分：全维为 0
        scores = {k: 0 for k in ["market_certainty", "user_demand", "category_validation",
                                   "ai_value", "org_capability", "quick_launch",
                                   "business_path", "cost_risk"]}
        result = calculate(scores)
        self.assertEqual(result["total_score"], 0)
        self.assertEqual(result["gate"], "S54")

        # 10 分：全维为 10（此处复用上一组 scores 的键集合）
        scores = {k: 10 for k in scores.keys()}
        result = calculate(scores)
        self.assertEqual(result["total_score"], 100)
        self.assertEqual(result["gate"], "S51")

    def test_gate_thresholds(self):
        """测试 Gate 阈值：80 / 65 / 50 三条分界与对应档位

        验证的业务规则：
            总分 ≥80 → S51；65–79 → S52；50–64 → S53；<50 → S54。
        为什么这么构造：
            起点取八维各 5 分（总分 50，恰在 S53 下边界）。
            通过抬高高权重维度把总分推入不同档：
              org_capability 权重 25、quick_launch 权重 15、user_demand 权重 15、
              market_certainty 权重 10，抬高它们对总分影响最大，
              便于用较少改动跨越阈值，从而精确命中边界断言。
        """
        base_scores = {k: 5 for k in ["market_certainty", "user_demand", "category_validation",
                                       "ai_value", "org_capability", "quick_launch",
                                       "business_path", "cost_risk"]}

        # S51: >= 80
        scores = base_scores.copy()
        scores["org_capability"] = 10  # 提高组织能力（权重 25）
        scores["quick_launch"] = 10      # 提高快速落地（权重 15）
        scores["user_demand"] = 10       # 提高用户需求（权重 15）
        scores["market_certainty"] = 8   # 提高市场确定性（权重 10）
        result = calculate(scores)
        self.assertGreaterEqual(result["total_score"], 80)
        self.assertEqual(result["gate"], "S51")

        # S52: 65-79
        scores = base_scores.copy()
        scores["org_capability"] = 8  # 组织能力 8（权重 25）
        scores["quick_launch"] = 8       # 快速落地 8（权重 15）
        scores["user_demand"] = 8        # 用户需求 8（权重 15）
        result = calculate(scores)
        self.assertGreaterEqual(result["total_score"], 65)
        self.assertLess(result["total_score"], 80)
        self.assertEqual(result["gate"], "S52")

        # S53: 50-64
        scores = base_scores.copy()
        result = calculate(scores)
        self.assertGreaterEqual(result["total_score"], 50)
        self.assertLess(result["total_score"], 65)
        self.assertEqual(result["gate"], "S53")

        # S54: < 50
        scores = {k: 2 for k in base_scores.keys()}
        result = calculate(scores)
        self.assertLess(result["total_score"], 50)
        self.assertEqual(result["gate"], "S54")

    def test_missing_dimension(self):
        """测试缺失维度：八维不全时必须报错，不得用默认值蒙混

        验证的业务规则：
            缺少任一必需维度 → 返回 {"error": "缺少维度: [...]"}。
            这是"评分必须完整"的硬约束，避免缺维被当成 0 分而虚高/虚低总分。
        """
        scores = {
            "market_certainty": 7,
            "user_demand": 9,
            # 缺少其他维度
        }
        result = calculate(scores)
        self.assertIn("error", result)
        self.assertIn("缺少维度", result["error"])

    def test_unknown_dimension(self):
        """测试未知维度：出现未定义维度时必须拒绝

        验证的业务规则：
            出现不在 WEIGHTS 里的维度键 → 返回 {"error": "未知维度: [...]"}。
            防止拼写错误（如自造维度名）被静默忽略，导致评分口径漂移。
        """
        scores = {
            "market_certainty": 7,
            "user_demand": 9,
            "category_validation": 7,
            "ai_value": 5,
            "org_capability": 6,
            "quick_launch": 7,
            "business_path": 5,
            "cost_risk": 3,
            "unknown_dim": 5  # 未知维度
        }
        result = calculate(scores)
        self.assertIn("error", result)
        self.assertIn("未知维度", result["error"])

    def test_invalid_score_range(self):
        """测试无效分数范围：>10 的分数被拒

        验证的业务规则：
            每维原始分取值范围为 [0, 10]；15 超出上界 →
            返回 {"error": ...} 且信息含"超出范围"。
        """
        scores = {
            "market_certainty": 7,
            "user_demand": 9,
            "category_validation": 7,
            "ai_value": 5,
            "org_capability": 6,
            "quick_launch": 7,
            "business_path": 5,
            "cost_risk": 15  # 超出 0-10
        }
        result = calculate(scores)
        self.assertIn("error", result)
        self.assertIn("超出范围", result["error"])

    def test_negative_score(self):
        """测试负数分数：<0 的分数被拒

        验证的业务规则：
            负分不属于 [0, 10]，calculate 应返回含 error 的结果。
        """
        scores = {
            "market_certainty": 7,
            "user_demand": 9,
            "category_validation": 7,
            "ai_value": 5,
            "org_capability": 6,
            "quick_launch": 7,
            "business_path": 5,
            "cost_risk": -1  # 负数
        }
        result = calculate(scores)
        self.assertIn("error", result)

    def test_nan_score(self):
        """测试 NaN 分数：NaN 被拒且错误信息含 "NaN"

        验证的业务规则：
            NaN（非数）会破坏比较运算语义，validate_score 必须显式拦截。
        """
        import math
        scores = {
            "market_certainty": 7,
            "user_demand": 9,
            "category_validation": 7,
            "ai_value": 5,
            "org_capability": 6,
            "quick_launch": 7,
            "business_path": 5,
            "cost_risk": float('nan')
        }
        result = calculate(scores)
        self.assertIn("error", result)
        self.assertIn("NaN", result["error"])

    def test_infinity_score(self):
        """测试 Infinity 分数：无穷被拒且错误信息含 "Infinity"

        验证的业务规则：
            正无穷会破坏范围判断（0 <= inf 为真但语义错误），必须显式拦截。
        """
        scores = {
            "market_certainty": 7,
            "user_demand": 9,
            "category_validation": 7,
            "ai_value": 5,
            "org_capability": 6,
            "quick_launch": 7,
            "business_path": 5,
            "cost_risk": float('inf')
        }
        result = calculate(scores)
        self.assertIn("error", result)
        self.assertIn("Infinity", result["error"])

    def test_non_numeric_score(self):
        """测试非数字分数：字符串等非数字类型被拒

        验证的业务规则：
            分数必须是 int/float；传入 "high" 这类字符串 →
            返回 {"error": ...} 且信息含"不是数字"。
        """
        scores = {
            "market_certainty": 7,
            "user_demand": 9,
            "category_validation": 7,
            "ai_value": 5,
            "org_capability": 6,
            "quick_launch": 7,
            "business_path": 5,
            "cost_risk": "high"  # 非数字
        }
        result = calculate(scores)
        self.assertIn("error", result)
        self.assertIn("不是数字", result["error"])


class TestValidateScore(unittest.TestCase):
    """validate_score 函数测试（单维校验，返回 (is_valid, error_message)）"""

    def test_valid_score(self):
        """合法分（5）：应通过，且错误信息为空字符串"""
        is_valid, error = validate_score("test", 5)
        self.assertTrue(is_valid)
        self.assertEqual(error, "")

    def test_boundary_0(self):
        """边界下界 0：应通过（闭区间 [0,10]）"""
        is_valid, error = validate_score("test", 0)
        self.assertTrue(is_valid)

    def test_boundary_10(self):
        """边界上界 10：应通过（闭区间 [0,10]）"""
        is_valid, error = validate_score("test", 10)
        self.assertTrue(is_valid)

    def test_out_of_range_high(self):
        """上越界 11：应不通过，错误信息含"超出范围" """
        is_valid, error = validate_score("test", 11)
        self.assertFalse(is_valid)
        self.assertIn("超出范围", error)

    def test_out_of_range_low(self):
        """下越界 -1：应不通过，错误信息含"超出范围" """
        is_valid, error = validate_score("test", -1)
        self.assertFalse(is_valid)
        self.assertIn("超出范围", error)


if __name__ == "__main__":
    # 直接运行时执行全部用例
    unittest.main(verbosity=2)
