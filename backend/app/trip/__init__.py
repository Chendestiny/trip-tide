"""TripTide 行程规划包。

模块职责（精简为 7 个文件）：
    models.py     ORM 三表：city / attraction / trip_plan
    schemas.py    对外协议 + LLM 输出契约（PlanResult）
    routers.py    4 个 HTTP 端点
    service.py    编排 + 提示词 + 输出兜底清洗
    llm.py        DeepSeek 客户端 + JSON 抽取 + 校验重试
    planner.py    地理工具 + 规则规划器（兜底引擎）
    seed.py       一次性数据初始化（LLM 出名单 → 高德补坐标 → 入库）
"""
