"""金蝶云星空本体底座。

五元（名词/动词/状态/链接/规则）的单一事实来源，以及长在它上面的几层：

    base/        本体、对象、调度、MCP 服务端
    aip/         判断层：能不能做 + 为什么（纯函数，只依赖 PyYAML）
    saga/        多步操作：守卫、逐步授权、逆序补偿
    pipeline/    数据加工：线 / 解析 / 标准 / 表
    indexlayer/  Funnel 索引
    harness/     操作链约束（事后检查）
    wikiskill/   回溯每日操作、沉淀经验

**分层安装**：判断层只需要 PyYAML，装它不必把 mcp / httpx / pyodbc 一起拖下来。
    pip install kingdee-mcp              # 本体 + 判断层
    pip install kingdee-mcp[server]      # 再加 MCP 服务端
    pip install kingdee-mcp[sql]         # 再加 SQL Server 目录探查
"""
__all__ = ["__version__"]
__version__ = "0.3.0"
