"""数据库 schema 定义与初始化。

设计要点（改表结构前务必读完这段）：

1. 为什么用 SQLite
   本地单机、零配置、单文件可备份。数据量很小（每天几十条净值、几笔订单），
   完全不需要客户端-服务器架构。

2. 为什么必须有 lots（份额批次）表
   这是整个系统最关键的设计，也是最容易做错的地方。
   公募基金赎回按「先进先出」消耗份额，不同批次买入日期不同 → 持有天数不同
   → 适用不同档位的赎回费。

   反例：如果持仓只存「总份额 + 平均成本」，那么
   1 号买 1 万、10 号买 1 万、15 号赎回 1 万时：
     - 正确（FIFO）：消耗 1 号那批，持有 14 天，赎回费 0.5%
     - 错误（平均成本）：持有天数被摊薄成 7 天，赎回费跳到 1.5%
   费用算错 3 倍，策略回测结果完全不可信。

   因此每一笔申购确认后都生成一个 lot，赎回时按 confirm_date 升序依次消耗。

3. 为什么 nav_daily 同时存单位净值和累计净值
   分红会导致单位净值除息下调（1.5 分红 0.1 后变 1.4）。如果只用单位净值算
   收益曲线，分红那天会显示暴跌 6.7%，图像完全失真。累计净值把分红加回去了，
   才是真实收益。两者都存，才能区分「真实下跌」和「分红除息」。

4. 所有金额字段统一用 REAL（元），所有比例字段统一存小数（0.015 = 1.5%）
   不混用「百分比数值」和「小数」，避免 0.5 到底是 0.5% 还是 50% 的经典歧义。
"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

# 数据库文件位置：项目根/data/fund_sandbox.db
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "fund_sandbox.db"

# 当前 schema 版本。结构变更时必须递增，便于以后做迁移判断
SCHEMA_VERSION = 2


TABLES: dict[str, str] = {}


TABLES["funds"] = """
-- 基金档案表。静态信息，抓取一次后更新即可
CREATE TABLE IF NOT EXISTS funds (
    code            TEXT PRIMARY KEY,   -- 基金代码，如 040001
    name            TEXT NOT NULL,      -- 基金全称
    short_name      TEXT,               -- 简称，界面显示用
    category        TEXT,               -- 类型：混合型 / 股票型 / 指数 / QDII / 债券
    is_qdii         INTEGER DEFAULT 0,  -- 是否 QDII。1=是，净值 T+2 滞后，需特殊处理
    purchase_fee_rate   REAL,           -- 原始申购费率（未打折），实际值以抓到的为准
    redeem_fee_note     TEXT,           -- 赎回费率说明（部分基金有特殊阶梯）
    management_fee_rate REAL,           -- 管理费率（仅展示，不参与计算，已含在净值中）
    custodian_fee_rate  REAL,           -- 托管费率（同上，仅展示）
    min_purchase   REAL DEFAULT 1.0,    -- 起投金额，支付宝多数 1 元起

    -- 以下四项是**动态/产品特性**字段。
    -- purchase_limit 需要人工维护：接口返回的申购状态只写"限大额"三个字，
    -- 不含具体金额，而限购额会随基金公司公告变动。抓不到就是抓不到，
    -- 系统据此处置（拒绝或告警），不编造一个数字出来。
    purchase_limit REAL,                -- 单日限购额（元）。NULL=未知
    period_type    TEXT,                -- 开放式|持有期|定期开放|封闭式
    hold_period_days INTEGER,           -- 持有期天数（持有期基金）
    next_open_date TEXT,                -- 下一开放日（定期开放基金）

    status         TEXT DEFAULT 'active',  -- active | suspended（停售）| delisted
    updated_at     TEXT
);
"""


TABLES["nav_daily"] = """
-- 每日净值表。主键是 (基金, 日期)，重复抓取会覆盖而不是产生重复行
CREATE TABLE IF NOT EXISTS nav_daily (
    fund_code       TEXT NOT NULL,
    nav_date        TEXT NOT NULL,      -- 净值日期（不是抓取日期！两者必须分开记）
    nav             REAL NOT NULL,      -- 单位净值
    acc_nav         REAL,               -- 累计净值。分红校准与长期收益曲线的唯一可靠来源
    daily_growth    REAL,               -- 日增长率（%），源站给的，用于交叉校验
    source          TEXT NOT NULL,      -- 数据来源，便于出问题时定位是哪个源的锅
    verified        INTEGER DEFAULT 0,  -- 是否通过双源交叉验证：1=已验证，0=单源未验证
    is_qdii_lagged  INTEGER DEFAULT 0,  -- 该值是否为 QDII 的 T+2 滞后值
    fetched_at      TEXT NOT NULL,      -- 抓取时间戳
    PRIMARY KEY (fund_code, nav_date)
);
CREATE INDEX IF NOT EXISTS idx_nav_date ON nav_daily(nav_date);
"""


TABLES["orders"] = """
-- 订单表。记录每一次申购/赎回的完整生命周期
-- 状态流转：pending(待确认) -> confirmed(份额已确认) -> settled(资金已到账)
--           rejected(被风控或规则拒绝) / cancelled(撤销)
CREATE TABLE IF NOT EXISTS orders (
    order_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no        TEXT UNIQUE,        -- 业务流水号，形如 B20260828001
    fund_code       TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),  -- buy(申购) | sell(赎回)
    amount          REAL,               -- 申购金额（元），buy 时有值
    shares          REAL,               -- 赎回份额，sell 时有值

    submit_date     TEXT NOT NULL,      -- T 日：下单日期
    submit_time     TEXT NOT NULL,      -- 下单时刻。超过 15:00 则 nav_date 顺延一天
    nav_date        TEXT,               -- 实际成交净值日。15:00 前=T 日，之后=T+1 交易日
    confirm_date    TEXT,               -- 份额确认日（T+1），持有期从这天起算
    arrival_date    TEXT,               -- 赎回资金到账日（T+3~T+4）

    status          TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'settled', 'rejected', 'cancelled')),
                    -- pending|confirmed|settled|rejected|cancelled
    reject_reason   TEXT,               -- 被拒原因。风控或规则拦截时必须写清楚，便于复盘

    decision_source TEXT NOT NULL,      -- rule|external|llm。记录这笔是谁决定的，便于归因
    reason          TEXT,               -- 决策理由。外部 AI 下单必须填写，空理由不允许下单

    fee_purchase    REAL DEFAULT 0,     -- 申购费（元）
    fee_redeem      REAL DEFAULT 0,     -- 赎回费（元）
    penalty_flag    INTEGER DEFAULT 0,  -- 是否触发 7 天惩罚性赎回费（1=是）
    deal_nav        REAL,               -- 成交净值
    deal_shares     REAL,               -- 成交份额
    deal_amount     REAL,               -- 成交金额

    created_at      TEXT NOT NULL,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_fund ON orders(fund_code);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


