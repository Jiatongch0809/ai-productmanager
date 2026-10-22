"""分片任务链路：增量落盘、缺片阻断、合并与落库。

【本文件在机制里的位置与职责】
    本文件是分片任务（innovation:task）链路的集成测试。
    分片机制解决的问题是：子 Agent 上下文随时可能丢失，
    于是把大阶段（如 SIX_LENS 六看）拆成多个分片，子 Agent 每完成一片就立即落盘，
    再由 merge 在脚本内合并（大块证据不经模型上下文）。
    本文件逐环节验证：manifest 登记 → 逐片落盘 → 缺片拒绝合并 → 合并去重 →
    交回主 Agent 写入正式台账 → 触发 Gate。这正是"子 Agent 只写暂存、主 Agent 才写台账"
    这条边界契约的可执行证据。

【测试数据声明】
    本文件中的产品名、来源（"测试来源"）、事实文本、日期等
    全部是虚构样例，仅用于驱动分片/合并/校验分支，不代表真实产品的证据或结论。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date


class TestTaskParts(unittest.TestCase):
    """分片任务链路端到端测试"""

    def setUp(self):
        """为每个用例准备隔离的临时工作区并复制 scripts/

        为什么这么设计：
            同 test_end_to_end：本测试通过真实子进程调用 ops.py，
            用临时目录 + 复制的 scripts/ 与真实 runtime/ 完全隔离，避免污染。
        """
        self.test_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.test_dir)
        os.makedirs("runtime/evidence_ledger")
        os.makedirs("runtime/workflow_state")
        os.makedirs("runtime/logs")
        os.makedirs("runtime/outputs")
        self.scripts_dir = os.path.join(self.test_dir, "scripts")
        shutil.copytree(os.path.join(self.original_cwd, "scripts"), self.scripts_dir)

    def tearDown(self):
        """恢复 CWD 并清理临时工作区"""
        os.chdir(self.original_cwd)
        shutil.rmtree(self.test_dir)

    def run_ops(self, args):
        """通过统一入口 ops.py 运行命令"""
        command = [sys.executable, os.path.join(self.scripts_dir, "ops.py")]
        command.extend(args)
        return subprocess.run(command, cwd=self.test_dir, capture_output=True, text=True)

    def init_ledger(self, opportunity_id="task_demo"):
        """辅助方法：初始化机会台账（提供写分片/合并的前置环境）"""
        result = self.run_ops([
            "innovation:ledger", "init", "--id", opportunity_id,
            "--product", "测试产品", "--user", "测试用户",
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def write_part_file(self, name, payload):
        """把分片 payload 写成临时 JSON 文件，返回其路径

        为什么这么设计：
            分片内容以文件路径传给 `task part --file`，
            这样大 payload 不必经过模型上下文与命令行转义（与生产用法一致）。
        """
        path = os.path.join(self.test_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def part_payload(self, dimension, conclusion="CONDITIONAL"):
        """构造一个合法分片的 payload

        为什么这么构造：
            字段刻意覆盖 dimension/conclusion/key_facts/unknowns/evidence，
            且 evidence 带齐 fact/source/date/grade/signal 五个必填字段，
            并附 corroborating_sources 以满足双源规则，
            使其能通过 task.validate_part 与 Gate 的可核查证据校验。
        """
        return {
            "dimension": dimension,
            "conclusion": conclusion,
            "key_facts": [f"{dimension} 关键事实"],
            "positive_signals": ["正向信号"],
            "negative_signals": ["反向信号"],
            "unknowns": [f"{dimension} 未知项"],
            "evidence": [{
                "dimension": dimension,
                "fact": f"{dimension} 事实",
                "source": "测试来源A",
                "corroborating_sources": ["测试来源B"],
                "date": date.today().isoformat(),
                "grade": "B",
                "signal": "正向",
            }],
        }

    def test_full_parts_flow_to_gate(self):
        """验证：六片齐备 → 合并 → 写台账 → 触发 Gate 的完整链路

        验证的业务规则：
            1) task init 登记 6 个分片，初始均为 PENDING；
            2) 逐片写入后，进度显示 done/missing；
            3) merge 合并出 6 条证据（去重后），缺片为空，证据等级统计 B:6，
               并生成 4 个中间文件（merged / evidence / dimensions / tasks）；
            4) 用 merge 产物写入正式六看台账，再跑 Gate，YES 应放行 CONTINUE。
        """
        opportunity_id = "task_demo"
        self.init_ledger(opportunity_id)
        dimensions = ["market", "capital", "industry", "policy", "user", "competitor"]

        # 1) 初始化分片清单：登记六维，初始状态均为 PENDING
        result = self.run_ops([
            "innovation:task", "init", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--parts", ",".join(dimensions),
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["parts"], {dim: "PENDING" for dim in dimensions})

        # 2) 逐片写入：每完成一片，已完成/缺失列表都应即时反映进度
        for index, dimension in enumerate(dimensions):
            part_file = self.write_part_file(
                f"{dimension}.json", self.part_payload(dimension)
            )
            result = self.run_ops([
                "innovation:task", "part", "--id", opportunity_id,
                "--stage", "SIX_LENS", "--part", dimension, "--file", part_file,
            ])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            progress = json.loads(result.stdout)
            self.assertEqual(progress["done"], dimensions[:index + 1])
            self.assertEqual(progress["missing"], dimensions[index + 1:])

        # 3) 合并：六片齐全，证据 6 条（每片 1 条），等级统计 B:6，缺片为空
        result = self.run_ops([
            "innovation:task", "merge", "--id", opportunity_id, "--stage", "SIX_LENS",
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        merged = json.loads(result.stdout)
        self.assertEqual(merged["evidence_count"], len(dimensions))
        self.assertEqual(merged["missing_parts"], [])
        self.assertEqual(merged["evidence_grades"]["B"], len(dimensions))
        # merge 产出 4 个中间文件都必须真实存在
        for path in merged["files"].values():
            self.assertTrue(os.path.exists(path), path)

        # 4) 用合并产物写入正式六看台账（大 payload 走文件，不经过命令行）
        result = self.run_ops([
            "innovation:ledger", "write-six-lens", "--id", opportunity_id,
            "--conclusion", "YES",
            "--evidence-file", merged["files"]["evidence"],
            "--dimensions-file", merged["files"]["dimensions"],
            "--tasks-file", merged["files"]["tasks"],
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        # 触发六看 Gate：六维齐备且核心维度证据达标，YES 应放行
        result = self.run_ops([
            "innovation:gate", "check", "--id", opportunity_id, "--stage", "SIX_LENS",
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "CONTINUE")

    def test_merge_blocks_on_missing_part_unless_partial(self):
        """验证：缺片默认拒绝合并，除非显式 --allow-partial

        验证的业务规则：
            merge 是"全片齐全才放行"的守卫：缺片时不带 --allow-partial → 退出码非 0，
            且提示缺失分片名（含 "user"）；带上 --allow-partial（明确降级）才允许合并，
            并在结果里如实标出 missing_parts=["user"]。
            这防止"漏证据却被悄悄当成完整结论"。
        """
        opportunity_id = "task_partial"
        self.init_ledger(opportunity_id)
        # 只登记 market,user 两片，且仅写入 market
        self.run_ops([
            "innovation:task", "init", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--parts", "market,user",
        ])
        market_file = self.write_part_file("market.json", self.part_payload("market"))
        self.run_ops([
            "innovation:task", "part", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--part", "market", "--file", market_file,
        ])

        # 缺 user：默认合并应失败，且错误信息含缺失片名
        result = self.run_ops([
            "innovation:task", "merge", "--id", opportunity_id, "--stage", "SIX_LENS",
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user", result.stdout)

        # 显式 --allow-partial：允许降级合并，并如实报告缺失片
        result = self.run_ops([
            "innovation:task", "merge", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--allow-partial",
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["missing_parts"], ["user"])

    def test_completed_part_survives_reinit(self):
        """上下文丢失后重新初始化，已完成分片不能被重置。

        验证的业务规则：
            子 Agent 上下文丢失后需要重新 task init（可能还追加了新分片）。
            此时已 DONE 的分片必须被保留（preserved_done），
            只有新增/未完成的片保持 PENDING，避免"重新初始化=进度清零"。
        """
        opportunity_id = "task_resume"
        self.init_ledger(opportunity_id)
        # 先登记 market,user，并写入 market
        self.run_ops([
            "innovation:task", "init", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--parts", "market,user",
        ])
        market_file = self.write_part_file("market.json", self.part_payload("market"))
        self.run_ops([
            "innovation:task", "part", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--part", "market", "--file", market_file,
        ])

        # 重新 init（新增 capital 分片）：已完成的 market 应被保留
        result = self.run_ops([
            "innovation:task", "init", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--parts", "market,user,capital",
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["preserved_done"], ["market"])
        self.assertEqual(payload["parts"]["market"], "DONE")
        self.assertEqual(payload["parts"]["user"], "PENDING")
        self.assertEqual(payload["parts"]["capital"], "PENDING")

    def test_part_validation_rejects_bad_input(self):
        """验证：分片校验拒绝三类脏输入

        验证的业务规则：
            1) 证据缺必填字段（该例缺 source/date/signal）→ 报"证据缺少必填字段"；
            2) 证据等级非法（如 "S"）→ 报"证据等级非法"；
            3) 写入未登记的分片名 → 报"未登记的分片"。
        """
        opportunity_id = "task_invalid"
        self.init_ledger(opportunity_id)
        self.run_ops([
            "innovation:task", "init", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--parts", "market",
        ])

        # 1) 证据缺少必填字段（缺少 source）
        bad_evidence = self.write_part_file("bad_evidence.json", {
            "conclusion": "YES", "evidence": [{"fact": "缺少来源"}],
        })
        result = self.run_ops([
            "innovation:task", "part", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--part", "market", "--file", bad_evidence,
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("证据缺少必填字段", result.stdout)

        # 2) 证据等级非法（grade="S"，不在 A/B/C 内；其余必填字段齐全）
        bad_grade = self.write_part_file("bad_grade.json", {
            "conclusion": "YES",
            "evidence": [{
                "fact": "事实", "source": "来源", "date": date.today().isoformat(),
                "grade": "S", "signal": "正向",
            }],
        })
        result = self.run_ops([
            "innovation:task", "part", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--part", "market", "--file", bad_grade,
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("证据等级非法", result.stdout)

        # 3) 写入未登记的分片名 "unknown"
        result = self.run_ops([
            "innovation:task", "part", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--part", "unknown", "--file", bad_grade,
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未登记的分片", result.stdout)

    def test_six_lens_rejects_invalid_dimension(self):
        """验证：六看分片名必须是合法维度，非法维度（bogus）被拒绝

        验证的业务规则：
            SIX_LENS 的分片只允许六个固定维度（market/capital/industry/policy/user/competitor）；
            传入 bogus 时 parse_parts 报"无效的六看维度"并非 0 退出。
        """
        self.init_ledger("task_dims")
        result = self.run_ops([
            "innovation:task", "init", "--id", "task_dims",
            "--stage", "SIX_LENS", "--parts", "market,bogus",
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无效的六看维度", result.stdout)


if __name__ == "__main__":
    # 直接运行时执行全部用例
    unittest.main(verbosity=2)
