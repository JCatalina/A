"""
后台数据保活与定时任务调度器 — v2.8

解决"数据只在启动时抓取"的问题: 服务常驻期间由本调度器持续刷新, 无需重启。
- 零依赖 daemon 线程, 与现有 threading 风格一致 (不引入 APScheduler);
- 交易时段感知:
  - 盘中 09:25~11:35 / 12:55~15:05: 每 60s 保活四大指数原始K线/快照/研判结果
    与冰点面板数据, 任意请求均命中新鲜缓存 (各引擎内部 TTL 缓存控制实际打源频率);
  - 盘后 15:05 当日一次: 自动全市场扫描 (选股雷达每日自动更新) + 冰点校准表重算;
  - 非交易时段: 每 10 分钟低频保活 (日/周K + 快照 + 研判/冰点结果),
    保证挂机数日后打开页面第一眼即为新鲜数据;
- 周末跳过; 法定节假日不做特殊处理, 当日至多多拉一次"最近交易日"数据, 无害。
  如需节假日停扫, 可在 HOLIDAYS 集合中补充日期 (YYYY-MM-DD)。
"""
import logging
import threading
import time
from datetime import date, datetime, time as dtime
from typing import Optional

logger = logging.getLogger(__name__)

INDEX_SYMBOLS = ("sh000001", "sz399001", "sz399006", "sh000688")
# 保活的指数K线周期与根数 (与 analyze 请求口径一致, 命中 _kline_cache 键)
INDEX_WARM_COUNTS = {"30": 100, "60": 130, "240": 250, "1200": 260}

# 交易时段 (含前后缓冲, 覆盖集合竞价与收盘撮合)
TRADING_SESSIONS = ((dtime(9, 25), dtime(11, 35)), (dtime(12, 55), dtime(15, 5)))
POST_CLOSE_TIME = dtime(15, 5)     # 收盘后任务触发时刻
TICK_INTERVAL = 30                 # 调度器心跳 (秒)
INTRADAY_WARM_INTERVAL = 60        # 盘中保活周期 (秒)
OFFHOURS_WARM_INTERVAL = 600       # 非交易时段保活周期 (秒)
AUTO_SCAN_LIMIT = 240              # 盘后自动全市场扫描股票数

# 法定节假日 (可选扩展): 命中则全天跳过扫描/校准, 盘中保活仍继续
HOLIDAYS = frozenset()