TABLES["lots"] = """
-- 份额批次表（本系统最核心的表，详见文件头部的说明）
-- 每一笔申购确认后生成一个 lot；赎回时按 confirm_date 升序 FIFO 消耗
CREATE TABLE IF NOT EXISTS lots (
    lot_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code       TEXT NOT NULL,
    order_id        INTEGER NOT NULL,   -- 关联的申购订单

    confirm_date    TEXT NOT NULL,      -- 份额确认日。持有天数 = 赎回日 - 该日期
    shares          REAL NOT NULL,      -- 该批次原始份额
    remaining_shares REAL NOT NULL,     -- 剩余份额。FIFO 消耗时递减，归零则 status=exhausted
    cost_nav        REAL NOT NULL,      -- 成交净值（该批次的买入单价）
    cost_amount     REAL NOT NULL,      -- 该批次成本金额（含申购费，即实际扣款额）

    status          TEXT DEFAULT 'active',  -- active | exhausted
    created_at      TEXT NOT NULL,

    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
-- FIFO 查询依赖 (fund_code, status, confirm_date) 这个顺序，索引必须建
CREATE INDEX IF NOT EXISTS idx_lots_fifo ON lots(fund_code, status, confirm_date);
"""


TABLES["cash_ledger"] = """
-- 资金流水表。账户余额不单独存字段，而是由本表流水累加得出，
-- 这样任何一笔钱都能追溯到来源，出现对不上账的情况可以逐笔核对
CREATE TABLE IF NOT EXISTS cash_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_date        TEXT NOT NULL,
    txn_type        TEXT NOT NULL,
    -- initial=初始资金  monthly_add=月度新增  test_capital=沙盒测试资金
    -- purchase=申购扣款(负数)  redeem=赎回到账  dividend=分红到账
    -- fee=费用扣除  refund=撤单退款
    amount          REAL NOT NULL,      -- 正数=入账，负数=出账
    balance_after   REAL NOT NULL,      -- 本笔之后的账户余额（冗余字段，便于快速查询）
    ref_order_id    INTEGER,            -- 关联订单（可空，如月度新增入金没有订单）
    note            TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cash_date ON cash_ledger(txn_date);
"""


