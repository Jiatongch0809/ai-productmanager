#!/usr/bin/env python3
"""统一操作入口 — 命令注册 + 自动发现架构

用法：
    python3 scripts/ops.py <namespace>:<command> [args]
    python3 scripts/ops.py innovation:state update --id xxx --stage INIT --status RUNNING
    python3 scripts/ops.py innovation:ledger write-fit --id xxx --scores '{...}'
    python3 scripts/ops.py innovation:gate check --id xxx --stage SIX_LENS

架构：
    - 入口层（本文件）：只做路由，不包含业务逻辑
    - 命令层（ops_commands/）：每个工作流一个目录，自注册命令
    - 底层脚本：保持不变，由命令层调用

扩展：
    - 新工作流：在 ops_commands/ 下新建目录，实现 register_commands()
    - 不需要修改本文件

本文件在整体机制里的位置（交接重点）：
    这是主 Agent（LLM）访问所有确定性能力的唯一命令行入口，处在「不确定交给 LLM，
    确定交给脚本」这条设计原则的『确定』一侧。它自身不含任何业务逻辑，只负责四件事：
      1) 自动发现 ops_commands/<namespace>/ 下各业务线注册的命令；
      2) 把本地 key（如 state.read）前缀化成 <namespace>:<key>（如 innovation:state.read）；
      3) 对创新机会 ID（--id）做白名单正则校验，防止路径片段进入本地文件名；
      4) 把剩余参数交给命令层 handler，并让底层脚本的退出码原样透传。

为什么这样分层：
    - 入口层只做路由：业务线扩展无需触碰这个高风险入口，降低误改概率；
    - 命令层（ops_commands/）是薄封装：负责参数解析与环境变量准备，多数命令用
      subprocess 调 scripts/ 下的底层脚本，并刻意保留底层退出码（吞掉退出码会让
      主 Agent 把失败当成功继续往下跑）；
    - 底层脚本（scripts/*.py）才是真正的确定性边界：证据校验、评分、Gate、状态迁移、
      幂等、退出码都由它们定义。
"""
import sys          # sys.argv 取参、sys.stderr 报错、sys.exit 控制退出码
import os           # 计算本文件目录，拼出 scripts/ 绝对路径供 import
import importlib    # 按模块名动态导入 ops_commands.<namespace>
import pkgutil      # 扫描 ops_commands 包下的子包，实现命名空间自动发现
import re           # 用正则 fullmatch 校验机会 ID 白名单

