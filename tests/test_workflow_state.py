#!/usr/bin/env python3
"""工作流状态脚本单元测试

【本文件在机制里的位置与职责】
    本文件验证 contracts.py 中"状态机与阶段契约"的纯函数：
    状态迁移校验（validate_transition）、终态判断（is_terminal_state）、
    旧状态值迁移（migrate_status）、Gate 阶段识别与映射、以及状态文件结构补齐
    （ensure_state_structure）。
    这些契约是整条流程的"红绿灯"——脚本据此拒绝非法跳转、识别等待与终态，
    因此这里把每条规则用断言固定下来，防止将来改动悄悄放宽。
    注意：状态迁移规则本身就是"确定交给脚本"的体现：LLM 不能任意改状态。

【测试数据声明】
    本文件只校验契约常量与纯函数，不含业务证据/产品数据。
"""
import sys
import os
import json
import unittest
import tempfile
import shutil

# 把 scripts 目录加入搜索路径，以便导入 contracts
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from contracts import STAGES, STATUSES, VALID_TRANSITIONS, TERMINAL_STATES


class TestWorkflowState(unittest.TestCase):
    """workflow_state.py（及 contracts 状态契约）测试"""

    def test_valid_transitions(self):
        """测试合法状态迁移

        验证的业务规则（均为应放行的迁移）：
            PENDING→RUNNING（开始）、RUNNING→COMPLETED（正常完成）、
            RUNNING→WAITING_USER（需用户输入）、WAITING_USER→CONFIRMED（用户批准）、
            WAITING_USER→REJECTED（用户否决）。
            这些是流程主干上的正常路径，不能误判为非法。
        """
        from contracts import validate_transition

        # PENDING -> RUNNING
        self.assertTrue(validate_transition("PENDING", "RUNNING"))

        # RUNNING -> COMPLETED
        self.assertTrue(validate_transition("RUNNING", "COMPLETED"))

        # RUNNING -> WAITING_USER
        self.assertTrue(validate_transition("RUNNING", "WAITING_USER"))

        # WAITING_USER -> CONFIRMED
        self.assertTrue(validate_transition("WAITING_USER", "CONFIRMED"))

        # WAITING_USER -> REJECTED
        self.assertTrue(validate_transition("WAITING_USER", "REJECTED"))

    def test_invalid_transitions(self):
        """测试非法状态迁移

        验证的业务规则（均为应拒绝的迁移）：
            STOPPED→COMPLETED、REJECTED→COMPLETED（终态不可恢复，不得"复活"）；
            PENDING→COMPLETED（必须经过 RUNNING，不能一步到位伪造完成）。
            拒绝这些迁移是防止流程状态被伪造/越权跳转的关键。
        """
        from contracts import validate_transition

        # STOPPED -> COMPLETED（禁止）
        self.assertFalse(validate_transition("STOPPED", "COMPLETED"))

        # REJECTED -> COMPLETED（禁止）
        self.assertFalse(validate_transition("REJECTED", "COMPLETED"))

        # PENDING -> COMPLETED（禁止）
        self.assertFalse(validate_transition("PENDING", "COMPLETED"))

    def test_terminal_states(self):
        """测试终态判断

        验证的业务规则：
            STOPPED、REJECTED 是终态（不可再恢复）；
            COMPLETED、WAITING_USER 不是终态（前者允许重跑，后者等待用户决定）。
        """
        from contracts import is_terminal_state

        self.assertTrue(is_terminal_state("STOPPED"))
        self.assertTrue(is_terminal_state("REJECTED"))
        self.assertFalse(is_terminal_state("COMPLETED"))
        self.assertFalse(is_terminal_state("WAITING_USER"))

    def test_migrate_status(self):
        """测试状态迁移（兼容旧值）

        验证的业务规则：
            migrate_status 把历史遗留状态值映射为现行值：
            WAITING_USER_INPUT → WAITING_USER；
            已是现行值的（WAITING_USER/COMPLETED）保持不变。
            这是"向后兼容旧状态文件"的机制，保证新旧数据都能被正确识别。
        """
        from contracts import migrate_status

        # 旧值迁移
        self.assertEqual(migrate_status("WAITING_USER_INPUT"), "WAITING_USER")

        # 新值保持不变
        self.assertEqual(migrate_status("WAITING_USER"), "WAITING_USER")
        self.assertEqual(migrate_status("COMPLETED"), "COMPLETED")

    def test_is_gate_stage(self):
        """测试 Gate 阶段判断

        验证的业务规则：
            Gate 阶段（SIX_LENS_GATE、FIT_GATE，及 PRD_MVP_GATE）为 Gate；
            分析阶段（SIX_LENS、FIT_ASSESSMENT）不是 Gate。
            此判定决定"是否允许用 CONFIRMED 状态"等门禁分支的走向。
        """
        from contracts import is_gate_stage

        self.assertTrue(is_gate_stage("SIX_LENS_GATE"))
        self.assertTrue(is_gate_stage("FIT_GATE"))
        self.assertFalse(is_gate_stage("SIX_LENS"))
        self.assertFalse(is_gate_stage("FIT_ASSESSMENT"))

    def test_get_gate_stage_for_analysis(self):
        """测试分析阶段到 Gate 阶段的映射

        验证的业务规则：
            SIX_LENS→SIX_LENS_GATE、FIT_ASSESSMENT→FIT_GATE；
            INIT 无对应 Gate（返回 None）。
            该映射用于把 Gate 结果写入"对应的 Gate 阶段"而非覆盖分析阶段。
        """
        from contracts import get_gate_stage_for_analysis

        self.assertEqual(get_gate_stage_for_analysis("SIX_LENS"), "SIX_LENS_GATE")
        self.assertEqual(get_gate_stage_for_analysis("FIT_ASSESSMENT"), "FIT_GATE")
        self.assertIsNone(get_gate_stage_for_analysis("INIT"))


class TestStateStructure(unittest.TestCase):
    """状态文件结构测试（ensure_state_structure 的补齐与保真行为）"""

    def test_ensure_state_structure(self):
        """测试确保状态文件结构完整

        验证的业务规则：
            传入空状态 {} 经 ensure_state_structure 后，
            必须补齐 stage_results（dict）、hitl（dict）、schema_version 三个字段，
            使后续按固定路径取值不因缺字段而 KeyError。
        """
        from contracts import ensure_state_structure

        # 空状态
        state = {}
        state = ensure_state_structure(state)

        self.assertIn("stage_results", state)
        self.assertIn("hitl", state)
        self.assertIn("schema_version", state)
        self.assertIsInstance(state["stage_results"], dict)
        self.assertIsInstance(state["hitl"], dict)

    def test_ensure_state_structure_preserves_data(self):
        """测试确保结构时保留已有数据

        验证的业务规则：
            ensure_state_structure 只"补缺"，不"覆盖"：
            已有的 opportunity_id 与 stage_results 内容必须原样保留，
            避免补齐结构时误清已有进度。
        """
        from contracts import ensure_state_structure

        state = {
            "opportunity_id": "test",
            "stage_results": {"SIX_LENS": {"status": "COMPLETED"}}
        }
        state = ensure_state_structure(state)

        self.assertEqual(state["opportunity_id"], "test")
        self.assertIn("SIX_LENS", state["stage_results"])


if __name__ == "__main__":
    # 直接运行时执行全部用例
    unittest.main(verbosity=2)