TABLES["dividends"] = """
-- 分红记录表。
-- 为什么需要：分红会让单位净值除息下调，引擎如果不识别，会把它记成单日暴跌，
-- 进而污染回撤曲线、波动率，AI 看到这个假信号还会做出错误决策。
CREATE TABLE IF NOT EXISTS dividends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code       TEXT NOT NULL,
    ex_date         TEXT NOT NULL,      -- 除息日（净值下调那天）
    record_date     TEXT,               -- 权益登记日
    pay_date        TEXT,               -- 派息日
    dividend_per_share REAL NOT NULL,   -- 每份分红金额（元）
    nav_before      REAL,               -- 除息前净值，用于校验
    nav_after       REAL,               -- 除息后净值
    reinvest        INTEGER DEFAULT 0,  -- 是否红利再投资：1=是，0=现金分红
    source          TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_div_fund ON dividends(fund_code, ex_date);
"""


# 没有 board_daily 表：板块涨跌幅/主力净流入由 sector_news 承担（每轮留痕更完整），
# 板块→基金的映射走「基金名称匹配」，不依赖 board_code；唯一独有的 PE 分位数据源已失效。


TABLES["fund_holdings"] = """
-- 基金持仓明细（季报）。用于把基金映射到行业板块，算出「行业暴露度」
-- 注意：季报滞后（每季度披露一次，且延迟约 15 个工作日），
-- 所以这里算出的行业暴露是「上一季度的」，不能当作实时数据用
CREATE TABLE IF NOT EXISTS fund_holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code       TEXT NOT NULL,
    report_date     TEXT NOT NULL,      -- 报告期，如 2026-06-30
    stock_code      TEXT,
    stock_name      TEXT,
    weight          REAL,               -- 占基金净值比（%）
    board_code      TEXT,               -- 映射后的行业板块代码
    source          TEXT,
    fetched_at      TEXT NOT NULL,
    UNIQUE(fund_code, report_date, stock_code)
);
"""


TABLES["intel_events"] = """
-- 情报事件表。记录政策 / 地缘 / 军事 / 会议 / 宏观 / 公司 等可能影响持仓的事件。
-- 来源分两类：
--   1) 自动采集：backend/engine/intel_collector.py 从 market_news RSS 用关键词启发式提取；
--   2) 人工/AI 补录：分析页表单手动写入或 AI 判断后写入。
-- 自动采集是规则级初筛（宁可漏报不谎报），明显错误或未覆盖的事件仍靠人工修正。
-- 状态区分「披露态」和「落地态」是实战中用教训换来的经验：
-- 把「拟建产线」当「产能落地」、把「BD 框架」当「收益到账」，是典型的踩坑方式
CREATE TABLE IF NOT EXISTS intel_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date      TEXT NOT NULL,
    category        TEXT NOT NULL,      -- policy|geo|military|conference|company|macro
    title           TEXT NOT NULL,
    detail          TEXT,
    status          TEXT NOT NULL,      -- disclosed(披露态，仅是放风) | landed(落地态，有细则/资金/订单)
    impact_boards   TEXT,               -- JSON 数组，受影响的板块代码
    impact_dir      TEXT,               -- positive | negative | neutral
    source          TEXT,               -- 来源描述，多个源用分号分隔
    cross_verified  INTEGER DEFAULT 0,  -- 是否 ≥2 个独立源交叉一致（含至少 1 个国际源）
    created_by      TEXT,               -- AI 或人工
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intel_date ON intel_events(event_date);
"""

TABLES["news_feed"] = """
-- 自动抓取的真实资讯流（与手动录入的 intel_events 分开）。
-- 来源：基金公告（东财）+ 持仓重仓股新闻（东财）。脚本定时/手动抓取，不编造。
CREATE TABLE IF NOT EXISTS news_feed (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code       TEXT,               -- 关联基金（公告/重仓股所属基金）
    fund_name       TEXT,
    kind            TEXT NOT NULL,      -- fund_announce(基金公告) | stock_news(重仓股新闻)
    title           TEXT NOT NULL,
    content         TEXT,               -- 新闻摘要/正文（截断存储）
    source          TEXT,               -- 文章来源
    pub_time        TEXT,               -- 新闻发布时间
    url             TEXT,               -- 原文链接
    related_stock   TEXT,               -- 重仓股代码（stock_news 时有值）
    fetched_at      TEXT NOT NULL,      -- 抓取时间
    UNIQUE(fund_code, kind, title, pub_time)
);
CREATE INDEX IF NOT EXISTS idx_news_feed_time ON news_feed(fetched_at);
CREATE INDEX IF NOT EXISTS idx_news_feed_fund ON news_feed(fund_code);
"""


