"""RSS 广义财经新闻抓取模块。

职责边界
--------
只服务总览「最新资讯」，与分析页「资讯流」(news_feed, 基金公告+重仓股) 是两套数据，
互不混用。本模块不做任何交易/分析判断，只负责「订阅 RSS → 落库」这一件事。

数据源（实测验证「还活着」的官方/代理 RSS，后续新增均须先 live 抓取确认 entries>0）：
  - 中国新闻网财经  https://www.chinanews.com.cn/rss/finance.xml  （主力，纯财经，UTF-8，200/30条）
  - 36氪            https://www.36kr.com/feed                      （科技财经，200/30条）
  - 日经中文网(FeedX代理)  https://feedx.net/rss/nikkei.xml        （亚太宏观/市场，中文，200/20条）
已死/不可用（不采用）：新浪财经、华尔街见闻、雪球、财新、证券时报、第一财经、经济观察网 RSS 均 404；
  Reuters 原生 SSL EOF；RSSHub 公共实例（财联社电报/热门、金十、东方财富研报、央视财经等）全线 403。
能抓但「原文读不了」，对交易决策无价值，故移除：
  - FT中文网           https://www.ftchinese.com/rss/news           （站点打不开，付费墙）
  - Bloomberg Markets  https://feeds.bloomberg.com/markets/news.rss （点开要注册才能看，且是英文）
  准则：RSS 只给标题+导语，正文必须跳原文；源站自己都读不了的新闻留着只占版面，不接。
  注意：财联社目前无任何可用 RSS，除非自建 RSSHub 私有实例。

抓取规则（避免被屏蔽）：
  1. 低频轮询：每 30 分钟一次，绝不秒级刷。
  2. 条件请求：带上 ETag / Last-Modified，源未更新时返回 304，不重复下载正文。
  3. 明确身份：User-Agent 标明 RSS 阅读器，不是匿名爬虫。
  4. 互斥抓取：同一时刻只允许一个抓取在跑，并发会打爆源站。
  5. 去重：market_news 表 UNIQUE(link)，重复抓取不产生重复行。
  6. 容错：单源失败不中断其余源。

RSS 只给「标题 + 导语摘要 + 原文链接 + 发布时间」，正文一律看原文 link——
这正是发布方允许的订阅方式；抓取原文正文反而有被屏蔽风险，故不做。

分层说明：本模块在 engine 层，允许依赖 db 层（api -> engine -> data -> db）。
"""

from __future__ import annotations

import html
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import feedparser

from backend.db import get_connection

# =============================================================================
# 常量
# =============================================================================

# 北京时间时区。RSS 的 pubDate 是带时区的（如 +0800），统一转成北京时间存库。
_TZ = timezone(timedelta(hours=8))

# 抓取间隔（秒）：30 分钟。RSS 是「拉式订阅」，发布方预期阅读器低频轮询。
FETCH_INTERVAL = 30 * 60

# 摘要最大长度（字符）。RSS 导语本就不长，截断只是兜底，避免异常长文本。
SUMMARY_MAX_LEN = 500

# 明确的 RSS 阅读器身份。不伪装成浏览器，也不匿名，便于源站识别「这是合规订阅者」。
USER_AGENT = "Mozilla/5.0 (compatible; fund-sandbox RSS reader/1.0; +local)"

# 订阅源清单。url=feed 地址，name=展示用源名。
FEEDS = [
    # ── 中文源 ──
    {"url": "https://www.chinanews.com.cn/rss/finance.xml", "name": "中国新闻网财经"},
    {"url": "https://www.chinanews.com.cn/rss/china.xml", "name": "中国新闻网时政"},
    {"url": "https://www.chinanews.com.cn/rss/world.xml", "name": "中国新闻网世界"},
    {"url": "https://www.36kr.com/feed", "name": "36氪"},
    {"url": "https://www.news.cn/fortune/news_fortune.xml", "name": "新华网财经"},
    {"url": "http://www.people.com.cn/rss/finance.xml", "name": "人民网财经"},
    {"url": "https://feedx.net/rss/nikkei.xml", "name": "日经中文网"},
    # ── 海外源（英文原文，交给翻译池处理；用于「信息时差」抢首发）──
    {"url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US", "name": "Yahoo财经"},
    {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "name": "CNBC"},
    {"url": "https://feeds.marketwatch.com/marketwatch/topstories/", "name": "MarketWatch"},
    {"url": "https://www.investing.com/rss/news.rss", "name": "Investing.com"},
    {"url": "https://news.un.org/feed/subscribe/zh/news/all/rss.xml", "name": "UN News"},
    {"url": "https://www.wto.org/library/rss/latest_news_e.xml", "name": "WTO News"},
]

# 源清单覆盖多个站点；
# 选取准则：
# ① 能抓到（entries>0）② 原文能读。
#   实测通过：中新网时政/世界 30、新华网财经 300、人民网财经 100、CNBC 30、
#            MarketWatch 10（301 重定向，feedparser 自动跟随）、Investing 10、UN 30、WTO 10、
#            Yahoo 19（原 `finance.yahoo.com/news/rssindex` 已 404，改用 `feeds.finance.yahoo.com`）
#   未通过：新华网原 `xinhuanet.com` 域名 403 → 换 `news.cn` 成功
# 快讯（财联社/东财/同花顺）与政府宏观数据**不走 RSS**，另由 akshare 接口抓（见 flash_news / macro_data）。

# =============================================================================
# 状态
# =============================================================================

# 每个源的 ETag / Last-Modified，用于条件请求（进程内存存，重启即丢也无妨）。
_etags: dict[str, tuple[str | None, str | None]] = {}

# 抓取互斥锁：保证同一时刻只有一个抓取在跑，既防并发打爆源站，也保护 _etags 读写。
_fetch_lock = threading.Lock()

