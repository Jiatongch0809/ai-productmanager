"""通过公开 ops 入口验证完整工作流。

【本文件在机制里的位置与职责】
    本文件是端到端（E2E）集成测试：不直接调用底层脚本函数，而是通过
    `python scripts/ops.py innovation:...` 这一"公开命令入口"驱动完整流程，
    以验证「入口路由 → 命令封装 → 底层脚本(subprocess) → 状态/台账落盘」
    这一整条链路在真实子进程环境下是否贯通。
    它覆盖的关键业务规则：业务命名空间自动发现、必须从 INIT 起步、
    Gate 的 ERROR/STOP/WAIT_USER 分支、以及报告生成的门禁。

【测试数据声明】
    本文件中的产品名（"测试产品"）、用户（"测试用户"）、机会 ID（test_xxx）
    以及写入的六看/适配度数值全部是虚构样例，仅用于驱动流程分支，
    不代表任何真实产品的证据或结论。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date


class TestEndToEnd(unittest.TestCase):
    """端到端测试：每个用例在独立临时工作区里跑真实子进程命令"""

    def setUp(self):
        """为每个用例准备隔离的临时工作区

        为什么这么设计：
            E2E 测试会真实写 runtime/ 下的状态与台账，必须与真实工作区隔离，
            否则会污染生产数据。做法是：新建临时目录 → 切换 CWD 到该目录 →
            在其中创建 runtime 子目录 + 复制一份 scripts/（含 ops.py），
            这样子进程运行的脚本、写入的相对路径都落在临时目录内。
        """
        self.test_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.test_dir)
        # 预建运行时目录结构（与生产一致），避免脚本因目录缺失失败
        os.makedirs("runtime/evidence_ledger")
        os.makedirs("runtime/workflow_state")
        os.makedirs("runtime/logs")
        os.makedirs("runtime/outputs")
        source_scripts = os.path.join(self.original_cwd, "scripts")
        self.scripts_dir = os.path.join(self.test_dir, "scripts")
        # 把生产 scripts/ 整棵复制进临时目录，保证子进程能 import contracts 等模块
        shutil.copytree(source_scripts, self.scripts_dir)

    def tearDown(self):
        """恢复 CWD 并删除临时工作区，确保用例间互不影响"""
        os.chdir(self.original_cwd)
        shutil.rmtree(self.test_dir)

    def run_script(self, name, args=None, env=None, stdin=None):
        """运行临时工作区内的某个脚本子进程

        参数：
            name: scripts/ 下的脚本文件名（如 ops.py / evidence_ledger.py）
            args: 命令行参数列表
            env: 额外环境变量（与当前环境合并）
            stdin: 标准输入内容
        返回：
            subprocess.CompletedProcess（含 returncode/stdout/stderr）
        为什么这么设计：
            用真实子进程而非函数调用，才能验证退出码、stdout 等"进程级契约"，
            这正是主 Agent 与脚本交互的真实方式。
        """
        command = [sys.executable, os.path.join(self.scripts_dir, name)]
        command.extend(args or [])
        return subprocess.run(
            command,
            cwd=self.test_dir,
            env={**os.environ, **(env or {})},
            input=stdin,
            capture_output=True,
            text=True,
        )

    def run_ops(self, args):
        """通过统一入口 ops.py 运行命令（E2E 的主入口）"""
        return self.run_script("ops.py", args)

    def test_business_namespace_is_discovered_without_router_change(self):
        """验证：新增一个业务命名空间无需改动路由即可被发现

        业务规则：
            ops.py 用 pkgutil 自动扫描 ops_commands/ 下的子包，
            子包只需实现 register_commands(registry) 即可注册命令，
            路由层无需硬编码任何业务名。本用例临时新建 marketing 命名空间，
            注册一个 health 命令，并验证 `innovation/marketing` 前缀能正确路由。
        为什么这么构造：
            动态写入 __init__.py 模拟"将来新增业务线"的场景，
            以此证明扩展性契约未被破坏。
        """
        # 在临时 scripts/ops_commands 下新建 marketing 子包
        namespace_dir = os.path.join(self.scripts_dir, "ops_commands", "marketing")
        os.makedirs(namespace_dir)
        # 写入一个最小 register_commands，注册 health 命令（打印固定 JSON）
        with open(os.path.join(namespace_dir, "__init__.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "def register_commands(commands):\n"
                "    commands['health'] = lambda args: print('{\\\"status\\\": \\\"ok\\\"}')\n"
            )

        # 通过入口调用新命名空间命令：返回码 0 且输出 status=ok
        result = self.run_ops(["marketing:health"])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")

    def test_state_cannot_start_after_init(self):
        """验证：新工作流必须从 INIT 阶段开始，不能凭空跳到后续阶段

        业务规则：
            workflow_state.update_state 规定：当状态文件不存在时，
            若传入阶段不是 INIT，则拒绝并提示"新的工作流必须从 INIT 阶段开始"。
            这是防止绕过 INIT 直接伪造后续阶段的硬约束。
        """
        result = self.run_ops([
            "innovation:state", "update", "--id", "bypass",
            "--stage", "FIT_ASSESSMENT", "--status", "RUNNING"
        ])
        # 期望非 0 退出码，且错误信息里出现 "INIT"
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INIT", result.stdout)

    def test_gate_error_marks_gate_stage_failed(self):
        """验证：Gate 判定 ERROR 时，Gate 阶段被标记为 FAILED、退出码为 2

        业务规则：
            六看结论缺失 → check_six_lens 返回 ERROR → ACTION_TO_STATUS 映射为 FAILED，
            写入 SIX_LENS_GATE（而非分析阶段），且进程退出码为 2。
            这保证"错误"能被主 Agent 明确识别，不会停在旧阶段静默继续。
        """
        opportunity_id = "missing_conclusion"
        self.init(opportunity_id)
        # 先把 SIX_LENS 标记为 RUNNING → COMPLETED（满足 Gate 要求分析阶段已完成）
        result = self.run_ops([
            "innovation:state", "update", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--status", "RUNNING"
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        result = self.run_ops([
            "innovation:state", "update", "--id", opportunity_id,
            "--stage", "SIX_LENS", "--status", "COMPLETED"
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        # 此时台账里没有六看结论 → Gate 应报 ERROR（退出码 2）
        result = self.run_ops([
            "innovation:gate", "check", "--id", opportunity_id,
            "--stage", "SIX_LENS"
        ])
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        state = json.loads(self.run_ops([
            "innovation:state", "read", "--id", opportunity_id
        ]).stdout)
        self.assertEqual(state["stage_results"]["SIX_LENS_GATE"]["status"], "FAILED")

    def init(self, opportunity_id):
        """辅助方法：初始化机会（建台账 + 把 INIT 置为 COMPLETED）"""
        result = self.run_ops([
            "innovation:ledger", "init", "--id", opportunity_id,
            "--product", "测试产品", "--user", "测试用户"
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def write_six_lens(self, opportunity_id, conclusion):
        """写入六看结果：六维齐备，核心三维带可核查证据

        为什么这么构造：
            YES 放行要求六维都有结论，且 market/user/competitor
            各有一条 A/B 级、正向、双源、时效内的证据。
            其余三维只给结论，用于驱动正常分支。
        """
        dimensions = json.dumps({
            dimension: {"conclusion": "CONDITIONAL"}
            for dimension in ["market", "capital", "industry", "policy", "user", "competitor"]
        })
        evidence = json.dumps([
            {
                "dimension": dimension,
                "fact": f"{dimension} 合成事实",
                "source": f"{dimension}-source-a",
                "corroborating_sources": [f"{dimension}-source-b"],
                "date": date.today().isoformat(),
                "grade": "A",
                "signal": "正向",
            }
            for dimension in ["market", "user", "competitor"]
        ], ensure_ascii=False)
        result = self.run_ops([
            "innovation:ledger", "write-six-lens", "--id", opportunity_id,
            "--conclusion", conclusion, "--dimensions", dimensions,
            "--evidence", evidence,
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def write_fit(self, opportunity_id):
        """写入适配度评分（八维全部 9 分 → 加权总分 90 → S51）

        为什么这么构造：
            八维全给 9 分，使加权总分达到 S51 高适配档，用于验证"高适配可出完整报告"。
            write-fit 要求 FIT_ASSESSMENT 子任务 manifest 已 completed 且评分一致，
            因此先按生产契约写入 manifest；证据覆盖三个关键内部适配维度，
            使 S51 Gate 能真正放行。
        """
        scores = {key: 9 for key in [
            "market_certainty", "user_demand", "category_validation", "ai_value",
            "org_capability", "quick_launch", "business_path", "cost_risk"
        ]}
        task_dir = os.path.join("runtime", "tasks", opportunity_id, "FIT_ASSESSMENT")
        os.makedirs(task_dir, exist_ok=True)
        manifest = {
            "opportunity_id": opportunity_id,
            "stage": "FIT_ASSESSMENT",
            "status": "completed",
            "result": {"scores": scores},
        }
        with open(os.path.join(task_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False)

        evidence = json.dumps([
            {
                "dimension": dimension,
                "fact": f"{dimension} 合成事实",
                "source": f"{dimension}-source-a",
                "corroborating_sources": [f"{dimension}-source-b"],
                "date": date.today().isoformat(),
                "grade": "A",
                "signal": "正向",
            }
            for dimension in ["org_capability", "quick_launch", "business_path"]
        ], ensure_ascii=False)
        result = self.run_ops([
            "innovation:ledger", "write-fit", "--id", opportunity_id,
            "--scores", json.dumps(scores), "--evidence", evidence,
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_full_workflow_yes_to_generate(self):
        """验证：YES 六看 → 六看 Gate 继续 → 适配度 → 生成完整评估报告

        业务规则：
            高适配（S51）且六看 YES 时，流程应一路 CONTINUE 直到出完整报告。
        """
        opportunity_id = "test_001"
        self.init(opportunity_id)
        self.write_six_lens(opportunity_id, "YES")

        # 六看 Gate 应判 CONTINUE
        result = self.run_ops([
            "innovation:gate", "check", "--id", opportunity_id, "--stage", "SIX_LENS"
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["action"], "CONTINUE")

        self.write_fit(opportunity_id)
        # 适配度 Gate（此处只断言进程成功，未断言具体 action）
        result = self.run_ops([
            "innovation:gate", "check", "--id", opportunity_id,
            "--stage", "FIT_ASSESSMENT"
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        # 生成完整评估报告：期望 status=generated
        result = self.run_ops([
            "innovation:report", "generate", "--id", opportunity_id,
            "--type", "full_evaluation"
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "generated")

    def test_conditional_wait_confirm_and_continue(self):
        """验证：CONDITIONAL → WAIT_USER → list-pending 可见 → resume 批准 → CONFIRMED

        业务规则：
            六看 CONDITIONAL 时 Gate 判 WAIT_USER，流程进入等待；
            `state list-pending` 应能列出该待办；
            用户 `resume --approve true` 后，顶层状态与 Gate 阶段状态都要变为 CONFIRMED
            （两者同步是报告门禁识别"已确认"的前提）。
        """
        opportunity_id = "test_002"
        self.init(opportunity_id)
        self.write_six_lens(opportunity_id, "CONDITIONAL")

        result = self.run_ops([
            "innovation:gate", "check", "--id", opportunity_id, "--stage", "SIX_LENS"
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["action"], "WAIT_USER")

        # 待处理列表应包含该机会
        result = self.run_ops(["innovation:state", "list-pending"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)[0]["opportunity_id"], opportunity_id)

        # 用户批准继续
        result = self.run_ops([
            "innovation:state", "resume", "--id", opportunity_id,
            "--approve", "true"
        ])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        # 直接读状态文件校验：顶层与 Gate 阶段状态都应同步为 CONFIRMED
        with open(os.path.join("runtime", "workflow_state", opportunity_id + ".json"), encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(state["status"], "CONFIRMED")
        self.assertEqual(state["stage_results"]["SIX_LENS_GATE"]["status"], "CONFIRMED")

        # 确认后继续走适配度评估
        self.write_fit(opportunity_id)

    def test_no_stops_and_blocks_report(self):
        """验证：六看 NO → Gate STOP → 报告生成被门禁拦截

        业务规则：
            六看结论为 NO（否定机会）时，Gate 判 STOP，流程终止，禁止再出报告。
            STOP 是正常业务结论而非脚本错误，因此 gate check 退出码为 0。
        """
        opportunity_id = "test_003"
        self.init(opportunity_id)
        self.write_six_lens(opportunity_id, "NO")

        result = self.run_ops([
            "innovation:gate", "check", "--id", opportunity_id, "--stage", "SIX_LENS"
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "STOP")

        # 报告生成应被拦截（退出码非 0）
        result = self.run_ops([
            "innovation:report", "generate", "--id", opportunity_id,
            "--type", "full_evaluation"
        ])
        self.assertEqual(result.returncode, 1)

    def test_missing_state_rejects_report(self):
        """验证：只有台账、没有状态文件时，报告生成必须被拒绝

        业务规则：
            check_all_gates 的第一道关：状态文件不存在则拒绝（fail-closed），
            而不是因"没有记录"就默认放行。这里刻意只初始化台账、不建状态文件，
            以验证该拒绝路径，错误信息应包含"状态文件不存在"。
        """
        opportunity_id = "test_004"
        # 直接用底层脚本初始化台账（会产生台账文件，但不会创建状态文件）
        result = self.run_script(
            "evidence_ledger.py", ["init"],
            env={"OPPORTUNITY_ID": opportunity_id},
            stdin=json.dumps({"product_name": "测试产品"}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        # 生成报告：应被门禁拒绝（退出码 1），且错误信息含"状态文件不存在"
        result = self.run_ops([
            "innovation:report", "generate", "--id", opportunity_id,
            "--type", "full_evaluation"
        ])
        self.assertEqual(result.returncode, 1)
        self.assertIn("状态文件不存在", json.loads(result.stdout)["error"])


if __name__ == "__main__":
    # 直接运行本文件时执行全部用例（verbosity=2 输出每个用例名）
    unittest.main(verbosity=2)
