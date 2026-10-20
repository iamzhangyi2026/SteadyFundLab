"""常驻调度器：软件打开即后台运行，不需要人盯着。

职责
----
1. 每日收盘后（20:00）全量刷新数据
2. 自动确认到期的订单（普通基金 T+1、QDII T+2）
3. 交易时段按间隔巡检：检查纪律线、罚期到期、风控
4. 刷新调度器自己的心跳（注意：这**不代表 AI 在线**）

巡检始终只记录提醒；AI 在线/离线状态不会改变交易决策归属。

为什么用简单的 while + sleep 而不是 APScheduler：
    需求很简单（一天一次全量 + 半小时一次巡检），
    引入调度框架只会多一个依赖和一层抽象。
    用标准库 threading 足够，出问题也容易排查。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path

from backend.config import get
from backend.db import get_connection
from backend.engine import calendar, coordinator, orders, rules, fund_pool


class Scheduler:
    """后台调度器。

    用法：
        sch = Scheduler()
        sch.start()      # 启动后台线程（非阻塞）
        ...
        sch.stop()       # 停止

    也可以只跑一次（不启动线程），用于手动触发或测试：
        sch.run_once()
    """

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.conn = conn or get_connection()
        self.cal = calendar.TradeCalendar()
        self.cal.load()

        self.running = False
        self._thread: threading.Thread | None = None
        self._last_refresh_date: date | None = None
        self._last_check: datetime | None = None

        self.refresh_time = get("schedule", "daily_refresh_time", "20:00")
        self.check_interval = get("runtime", "intraday_check_minutes", 30)
        self.refresh_on_startup = get("runtime", "refresh_on_startup", True)
        # 全市场申购状态同步间隔（秒）：申购状态随时在变，需不定时主动刷新
        self.purchase_sync_interval = get("runtime", "purchase_sync_minutes", 30) * 60
        # 板块行情同步间隔（缺失会导致板块数据停更）
        self.sector_sync_interval = get("runtime", "sector_sync_minutes", 15) * 60
        # 政府宏观数据（月频/季频，6 小时查一次足够）
        self.macro_sync_interval = get("runtime", "macro_sync_hours", 6) * 3600
        # 分红处理（每日一次，**只处理持仓基金**，不抓全市场）
        self.dividend_sync_interval = get("runtime", "dividend_sync_hours", 24) * 3600
        # 资讯流（news_feed：持仓基金公告 + 重仓股新闻）抓取间隔（小时）
        self.news_feed_sync_interval = get("runtime", "news_feed_sync_hours", 24) * 3600

    # ---------------------------------------------------------------- 控制

    def start(self) -> None:
        """启动后台线程。重复调用无效。"""
        if self.running:
            return
        self.running = True
        # daemon=True：主程序退出时线程自动结束，不会卡住进程
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[调度器] 已启动  每日 {self.refresh_time} 刷新，"
              f"每 {self.check_interval} 分钟巡检")

    def stop(self) -> None:
        """停止后台线程。"""
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        print("[调度器] 已停止")

    def _loop(self) -> None:
        # 在守护线程内创建专用连接：SQLite 连接不能跨线程复用，
        # 若用 __init__ 在主线程创建的 self.conn，守护线程里会抛
        # "SQLite objects created in a thread can only be used in that same thread"，
        # 导致刷新/巡检全部静默失败（无健康记录、心跳不更新）。
        conn = get_connection()
        last_purchase_sync = 0.0
        last_sector_sync = 0.0
        last_macro_sync = 0.0
        last_dividend_sync = 0.0
        last_news_feed_sync = 0.0
        try:
            if self.refresh_on_startup:
                print("[调度器] 启动即刷新一次")
                self._refresh(conn)
                # 启动即同步一次全市场申购状态
                try:
                    self._sync_purchase_status(conn)
                    last_purchase_sync = time.time()
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 启动同步申购状态出错：{type(e).__name__}: {e}")
                # 启动即同步一次板块行情
                try:
                    self._sync_sector_news(conn)
                    last_sector_sync = time.time()
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 启动同步板块行情出错：{type(e).__name__}: {e}")
                # 启动即同步一次宏观数据
                try:
                    self._sync_macro(conn)
                    last_macro_sync = time.time()
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 启动同步宏观数据出错：{type(e).__name__}: {e}")
                # 启动即抓一次持仓基金的公告 / 重仓股新闻（写 news_feed，只增量不删旧）
                try:
                    self._sync_news_feed(conn)
                    last_news_feed_sync = time.time()
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 启动抓取资讯流出错：{type(e).__name__}: {e}")
                # 启动即处理一次持仓分红
                try:
                    self._sync_dividends(conn)
                    last_dividend_sync = time.time()
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 启动处理分红出错：{type(e).__name__}: {e}")
            while self.running:
                try:
                    self.run_once(conn)
                except Exception as e:  # noqa: BLE001
                    # 单轮异常绝不能让线程死掉，否则后台静默停摆
                    print(f"[调度器] 本轮执行出错：{type(e).__name__}: {e}")
                # 不定时同步全市场申购状态（申购状态随时变，不能只查一次）
                try:
                    now = time.time()
                    if now - last_purchase_sync >= self.purchase_sync_interval:
                        self._sync_purchase_status(conn)
                        last_purchase_sync = now
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 同步申购状态出错：{type(e).__name__}: {e}")
                # 定时同步板块行情（行业 + 概念）
                try:
                    now = time.time()
                    if now - last_sector_sync >= self.sector_sync_interval:
                        self._sync_sector_news(conn)
                        last_sector_sync = now
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 同步板块行情出错：{type(e).__name__}: {e}")
                # 定时同步政府宏观数据（月频/季频，间隔较长）
                try:
                    now = time.time()
                    if now - last_macro_sync >= self.macro_sync_interval:
                        self._sync_macro(conn)
                        last_macro_sync = now
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 同步宏观数据出错：{type(e).__name__}: {e}")
                # 定时处理持仓分红（每日一次）
                try:
                    now = time.time()
                    if now - last_dividend_sync >= self.dividend_sync_interval:
                        self._sync_dividends(conn)
                        last_dividend_sync = now
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 处理分红出错：{type(e).__name__}: {e}")
                # 定时抓持仓基金的公告 / 重仓股新闻（写入 news_feed，只增量不删旧）
                try:
                    now = time.time()
                    if now - last_news_feed_sync >= self.news_feed_sync_interval:
                        self._sync_news_feed(conn)
                        last_news_feed_sync = now
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] 抓取资讯流出错：{type(e).__name__}: {e}")
                # 每 60 秒醒一次检查是否该干活
                time.sleep(60)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 主循环

    def run_once(self, conn=None) -> dict:
        """执行一轮检查。返回本轮做了什么。"""
        conn = conn or self.conn
        # 当前时刻按本机时区取：每日刷新时刻、巡检间隔、到期确认的「今天」
        # 都以本机时区为准。
        now = datetime.now()
        result: dict = {"at": now.replace(tzinfo=None).isoformat(timespec="seconds"),
                        "actions": []}

        # 1. 确认到期订单（每轮都做，重试成本极低）
        confirmed = self._confirm_due_orders(conn)
        if confirmed:
            result["actions"].append(f"确认订单 {len(confirmed)} 笔")

        # 2. 每日全量刷新
        if self._should_refresh(now):
            result["actions"].append("执行全量刷新")
            self._refresh(conn)

        # 3. 定时巡检
        if self._should_check(now):
            result["actions"].append("执行巡检")
            self._check(conn, result)

        # 4. 刷新调度器自身心跳
        coordinator.touch_heartbeat(source="scheduler", note="调度器例行巡检")

        return result

    def _sync_sector_news(self, conn=None) -> None:
        """定时同步板块行情（行业 + 概念）到 `sector_news`。

         板块行情同步调度：缺失会导致板块数据停更；
        概念侧更是连抓取代码都没有（`board_type` 字段在、写入方无）。
        """
        try:
            from . import sector_news as _sector_news

            conn = conn or self.conn
            r = _sector_news.refresh_all_boards(conn)
            ind = r.get("industry") or {}
            con = r.get("concept") or {}
            print(
                f"[调度器] 板块同步完成：行业 {ind.get('saved', 0)} 条（{ind.get('source')}）、"
                f"概念 {con.get('saved', 0)} 条（{con.get('source')}）"
            )
        except Exception as e:  # noqa: BLE001
            print(f"[调度器] 板块同步失败：{type(e).__name__}: {e}")

    def _sync_macro(self, conn=None) -> None:
        """定时抓政府宏观数据（CPI/PPI/GDP/M2/PMI/社融）并落 `market_news`。
        """
        try:
            from . import macro_data as _macro

            conn = conn or self.conn
            r = _macro.fetch_macro(conn)
            if r:
                print(f"[调度器] 宏观数据同步完成：{len(r)} 项")
        except Exception as e:  # noqa: BLE001
            print(f"[调度器] 宏观数据同步失败：{type(e).__name__}: {e}")

    def _sync_dividends(self, conn=None) -> None:
        """定时处理持仓基金的分红（公告 → 净值算金额 → 记账）。

        `process_held_dividends` 在 `scheduler` 里**完全没有调用点** ——
        分红记账缺失时，除息会被 `detect_dividends` 当成单日暴跌，
        **污染回撤与波动率**，进而让筛选规则（max_dd<50%/30%）和纪律线提醒误判。
        """
        try:
            from . import dividend as _dividend

            conn = conn or self.conn
            r = _dividend.process_held_dividends(conn)
            print(
                f"[调度器] 分红处理完成：持仓 {r.get('funds', 0)} 只、"
                f"检出 {r.get('detected', 0)}、新入账 {r.get('applied', 0)}、"
                f"跳过 {r.get('skipped', 0)}"
            )
        except Exception as e:  # noqa: BLE001
            print(f"[调度器] 分红处理失败：{type(e).__name__}: {e}")

    def _sync_news_feed(self, conn=None) -> None:
        """定时抓**持仓基金**的公告 / 重仓股新闻，写入 news_feed（只增量，不删旧）。

        news_feed 由 `news_collector` 统一抓取（只增量，不删旧）：
        按真实持仓抓取，全部 INSERT OR IGNORE（旧数据后续还要用于总结分析，
        一条都不能删）。
        """
        try:
            from . import news_collector as _nc

            conn = conn or self.conn
            r = _nc.refresh_news_feed(conn)
            print(
                f"[调度器] 资讯流抓取完成：持仓 {r.get('funds', 0)} 只、"
                f"公告新增 {r.get('announce', 0)}、重仓股新闻新增 {r.get('stock_news', 0)}"
                + (f"（{r.get('note')}）" if r.get("note") else "")
            )
        except Exception as e:  # noqa: BLE001
            print(f"[调度器] 资讯流抓取失败：{type(e).__name__}: {e}")

    def _real_holding_codes(self) -> set[str]:
        """B 区（真实仓位）持仓的基金代码 —— **只读**，仅为把公共行情抓全。

        行情是真实世界的公共数据（东财同一份），两个区共用：**A 区调度器负责抓、
        B 区只读复用**。若抓取范围不含 B 区持仓，B 区就只能自己再抓一遍、或读到过期值
        —— 这正是「同一份真实数据被两个区各抓一遍」的成因。

        这里**只读代码列表**：不碰 B 区账本、不写 B 区库、不做任何 JOIN。
        边界：**账本分区 / 真实资源共用**。
        """
        try:
            from backend.real.db import get_connection as _real_conn

            c = _real_conn()
            try:
                return {r["fund_code"] for r in c.execute("SELECT fund_code FROM real_holdings").fetchall()}
            finally:
                c.close()
        except Exception:
            return set()

    def _tracked_codes(self, conn=None) -> list[str]:
        """要长期保持资料新鲜的基金 = **持仓(lots) ∪ 关注(pool) ∪ B 区真实持仓**。

        项目口径是「持仓 > 关注」，两者都该抓：关注池为空时只遍历交易池
        会漏掉持仓基金，故取并集后逐只补齐：
        历史净值（交叉验证）、前十大重仓股（落 fund_holdings）、行业配置（缓存）。

        B 区真实持仓一并并入：行情是公共真实数据，两区共用一份，
        统一由 A 区抓（B 区只读），不再各抓一遍。
        """
        conn = conn or self.conn
        held = {
            r["fund_code"]
            for r in conn.execute(
                "SELECT DISTINCT fund_code FROM lots WHERE remaining_shares > 0"
            ).fetchall()
        }
        pool = {f["code"] for f in fund_pool.pool_funds(conn)}
        return sorted(held | pool | self._real_holding_codes())

    def _fetch_and_save_holdings(self, conn, code: str) -> int:
        """按最近几个报告期倒序试抓前十大重仓股并落库（仅保留 6 位 A 股代码）。"""
        from backend.data import fetcher
        from . import holdings as _holdings

        today = datetime.now().date()
        periods: list[str] = []
        for y in (today.year, today.year - 1):
            periods += [f"{y}0630", f"{y}0331", f"{y}1231"]
        for rd in periods:
            try:
                hs = fetcher.fetch_holdings(code, rd)
            except Exception:  # noqa: BLE001
                hs = []
            if not hs:
                continue
            kept = [
                h for h in hs
                if (str(h.get("stock_code") or "").strip().isdigit()
                     and len(str(h.get("stock_code")).strip()) == 6)
            ][:10]
            if kept:
                return _holdings.save_holdings(conn, code, kept, "em_portfolio_hold")
        return 0

    def _sync_fund_profile(self, conn, code: str) -> None:
        """补齐单只基金的「详细资料」：行业配置（缓存）+ 前十大重仓股（落库）。

        缺数据才联网，避免每日重复抓取被源站限流；单只失败不影响其他基金。
        仅在 `_refresh` 的逐只循环里被调用（每日 20:00 + 启动即刷新均覆盖）。
        """
        from backend.data import fetcher
        from . import analysis as _analysis

        # 行业配置：分析页「持仓穿透·行业暴露」依赖它；缓存 24h，这里主动刷新缓存。
        name = code
        try:
            row = conn.execute(
                "SELECT name FROM funds WHERE code=?", (code,)
            ).fetchone()
            if row and row["name"]:
                name = row["name"]
            _analysis.exposure_for(conn, code, name)
        except Exception as e:  # noqa: BLE001
            try:
                fetcher.record_health(
                    conn, "exposure", "industry", "failed", 0,
                    f"{code} 行业配置抓取异常：{type(e).__name__}: {e}",
                )
            except Exception:  # noqa: BLE001
                pass

        # 前十大重仓股：缺数据才抓（季报变化慢，已有则不重复拉）
        try:
            has = conn.execute(
                "SELECT 1 FROM fund_holdings WHERE fund_code=? LIMIT 1", (code,)
            ).fetchone()
            if not has:
                self._fetch_and_save_holdings(conn, code)
        except Exception as e:  # noqa: BLE001
            try:
                fetcher.record_health(
                    conn, "fund_holdings", "holdings", "failed", 0,
                    f"{code} 重仓股抓取异常：{type(e).__name__}: {e}",
                )
            except Exception:  # noqa: BLE001
                pass


    def _sync_purchase_status(self, conn=None) -> None:
        """定时同步全市场基金申购状态（开放申购 / 限大额 / 暂停申购 / 封闭期）。

        申购状态随时在变（今天开放明天可能暂停申购），不能只在「添加基金」时查一次。
        这里 force 刷新全市场购买配置表（akshare fund_purchase_em，约 2.7 万只），
        覆盖所有被抓取 / 搜索到的基金；pool_funds 与 search_funds 经 lookup_purchase
        实时读取这张表，从而所有基金的申购状态都保持新鲜、且前端统一标出。
        """
        conn = conn or self.conn
        try:
            from backend.data import fetcher

            df = fund_pool.get_purchase_df(force=True)
            if df is None or getattr(df, "empty", True):
                fetcher.record_health(conn, "em_purchase", "purchase", "failed", 0,
                                      "同步失败：空数据")
                print("[调度器] 申购状态同步失败：空数据")
                return
            n = len(df)
            fetcher.record_health(conn, "em_purchase", "purchase", "ok", n,
                                  f"全市场申购状态同步 {n} 只")
            print(f"[调度器] 申购状态同步完成：{n} 只")
            # 把真实起购/日限回写 funds 表快照：DB 快照只在实时接口拿不到
            # 该基金时作下单校验回退，回退值必须是同步后的真值，不能停留在添加时的旧值。
            try:
                # 不排除 delisted：`status` 是**池状态**（active/delisted），
                # 与「申购状态」无关；delisted 基金同样要能查到申购状态。
                rows = conn.execute("SELECT code FROM funds").fetchall()
                updated = 0
                for r in rows:
                    pi = fund_pool.lookup_purchase(r["code"])
                    if not pi:
                        continue
                    # 落库到 fund_status —— 前端 /api/funds 从这里取申购状态；
                    # 只写 data_health 的话前端拿不到状态，故必须落库。
                    try:
                        from . import status as _status

                        _status.fetch_and_save(conn, r["code"])
                    except Exception:  # noqa: BLE001
                        pass
                    sets, vals = [], []
                    if pi.get("min_purchase") is not None:
                        sets.append("min_purchase=?")
                        vals.append(pi["min_purchase"])
                    # limit None=清洗后的「不限」，同样是真值
                    sets.append("purchase_limit=?")
                    vals.append(pi.get("purchase_limit"))
                    vals.append(r["code"])
                    conn.execute(
                        f"UPDATE funds SET {', '.join(sets)} WHERE code=?", vals
                    )
                    updated += 1
                conn.commit()
                # 结果写 data_health（/api/health 可见）——print 到文件是块缓冲，
                # 进程不死看不到，失败会变成"静默没更新"假象
                fetcher.record_health(conn, "funds_snapshot", "purchase", "ok", updated,
                                      f"funds 表起购/日限快照已回写 {updated} 只")
                if updated:
                    print(f"[调度器] funds 表起购/日限快照已回写 {updated} 只")
            except Exception as e:  # noqa: BLE001
                try:
                    fetcher.record_health(conn, "funds_snapshot", "purchase", "failed", 0,
                                          f"回写异常：{type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001
                    pass
                print(f"[调度器] funds 快照回写异常：{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            try:
                from backend.data import fetcher

                fetcher.record_health(conn, "em_purchase", "purchase", "failed", 0,
                                      f"同步异常：{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
            print(f"[调度器] 申购状态同步异常：{type(e).__name__}: {e}")

    def _should_refresh(self, now: datetime) -> bool:
        """是否到了每日刷新时刻，且今天还没刷过。"""
        hh, mm = (self.refresh_time.split(":") + ["0"])[:2]
        due = now.hour > int(hh) or (now.hour == int(hh) and now.minute >= int(mm))
        return due and self._last_refresh_date != now.date()

    def _should_check(self, now: datetime) -> bool:
        if self._last_check is None:
            return True
        elapsed = (now - self._last_check).total_seconds() / 60
        return elapsed >= self.check_interval

    # ---------------------------------------------------------------- 任务

    def _confirm_due_orders(self, conn=None) -> list[int]:
        """确认所有已到净值日的 pending 订单。

        净值还没公布时 confirm_order 会返回失败原因，
        这里静默跳过，下一轮再试——不是错误，只是时候未到。
        """
        conn = conn or self.conn
        rows = conn.execute(
            """
            SELECT order_id, nav_date FROM orders
            WHERE status = 'pending' AND nav_date <= ?
            ORDER BY nav_date
            """,
            (datetime.now().date().isoformat(),),
        ).fetchall()

        done: list[int] = []
        for r in rows:
            res = orders.confirm_order(conn, r["order_id"], self.cal)
            if res.get("ok"):
                done.append(r["order_id"])
                side = "申购" if res["side"] == "buy" else "赎回"
                print(f"[调度器] 订单 {r['order_id']} 已确认（{side}）")
            # 失败不刷屏：多数情况是净值未公布，下一轮会自动重试
        if rows:
            conn.commit()
        return done

    def _refresh(self, conn=None) -> None:
        """全量刷新数据。

        逐只隔离：单只基金取数异常只记一条 failed 健康记录并 continue，
        绝不连累整批——否则一只基金抛异常会让整个 _refresh 中断、
        `_loop` 线程直接死掉，净值从此一路过期。外层 try 兜底保护
        `_loop` 线程（启动即刷新那次没有 try 包裹，不能再让异常冒泡）。
        """
        try:
            from backend.data import fetcher

            conn = conn or self.conn
            refreshed = 0
            # 持仓 ∪ 关注并集：已移出交易池但仍在持有/关注的基金也要刷新净值。
            for code in self._tracked_codes(conn):
                try:
                    primary = fetcher.fetch_nav_em(code)
                    if not primary:
                        fetcher.record_health(
                            conn, "em_fund_info", "nav", "failed", 0,
                            f"{code} 主源返回空",
                        )
                        continue
                    secondary = fetcher.fetch_nav_daily_em(code)
                    verified, conflicts = fetcher.cross_verify(primary, secondary)
                    clean = [r for r in verified if fetcher.pass_nav_gate(r["nav"])]
                    n = fetcher.save_nav(conn, code, clean, "em_fund_info")
                    fetcher.record_health(
                        conn, "em_fund_info", "nav", "ok", n,
                        f"{code} 入库 {n} 条，冲突 {len(conflicts)} 条",
                    )
                    refreshed += 1
                    print(f"[调度器] {code} 刷新 {n} 条净值")
                except Exception as e:  # noqa: BLE001
                    # 单只取数异常：记 failed 健康记录，其余基金照常刷。
                    try:
                        fetcher.record_health(
                            conn, "em_fund_info", "nav", "failed", 0,
                            f"{code} 取数异常：{type(e).__name__}: {e}",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    print(f"[调度器] {code} 刷新异常：{type(e).__name__}: {e}")
                # 详细资料（持仓 + 关注都要）：重仓股 + 行业配置，独立 try 不影响净值刷新。
                try:
                    self._sync_fund_profile(conn, code)
                except Exception as e:  # noqa: BLE001
                    print(f"[调度器] {code} 详细资料同步异常：{type(e).__name__}: {e}")

            self._last_refresh_date = datetime.now().date()
            print(f"[调度器] 全量刷新完成：{refreshed} 只成功")
        except Exception as e:  # noqa: BLE001
            print(f"[调度器] 刷新失败：{type(e).__name__}: {e}")

    def _check(self, conn, result: dict) -> None:
        """巡检：检查纪律线与罚期。

        AI 在线或离线都只记录纪律提醒；决策源始终是接管 AI。

        紧急停止开启时，直接跳过所有自动交易决策（定投/减仓/止损告警仍记录）。
        """
        self._last_check = datetime.now()

        # 紧急停止：人为暂停自动交易。只记录状态、不执行任何自动下单。
        if coordinator.is_emergency_stop():
            result["emergency_stop"] = True
            result.setdefault("alerts", []).append("紧急停止已开启，自动交易暂停")
            print("[调度器] 紧急停止开启，本轮跳过自动交易")
            return
        ai_online = coordinator.is_ai_online()
        result["decision_source"] = coordinator.decision_source()

        # 1. 定投：金额固定的纪律性操作，**无论 AI 在线与否都自动执行**。
        #    它与灵活加仓不同——加仓金额由分析决定，定投金额不因行情改变。
        #    execute_dingtou 内部幂等，同一自然周不会重复扣款。
        try:
            from backend.engine import dingtou

            dr = dingtou.execute_dingtou(conn, cal=self.cal)
            if dr["ok"]:
                result.setdefault("actions", []).append(
                    f"定投 {len(dr['ok'])} 笔"
                )
                for o in dr["ok"]:
                    print(f"[调度器] 定投 {o['fund_code']} {o['amount']:.0f} 元")
            result["dingtou_skipped"] = dr["skipped"]
        except Exception as e:  # noqa: BLE001
            result.setdefault("errors", []).append(f"定投执行失败：{e}")

        # 2. 账户回撤止损（峰值法）。仅告警、不拦截、不暂停自动交易——
        #    风控边界仅为提醒；是否减仓由人或 AI 决定，
        #    自动割肉往往割在低点。绝不调用 set_emergency_stop。
        stop = rules.check_account_stop(conn)
        result["stop"] = stop
        if stop["triggered"]:
            msg = (f"账户回撤 {abs(stop['drawdown_pct']):.2f}% 触及止损线 "
                   f"{stop['limit_pct']}%（仅提醒，未暂停自动交易）")
            result.setdefault("alerts", []).append(msg)
            print(f"[调度器] ⚠ {msg}")
            # 持久化最新风控提醒，供前端轮询展示（否则只在后端日志里看不见）
            try:
                _persist_risk_alert(msg)
            except Exception as e:  # noqa: BLE001
                print(f"[调度器] 持久化风控提醒失败：{type(e).__name__}: {e}")

        # 注意：config.toml 里基金池是顶层的 [[funds]] 数组表，
        # 不是 [section] 下的键值，所以要用 test_funds() 而不是 get("funds")
        # 持仓监管闭环：本轮触发的纪律线先收集，循环后统一去重写库（前端轮询可见）
        #
        # 遍历口径 = **持仓 ∪ 关注**（与 `_refresh` 的 `_tracked_codes` 同源）。
        # 原为 `fund_pool.pool_funds(conn)`，但该函数会**过滤 status='delisted'**，
        # 而 4 只持仓基金在 funds 表里正是 delisted → 持仓基金的纪律线 / 罚期
        # 从不被巡检（与「模拟持仓基金不被抓取」是同一类口径断裂）。
        # 项目口径「持仓 > 关注」，持仓必须进监管闭环。
        triggered_alerts: list[dict] = []
        for code in self._tracked_codes(conn):
            if not code:
                continue
            row = conn.execute(
                """
                SELECT COALESCE(acc_nav, nav) AS v FROM nav_daily
                WHERE fund_code = ? ORDER BY nav_date DESC LIMIT 1
                """,
                (code,),
            ).fetchone()
            if not row:
                continue
            nav_v = float(row["v"]) if row["v"] else 0.0

            # 纪律检查按基金隔离：单只异常不连累整轮巡检
            try:
                lines = rules.check_discipline(conn, code, nav_v,
                                               datetime.now().date())
            except Exception as e:  # noqa: BLE001
                print(f"[调度器] {code} 纪律检查异常：{type(e).__name__}: {e}")
                lines = []
            for ln in lines:
                msg = f"{code} 触发 {ln['type']}：{ln['detail']}"
                result.setdefault("alerts", []).append(msg)
                triggered_alerts.append(
                    {
                        "fund_code": code,
                        "line_type": ln["type"],
                        "detail": ln["detail"],
                        "action": ln["action"],
                        "suggested_ratio": ln.get("suggested_ratio"),
                        "nav": nav_v,
                    }
                )
                if ai_online:
                    print(f"[调度器] 提醒（AI 在线，不自动执行）：{msg}")
                else:
                    print(f"[调度器] 提醒（AI 离线，不切换决策源）：{msg}")

            pen = rules.penalty_risk(conn, code, datetime.now().date())
            if pen.get("has_penalty_risk"):
                result.setdefault("alerts", []).append(f"{code}：{pen['detail']}")

        # 持久化纪律线告警：去重写入 + 本轮未触发的活跃告警自动消解
        if triggered_alerts:
            try:
                persist_discipline_alerts(
                    conn, triggered_alerts, datetime.now().isoformat(timespec="seconds")
                )
            except Exception as e:  # noqa: BLE001
                print(f"[调度器] 持久化纪律线告警失败：{type(e).__name__}: {e}")


def _persist_risk_alert(msg: str) -> None:
    """把最新的风控提醒写到 data/risk_alert.json，供前端轮询展示。

    保留最近 20 条，按时间倒序。风控边界只是提醒，这里只做展示用，
    不影响任何交易逻辑（不暂停、不拦截）。
    """
    path = Path(__file__).resolve().parents[2] / "data" / "risk_alert.json"
    try:
        prev = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        prev = {}
    alerts = prev.get("alerts", [])
    alerts.insert(0, {"at": datetime.now().isoformat(timespec="seconds"), "msg": msg})
    alerts = alerts[:20]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"updated_at": datetime.now().isoformat(timespec="seconds"), "alerts": alerts},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def persist_discipline_alerts(conn, items: list[dict], now_iso: str) -> None:
    """把本轮触发的纪律线写入 discipline_alerts，并自动消解已回落的活跃告警。

    去重规则（纪律线闭环要求）：
    - 同类 (fund_code, line_type) 已有活跃告警（resolved_at IS NULL）→ 仅更新
      nav/detail/triggered_at，不重复插库；
    - 本轮未再触发的活跃告警 → 自动填 resolved_at（阈值回落即消解）；
    - 纯提醒不触发交易，决策只走 POST /api/order。
    """
    keep_ids: list[int] = []
    for it in items:
        existing = conn.execute(
            "SELECT id FROM discipline_alerts "
            "WHERE fund_code=? AND line_type=? AND resolved_at IS NULL "
            "ORDER BY triggered_at DESC LIMIT 1",
            (it["fund_code"], it["line_type"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE discipline_alerts SET nav=?, detail=?, triggered_at=? WHERE id=?",
                (it["nav"], it["detail"], now_iso, existing["id"]),
            )
            keep_ids.append(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO discipline_alerts "
                "(fund_code, line_type, detail, action, suggested_ratio, nav, triggered_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (it["fund_code"], it["line_type"], it["detail"],
                 it["action"], it.get("suggested_ratio"), it["nav"], now_iso),
            )
            keep_ids.append(cur.lastrowid)

    # 自动消解：本轮未触发的活跃告警
    if keep_ids:
        q = ("UPDATE discipline_alerts SET resolved_at=? "
             "WHERE resolved_at IS NULL AND id NOT IN (%s)"
             % ",".join("?" * len(keep_ids)))
        conn.execute(q, [now_iso, *keep_ids])
    else:
        conn.execute(
            "UPDATE discipline_alerts SET resolved_at=? WHERE resolved_at IS NULL",
            (now_iso,),
        )
    conn.commit()


if __name__ == "__main__":
    print("=" * 78)
    print("调度器演示（只跑一轮，不启动后台线程）")
    print("=" * 78)
    sch = Scheduler()
    r = sch.run_once()
    print(f"\n执行时间：{r['at']}")
    print(f"决策归属：{r.get('decision_source', '（未到巡检时间）')}")
    print(f"本轮动作：{r['actions'] or '无'}")
    for a in r.get("alerts", [])[:10]:
        print(f"  ⚠ {a}")
    print("\n说明：正式运行时用 sch.start() 启动后台线程，常驻执行。")
    print("=" * 78)


# ===== 调度器实例管理 =====

_scheduler: "Scheduler | None" = None
_sched_lock = threading.Lock()


def get_scheduler() -> "Scheduler | None":
    """返回当前调度器实例；**未启动则为 None**（调用方需判空）。

    为什么要模块级单例：调度器是**进程级**的后台线程。若每次调用都新建一个，
    应用热重载 / 重复启动时会出现**两个线程同时刷新、写库互抢锁**，
    表现为数据莫名回滚或 "database is locked"。
    """
    return _scheduler


def start_scheduler(conn=None) -> "Scheduler":
    """启动调度器（**幂等**：已启动就直接返回现有实例，不会起第二个）。"""
    global _scheduler
    with _sched_lock:
        if _scheduler is None:
            _scheduler = Scheduler(conn)
            _scheduler.start()
        return _scheduler


def stop_scheduler() -> None:
    """停止调度器（供关闭流程调用；未启动则什么也不做）。"""
    global _scheduler
    with _sched_lock:
        if _scheduler is not None:
            _scheduler.stop()
            _scheduler = None
