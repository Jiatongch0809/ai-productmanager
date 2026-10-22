#!/usr/bin/env python3
"""工作流 B 轻量 Gate 检查器

【本文件在机制中的位置与职责】
    工作流 B（DEMAND_INSIGHT → BP_GENERATE → PRD_MVP → PRD_MVP_GATE → MODAO_DEMO）
    的产物存在性与结构完整性检查。它属于"确定性边界"的轻量一侧：
       - 只做文件/字段级的确定性校验（目录在不在、文件空不空、章节/键齐不齐），
         不做业务质量判断（质量判断留给 LLM 与人工）。
       - 在 PRD 输出阶段执行 HITL（Human-in-the-Loop）检查，确保：
         1. PRD 文档已生成；
         2. PRD 包含必要章节；
         3. 等待用户确认后才能进入 Demo 阶段。
       - 其他阶段（需求洞察、BP、Demo）执行轻量检查：
         1. 产物文件存在；2. 产物非空；3. 基本结构完整。

【与 gate_check.py 的分工】
    gate_check.py 管工作流 A 的证据/适配 Gate（重规则）；
    本文件管工作流 B 的产物检查（轻规则）。两者都只给"通过/不通过"，不替模型下业务结论。

【用法】
    python3 workflow_b_gate.py <opportunity_id> <stage>
    阶段: DEMAND_INSIGHT, BP_GENERATE, PRD_MVP, MODAO_DEMO

【退出码语义】
    0 = 该阶段产物检查通过
    1 = 未通过（存在 issue）或参数不足
"""
import json
import os
import sys
from typing import Dict, List, Tuple

# SCRIPTS_DIR 取的是本文件所在目录的"上一级"，即工作区根目录（scripts 的父目录）。
# 注意：变量名叫 SCRIPTS_DIR 但实际指向的是项目根，后续用它来定位 runtime/ 等路径；
# 命名与语义不一致属于历史遗留，交接时留意不要按名字误解其含义。
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

# 统一从 contracts 导入常量：阶段集合与状态枚举的唯一来源，禁止本文件硬编码。
from contracts import (
    WORKFLOW_B_STAGES,
    STATUSES,
    now_iso,
)


