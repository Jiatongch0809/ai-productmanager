# ops_commands 包 - 命令注册目录
"""ops_commands —— 命令注册目录（各业务线命名空间的父包）。

位置与职责：
    本包是 scripts/ops.py 自动发现机制里的“命令层”根目录。ops.py 用 pkgutil 扫描本
    目录下的每一个子包（子包名即命名空间，如 innovation），动态导入后调用其约定接口
    register_commands(registry)，把注册的本地 key 前缀化成 <namespace>:<key> 后汇总成
    全局路由表。

为什么设计成“一个子包 = 一条业务线”：
    - 命名空间隔离：不同业务线可安全复用同名命令（都用 state.read / gate.check），
      前缀化后 key 全局唯一，互不覆盖；
    - 零改入口扩展：新增业务线只需在本目录下建子包并实现 register_commands()，
      完全不用改动 ops.py 这个高风险入口文件。

注意：本 __init__.py 仅作包标识占位，不含任何业务逻辑、不注册任何命令。
"""
