# innovation 工作流命令集
"""innovation 命名空间 —— 创新工作流（工作流 A / B）命令注册入口。

位置与职责：
    本包对应路由表里的 innovation: 前缀。ops.py 自动发现本包后会调用下面的
    register_commands()，把各子模块（state/ledger/gate/report/task/workflow_b）注册的
    本地 key（如 state.update、ledger.write-fit）汇总，再由 ops.py 前缀化成
    innovation:state.update 等全限定命令名。

为什么把所有子模块聚在一处注册：
    - 单一注册点：本包是 innovation 这条业务线的“总登记处”，一览即可知道该线暴露了
      哪些命令；新增命令只需改对应子模块并在此处补一行 register 调用；
    - 与 ops.py 解耦：新增/调整命令都不需要动入口路由器，入口只认“命名空间前缀 + 本地 key”。
"""
from . import state, ledger, gate, report, task, workflow_b

def register_commands(commands: dict):
    """注册 innovation 工作流的所有命令。

    参数：
        commands —— 由 ops.py 传入的“本命名空间局部 registry”（普通 dict）。
                    各子模块会把 <local_key> -> <handler> 写进它，例如
                    'state.update' -> cmd_update。ops.py 随后为每个 key 加上
                    'innovation:' 前缀，得到最终命令名。

    返回值：无（就地修改传入的 commands 字典）。

    异常与退出码：无。本函数只做登记；真正的参数解析、底层脚本调用与退出码
        由各子模块的 handler 负责。

    为什么这么设计：
        采用“注册表回调”而非让 ops.py 逐个 import：新增业务线/命令时只动本文件，
        入口保持稳定；同时局部 key 不带前缀，命名空间隔离交给上层统一处理，避免
        每个子模块重复书写 'innovation:' 前缀。
    """
    state.register_commands(commands)
    ledger.register_commands(commands)
    gate.register_commands(commands)
    report.register_commands(commands)
    task.register_commands(commands)
    workflow_b.register_commands(commands)