class WorkflowBGateChecker:
    """工作流 B Gate 检查器

    一个机会对应一个实例。检查逻辑按"阶段"拆成独立方法，
    便于上层编排器按阶段分派，也让每个检查点的规则集中在一处便于维护。
    """
    
    def __init__(self, opportunity_id: str):
        """初始化检查器，定位该机会在工作流 B 下的产物目录。

        参数：
            opportunity_id: 机会唯一标识，用于拼出产物目录。

        为什么这么设计：
            所有检查都围绕 self.workflow_b_dir 下的子目录（demand_insight/bp/prd/demo）进行，
            在构造函数里一次算好目录，避免各方法重复拼接路径而出错。
            注意路径写法为 evidence_ledger/<id>/workflow_b，即工作流 B 的产物
            挂在证据台账目录下，属约定而非物理隔离。
        """
        self.opportunity_id = opportunity_id
        # 产物根目录：runtime/evidence_ledger/<opportunity_id>/workflow_b
        # （SCRIPTS_DIR 实为项目根，'..' 再上一级……此处按原样保留，注意与注释不符的历史写法）
        self.workflow_b_dir = os.path.join(
            SCRIPTS_DIR,
            '..',
            'runtime',
            'evidence_ledger',
            opportunity_id,
            'workflow_b'
        )
    
    def check_demand_insight(self) -> Tuple[bool, List[str]]:
        """检查需求洞察阶段的产物。

        参数：无（使用实例上的 opportunity_id / workflow_b_dir）。

        返回：
            (通过, [问题列表])：通过=True 且问题列表为空；否则 False 并列出每个问题。

        为什么这么设计：
            每个问题单独成条而不是"首个错误即返回"，是为了让用户一次性看到全部缺失项，
            减少来回补件的次数；目录整体缺失时直接返回（此时列文件清单无意义）。
        """
        issues = []
        demand_insight_dir = os.path.join(self.workflow_b_dir, 'demand_insight')
        
        # 检查目录存在：目录都没有，说明该阶段根本没产出 → 直接判不通过并早退
        if not os.path.exists(demand_insight_dir):
            return False, ["需求洞察目录不存在"]
        
        # 检查必要文件：需求洞察阶段约定产出三个文件（叙述 + 结构化痛点 + 场景）
        required_files = [
            'demand_insight.md',
            'pain_points.json',
            'scenarios.json'
        ]
        
        for filename in required_files:
            filepath = os.path.join(demand_insight_dir, filename)
            if not os.path.exists(filepath):
                # 文件缺失：记为"缺少文件"
                issues.append(f"缺少文件: {filename}")
            elif os.path.getsize(filepath) == 0:
                # 空文件视同未产出：占位空文件不算有效产物
                issues.append(f"文件为空: {filename}")
        
        # 检查 pain_points.json 结构
        # 注意：仅当文件"存在"时才尝试解析；已判为缺失的文件不重复报错。
        pain_points_file = os.path.join(demand_insight_dir, 'pain_points.json')
        if os.path.exists(pain_points_file):
            try:
                with open(pain_points_file, 'r', encoding='utf-8') as f:
                    pain_points = json.load(f)
                if not isinstance(pain_points, list):
                    # 约定痛点列表为数组；不是数组则结构不符
                    issues.append("pain_points.json 应为数组格式")
                elif len(pain_points) == 0:
                    # 空数组等于没找到痛点，属实质缺失
                    issues.append("痛点列表为空")
            except json.JSONDecodeError:
                # JSON 损坏：结构无法校验，记为格式错误（不影响其它检查继续走）
                issues.append("pain_points.json 格式错误")
        
        # 通过 = 无任何 issue
        return len(issues) == 0, issues
    
    def check_bp(self) -> Tuple[bool, List[str]]:
        """检查 BP（商业计划）生成阶段的产物。

        参数：无。

        返回：
            (通过, [问题列表])，语义同 check_demand_insight。

        为什么这么设计：
            BP 是可选的中间产物，但一旦产出就要保证结构完整（关键字段齐全），
            否则后续 PRD 阶段引用商业模型时会取到空值。
        """
        issues = []
        bp_dir = os.path.join(self.workflow_b_dir, 'bp')
        
        # 检查目录存在
        if not os.path.exists(bp_dir):
            return False, ["BP 目录不存在"]
        
        # 检查必要文件：一份人读的商业计划 + 一份机读的商业模型
        required_files = [
            'business_plan.md',
            'business_model.json'
        ]
        
        for filename in required_files:
            filepath = os.path.join(bp_dir, filename)
            if not os.path.exists(filepath):
                issues.append(f"缺少文件: {filename}")
            elif os.path.getsize(filepath) == 0:
                issues.append(f"文件为空: {filename}")
        
        # 检查 business_model.json 结构
        business_model_file = os.path.join(bp_dir, 'business_model.json')
        if os.path.exists(business_model_file):
            try:
                with open(business_model_file, 'r', encoding='utf-8') as f:
                    business_model = json.load(f)
                # 商业模型必须齐备这三个键：价值主张 / 收入模型 / 目标市场
                required_keys = ['value_proposition', 'revenue_model', 'target_market']
                for key in required_keys:
                    if key not in business_model:
                        issues.append(f"business_model.json 缺少字段: {key}")
            except json.JSONDecodeError:
                issues.append("business_model.json 格式错误")
        
        return len(issues) == 0, issues
    
    def check_prd_mvp(self) -> Tuple[bool, List[str]]:
        """检查 PRD MVP 阶段的产物（本阶段同时是 HITL Gate）。

        参数：无。

        返回：
            (通过, [问题列表])，语义同上。

        为什么这么设计：
            这是进入 Demo 前的最后一道自动检查，因此规则比前两阶段更细：
            除文件存在/非空外，还要求 PRD 正文含固定章节标题、用户故事与功能列表
            须成数组且逐条具备必要字段。检查越细，Demo 阶段返工的概率越低。
            注意：本方法只校验"齐不齐"，不评价 PRD 内容质量；质量由后续人工 HITL 确认。
        """
        issues = []
        prd_dir = os.path.join(self.workflow_b_dir, 'prd')
        
        # 检查目录存在
        if not os.path.exists(prd_dir):
            return False, ["PRD 目录不存在"]
        
        # 检查必要文件：正文 + 用户故事 + 功能清单
        required_files = [
            'prd.md',
            'user_stories.json',
            'features.json'
        ]
        
        for filename in required_files:
            filepath = os.path.join(prd_dir, filename)
            if not os.path.exists(filepath):
                issues.append(f"缺少文件: {filename}")
            elif os.path.getsize(filepath) == 0:
                issues.append(f"文件为空: {filename}")
        
        # 检查 PRD 文档结构
        # 用"章节标题子串是否出现"来判结构：约定标题为一级标题固定文案。
        # 这种检查廉价但脆弱（大小写/编号变化会误判），交接时留意 PRD 模板需与这里保持字面一致。
        prd_file = os.path.join(prd_dir, 'prd.md')
        if os.path.exists(prd_file):
            with open(prd_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 检查必要章节
            required_sections = [
                '# 产品概述',
                '# 目标用户',
                '# 核心功能',
                '# 用户故事',
                '# 验收标准'
            ]
            
            for section in required_sections:
                # 任一必需章节文案未出现即视为缺章节
                if section not in content:
                    issues.append(f"PRD 缺少章节: {section}")
        
        # 检查 user_stories.json 结构
        user_stories_file = os.path.join(prd_dir, 'user_stories.json')
        if os.path.exists(user_stories_file):
            try:
                with open(user_stories_file, 'r', encoding='utf-8') as f:
                    user_stories = json.load(f)
                if not isinstance(user_stories, list):
                    issues.append("user_stories.json 应为数组格式")
                elif len(user_stories) == 0:
                    issues.append("用户故事列表为空")
                else:
                    # 检查每个用户故事的必要字段：经典"角色-目标-收益"三要素
                    for i, story in enumerate(user_stories):
                        if 'role' not in story or 'goal' not in story or 'benefit' not in story:
                            issues.append(f"用户故事 {i+1} 缺少必要字段 (role/goal/benefit)")
            except json.JSONDecodeError:
                issues.append("user_stories.json 格式错误")
        
        # 检查 features.json 结构
        features_file = os.path.join(prd_dir, 'features.json')
        if os.path.exists(features_file):
            try:
                with open(features_file, 'r', encoding='utf-8') as f:
                    features = json.load(f)
                if not isinstance(features, list):
                    issues.append("features.json 应为数组格式")
                elif len(features) == 0:
                    issues.append("功能列表为空")
                else:
                    # 检查每个功能的必要字段：名称 + 优先级（用于排期取舍）
                    for i, feature in enumerate(features):
                        if 'name' not in feature or 'priority' not in feature:
                            issues.append(f"功能 {i+1} 缺少必要字段 (name/priority)")
            except json.JSONDecodeError:
                issues.append("features.json 格式错误")
        
        return len(issues) == 0, issues
    
    def check_modao_demo(self) -> Tuple[bool, List[str]]:
        """检查墨刀（MODAO）Demo 阶段的产物。

        参数：无。

        返回：
            (通过, [问题列表])，语义同上。

        为什么这么设计：
            Demo 是流程终点，检查重点是"有没有可交付物"：一份 manifest（含原型 ID、
            时间、页面）与一份原型链接。只要这两样齐备即算通过，不校验 UI 质量。
        """
        issues = []
        demo_dir = os.path.join(self.workflow_b_dir, 'demo')
        
        # 检查目录存在
        if not os.path.exists(demo_dir):
            return False, ["Demo 目录不存在"]
        
        # 检查必要文件：清单 + 原型链接
        required_files = [
            'demo_manifest.json',
            'prototype_url.txt'
        ]
        
        for filename in required_files:
            filepath = os.path.join(demo_dir, filename)
            if not os.path.exists(filepath):
                issues.append(f"缺少文件: {filename}")
            elif os.path.getsize(filepath) == 0:
                issues.append(f"文件为空: {filename}")
        
        # 检查 demo_manifest.json 结构
        manifest_file = os.path.join(demo_dir, 'demo_manifest.json')
        if os.path.exists(manifest_file):
            try:
                with open(manifest_file, 'r', encoding='utf-8') as f:
                    manifest = json.load(f)
                # 原型 ID / 生成时间 / 页面列表为必需字段
                required_keys = ['prototype_id', 'created_at', 'pages']
                for key in required_keys:
                    if key not in manifest:
                        issues.append(f"demo_manifest.json 缺少字段: {key}")
            except json.JSONDecodeError:
                issues.append("demo_manifest.json 格式错误")
        
        return len(issues) == 0, issues
    
    def run_gate_check(self, stage: str) -> Dict:
        """执行指定阶段的 Gate 检查，统一返回结构化结果。

        Args:
            stage: 阶段名称（应在 WORKFLOW_B_STAGES 内）。

        Returns:
            {
                'stage': str,
                'passed': bool,
                'issues': List[str],
                'requires_hitl': bool,
                'timestamp': str
            }

        为什么这么设计：
            把"检查"与"结果封装"分开：上层只需读取 passed 与 requires_hitl，
            不必关心各阶段内部细节。requires_hitl 标记该阶段是否需要人工确认
            （本设计里仅 PRD_MVP 为 True）——注意此处只"标记需要 HITL"，
            真正的"必须等用户确认后才能进 Demo"由状态机/上层编排强制执行。
        """
        # 阶段合法性校验：不在工作流 B 阶段集合内 → 直接判不通过（注意 passed=False）
        if stage not in WORKFLOW_B_STAGES:
            return {
                'stage': stage,
                'passed': False,
                'issues': [f"未知阶段: {stage}"],
                'requires_hitl': False,
                'timestamp': now_iso()
            }
        
        # 执行对应阶段的检查
        if stage == 'DEMAND_INSIGHT':
            passed, issues = self.check_demand_insight()
            requires_hitl = False
        elif stage == 'BP_GENERATE':
            passed, issues = self.check_bp()
            requires_hitl = False
        elif stage == 'PRD_MVP':
            passed, issues = self.check_prd_mvp()
            requires_hitl = True  # PRD 阶段需要 HITL
        elif stage == 'MODAO_DEMO':
            passed, issues = self.check_modao_demo()
            requires_hitl = False
        else:
            # 注意：本分支会兜住"在 WORKFLOW_B_STAGES 内但没有专属检查方法"的阶段，
            # 例如 PRD_MVP_GATE（HITL Gate 阶段本身）。此处默认 passed=True（fail-open），
            # 即该阶段会"无条件通过"。交接时需确认这是否符合预期——
            # 若 PRD_MVP_GATE 也需要校验，应补检查方法；当前仅标注，不改代码。
            passed, issues = True, []
            requires_hitl = False
        
        return {
            'stage': stage,
            'passed': passed,
            'issues': issues,
            'requires_hitl': requires_hitl,
            'timestamp': now_iso()
        }


def main():
    """命令行入口：解析参数、执行检查、按结果设置退出码。"""
    # 参数不足（缺 opportunity_id 或 stage）→ 打印用法并退出码 1
    if len(sys.argv) < 3:
        print("用法: python3 workflow_b_gate.py <opportunity_id> <stage>")
        print("阶段: DEMAND_INSIGHT, BP_GENERATE, PRD_MVP, MODAO_DEMO")
        sys.exit(1)
    
    # 参数顺序：argv[1]=机会 ID，argv[2]=阶段名
    opportunity_id = sys.argv[1]
    stage = sys.argv[2]
    
    checker = WorkflowBGateChecker(opportunity_id)
    result = checker.run_gate_check(stage)
    
    # 输出带缩进的 JSON，便于人读排查
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # 返回码：0=通过，1=未通过
    # 注意：这里的"未通过"含两种情形——产物有问题（passed=False），
    # 以及未知阶段（passed=False），调用方应结合 issues 区分。
    sys.exit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