TABLES["market_news"] = """
-- 广义财经新闻（RSS 拉式订阅）。只服务总览「最新资讯」，
-- 与分析页「资讯流」(news_feed, 基金公告+重仓股) 是两套数据，互不混用。
-- 来源：官方还活着的 RSS（中国新闻网财经 / FT中文网 / 36氪）。
-- 抓取遵守「节流 + 条件请求(ETag/Last-Modified) + 去重」，避免被源站屏蔽。
-- RSS 只提供标题 + 导语摘要 + 原文链接 + 发布时间，正文一律看原文 link。
CREATE TABLE IF NOT EXISTS market_news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    link            TEXT NOT NULL,      -- 原文链接（去重键）
    summary         TEXT,               -- RSS 导语摘要（非全文）
    source          TEXT,               -- 源名：中国新闻网财经 | FT中文网 | 36氪
    source_url      TEXT,               -- 具体 feed 地址
    pub_time        TEXT,               -- 发布时间（本机时区 ISO，YYYY-MM-DD HH:MM:SS）
    fetched_at      TEXT NOT NULL,      -- 抓取时间
    is_official     INTEGER DEFAULT 0,  -- 是否官媒来源（情报采集的信任度加权用）
    UNIQUE(link)
);
CREATE INDEX IF NOT EXISTS idx_market_news_time ON market_news(pub_time);
"""


TABLES["data_health"] = """
-- 数据健康日志。每次抓取后写入，供界面展示和 AI 定性使用
-- 记录「静默失败」是本表存在的最大意义：接口返回 HTTP 200 但数据是旧的/空的，
-- 这种情况脚本不一定报错，但必须在这里留下痕迹
CREATE TABLE IF NOT EXISTS data_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    check_date      TEXT NOT NULL,
    source          TEXT NOT NULL,      -- 数据源标识
    data_type       TEXT NOT NULL,      -- nav | board | holdings | news
    status          TEXT NOT NULL,      -- ok | delayed | suspicious | failed
    last_success_at TEXT,               -- 最后一次成功时间。用于判断「延迟多久」
    record_count    INTEGER DEFAULT 0,  -- 本次抓到多少条。骤减通常意味着接口改版或限流
    message         TEXT,               -- 具体说明
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_date ON data_health(check_date);
"""


TABLES["fund_status"] = """
-- 基金交易状态每日快照。
--
-- 为什么必须每日抓取而不是写死在配置里：
--   申购/赎回状态是**动态变化**的。基金公司会因为规模控制、外汇额度、
--   海外节假日、巨额赎回等原因随时调整，今天「限大额」明天就可能「暂停申购」。
--   写死在配置里等于埋雷——按过期状态下单必然失败，更糟的是在封闭期
--   反复提交无效订单。
--
-- 五类状态处置方式完全不同，不能混为一谈：
--   限购（限大额）  能买，但单日有金额上限       → 下单前校验金额是否超限
--   暂停申购        不能买，但通常仍可赎回       → 禁止买入
--   暂停赎回        不能卖                       → 禁止卖出
--   封闭期          不能买也不能卖（新基金成立初期）→ 双向禁止
--   持有期          能买，但每笔份额锁定期满才能卖 → 按批次算解锁日
--
-- QDII 还有一类特殊情形：因**海外市场节假日**临时暂停交易
-- （如港股 ETF 因复活节休市）。这与「限购」意义完全不同——
-- 前者是日历性的、可预期的，后者是规模控制手段，处置逻辑也不同。
CREATE TABLE IF NOT EXISTS fund_status (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code        TEXT NOT NULL,
    status_date      TEXT NOT NULL,     -- 状态快照所属日期

    purchase_status  TEXT CHECK (purchase_status IN ('open', 'limited', 'closed', 'unknown')),
                    -- 英文枚举入库：open=开放申购 | limited=限大额 | closed=暂停申购/封闭期 | unknown=未抓到
    redeem_status    TEXT CHECK (redeem_status IN ('open', 'closed', 'unknown')),
                    -- 英文枚举入库：open=开放赎回 | closed=暂停赎回/封闭期 | unknown=未抓到
    purchase_limit   REAL,              -- 单日申购限额（元）。NULL=不限

    period_type      TEXT CHECK (period_type IN ('开放式', '封闭式', '持有期', '定期开放')),
    hold_period_days INTEGER,           -- 持有期天数（持有期基金）
    next_open_date   TEXT,              -- 下一开放日（定期开放基金）
    lock_until       TEXT,              -- 份额锁定截止日

    raw_fee          TEXT,              -- 原始费率文本，抓来什么样存什么样
    note             TEXT,
    source           TEXT,
    fetched_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fstatus ON fund_status(fund_code, status_date);
"""