class RefreshScheduler(threading.Thread):
    """常驻后台调度器: 保活 + 盘后自动任务, 所有失败仅记日志, 不影响正常接口"""

    def __init__(self, data_fetcher, scanner_engine, index_engine, ice_engine):
        super().__init__(daemon=True, name="refresh-scheduler")
        self.fetcher = data_fetcher
        self.scanner = scanner_engine
        self.index = index_engine
        self.ice = ice_engine
        self._last_intraday_ts = 0.0
        self._last_offhours_ts = 0.0
        self._scan_date: Optional[date] = None    # 已完成自动扫描的日期
        self._calib_date: Optional[date] = None   # 已完成盘后校准的日期
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> None:
        logger.info("RefreshScheduler started (tick=%ss, intraday=%ss, offhours=%ss)",
                    TICK_INTERVAL, INTRADAY_WARM_INTERVAL, OFFHOURS_WARM_INTERVAL)
        while not self._stop.wait(TICK_INTERVAL):
            try:
                self._tick()
            except Exception as e:
                logger.warning(f"RefreshScheduler tick error: {e}")

    def stop(self) -> None:
        self._stop.set()

    def _tick(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now()
        if now.weekday() >= 5:                      # 周末: 整体跳过
            return
        today = now.date()
        if today.isoformat() in HOLIDAYS and now.time() >= POST_CLOSE_TIME:
            return                                  # 节假日不跑盘后任务 (盘中保活继续)

        if now.time() >= POST_CLOSE_TIME:
            # 盘后一次性任务: 自动全市场扫描 + 冰点校准重算 (各自在独立线程执行)
            if self._scan_date != today:
                self._launch_auto_scan(today)
            if self._calib_date != today:
                self._launch_postclose_calib(today)
            return

        session = self._session(now)
        if session:
            if time.time() - self._last_intraday_ts >= INTRADAY_WARM_INTERVAL:
                self._last_intraday_ts = time.time()
                self.warm_intraday()
        elif time.time() - self._last_offhours_ts >= OFFHOURS_WARM_INTERVAL:
            self._last_offhours_ts = time.time()
            self.warm_offhours()

    @staticmethod
    def _session(now: datetime) -> Optional[str]:
        t = now.time()
        for i, (start, end) in enumerate(TRADING_SESSIONS):
            if start <= t <= end:
                return "morning" if i == 0 else "afternoon"
        return None

    # ------------------------------------------------------------------
    # 盘中保活: 分钟级刷新, 各引擎内部 TTL 控制真实打源频率
    # ------------------------------------------------------------------
    def warm_intraday(self) -> None:
        self._warm_headline()

        # 四大指数 x 四大周期原始K线 + 实时快照 (命中各自 TTL 缓存, 过期才打源)
        for sym in INDEX_SYMBOLS:
            for scale, count in INDEX_WARM_COUNTS.items():
                try:
                    self.index.fetch_index_kline(sym, scale=scale, count=count)
                except Exception as e:
                    logger.warning(f"Scheduler warm kline failed {sym}:{scale}: {e}")
            try:
                self.index.fetch_index_realtime(sym)
            except Exception as e:
                logger.warning(f"Scheduler warm realtime failed {sym}: {e}")

        self._warm_analysis_results()

    def warm_offhours(self) -> None:
        """非交易时段低频保活: 日/周K + 快照 + 研判/冰点结果, 保证打开页面即新鲜"""
        self._warm_headline()
        for sym in INDEX_SYMBOLS:
            for scale, count in (("240", 250), ("1200", 260)):
                try:
                    self.index.fetch_index_kline(sym, scale=scale, count=count)
                except Exception as e:
                    logger.warning(f"Scheduler offhours kline failed {sym}:{scale}: {e}")
        self._warm_analysis_results()

    def _warm_headline(self) -> None:
        """大盘指数条快照 (fetcher 内部 15s TTL, 前端 30s 轮询秒回)"""
        try:
            self.fetcher.get_market_indices()
        except Exception as e:
            logger.warning(f"Scheduler warm indices failed: {e}")

    def _warm_analysis_results(self) -> None:
        """研判/冰点结果保活: TTL 内直返; 过期各自后台刷新 (引擎内部去重), 不让旧结果滞留"""
        for sym in INDEX_SYMBOLS:
            try:
                self.index.analyze_index_macro(sym, scale="240")
            except Exception as e:
                logger.warning(f"Scheduler warm macro failed {sym}: {e}")
            try:
                self.ice.predict(sym)
            except Exception as e:
                logger.warning(f"Scheduler warm ice failed {sym}: {e}")

    # ------------------------------------------------------------------
    # 盘后一次性任务
    # ------------------------------------------------------------------
    def _launch_auto_scan(self, scan_day: date) -> None:
        """15:05 后当日首次: 独立线程全市场自动扫描 (scanner 的 is_scanning 防重入)。
        先占位日期防同一 tick 周期重复派发; 异常时回滚, 下个 tick 自动重试。"""
        self._scan_date = scan_day

        def _run():
            try:
                logger.info(f"Auto market scan start (date={scan_day}, limit={AUTO_SCAN_LIMIT})")
                self.scanner.scan_market("ALL", limit_stocks=AUTO_SCAN_LIMIT)
                logger.info("Auto market scan finished")
            except Exception as e:
                logger.warning(f"Auto market scan failed: {e}")
                if self._scan_date == scan_day:
                    self._scan_date = None

        threading.Thread(target=_run, name="auto-scan", daemon=True).start()

    def _launch_postclose_calib(self, calib_day: date) -> None:
        """15:05 后当日首次: 独立线程重算四大指数冰点校准表并落盘 (与 run_eval.py 同文件)"""
        self._calib_date = calib_day

        def _run():
            try:
                logger.info(f"Post-close ice calibration start (date={calib_day})")
                for sym in INDEX_SYMBOLS:
                    try:
                        self.ice.calibrate(sym)
                    except Exception as e:
                        logger.warning(f"Ice calibration failed {sym}: {e}")
                logger.info("Post-close ice calibration finished")
            except Exception as e:
                logger.warning(f"Post-close ice calibration failed: {e}")
                if self._calib_date == calib_day:
                    self._calib_date = None

        threading.Thread(target=_run, name="postclose-calib", daemon=True).start()