# 添加 scripts 目录到路径
# 目的：无论调用方从哪个 cwd 执行 ops.py，都能直接 import scripts/ 下的同级模块
# （命令层里的 innovation/*.py 需要 `import contracts` 等），不依赖调用方的工作目录。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def discover_commands():
    """自动发现并汇总所有命名空间的命令，构建路由表。

    What（做什么）：
        逐个扫描 ops_commands/ 包下的子包（每个子包 = 一条业务线/工作流，例如 innovation），
        动态导入它，调用约定接口 register_commands(registry)，把该命名空间注册的本地 key
        （如 state.read）前缀化成 <namespace>:<key>（如 innovation:state.read），汇总成命令表。

    参数：无。命名空间由 pkgutil 扫描目录自动发现，不依赖手工维护清单。

    返回值：
        dict[str, callable] —— key 为全限定命令名（如 innovation:gate.check），
        value 为命令层 handler 函数（接收“剩余参数列表 args”，形如 ['--id','xxx']）。

    异常与退出码：
        本函数不主动 sys.exit。加载单个命名空间失败只在 stderr 打 WARNING 并继续，
        保证坏模块不影响其它命令；若连 ops_commands 包本身都导不进来，才打 ERROR。

    为什么这么设计：
        - 前缀化命名空间：不同业务线可以安全复用同名命令（都用 state.read / gate.check），
          路由表 key 全局唯一且可读，报错与日志里能一眼看出命令属于哪条线。
        - 约定式注册：新增业务线只需在 ops_commands/ 下建目录并实现 register_commands()，
          完全不用改动本入口文件，扩展成本为零。
        - 隔离失败：逐个模块 try/except，坏模块只影响自己，不会让整个入口不可用。
    """
    # 最终命令表：key 为 "<namespace>:<local_key>"，value 为对应 handler 函数
    commands = {}
    
    try:
        # 导入 ops_commands 包本体，取得其 __path__ 用于扫描子包
        import ops_commands
        # iter_modules 逐个产出包下的模块/子包，三元组为 (finder, name, ispkg)
        for _, namespace, ispkg in pkgutil.iter_modules(ops_commands.__path__):
            # 只认“子包”（含 __init__.py 的目录）：单个 .py 文件不构成命名空间
            # 下划线开头的目录视为私有/工具包，跳过，避免被误当作命令命名空间
            if not ispkg or namespace.startswith('_'):
                continue
            modname = f'ops_commands.{namespace}'
            try:
                # 动态导入该命名空间包；其 __init__.py 里应暴露 register_commands
                module = importlib.import_module(modname)
                if hasattr(module, 'register_commands'):
                    # 用一个独立 registry 先收集该命名空间的本地 key，
                    # 收集完再统一前缀化，便于在这里集中做重名冲突检测
                    namespace_commands = {}
                    module.register_commands(namespace_commands)
                    for key, handler in namespace_commands.items():
                        # 前缀化：state.read -> innovation:state.read，隔离各业务线命名空间
                        qualified_key = f'{namespace}:{key}'
                        # 重名直接抛错：同名命令会静默覆盖造成路由歧义，宁可启动即失败
                        if qualified_key in commands:
                            raise RuntimeError(f'duplicate command: {qualified_key}')
                        commands[qualified_key] = handler
            except Exception as e:
                # 单个命名空间加载失败：只告警、不中断，其它命令仍可用
                print(f"WARNING: 加载模块 {modname} 失败: {e}", file=sys.stderr)
    except ImportError as e:
        # ops_commands 包整体缺失属结构性错误，提示到 stderr（函数仍返回已收集的命令）
        print(f"ERROR: 无法加载 ops_commands 包: {e}", file=sys.stderr)
    
    return commands