TABLES["quota_usage"] = """
-- 月度配额使用记录。
-- 三个用途（定投 / 灵活加仓 / 开新仓）共享同一个池子，必须逐笔记账，
-- 否则会出现「以为还有额度其实已经超支」的情况
CREATE TABLE IF NOT EXISTS quota_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month      TEXT NOT NULL,      -- 形如 2026-08
    purpose         TEXT NOT NULL,      -- buy（一次性手动买入）| dingtou（登记定投计划，首期计入）
    fund_code       TEXT,
    amount          REAL NOT NULL,
    ref_order_id    INTEGER,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quota_month ON quota_usage(year_month);
"""


TABLES["portfolio_daily"] = """
-- 组合每日快照表。用于画「组合收益曲线」。
-- 为什么不实时回放历史：lots 只存**当前**剩余份额，历史每日份额要回放
-- 所有订单/批次才能还原，成本高且易错。改为每日收盘后写一笔快照，
-- 曲线就有了真实的历史轨迹（新增字段也只需往后累积）。
CREATE TABLE IF NOT EXISTS portfolio_daily (
    snap_date       TEXT PRIMARY KEY,   -- 快照日期
    market_value    REAL NOT NULL,      -- 持仓市值
    cash            REAL NOT NULL,      -- 现金余额
    total_assets    REAL NOT NULL,      -- 总资产 = 市值 + 现金
    total_cost      REAL NOT NULL,      -- 持仓成本（剩余份额对应成本）
    unrealized_pnl  REAL,               -- 浮动盈亏 = 市值 − 成本
    realized_pnl    REAL,               -- 已实现盈亏（赎回 + 分红 − 已耗成本）
    cumulative_pnl  REAL,               -- 累计收益 = 浮动 + 已实现
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_port_date ON portfolio_daily(snap_date);
"""


TABLES["dingtou_plan"] = """
-- 定投计划表。把"定投"从固定写死的纪律工具升级为可配置计划。
-- 每只基金可单独设定：每期金额、频率（每周/每月）、星期几/几号、是否启用。
CREATE TABLE IF NOT EXISTS dingtou_plan (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code       TEXT NOT NULL,      -- 基金代码
    fund_name       TEXT,               -- 基金名称（冗余存，便于展示）
    amount          REAL NOT NULL,      -- 每期定投金额（元）
    frequency       TEXT NOT NULL,      -- weekly（每周）| monthly（每月）
    weekday         INTEGER DEFAULT 0,  -- 周定投：星期几（0=周一，6=周日）
    day_of_month    INTEGER DEFAULT 1,  -- 月定投：每月几号（1~28）
    enabled         INTEGER DEFAULT 1,  -- 1=启用，0=暂停
    created_at      TEXT NOT NULL,
    -- 本期已投标记（期起始日字符串，如 2026-08-31）。
    -- 用于 CAS 原子抢占：并发执行时只有抢到标记的那个请求能建单，
    -- 避免"先查后建"的竞态导致同一期重复扣款。
    last_invested_period TEXT,
    UNIQUE(fund_code, frequency)
);
CREATE INDEX IF NOT EXISTS idx_plan_code ON dingtou_plan(fund_code);
"""


