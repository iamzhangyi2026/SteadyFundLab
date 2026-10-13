"""fund-sandbox 后端包。

分层结构：

    backend/db      数据库 schema 与连接管理
    backend/data    数据抓取（akshare）+ 双源交叉验证 + 健康度检测
    backend/engine  交易引擎：交易日历、费率、份额批次（lot）、记账
    backend/api     HTTP 接口（仅本机）

层间依赖方向固定：api -> engine -> data -> db
不允许反向依赖，也不允许 engine 直接调 api。
"""
