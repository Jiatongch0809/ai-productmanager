#!/usr/bin/env python3
"""Gate 检查脚本单元测试

【本文件在机制里的位置与职责】
    本文件是 scripts/gate_check.py 的单元测试，专注验证 Gate 的纯判定逻辑
    （check_six_lens / check_fit_assessment / check_priority）。
    Gate 是流程的"确定边界"：LLM 给出分析结论，但"继续/等待/停止/报错"由脚本机械判定，
    因此这里把每条结论 → 动作(action) 的映射、以及核心维度缺证降级规则逐一钉死。
    重点关注：Gate 动作 CONTINUE/WAIT_USER/STOP/ERROR 与后续状态映射的一致性。

【测试数据声明】
    本文件中的六看结论、适配度分数全部是虚构样例，仅用于触发分支，
    不对应任何真实产品的评估结论。
"""
import sys
import os
import json
import unittest
import tempfile
import shutil
from datetime import date

# 把 scripts 目录加入搜索路径，以便导入 contracts 与 gate_check
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from contracts import STAGES, STATUSES, GATE_ACTIONS


class TestGateCheck(unittest.TestCase):
    """gate_check.py 测试"""

    def setUp(self):
        """创建临时目录

        注意（供交接参考）：
            本文件测的是 check_six_lens / check_fit_assessment 等"纯函数"——
            它们只吃 ledger 字典、不读写磁盘，因此这里创建的 runtime 临时目录
            其实并未被这些用例使用（属于预留/历史脚手架）。保留原样，不改代码。
        """
        self.test_dir = tempfile.mkdtemp()
        self.ledger_dir = os.path.join(self.test_dir, "runtime", "evidence_ledger")
        self.state_dir = os.path.join(self.test_dir, "runtime", "workflow_state")
        os.makedirs(self.ledger_dir)
        os.makedirs(self.state_dir)

    def tearDown(self):
        """清理临时目录"""
        shutil.rmtree(self.test_dir)

    @staticmethod
    def complete_six_lens(overall):
        """构造六个维度齐备的六看结果

        为什么这么构造：
            Gate 对核心三维（market/user/competitor）有专门守卫：
            核心维度任一无结论、或结论为 WATCH/NO，都会把 YES 降级为 WAIT_USER。
            这里给六个维度都填 CONDITIONAL（既非缺失也非 WATCH/NO），
            用于隔离"核心维度守卫"这一条规则，专门测试其它分支。
        """
        result = {"overall": overall}
        for dimension in ("market", "capital", "industry", "policy", "user", "competitor"):
            result[dimension] = {"conclusion": "CONDITIONAL"}
        return result

    @staticmethod
    def evidence(dimension, source="source-a"):
        """构造一条能通过 verified_support 的合成证据。

        要求：A/B 级、正向、维度匹配、时效内、至少两个不同来源字符串。
        """
        return {
            "dimension": dimension,
            "fact": f"{dimension} 合成事实",
            "source": source,
            "corroborating_sources": [f"{source}-b"],
            "date": date.today().isoformat(),
            "grade": "A",
            "signal": "正向",
        }

    def test_six_lens_yes(self):
        """测试六看 YES 结论（六维齐备 + 核心维度有可核查证据）

        验证的业务规则：
            六维齐备、核心维度证据达标且结论为 YES 时，Gate 判 CONTINUE，
            并把下一阶段指向 FIT_ASSESSMENT。
        """
        from gate_check import check_six_lens
        ledger = {
            "six_lens": self.complete_six_lens("YES"),
            "evidence": [self.evidence(dim) for dim in ("market", "user", "competitor")],
        }
        result = check_six_lens(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["CONTINUE"])
        self.assertEqual(result["next_stage"], STAGES["FIT_ASSESSMENT"])

    def test_six_lens_yes_blocked_by_missing_core_dimension(self):
        """YES 但核心维度缺失时降级为 WAIT_USER，不得当作证据充分

        验证的业务规则：
            overall=YES 但核心维度（user/competitor）无结论时，Gate 不能放行，
            必须降级 WAIT_USER，并在 core_dimension_gaps 里指名缺失维度（形如 "user(缺失)"）。
            这体现"六看结论由模型给，但权威性由脚本按证据完整性把关"。
        """
        from gate_check import check_six_lens
        # 只给了 market 一个核心维度，user/competitor 缺失
        ledger = {"six_lens": {"overall": "YES", "market": {"conclusion": "YES"}}}
        result = check_six_lens(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["WAIT_USER"])
        self.assertIn("user(缺失)", result["core_dimension_gaps"])
        self.assertIn("competitor(缺失)", result["core_dimension_gaps"])

    def test_six_lens_yes_blocked_by_watch_core_dimension(self):
        """YES 但核心维度为 WATCH 时降级为 WAIT_USER

        验证的业务规则：
            核心维度结论为 WATCH 也视为"不成立"，即使 overall=YES 也降级 WAIT_USER，
            并把该维度以 "user(WATCH)" 形式记入 core_dimension_gaps。
        """
        from gate_check import check_six_lens
        ledger = {"six_lens": self.complete_six_lens("YES")}
        ledger["six_lens"]["user"] = {"conclusion": "WATCH"}
        result = check_six_lens(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["WAIT_USER"])
        self.assertEqual(result["core_dimension_gaps"], ["user(WATCH)"])

    def test_six_lens_conditional(self):
        """测试六看 CONDITIONAL 结论

        验证的业务规则：
            CONDITIONAL（中性，待补证）→ WAIT_USER，并把补证任务列表
            (six_lens.evidence_tasks) 一并带出，供通知用户决策。
        """
        from gate_check import check_six_lens
        ledger = {
            "six_lens": {
                "overall": "CONDITIONAL",
                "evidence_tasks": ["任务1", "任务2"]
            }
        }
        result = check_six_lens(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["WAIT_USER"])
        self.assertIn("evidence_tasks", result)

    def test_six_lens_no(self):
        """测试六看 NO 结论：应判 STOP（流程终止）"""
        from gate_check import check_six_lens
        ledger = {
            "six_lens": {"overall": "NO"}
        }
        result = check_six_lens(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["STOP"])

    def test_six_lens_watch(self):
        """测试六看 WATCH 结论：与 NO 同为 STOP（观察但不放行）"""
        from gate_check import check_six_lens
        ledger = {
            "six_lens": {"overall": "WATCH"}
        }
        result = check_six_lens(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["STOP"])

    def test_six_lens_unknown(self):
        """测试六看未知结论：无法判定 → ERROR（不是 STOP，避免误判为正常终止）"""
        from gate_check import check_six_lens
        ledger = {
            "six_lens": {"overall": "UNKNOWN"}
        }
        result = check_six_lens(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["ERROR"])

    def test_six_lens_missing(self):
        """测试六看结论缺失：空台账 → ERROR（缺结论不能当通过，也不能当停止）"""
        from gate_check import check_six_lens
        ledger = {}
        result = check_six_lens(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["ERROR"])

    def test_fit_assessment_s51(self):
        """测试适配度 S51（>=80）

        验证的业务规则：
           总分 ≥80 属 S51 高适配档，且关键内部适配维度
            (org_capability/quick_launch/business_path) 有可核查证据时，
            Gate 判 CONTINUE 进入 GENERATE。
        """
        from gate_check import check_fit_assessment
        ledger = {
            "fit_assessment": {"total_score": 85},
            "evidence": [self.evidence(dim) for dim in
                         ("org_capability", "quick_launch", "business_path")],
        }
        result = check_fit_assessment(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["CONTINUE"])
        self.assertEqual(result["gate"], "S51")

    def test_fit_assessment_s52(self):
        """测试适配度 S52（65-79）：中适配 → WAIT_USER（需用户决定是否补证）"""
        from gate_check import check_fit_assessment
        ledger = {
            "fit_assessment": {"total_score": 70}
        }
        result = check_fit_assessment(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["WAIT_USER"])
        self.assertEqual(result["gate"], "S52")

    def test_fit_assessment_s53(self):
        """测试适配度 S53（50-64）：低适配 → STOP（进入观察池）"""
        from gate_check import check_fit_assessment
        ledger = {
            "fit_assessment": {"total_score": 55}
        }
        result = check_fit_assessment(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["STOP"])
        self.assertEqual(result["gate"], "S53")

    def test_fit_assessment_s54(self):
        """测试适配度 S54（<50）：不适配 → STOP（流程暂停）"""
        from gate_check import check_fit_assessment
        ledger = {
            "fit_assessment": {"total_score": 40}
        }
        result = check_fit_assessment(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["STOP"])
        self.assertEqual(result["gate"], "S54")

    def test_fit_assessment_missing_score(self):
        """测试适配度总分缺失：无 total_score → ERROR

        验证的业务规则：
            适配度 Gate 只认台账里已计算好的 total_score（必须先经 fit_scorer.py 计算），
            缺失即报 ERROR，不允许从原始 scores 现算——以免绕过评分脚本的口径。
        """
        from gate_check import check_fit_assessment
        ledger = {
            "fit_assessment": {}
        }
        result = check_fit_assessment(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["ERROR"])

    def test_priority_not_implemented(self):
        """测试业务线 B 未实现

        验证的业务规则：
            业务线 B（优先级评估）尚未实现，check_priority 必须返回 ERROR（而非 CONTINUE），
            以免未实现的判定被当成"通过"继续下去。reason 中应含"未实现"。
        """
        from gate_check import check_priority
        ledger = {}
        result = check_priority(ledger)
        self.assertEqual(result["action"], GATE_ACTIONS["ERROR"])
        self.assertIn("未实现", result["reason"])


if __name__ == "__main__":
    # 直接运行时执行全部用例
    unittest.main(verbosity=2)