# ===== 数据健康与抓取状态相关表 =====
# 记录各数据源的抓取/同步状态，便于事后发现接口异常。
# 以保证「新建库」也能建出完整结构（不是只迁就当前这个库）。
TABLES["analysis_log"] = """
CREATE TABLE IF NOT EXISTS analysis_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER,
    decision_type   TEXT,
    fund_code       TEXT,
    fund_name       TEXT,
    step1_status    TEXT,
    step1_ok        INTEGER DEFAULT 0,
    step2_cash      REAL,
    step2_available REAL,
    step2_check     TEXT,
    step3_pool_size INTEGER,
    step3_criteria  TEXT,
    step3_results   TEXT,
    step4_status    TEXT,
    step4_ok        INTEGER DEFAULT 0,
    step5_percentile REAL,
    step5_trend_1y  REAL,
    step5_max_dd    REAL,
    step5_news_summary TEXT,
    step6_strategy  TEXT,
    step6_signals   TEXT,
    step7_decision  TEXT,
    step7_reason    TEXT,
    step7_amount    REAL,
    created_at      TEXT NOT NULL,
    step5_detail    TEXT,
    step6_detail    TEXT,
    step7_detail    TEXT,
    full_analysis_json TEXT,
    valuation       TEXT,
    decision_detail TEXT,
    strategy        TEXT,
    decision        TEXT
)
"""

TABLES["discipline_alerts"] = """
CREATE TABLE IF NOT EXISTS discipline_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code       TEXT NOT NULL,       -- 基金代码
    line_type       TEXT NOT NULL,       -- profit_take(止盈) | drawdown_cut(减仓) | admit_wrong(认错)
    detail          TEXT,                -- 人读原因（含浮盈/回撤幅度与阈值）
    action          TEXT,                -- reducing(减仓) | clearing(清仓)
    suggested_ratio REAL,                -- 建议处置比例（0~1）
    nav             REAL,                -- 触发时的（累计）净值
    triggered_at    TEXT NOT NULL,       -- 首次触发时间
    resolved_at     TEXT,                -- 自动消解时间：阈值不再触发时填（NULL=仍活跃）
    ack             INTEGER NOT NULL DEFAULT 0  -- 1=已读，不影响任何交易逻辑
)
;    -- 抓取/同步状态记录表
"""

TABLES["fund_fee"] = """
CREATE TABLE IF NOT EXISTS fund_fee (
    fund_code       TEXT PRIMARY KEY,
    redeem_tiers    TEXT,               -- JSON: [{label, days_min, days_max, rate}]
    redeem_raw      TEXT,               -- 原始返回文本（便于人工核对，不丢证据）
    fetched_at      TEXT NOT NULL
)
;    -- 抓取/同步状态记录表
"""

TABLES["fx_rate"] = """
CREATE TABLE IF NOT EXISTS fx_rate (
    pair        TEXT NOT NULL,          -- 货币对，如 USD/CNY
    rate_date   TEXT NOT NULL,          -- 日期（YYYY-MM-DD）
    rate        REAL NOT NULL,          -- 中间价（标准写法，如 6.7521）
    source      TEXT,                   -- 数据源
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (pair, rate_date)
)
;    -- 抓取/同步状态记录表
"""

TABLES["meta"] = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
;    -- 抓取/同步状态记录表
"""

TABLES["news_triage"] = """
CREATE TABLE IF NOT EXISTS news_triage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key       TEXT NOT NULL,       -- 事件指纹（归一化标题哈希），同事件唯一
    title           TEXT NOT NULL,
    summary         TEXT,
    url             TEXT,
    sources         TEXT,                -- JSON 数组：参与本事件的所有来源名
    src_count       INTEGER DEFAULT 1,   -- 独立来源数（交叉验证依据）
    is_official     INTEGER DEFAULT 0,   -- 其中是否含官媒
    has_intl        INTEGER DEFAULT 0,   -- 其中是否含国际源
    category        TEXT,                -- macro|policy|geo|conference|company|fund|holding|sector
    impact_dir      TEXT,                -- positive | negative | neutral
    status          TEXT,                -- disclosed(放风/拟) | landed(已落地：有细则/资金/订单)
    credibility     TEXT NOT NULL,       -- verified|official_single|single_pending|doubt
    hit_holdings    INTEGER DEFAULT 0,   -- 是否命中当前持仓（基金名/代码/重仓股）
    related_fund    TEXT,                -- 命中时的基金代码
    major           INTEGER DEFAULT 0,   -- 是否重大事件（触发前端强推）
    pushed          INTEGER DEFAULT 0,   -- 是否已强推过（避免重复弹）
    llm_judged      INTEGER DEFAULT 0,   -- 是否经过 LLM 兜底研判
    llm_note        TEXT,                -- LLM 研判结论原文（可空）
    first_pub_time  TEXT,                -- 事件最早发布时间
    processed_at    TEXT NOT NULL