# 后台线程状态
_running = False
_thread: threading.Thread | None = None


# =============================================================================
# 工具函数
# =============================================================================

def _strip_html(text: str) -> str:
    """把 RSS 摘要转成纯文本：先解 HTML 实体，再去标签，最后压缩空白。

    顺序有讲究：必须先解实体再剥标签。若反过来，`&lt;b&gt;` 这类实体在
    剥标签阶段不是真正的 `<b>` 会被漏掉，解实体后才残留成 `<b>` 文本。
    """
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_time(entry, fallback: str) -> str:
    """解析发布时间为北京时间 ISO 字符串（YYYY-MM-DD HH:MM:SS）。

    feedparser 的 published_parsed / updated_parsed 是 UTC struct_time。
    无发布时间的条目回退到 fallback（抓取时间），避免它永远进不了「今日」过滤。
    """
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return fallback
    try:
        dt = datetime(*st[:6], tzinfo=timezone.utc).astimezone(_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return fallback


# =============================================================================
# 抓取
# =============================================================================

def fetch_market_news(conn: sqlite3.Connection) -> int:
    """抓取所有订阅源，去重写入 market_news。返回本次新写入条数。

    加互斥锁：同一时刻只允许一个抓取在跑（防并发打源站 + 保护 _etags）。
    """
    with _fetch_lock:
        return _fetch_unlocked(conn)


def _fetch_unlocked(conn: sqlite3.Connection) -> int:
    """fetch_market_news 的实际实现（调用方须已持有 _fetch_lock）。"""
    inserted = 0
    fetched_at = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")

    for feed in FEEDS:
        url, name = feed["url"], feed["name"]
        etag, modified = _etags.get(url, (None, None))
        try:
            parsed = feedparser.parse(url, agent=USER_AGENT, etag=etag, modified=modified)
        except Exception as exc:  # noqa: BLE001  网络异常只跳过当前源
            print(f"[rss] {name} 抓取失败: {type(exc).__name__}: {exc}")
            continue

        # 记录条件请求凭据，下次带上；源未变则 304，不重复下载、不打扰源站
        etag_new = getattr(parsed, "etag", None)
        modified_new = getattr(parsed, "modified", None)
        if etag_new or modified_new:
            _etags[url] = (etag_new, modified_new)

        status = getattr(parsed, "status", None)
        if status == 304:
            continue  # 源未更新，跳过

        if not parsed.entries:
            print(f"[rss] {name} 无条目（status={status}）")
            continue

        src_inserted = _persist_entries(conn, parsed.entries, name, url, fetched_at)
        inserted += src_inserted
        print(f"[rss] {name} 抓取完成，status={status}，本次新增 {src_inserted} 条")

    # 每次 RSS 更新后，自动从广义新闻里提取宏观/政策/地缘/会议/公司情报
    if inserted > 0:
        try:
            from backend.engine import intel_collector
            n = intel_collector.collect_from_market_news(conn, lookback_days=2)
            if n:
                print(f"[rss] 自动提取情报 {n} 条")
        except Exception as exc:  # noqa: BLE001
            print(f"[rss] 自动提取情报失败: {type(exc).__name__}: {exc}")

    return inserted


def _persist_entries(
    conn: sqlite3.Connection,
    entries: list,
    source_name: str,
    source_url: str,
    fetched_at: str,
) -> int:
    """把一个源的条目去重写入 market_news，返回本次新写入条数。"""
    c = conn.cursor()
    inserted = 0
    for entry in entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = _strip_html(entry.get("summary") or entry.get("description") or "")[:SUMMARY_MAX_LEN]
        pub_time = _parse_time(entry, fallback=fetched_at)
        try:
            c.execute(
                """INSERT OR IGNORE INTO market_news
                   (title, link, summary, source, source_url, pub_time, fetched_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (title, link, summary, source_name, source_url, pub_time, fetched_at),
            )
            inserted += c.rowcount
        except Exception as exc:  # noqa: BLE001  单条失败不拖垮整个源
            print(f"[rss] 入库失败 {title[:30]}: {type(exc).__name__}: {exc}")
    conn.commit()
    return inserted


# =============================================================================
# 后台定时抓取线程（独立于 scheduler，不碰交易调度逻辑）
# =============================================================================

def start_background() -> None:
    """启动后台定时抓取线程（daemon）。重复调用无效，不阻塞调用方。"""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print(f"[rss] 后台抓取线程已启动，每 {FETCH_INTERVAL // 60} 分钟一次")


def _loop() -> None:
    """后台线程主循环：立即抓一次，之后每 FETCH_INTERVAL 秒抓一次。

     扩展：本循环现在负责**整个资讯抓取桶**（RSS + 盘中快讯），
    两者都落 `market_news`。**先快讯后 RSS** —— 因为事件提取挂在 RSS 抓取末尾，
    这样一次循环里快讯的新条目也能被同一轮提取覆盖，不必跑两遍。

    ⚠️ 已知限制：财联社 / 同花顺快讯接口每次只返回**最新 20 条**，
    若单位时间产量超过 20 条（重大行情时可能），中间会有遗漏 —— 属接口能力边界，
    不编造补齐。东财接口返回 200 条，覆盖足够。
    """
    while _running:
        try:
            conn = get_connection()
            try:
                try:
                    from backend.engine import flash_news

                    flash_news.fetch_flash_news(conn)
                except Exception as exc:  # noqa: BLE001 快讯失败不影响 RSS
                    print(f"[rss] 快讯抓取出错: {type(exc).__name__}: {exc}")
                fetch_market_news(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001  单轮异常不能让线程死掉
            print(f"[rss] 本轮抓取出错: {type(exc).__name__}: {exc}")
        time.sleep(FETCH_INTERVAL)