def main():
    """入口主流程：解析命令名 → 构建路由表 → 校验机会 ID → 调用 handler。

    参数：无（直接读取 sys.argv）。
        约定 sys.argv[1] 为命令名，支持两种等价写法：
            namespace:command subcommand   —— 子命令作为独立参数传入（如 innovation:state update）
            namespace:command:subcommand   —— 子命令写在命令名里（如 innovation:state:update）
        sys.argv[2:] 为剩余参数，原样透传给 handler（如 --id、--stage、--status）。

    返回值：无。正常路径下由被调用的 handler 决定实际行为（多会 subprocess 调底层脚本）。

    退出码：
        缺用法/命令名格式错误/未知命令 → sys.exit(1)；
        机会 ID 不合法（仅 innovation 命名空间）→ sys.exit(2)；
        正常时本函数不 sys.exit，最终退出码由 handler 内部的底层脚本决定并透传。

    为什么这么设计：
        - 入口不入侵 handler：路由与业务解耦，新增命令只需改 ops_commands/。
        - ID 校验前置：机会 ID 会被拼进 runtime/ 下的文件名（台账、分片、日志），
          若放行 ../、/、\ 等路径片段将造成目录穿越/越权写文件，所以在这一层就用白名单
          正则拦下；用独立退出码 2 与一般参数错误（1）区分开，便于上层/告警识别。
    """
    # 情况一：连命令名都没有 —— 打印用法并列出全部可用命令，退出码 1
    if len(sys.argv) < 2:
        print("用法: python3 scripts/ops.py <namespace>:<command> [args]")
        print("示例: python3 scripts/ops.py innovation:state update --id xxx --stage INIT --status RUNNING")
        print("\n可用命令:")
        # 现取现列：把当前所有已注册命令打印给用户，便于发现拼写错误
        commands = discover_commands()
        for cmd_name in sorted(commands.keys()):
            print(f"  {cmd_name}")
        sys.exit(1)
    
    # 解析命令名
    command_arg = sys.argv[1]
    
    # 支持两种格式：
    # 1. namespace:command subcommand (如 innovation:state update)
    # 2. namespace:command:subcommand (如 innovation:state:update)
    # 完全没有冒号说明连命名空间都缺失，判定为格式错误
    if ':' not in command_arg:
        print(f"ERROR: 命令格式错误: {command_arg}")
        print("正确格式: namespace:command 或 namespace:command:subcommand")
        sys.exit(1)
    
    # 按冒号切分：2 段 = 命名空间+命令；3 段 = 再加上写在命令名里的子命令
    parts = command_arg.split(':')
    if len(parts) == 2:
        namespace, command = parts
        subcommand = None
        remaining_args = sys.argv[2:]
    elif len(parts) == 3:
        namespace, command, subcommand = parts
        remaining_args = sys.argv[2:]
    else:
        # 多于 3 段（如 a:b:c:d）无法归属命名空间/命令/子命令，判定为格式错误
        print(f"ERROR: 命令格式错误: {command_arg}")
        sys.exit(1)

    # 发现命令
    commands = discover_commands()
    
    # 查找命令
    if subcommand:
        # 格式: namespace:command:subcommand —— 子命令已写在命令名里，直接拼装全限定 key
        full_key = f"{namespace}:{command}.{subcommand}"
    elif remaining_args and not remaining_args[0].startswith('-'):
        # 格式: namespace:command subcommand
        # 仅当首个剩余参数不以 '-' 开头（不是选项）时才尝试当作子命令：
        # 用“拼装后是否命中路由表”来判定，命中才消费掉它，否则回退为无子命令形态。
        # 这样可避免把普通位置参数误认成不存在的子命令而报“未知命令”。
        potential_sub = remaining_args[0]
        full_key = f"{namespace}:{command}.{potential_sub}"
        if full_key in commands:
            subcommand = potential_sub
            remaining_args = remaining_args[1:]   # 子命令已被识别，从待传参数中移除
        else:
            full_key = f"{namespace}:{command}"
    else:
        # 无子命令：首个参数就是选项（以 '-' 开头），或根本没有剩余参数
        full_key = f"{namespace}:{command}"
    
    # 路由表里找不到该命令 —— 只列出与当前 namespace:command 相关的候选，帮助纠错
    if full_key not in commands:
        print(f"ERROR: 未知命令: {command_arg}")
        print(f"\n可用命令:")
        for cmd_name in sorted(commands.keys()):
            if cmd_name.startswith(f"{namespace}:{command}.") or cmd_name == f"{namespace}:{command}":
                print(f"  {cmd_name}")
        sys.exit(1)

    # innovation 的机会 ID 会用于本地文件名；入口处拒绝路径片段。
    # 逐个扫描剩余参数，定位到 --id 后校验紧随其后的取值：
    # 白名单为“首字符为字母/数字，随后仅字母/数字/下划线/连字符，总长 ≤80”。
    # 这不只是格式约束，更是安全边界：禁止 '.' '/' '\' 等分隔符进入文件名，防目录穿越。
    if namespace == "innovation":
        for index, value in enumerate(remaining_args):
            if value == "--id" and index + 1 < len(remaining_args):
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", remaining_args[index + 1]):
                    print("ERROR: 机会 ID 仅允许字母、数字、下划线和连字符（最长 80 位）")
                    sys.exit(2)
    
    # 执行命令
    # 取出 handler，把剩余参数整体交给它；handler 内部按需 argparse 调底层脚本，
    # 并把底层退出码透传出去（入口不在此时额外包装退出码）。
    cmd_func = commands[full_key]
    cmd_func(remaining_args)


if __name__ == '__main__':
    # 仅作为脚本直接运行时才进入 main；被 import 时无副作用（便于测试/复用）
    main()