, impact_boards TEXT, created_by TEXT, impact_concepts TEXT, surprise TEXT)
;    -- 抓取/同步状态记录表
"""

TABLES["position_health"] = """
CREATE TABLE IF NOT EXISTS position_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code       TEXT NOT NULL,      -- 基金代码
    fund_name       TEXT,               -- 基金名称（冗余存，便于展示）
    as_of           TEXT NOT NULL,      -- 评估日期 YYYY-MM-DD
    score           REAL NOT NULL,      -- 健康分 0~100
    level           TEXT NOT NULL,      -- healthy / watch / alert / danger
    action          TEXT NOT NULL,      -- hold / watch / reduce / clear
    suggested_ratio REAL DEFAULT 0,     -- 建议处置比例（0~1）
    persist_days    INTEGER DEFAULT 0,  -- 连续预警天数（持续性，防单日误杀）
    d_trend         REAL,               -- 各维度得分，留着便于复盘"到底哪一维拖后腿"
    d_persist       REAL,
    d_relative      REAL,
    d_drawdown      REAL,
    d_valuation     REAL,
    d_news          REAL,
    reasons         TEXT,               -- JSON：各维度的人读依据 + 处置理由
    created_at      TEXT NOT NULL,
    UNIQUE(fund_code, as_of)
)
;    -- 抓取/同步状态记录表
"""

TABLES["rule_change_log"] = """
CREATE TABLE IF NOT EXISTS rule_change_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_code   TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- purchase_status | redeem_fee
    old_value   TEXT,
    new_value   TEXT,
    detected_at TEXT NOT NULL
)
;    -- 抓取/同步状态记录表
"""

TABLES["sector_news"] = """
CREATE TABLE IF NOT EXISTS sector_news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sector          TEXT NOT NULL,       -- 板块名称，如 半导体
    sector_code     TEXT,                -- 板块代码（东财 BKxxxx / 同花顺），无则空
    change_pct      REAL,                -- 当日涨跌幅（%）
    net_inflow      REAL,                -- 主力净流入（元）。正=流入，负=流出
    leader_stock    TEXT,                -- 领涨股名
    leader_change_pct REAL,              -- 领涨股涨跌幅（%）
    hot_rank        INTEGER,             -- 热门度排名（1=最热），按涨跌幅+净流入综合
    news_title      TEXT,                -- 关联广域新闻标题（无则空串）
    news_url        TEXT,
    news_source     TEXT,
    news_summary    TEXT,
    fetched_at      TEXT NOT NULL, board_type TEXT DEFAULT 'industry',       -- 抓取时间
    UNIQUE(sector, fetched_at)
)
;    -- 抓取/同步状态记录表
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """获取数据库连接。

    为什么设置 row_factory = sqlite3.Row：
    这样查出来的行可以像字典一样用 row["nav"] 取值，
    比 row[2] 这种下标写法可读性强得多，改表结构时也不容易错位。

    为什么开启外键约束：
    默认 SQLite 不强制外键，lots.order_id 这样的关联就形同虚设，
    删了订单可能留下孤儿 lot。显式开启能拦住这类问题。
    """
    path = db_path or DB_PATH
    # 内存数据库（":memory:"）没有文件路径，不能建父目录。
    # 支持它是为了写单元测试和演示脚本——不污染真实数据文件。
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 写锁等待 30s（Python 默认仅 5s）：启动时净值批量落库与调度器回写并发，
    # 拿不到锁会抛 "database is locked" 且常被 except 静默吞掉，表现为数据莫名没更新。
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# 增量字段。表已存在时 CREATE TABLE IF NOT EXISTS 不会补新列，
# 需要显式 ALTER。这样做是为了**不删库重建**——
# 净值数据抓一次不容易，不能因为加个字段就全丢。
EXTRA_COLUMNS = [
    ("funds", "purchase_limit", "REAL"),
    ("funds", "period_type", "TEXT"),
    ("funds", "hold_period_days", "INTEGER"),
    ("funds", "next_open_date", "TEXT"),
    # 已存在的库补列：定投计划本期已投标记（CAS 幂等用）
    ("dingtou_plan", "last_invested_period", "TEXT"),
    # 分红方式：cash=现金分红（默认），reinvest=红利再投
    ("funds", "dividend_method", "TEXT"),
    # 基金转换：转出单与转入单互相关联，便于回看"这是一笔转换"
    ("orders", "related_order_id", "INTEGER"),
    ("funds", "is_qdii", "INTEGER DEFAULT 0"),
    ("funds", "min_purchase", "REAL DEFAULT 1.0"),
    # 基金入选理由（前端列表展示），建表语句未含、依赖此处在旧库/新库统一补齐
    ("funds", "add_reason", "TEXT"),
    # market_news 官媒标记：情报采集（collect_from_market_news）按此列读取，旧库补齐防缺列
    ("market_news", "is_official", "INTEGER DEFAULT 0"),
    ("news_triage", "surprise", "TEXT"),
    ("news_triage", "impact_concepts", "TEXT"),
    ("sector_news", "board_type", "TEXT"),
    ("analysis_log", "valuation", "TEXT"),
    ("analysis_log", "decision_detail", "TEXT"),
    ("analysis_log", "strategy", "TEXT"),
    ("analysis_log", "decision", "TEXT"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """补齐后加的列。已存在则跳过，安全重复执行。"""
    for table, col, decl in EXTRA_COLUMNS:
        # PRAGMA 与 ALTER TABLE 都不支持绑定参数，表名/列名只能拼进 SQL。
        # 三者均来自本文件顶部的 EXTRA_COLUMNS 硬编码清单，仍做白名单校验，
        # 防止将来误把外部输入混进清单时形成注入面。
        if table not in TABLES:
            raise ValueError(f"_ensure_columns: 未知表名 {table!r}")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", col):
            raise ValueError(f"_ensure_columns: 非法列名 {col!r}")
        if not re.fullmatch(r"(INTEGER|REAL|TEXT)( DEFAULT -?[0-9.]+)?", decl):
            raise ValueError(f"_ensure_columns: 非法列声明 {decl!r}")
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def init_db(db_path: Path | None = None) -> Path:
    """初始化数据库，创建所有表。

    用 CREATE TABLE IF NOT EXISTS，因此可以安全地重复执行，
    不会因为数据库已存在而报错或清空数据。

    注意：CHECK 约束只对**新建库**生效——SQLite 不支持给已存在的表
    添加约束，已存在的库沿用建库时的结构，由应用层校验兜底。
    """
    path = db_path or DB_PATH
    conn = get_connection(path)
    try:
        for name, sql in TABLES.items():
            try:
                conn.executescript(sql)
            except sqlite3.OperationalError as e:
                # 防御性兜底：建表语句均带 IF NOT EXISTS，正常不会走到这里；
                # 万一并发初始化撞上已存在的表，跳过而不阻断启动
                if "already exists" in str(e):
                    continue
                raise
        # 补齐后加的列（已存在的库也能平滑升级）
        _ensure_columns(conn)
        # 记录 schema 版本，便于以后判断是否需要迁移
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()
        return path
    finally:
        conn.close()


if __name__ == "__main__":
    p = init_db()
    print(f"数据库已初始化：{p}")
    conn = get_connection(p)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    print(f"共 {len(tables)} 张表：{', '.join(tables)}")
    conn.close()


# ===== 数据健康辅助工具函数 =====

def get_db_path() -> Path:
    """返回当前数据库文件路径。

    存在意义：日志 / 审计 / 备份等模块需要在**数据库旁边**落文件（如 `data/audit.log`），
    但不该各自去拼路径 —— 统一由 schema 提供，避免多处硬编码 `data/` 目录。
    """
    return Path(DB_PATH)


def row_to_dict(row) -> dict | None:
    """单行 → 字典。row 为 None 时返回 None（**不是**空字典 ——
    免得把「没查到」伪装成「查到了一条空记录」）。"""
    if row is None:
        return None
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:  # noqa: BLE001
        return dict(row)


def rows_to_dicts(rows) -> list[dict]:
    """多行 → 字典列表。"""
    out = []
    for r in rows or []:
        d = row_to_dict(r)
        if d is not None:
            out.append(d)
    return out


def record_health(conn, source: str, data_type: str, status: str,
                  count: int = 0, message: str = "") -> None:
    """写一条数据健康记录（供各巡检模块统一调用）。

    为什么集中在这里：抓取的**静默失败**（HTTP 200 但数据是旧的/空的）不会抛异常，
    只有把「本次抓到几条、什么状态」写进 `data_health`，事后才能发现异常。
    各模块自己拼 SQL 容易漏字段、格式也不统一。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO data_health "
        "(check_date, source, data_type, status, last_success_at, record_count, message, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now[:10], source, data_type, status,
         now if status == "ok" else None, count, message, now),
    )
    conn.commit()
