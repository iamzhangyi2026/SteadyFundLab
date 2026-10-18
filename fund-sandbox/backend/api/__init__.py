"""HTTP 接口层。

只监听 127.0.0.1，不对外暴露，保证数据不出本机。

对外接口（详见 routes）：
    POST /api/order       下单（外部 AI 通过这里下达交易指令）
    GET  /api/account     账户概览
    GET  /api/positions   持仓列表
    GET  /api/health      数据健康度
    POST /api/intel       情报入库

这一层不做任何业务判断，只负责收参数、调 engine、返回结果。
所有业务规则都在 engine 层，避免规则散落两处导致不一致。
"""
