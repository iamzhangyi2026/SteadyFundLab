"""数据层：抓取与校验。

模块划分：
    fetcher.py              akshare 抓取（净值、板块、购买规则、基金持仓明细）
                            + 双源交叉验证
                            + 异常检测（detect_anomalies：越闸门/单日跳变/净值停滞，
                              只报异常不猜原因，定性交给 AI，清单经 anomalies_to_json 落盘）
                            + 健康日志（record_health → data_health 表）
    _mini_racer_shield.py   py_mini_racer V8 崩溃隔离层（子进程代理）
    _mini_racer_worker.py   shield 的子进程工作端（长度前缀帧协议）

数据接入原则：
    1. 单源不采信，必须 ≥2 个独立源交叉一致才进分析层
    2. 拿不到就明确标「无数据」，绝不拿旧值冒充新值
    3. 脏数据物理拦截，不进库
    4. 脚本只报异常，不猜原因——定性交给 AI
"""
