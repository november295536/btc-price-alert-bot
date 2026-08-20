#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多警報 即時監看（單一檔案、本機 terminal 直接跑）

設定驅動的警報清單（見下方 ALERTS）。目前支援兩種警報：

  1. volume_spike（放量突破）——監看某商品某週期的「形成中」K 棒，當「爆量」
     （形成中量 ≥ 上一根已收完棒的 N 倍）且「振幅」（(high-low)/low ≥ 門檻）同時
     成立時**棒內即發**。行為與訊息與舊版單一 BTC 警報完全一致（迴歸鎖定）。

  2. ema_breakout（EMA20 突破・兩根K嚴格版 strict2）——只用**已收盤**K 棒判定：
     前一棒實體上穿 EMA（open[-1] < ema[-1] 且 close[-1] > ema[-1]）且本棒整根含影
     線站上 EMA（low > ema）→ 多；空方鏡像。語義逐行對照 quantitive-trading 專案
     `ema_breakout/ml/mode_scan.py` 的 `_signal_masks` strict2 分支（ground truth），
     不自創變體。附「靜默突破」判定（本訊號棒振幅在該品種×該框架歷史訊號棒振幅
     分佈中的分位 < quiet_pctile → 是）。

只讀公開行情、不下單，因此不需要任何 Bybit API key。

跑法：
    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    # 把 .env 的 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 填好（沒填則只印 log、不發訊）
    python alert.py

依賴：只有 ccxt（含 ccxt.pro，串流用）。發 Telegram 用 Python 標準庫 urllib，不需 requests。

架構重點（改動前務必理解）：
  * volume_spike 走 ccxt.pro `watch_ohlcv` 串流（棒內即時；ccxt.pro 內建重連 + keepalive），
    每次(重)連線都用 REST 重新 seed baseline。watchdog 逾時後先用 REST 探測：真安靜就容忍
    （不重連），真殭屍/斷線才重連。
  * ema_breakout 走 REST 輪詢（只吃已收盤棒；輪詢天然容忍安靜時段：無新棒＝正常，不是故障，
    也不會誤判殭屍）。用官方 kline OHLC，與 mode_scan 對得起來。
  * 每則警報獨立狀態（冷卻 / K 棒追蹤 / warmup / EMA / 振幅分佈），互不干擾；單一品種
    斷流 / 停盤不影響其他警報。
"""

# =============================================================================
#  設定區（要改的東西都在這裡）
# =============================================================================

# --- 既有放量警報的「預設值」；也是舊版單一 BTC 警報的相容常數（handle_candles 用） ---
# 監看商品：Bybit USDT 本位線性永續。
SYMBOL = "BTC/USDT:USDT"
# 時間框架：15 分鐘 K 棒
TIMEFRAME = "15m"
# 爆量倍數門檻：當前「形成中」棒的累積量 ≥ 上一根「已收完」棒完整量的幾倍
VOL_MULT = 2.0
# 振幅門檻：(high - low) / low ≥ 多少（0.005 = 0.5%）
RANGE_THRESHOLD = 0.005
# 冷卻秒數：觸發後幾秒內不再發，避免同一波洗版（300 = 5 分鐘）
COOLDOWN_SEC = 300

# --- 警報清單（設定驅動）。type 決定用哪種偵測器，其餘欄位是該警報的參數。 ---
#   volume_spike: symbol, timeframe, vol_mult, range_pct(百分比，0.5=0.5%), cooldown_sec
#   ema_breakout: symbol, timeframe, ema_len(預設20), quiet_pctile(預設20)
# 每則警報獨立狀態、互不干擾。同一 (symbol,timeframe) 的多則警報共用一條資料流。
ALERTS = [
    # 既有放量突破——參數照舊、行為零改變（迴歸驗收）
    {"type": "volume_spike", "symbol": "BTC/USDT:USDT", "timeframe": "15m",
     "vol_mult": 2.0, "range_pct": 0.5, "cooldown_sec": 300},

    # EMA 突破（兩根K嚴格版 strict2）×（品種 × {15m, 1h}）
    {"type": "ema_breakout", "symbol": "BTC/USDT:USDT",     "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "BTC/USDT:USDT",     "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "ETH/USDT:USDT",     "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "ETH/USDT:USDT",     "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "HYPE/USDT:USDT",    "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "HYPE/USDT:USDT",    "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "TSLA/USDT:USDT",    "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "TSLA/USDT:USDT",    "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "SPCX/USDT:USDT",    "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "SPCX/USDT:USDT",    "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "SKHYNIX/USDT:USDT", "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "SKHYNIX/USDT:USDT", "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "MU/USDT:USDT",      "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "MU/USDT:USDT",      "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "SNDK/USDT:USDT",    "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "SNDK/USDT:USDT",    "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
    # SKHY 與 SKHYNIX 是 Bybit 上兩個不同合約（使用者確認兩個都要，2026-08-21）。
    {"type": "ema_breakout", "symbol": "SKHY/USDT:USDT",    "timeframe": "15m", "ema_len": 20, "quiet_pctile": 20},
    {"type": "ema_breakout", "symbol": "SKHY/USDT:USDT",    "timeframe": "1h",  "ema_len": 20, "quiet_pctile": 20},
]

# --- EMA strict2 凍結常數（source of truth: mode_scan.py / decision_bars.py）---
EMA_LEN_DEFAULT = 20               # EMA 週期
EMA_QUIET_PCTILE_DEFAULT = 20      # 靜默突破分位門檻（振幅分位 < 此值 → 靜默）
EMA_MIN_SIGNALS_FOR_QUIET = 30     # 歷史訊號 < 此數 → 顯示「資料不足」、不硬判靜默
# strict2 gate = max(window_len=3, SL_LOOKBACK=5, 3) = 5（mode_scan `_signal_masks` 收尾 gate）；
# 即訊號棒 index 需 ≥ gate-1 = 4（前面至少 5 根已收棒）。
STRICT2_SL_LOOKBACK = 5
STRICT2_GATE = 5
EMA_SEED_DAYS = 365                # 啟動時 seed 歷史天數（盡量拉滿一年；新上市品種自動拉到上市日）
EMA_SEED_PAGE = 1000               # 分頁抓每頁筆數（Bybit fetch_ohlcv 上限）
EMA_POLL_SEC = 15                  # ema_breakout 輪詢間隔（秒）；已收棒偵測延遲 ≤ 此值
EMA_POLL_LIMIT = 10                # 每次輪詢抓最近幾根（涵蓋兩次輪詢間可能收完的棒）

# --- 連線 / 韌性 ---
# Watchdog 逾時秒數：超過這麼久沒收到任何行情更新，就（對 volume 串流）探測是否殭屍
WATCHDOG_TIMEOUT = 60
# 重連前等待秒數
RECONNECT_DELAY = 2
# 狀態行印出方式（非 TTY 每隔這麼多秒印一行）
STATUS_LOG_EVERY_SEC = 60
# 存活監控（healthchecks.io）：每隔這麼多秒，在「有任一資料流仍在吐資料」時 ping 一次
HEARTBEAT_EVERY_SEC = 300
# 存活監控判定「還活著」的資料新鮮度上限（秒）：最近一次收到任何行情在此秒數內才 ping。
# 有 BTC 這種永遠活躍的市場當基準，安靜的股票永續不會拉低整體心跳。
HEARTBEAT_STALE_SEC = 180
# 安靜市場 watchdog 探測到「真安靜」時，log 節流秒數（避免洗版）
QUIET_LOG_EVERY_SEC = 600

# Telegram / healthchecks.io 設定：放 .env（見檔尾）。也可用環境變數覆蓋。
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
HEALTHCHECK_URL = ""

# =============================================================================
#  以下為實作，一般不用改
# =============================================================================

import asyncio
import json
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

import ccxt.pro as ccxtpro  # ccxt.pro 內含在 ccxt 套件裡（pip install ccxt）

# 收到 Ctrl+C / SIGTERM 時設為 True，讓所有迴圈優雅收尾退出
STOP = False

# 全域「最近一次收到任何行情」時戳（monotonic），給心跳判斷整體是否還活著
_LAST_DATA_TS = 0.0
_LAST_HEARTBEAT = 0.0


def _load_env() -> None:
    """從同目錄的 .env 載入設定（標準庫解析，不需 python-dotenv）。
    已存在的環境變數優先，其次才是 .env；最後寫回模組層的設定變數。"""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HEALTHCHECK_URL
    values = {}
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass  # 沒有 .env 也沒關係，後面會提示尚未設定
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", values.get("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", values.get("TELEGRAM_CHAT_ID", ""))
    HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", values.get("HEALTHCHECK_URL", ""))


_load_env()


# ----------------------------------------------------------------------------
#  小工具：log（事件，留在 scrollback）、status（每秒狀態，原地刷新/節流）
# ----------------------------------------------------------------------------
_IS_TTY = sys.stdout.isatty()   # 互動式終端機才用「原地刷新」；被導到檔案則不用
_status_active = False          # 畫面上目前是否有一行原地刷新的狀態行待清除
_last_status_ts = 0.0           # 非 TTY 模式下，上次印狀態行的時間（節流用）


def log(msg: str) -> None:
    """事件訊息：帶時間戳、獨立一行、留在 scrollback。觸發/換棒/重連/錯誤等都用這個。"""
    global _status_active
    if _IS_TTY and _status_active:
        sys.stdout.write("\r\033[2K")
        _status_active = False
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def status(msg: str) -> None:
    """每秒的行情狀態：TTY 原地刷新同一行；非 TTY 每 STATUS_LOG_EVERY_SEC 秒印一行。"""
    global _status_active, _last_status_ts
    if _IS_TTY:
        sys.stdout.write(f"\r\033[2K[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        sys.stdout.flush()
        _status_active = True
    else:
        now = time.monotonic()
        if now - _last_status_ts >= STATUS_LOG_EVERY_SEC:
            _last_status_ts = now
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fmt_ts(ms: int) -> str:
    """把毫秒 timestamp 轉成本機時間字串（K 棒的開盤時間）。"""
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def _timeframe_ms(tf: str) -> int:
    """把 '15m' / '1h' / '1d' 這種 timeframe 字串轉成毫秒。"""
    unit = tf[-1]
    n = int(tf[:-1])
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
    return n * mult


async def _sleep_stop(seconds: float) -> None:
    """可被 STOP 中斷的 sleep（小步輪詢，讓 Ctrl+C 能盡快收尾）。"""
    end = time.monotonic() + seconds
    while not STOP:
        remain = end - time.monotonic()
        if remain <= 0:
            break
        await asyncio.sleep(min(0.5, remain))


def _mark_data_alive() -> None:
    """任一資料流成功收到行情時呼叫；心跳據此判斷整體是否還活著。"""
    global _LAST_DATA_TS
    _LAST_DATA_TS = time.monotonic()


def _new_exchange():
    """建立一個 Bybit 線性永續用的 ccxt.pro exchange。
    fetchMarkets 只載 spot/linear/inverse（**不載 option**）——我們只用線性永續，這樣
    load_markets 更快、也避開 Bybit option instruments 端點偶發的 ExchangeNotAvailable。"""
    return ccxtpro.bybit({
        "enableRateLimit": True,
        "options": {"defaultType": "swap", "fetchMarkets": ["spot", "linear", "inverse"]},
    })


async def _load_markets_retry(exchange, attempts: int = 4):
    """load_markets 帶重試（交易所端點偶發抖動時不要一開機就死）。回傳 markets 或 None。"""
    for i in range(1, attempts + 1):
        try:
            return await exchange.load_markets()
        except Exception as e:
            log(f"⚠️ load_markets 第 {i}/{attempts} 次失敗：{type(e).__name__}: {e}")
            if i < attempts and not STOP:
                await _sleep_stop(RECONNECT_DELAY * i)
    return None


# ----------------------------------------------------------------------------
#  Telegram 發送（標準庫 urllib，跑在 executor 執行緒裡，不阻塞事件迴圈）
# ----------------------------------------------------------------------------
def _telegram_send_sync(text: str):
    """同步送出一則 Telegram 訊息，回傳 (ok: bool, info: str)。"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 尚未設定"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            ok = bool(json.loads(body).get("ok", False))
            return ok, body
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def telegram_send(text: str) -> bool:
    """非同步包裝：把阻塞的 HTTP 送出丟到執行緒池，避免卡住行情迴圈。"""
    loop = asyncio.get_running_loop()
    ok, info = await loop.run_in_executor(None, _telegram_send_sync, text)
    if ok:
        log("📨 Telegram 已送出")
    else:
        log(f"❌ Telegram 送出失敗：{info}")
    return ok


# ----------------------------------------------------------------------------
#  存活監控（healthchecks.io 死人開關）：ping 同樣跑在 executor，不阻塞行情迴圈
# ----------------------------------------------------------------------------
def _healthcheck_ping_sync(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            resp.read()
            return True, str(resp.status)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def healthcheck_ping() -> None:
    """對 HEALTHCHECK_URL 發一次心跳。失敗只記 log、不影響主程式（沒設 URL 就跳過）。"""
    if not HEALTHCHECK_URL:
        return
    loop = asyncio.get_running_loop()
    ok, info = await loop.run_in_executor(None, _healthcheck_ping_sync, HEALTHCHECK_URL)
    if not ok:
        log(f"⚠️ healthchecks.io 心跳發送失敗：{info}")


# ----------------------------------------------------------------------------
#  警報日誌（JSONL）：每則已發警報追加一行；失敗只記 log、不影響發送
# ----------------------------------------------------------------------------
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_ALERTS_JSONL = os.path.join(_LOG_DIR, "alerts.jsonl")


def _append_alert_log(record: dict) -> None:
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(_ALERTS_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"⚠️ 寫入 alerts.jsonl 失敗（不影響發送）：{type(e).__name__}: {e}")


# ============================================================================
#  strict2（兩根K嚴格版）EMA 突破偵測 —— 逐行對照 mode_scan `_signal_masks`
# ============================================================================
#  long_ok[i]  = (o[i-1] <  e[i-1]) & (c[i-1] >  e[i-1]) & (low[i] > e[i])
#  short_ok[i] = (o[i-1] >  e[i-1]) & (c[i-1] <  e[i-1]) & (h[i]   < e[i])
#  gate: 訊號棒 index i ≥ STRICT2_GATE-1（=4）。EMA 遞迴 seed 於第一根收盤價。
# ----------------------------------------------------------------------------

def ema_series(closes, ema_len: int):
    """遞迴 EMA，seed 於第一根收盤價：ema[i] = c*k + prev*(1-k), k = 2/(len+1)。
    與 decision_bars._ema_seeded 逐步一致（純 python float 迴圈）。"""
    n = len(closes)
    if n == 0:
        return []
    k = 2.0 / (ema_len + 1)
    one_minus_k = 1.0 - k
    out = [float(closes[0])]
    prev = out[0]
    for i in range(1, n):
        prev = closes[i] * k + prev * one_minus_k
        out.append(prev)
    return out


def strict2_scan(opens, highs, lows, closes, emas, gate: int = STRICT2_GATE):
    """批次 strict2 偵測：回傳 [(idx, side)]（side ∈ {'long','short'}）。
    逐行對照 mode_scan `_signal_masks` 的 strict2 分支 + 收尾 gate。"""
    n = len(closes)
    out = []
    for i in range(2, n):
        if i < gate - 1:
            continue
        po, pc, pe = opens[i - 1], closes[i - 1], emas[i - 1]
        e_i = emas[i]
        if (po < pe) and (pc > pe) and (lows[i] > e_i):
            out.append((i, "long"))
        elif (po > pe) and (pc < pe) and (highs[i] < e_i):
            out.append((i, "short"))
    return out


def amplitude(high: float, low: float) -> float:
    """訊號棒振幅 (high-low)/low（除零保護）。"""
    return ((high - low) / low) if low > 0 else 0.0


def percentile_rank(dist, value: float) -> float:
    """value 在 dist（歷史振幅）中的百分位＝嚴格小於 value 的比例 × 100。"""
    n = len(dist)
    if n == 0:
        return 0.0
    below = 0
    for a in dist:
        if a < value:
            below += 1
    return 100.0 * below / n


class Strict2Detector:
    """串流版 strict2 偵測器：一根一根餵已收盤棒，維護遞迴 EMA、gate 計數、
    歷史訊號振幅分佈與去重。與 strict2_scan（批次）結果逐棒一致。"""

    def __init__(self, ema_len: int = EMA_LEN_DEFAULT,
                 quiet_pctile: float = EMA_QUIET_PCTILE_DEFAULT,
                 min_signals: int = EMA_MIN_SIGNALS_FOR_QUIET,
                 gate: int = STRICT2_GATE):
        self.ema_len = ema_len
        self.k = 2.0 / (ema_len + 1)
        self.quiet_pctile = quiet_pctile
        self.min_signals = min_signals
        self.gate = gate
        self.ema = None          # e[idx-1]（上一根已處理棒的 EMA）；None 代表尚未有棒
        self.prev_o = None       # 上一根棒 open
        self.prev_c = None       # 上一根棒 close
        self.n = 0               # 已處理已收棒數（下一根的 index = self.n）
        self.last_ts = None      # 上一根已處理棒的開盤時戳（去重/續接用）
        self.last_sig_ts = None  # 上一則訊號棒的開盤時戳（同一棒只發一次）
        self.amp_dist = []       # 歷史訊號棒振幅分佈（seed + 滾動加入）

    def feed_bar(self, bar, silent: bool = False):
        """餵一根已收盤棒 bar=[ts,o,h,l,c,(v)]。回傳訊號 dict 或 None。
        silent=True（seed 用）：只收集振幅、不算分位、不回傳訊號。"""
        ts, o, h, l, c = bar[0], bar[1], bar[2], bar[3], bar[4]
        if self.last_ts is not None and ts <= self.last_ts:
            return None  # 去重：時戳未前進
        idx = self.n
        prev_ema = self.ema  # e[idx-1]
        cur_ema = float(c) if self.ema is None else (c * self.k + self.ema * (1.0 - self.k))

        sig = None
        if idx >= self.gate - 1 and self.prev_o is not None:
            is_long = (self.prev_o < prev_ema) and (self.prev_c > prev_ema) and (l > cur_ema)
            is_short = (self.prev_o > prev_ema) and (self.prev_c < prev_ema) and (h < cur_ema)
            if is_long or is_short:
                side = "long" if is_long else "short"
                amp = amplitude(h, l)
                if silent:
                    self.amp_dist.append(amp)
                else:
                    n_hist = len(self.amp_dist)
                    if n_hist >= self.min_signals:
                        pct = percentile_rank(self.amp_dist, amp)
                        quiet = pct < self.quiet_pctile
                        insufficient = False
                    else:
                        pct = None
                        quiet = None
                        insufficient = True
                    self.amp_dist.append(amp)
                    sig = {
                        "side": side,
                        "bar_open_ts": ts,
                        "open": o, "high": h, "low": l, "close": c,
                        "ema": cur_ema,
                        "amplitude": amp,
                        "amp_pctile": pct,
                        "quiet": quiet,
                        "insufficient": insufficient,
                        "n_hist": n_hist,
                        "idx": idx,
                    }
                self.last_sig_ts = ts

        # 推進狀態
        self.prev_o, self.prev_c = o, c
        self.ema = cur_ema
        self.last_ts = ts
        self.n += 1
        return sig

    def seed(self, closed_bars) -> int:
        """對 seed 歷史逐棒跑一遍 strict2，收集歷史訊號振幅分佈；回傳收集到的訊號數。"""
        for b in closed_bars:
            self.feed_bar(b, silent=True)
        return len(self.amp_dist)


# ============================================================================
#  訊息 / 日誌 builder（純函式，供 live 路徑、demo、單元測試共用）
# ============================================================================

def build_ema_message(cfg: dict, sig: dict, now: datetime = None) -> str:
    """組 ema_breakout 的 Telegram 訊息（使用者逐字核准的格式，不得加料）。"""
    now = now or datetime.now()
    base = cfg["base"]
    tf = cfg["timeframe"]
    symbol = cfg["symbol"]
    ema_len = cfg["ema_len"]
    side = sig["side"]
    emoji = "🟢" if side == "long" else "🔴"
    side_zh = "多" if side == "long" else "空"
    close = sig["close"]
    ts = sig["bar_open_ts"]
    tf_ms = cfg["tf_ms"]
    sig_open = fmt_ts(ts)
    sig_close = fmt_ts(ts + tf_ms)
    if sig["insufficient"]:
        quiet_short = "資料不足"
        quiet_line = f"靜默突破：資料不足（歷史訊號 n={sig['n_hist']}）"
    else:
        quiet = sig["quiet"]
        pct = sig["amp_pctile"]
        quiet_short = "是" if quiet else "否"
        quiet_line = f"靜默突破：{'是' if quiet else '否'}（振幅分位 P{int(round(pct))}）"
    return (
        f"{emoji} {base} 突破{side_zh}（{tf}）|{close}|靜默:{quiet_short}\n"
        f"商品：{symbol}（{tf}・EMA{ema_len}突破）\n"
        f"時間：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"訊號K棒：{sig_open} 開 / {sig_close} 收\n"
        f"收盤價：{close}\n"
        f"{quiet_line}"
    )


def _ema_log_record(cfg: dict, sig: dict, now: datetime) -> dict:
    return {
        "ts": now.isoformat(timespec="seconds"),
        "type": "ema_breakout",
        "symbol": cfg["symbol"],
        "timeframe": cfg["timeframe"],
        "side": sig["side"],
        "close": sig["close"],
        "amplitude": sig["amplitude"],
        "amp_pctile": sig["amp_pctile"],
        "quiet": sig["quiet"],
        "bar_open_ts": sig["bar_open_ts"],
    }


def _volume_text(cfg, ts, o, high, low, close, vol, baseline, vol_ratio, range_pct, now: datetime = None) -> str:
    """組 volume_spike 的 Telegram 訊息（與舊版單一 BTC 警報逐字一致：預設 cfg 下 byte-identical）。"""
    now = now or datetime.now()
    base = cfg["base"]
    chg_pct = ((close - o) / o * 100) if o > 0 else 0.0
    diff = close - o
    if close > o:
        emoji, move = "🟢", f"+{chg_pct:.2f}% (+{diff:.1f})"
    elif close < o:
        emoji, move = "🔴", f"{chg_pct:.2f}% ({diff:.1f})"
    else:
        emoji, move = "⚪", "+0.00% (0)"
    return (
        f"{emoji} {base} {move}|{close}|{vol_ratio:.2f}x|振幅{range_pct * 100:.2f}%\n"
        f"商品：{cfg['symbol']}（{cfg['timeframe']}）\n"
        f"時間：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"K 棒開盤：{fmt_ts(ts)}\n"
        f"現價：{close}\n"
        f"爆量：{vol_ratio:.2f}x（門檻 {cfg['vol_mult']:.1f}x）\n"
        f"  當前量 {vol:.3f} {base} / 上一根 {baseline:.3f} {base}\n"
        f"振幅：{range_pct * 100:.3f}%（門檻 {cfg['range_threshold'] * 100:.2f}%）\n"
        f"  high {high} / low {low}"
    )


def _volume_log_record(cfg, ts, o, close, range_pct, now: datetime) -> dict:
    side = "up" if close > o else ("down" if close < o else "flat")
    return {
        "ts": now.isoformat(timespec="seconds"),
        "type": "volume_spike",
        "symbol": cfg["symbol"],
        "timeframe": cfg["timeframe"],
        "side": side,
        "close": close,
        "amplitude": range_pct,
        "amp_pctile": None,
        "quiet": None,
        "bar_open_ts": ts,
    }


# ----------------------------------------------------------------------------
#  狀態（放記憶體，程式重啟就重來）—— volume_spike 用
# ----------------------------------------------------------------------------
def new_state() -> dict:
    return {
        "baseline_vol": None,   # 上一根「已收完」棒的完整量（BTC），爆量判斷的分母
        "cur_ts": None,         # 目前「形成中」棒的開盤 timestamp（毫秒）
        "cur_vol": 0.0,         # 目前「形成中」棒最後一次觀測到的累積量
        "last_trigger": 0.0,    # 上次發警報的時間（time.monotonic()），給冷卻用
        "last_data_ts": 0.0,    # 上次收到行情更新的時間，給 watchdog 觀察用
        "last_heartbeat": 0.0,  # 上次對 healthchecks.io ping 的時間
    }


def _default_volume_cfg() -> dict:
    """由目前的模組全域組出「預設 volume 警報 cfg」（相容 handle_candles 舊行為）。"""
    return {
        "type": "volume_spike",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "base": SYMBOL.split("/")[0],
        "vol_mult": VOL_MULT,
        "range_threshold": RANGE_THRESHOLD,
        "cooldown_sec": COOLDOWN_SEC,
    }


async def seed_baseline(exchange, state: dict) -> None:
    """（相容舊版）用 REST 重新 seed 預設 BTC 警報的 baseline。新架構的 volume 串流
    改用 VolumeSpikeAlert.seed_baseline，但保留此函式不破壞既有呼叫。"""
    candles = await exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=2)
    if len(candles) < 2:
        raise RuntimeError(f"fetch_ohlcv 只回傳 {len(candles)} 根，無法 seed baseline")
    prev_closed = candles[-2]
    current = candles[-1]
    state["baseline_vol"] = prev_closed[5]
    state["cur_ts"] = current[0]
    state["cur_vol"] = current[5]
    log(
        f"🌱 seed baseline 完成：上一根已收完量 = {prev_closed[5]:.3f} BTC"
        f"（收盤於 {fmt_ts(prev_closed[0])}）；"
        f"目前形成中棒 {fmt_ts(current[0])} 起始量 = {current[5]:.3f} BTC"
    )


async def _volume_process(candles, state: dict, cfg: dict, console_prefix: str = "") -> None:
    """volume_spike 核心：計算 vol_ratio / range_pct、印狀態、判斷觸發並發訊 + 記 JSONL。
    console_prefix="" 時 log/status 與舊版 byte-identical（handle_candles 用）。"""
    if not candles:
        return
    ts, o, high, low, close, vol = candles[-1][:6]

    # --- 偵測棒收盤：timestamp 變了 → 上一根已收完、新的一根開始 ---
    if state["cur_ts"] is not None and ts != state["cur_ts"]:
        closed_vol = state["cur_vol"]
        state["baseline_vol"] = closed_vol
        log(
            f"🕒 {console_prefix}新的一根 {cfg['timeframe']} K 棒開始（{fmt_ts(ts)}）；"
            f"baseline 更新為剛收完那根的量 = {closed_vol:.3f} {cfg['base']}"
        )
    state["cur_ts"] = ts
    state["cur_vol"] = vol

    baseline = state["baseline_vol"]
    vol_ratio = (vol / baseline) if (baseline and baseline > 0) else float("inf")
    range_pct = ((high - low) / low) if low > 0 else 0.0

    both = vol_ratio >= cfg["vol_mult"] and range_pct >= cfg["range_threshold"]
    now = time.monotonic()
    elapsed = now - state["last_trigger"]
    cooling = state["last_trigger"] > 0 and elapsed < cfg["cooldown_sec"]

    flag = ""
    if both and cooling:
        flag = f"  [⏳ 冷卻中 {cfg['cooldown_sec'] - elapsed:.0f}s]"
    elif both:
        flag = "  [⚡ 條件成立]"
    status(
        f"{console_prefix}K[{fmt_ts(ts)}] price={close} vol={vol:.3f} baseline={baseline:.3f} "
        f"vol_ratio={vol_ratio:.2f}x range_pct={range_pct * 100:.3f}%{flag}"
    )

    if both and not cooling:
        state["last_trigger"] = now
        now_dt = datetime.now()
        text = _volume_text(cfg, ts, o, high, low, close, vol, baseline, vol_ratio, range_pct, now=now_dt)
        log(f"🚨 {console_prefix}觸發警報！vol_ratio={vol_ratio:.2f}x range_pct={range_pct * 100:.3f}%")
        await telegram_send(text)
        _append_alert_log(_volume_log_record(cfg, ts, o, close, range_pct, now_dt))


async def handle_candles(candles, state: dict) -> None:
    """（相容舊版 test_alert.py 的入口）以「目前全域設定」當預設 volume 警報處理。
    行為與訊息與舊版單一 BTC 警報逐字一致。"""
    await _volume_process(candles, state, _default_volume_cfg(), console_prefix="")


# ============================================================================
#  警報物件（每則獨立狀態）
# ============================================================================
class VolumeSpikeAlert:
    """放量突破警報（走 ccxt.pro watch_ohlcv 串流；棒內即發）。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = new_state()
        self.base = cfg["base"]

    async def seed_baseline(self, exchange) -> None:
        candles = await exchange.fetch_ohlcv(self.cfg["symbol"], self.cfg["timeframe"], limit=2)
        if len(candles) < 2:
            raise RuntimeError(f"fetch_ohlcv 只回傳 {len(candles)} 根，無法 seed baseline")
        prev_closed = candles[-2]
        current = candles[-1]
        self.state["baseline_vol"] = prev_closed[5]
        self.state["cur_ts"] = current[0]
        self.state["cur_vol"] = current[5]
        log(
            f"🌱 {self.base} seed baseline：上一根已收完 = {prev_closed[5]:.3f} {self.base}"
            f"（{fmt_ts(prev_closed[0])} 收）；形成中 {fmt_ts(current[0])} 起 {current[5]:.3f} {self.base}"
        )

    async def on_update(self, candles) -> None:
        await _volume_process(candles, self.state, self.cfg, console_prefix=f"{self.base} ")


class EmaBreakoutAlert:
    """EMA20 突破（strict2）警報（走 REST 輪詢；只吃已收盤棒）。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["base"]
        self.detector = Strict2Detector(
            ema_len=cfg["ema_len"],
            quiet_pctile=cfg["quiet_pctile"],
            min_signals=EMA_MIN_SIGNALS_FOR_QUIET,
        )
        self.seeded = False

    def seed(self, closed_bars) -> None:
        """對 seed 歷史（已收盤棒）跑一遍 strict2，建立 EMA 狀態與歷史振幅分佈。"""
        n_sig = self.detector.seed(closed_bars)
        self.seeded = True
        last = fmt_ts(self.detector.last_ts) if self.detector.last_ts else "—"
        log(
            f"🌱 {self.base} {self.cfg['timeframe']} EMA{self.cfg['ema_len']} seed 完成："
            f"{len(closed_bars)} 根已收棒、歷史訊號 {n_sig} 筆（末棒 {last} 收）"
        )

    async def on_update(self, candles) -> None:
        """輪詢/探測回來的 K 棒序列：對「最後一根之外（已收盤）」中時戳前進的棒跑判定。"""
        if not candles:
            return
        closed = candles[:-1]  # 最後一根是形成中棒，只用已收盤棒
        for b in closed:
            if self.detector.last_ts is not None and b[0] <= self.detector.last_ts:
                continue
            sig = self.detector.feed_bar(b, silent=False)
            if sig is not None:
                await self._emit(sig)

    async def _emit(self, sig: dict) -> None:
        now_dt = datetime.now()
        text = build_ema_message(self.cfg, sig, now=now_dt)
        if sig["insufficient"]:
            qtag = f"資料不足(n={sig['n_hist']})"
        else:
            qtag = f"P{int(round(sig['amp_pctile']))}{'・靜默' if sig['quiet'] else ''}"
        log(f"🚨 {self.base} EMA strict2 突破{'多' if sig['side'] == 'long' else '空'}"
            f"（{self.cfg['timeframe']}）close={sig['close']} {qtag}")
        await telegram_send(text)
        _append_alert_log(_ema_log_record(self.cfg, sig, now_dt))


# ============================================================================
#  設定解析 / symbol 驗證
# ============================================================================
def _parse_alert_cfg(raw: dict) -> dict:
    """把 ALERTS 裡的一筆設定 dict 正規化成內部 cfg（補 base / tf_ms / 型別轉換）。"""
    typ = raw["type"]
    symbol = raw["symbol"]
    timeframe = raw["timeframe"]
    cfg = {
        "type": typ,
        "symbol": symbol,
        "timeframe": timeframe,
        "base": symbol.split("/")[0],
        "tf_ms": _timeframe_ms(timeframe),
    }
    if typ == "volume_spike":
        cfg["vol_mult"] = float(raw.get("vol_mult", VOL_MULT))
        # ALERTS 用「百分比」表示振幅門檻（0.5 = 0.5%）；內部一律用比例（0.005）
        cfg["range_threshold"] = float(raw.get("range_pct", RANGE_THRESHOLD * 100)) / 100.0
        cfg["cooldown_sec"] = float(raw.get("cooldown_sec", COOLDOWN_SEC))
    elif typ == "ema_breakout":
        cfg["ema_len"] = int(raw.get("ema_len", EMA_LEN_DEFAULT))
        cfg["quiet_pctile"] = float(raw.get("quiet_pctile", EMA_QUIET_PCTILE_DEFAULT))
    else:
        raise ValueError(f"未知的警報 type：{typ!r}")
    return cfg


# ============================================================================
#  歷史抓取（分頁）
# ============================================================================
async def _fetch_ohlcv_history(exchange, symbol: str, timeframe: str, days: int):
    """分頁抓約 `days` 天的 OHLCV（升冪、去重）。新上市品種只會拿到上市日之後的資料。
    每頁帶重試 + 退避（Bybit 分頁突發偶爾觸發 RateLimitExceeded）；重試耗盡則以已取得的
    部分收尾（部分歷史勝過全無）。分頁間留輕微間隔，降低打限機率。"""
    tf_ms = _timeframe_ms(timeframe)
    now = exchange.milliseconds()
    since = now - days * 86_400_000
    out = []
    prev_last = None
    while not STOP:
        batch = None
        for attempt in range(1, 6):
            try:
                batch = await exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=EMA_SEED_PAGE)
                break
            except Exception as e:
                if attempt >= 5:
                    log(f"⚠️ {symbol} {timeframe} 分頁抓取重試 5 次仍失敗（{type(e).__name__}），"
                        f"以已取得的 {len(out)} 根收尾")
                    batch = None
                    break
                await _sleep_stop(min(8.0, 0.8 * (2 ** (attempt - 1))))
        if not batch:
            break
        out.extend(batch)
        last = batch[-1][0]
        if prev_last is not None and last <= prev_last:
            break  # 無進展，避免無限迴圈
        prev_last = last
        if last >= now - tf_ms:
            break  # 追到目前形成中棒
        since = last + tf_ms
        await asyncio.sleep(0.1)  # 分頁間輕微間隔，降低突發打限機率
    # 去重 + 升冪
    seen = {}
    for c in out:
        seen[c[0]] = c
    return [seen[t] for t in sorted(seen)]


# ============================================================================
#  資料流 runner
# ============================================================================
async def _rest_probe(exchange, symbol: str, timeframe: str):
    """watchdog 逾時後的 REST 探測：回傳 (ok, candles)。ok=True 代表能連上交易所。"""
    try:
        candles = await exchange.fetch_ohlcv(symbol, timeframe, limit=2)
        return True, candles
    except Exception:
        return False, None


_last_quiet_log = {}


def _log_quiet(base: str, timeframe: str) -> None:
    key = (base, timeframe)
    now = time.monotonic()
    if now - _last_quiet_log.get(key, 0.0) >= QUIET_LOG_EVERY_SEC:
        _last_quiet_log[key] = now
        log(f"😴 {base} {timeframe}：市場安靜（{WATCHDOG_TIMEOUT}s 無新行情，REST 探測正常）；維持連線、不誤判殭屍")


def _probe_shows_activity(alert: "VolumeSpikeAlert", probe) -> bool:
    """探測結果是否顯示「其實有活動」（ws 卻沒吐 → 殭屍）。比對最後一根的時戳/量。"""
    if not probe:
        return False
    last = probe[-1]
    cur_ts = alert.state.get("cur_ts")
    cur_vol = alert.state.get("cur_vol") or 0.0
    if cur_ts is None:
        return False
    if last[0] > cur_ts:
        return True  # 出現更新的棒 → 有換棒、ws 卻沒通知
    if last[0] == cur_ts and last[5] > cur_vol * 1.0001 + 1e-9:
        return True  # 同一根但量前進了 → 有成交、ws 卻沒吐
    return False


async def run_volume_stream(alert: VolumeSpikeAlert) -> None:
    """單一 volume_spike 串流：(重)連線 → REST seed → watch_ohlcv → watchdog（安靜容忍）。
    每條串流自己的 exchange，完全隔離：單一品種斷流不影響其他警報。"""
    symbol = alert.cfg["symbol"]
    tf = alert.cfg["timeframe"]
    base = alert.base
    while not STOP:
        exchange = _new_exchange()
        try:
            markets = await _load_markets_retry(exchange, attempts=3)
            if markets is None:
                raise RuntimeError("load_markets 連續失敗")
            await alert.seed_baseline(exchange)
            _mark_data_alive()
            log(f"📡 {base} 開始串流 watch_ohlcv({symbol}, {tf}) ...")

            while not STOP:
                try:
                    candles = await asyncio.wait_for(
                        exchange.watch_ohlcv(symbol, tf), timeout=WATCHDOG_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    ok, probe = await _rest_probe(exchange, symbol, tf)
                    if ok and not _probe_shows_activity(alert, probe):
                        # 真安靜：REST 正常、沒有新活動 → 容忍，維持連線
                        _log_quiet(base, tf)
                        _mark_data_alive()
                        if probe:
                            await alert.on_update(probe)
                        continue
                    log(
                        f"🐶 {base} Watchdog：{WATCHDOG_TIMEOUT}s 無更新且"
                        f"{'偵測到活動' if ok else 'REST 也連不上'}，判定殭屍/斷線 → 重連"
                    )
                    break

                _mark_data_alive()
                await alert.on_update(candles)

        except Exception as e:
            log(f"⚠️ {base} 連線/串流錯誤：{type(e).__name__}: {e}")
        finally:
            try:
                await exchange.close()
            except Exception:
                pass

        if not STOP:
            await _sleep_stop(RECONNECT_DELAY)
            log(f"🔄 {base} 重新連線並重新 seed baseline ...")


async def run_ema_poller(exchange, symbol: str, timeframe: str, alerts) -> None:
    """單一 (symbol,timeframe) 的 ema_breakout 輪詢：定期 REST 抓已收盤棒 → 分派給該組所有警報。
    輪詢天然容忍安靜時段（無新棒＝正常，不誤判殭屍）；單一品種拉取失敗只重試、不影響他人。"""
    base = symbol.split("/")[0]
    fail = 0
    while not STOP:
        try:
            candles = await asyncio.wait_for(
                exchange.fetch_ohlcv(symbol, timeframe, limit=EMA_POLL_LIMIT),
                timeout=WATCHDOG_TIMEOUT,
            )
            fail = 0
            _mark_data_alive()
            for a in alerts:
                await a.on_update(candles)
        except asyncio.TimeoutError:
            fail += 1
            log(f"🐶 {base} {timeframe} EMA 輪詢逾時（第 {fail} 次），稍後重試")
        except Exception as e:
            fail += 1
            if fail <= 3 or fail % 20 == 0:
                log(f"⚠️ {base} {timeframe} EMA 輪詢失敗（第 {fail} 次，將重試）：{type(e).__name__}: {e}")
        await _sleep_stop(EMA_POLL_SEC)


async def heartbeat_loop() -> None:
    """全域心跳：只要最近有任一資料流仍在吐資料，就（節流）對 healthchecks.io ping。"""
    global _LAST_HEARTBEAT
    if not HEALTHCHECK_URL:
        return
    while not STOP:
        await _sleep_stop(min(30, HEARTBEAT_EVERY_SEC))
        now = time.monotonic()
        alive = (now - _LAST_DATA_TS) <= HEARTBEAT_STALE_SEC
        if alive and (now - _LAST_HEARTBEAT) >= HEARTBEAT_EVERY_SEC:
            _LAST_HEARTBEAT = now
            await healthcheck_ping()


# ============================================================================
#  主流程：驗證 symbol → seed → 啟動所有資料流
# ============================================================================
async def run_forever() -> None:
    parsed = [_parse_alert_cfg(a) for a in ALERTS]

    # 共用一個 REST exchange 做：symbol 驗證 + ema seed + ema 輪詢
    rest = _new_exchange()
    try:
        markets = await _load_markets_retry(rest, attempts=4)
        if markets is None:
            log("❌ load_markets 連續失敗，無法啟動（稍後再試或檢查網路/交易所可用性）。")
            return

        # --- symbol 解析驗證 ---
        valid, invalid = [], []
        for cfg in parsed:
            if cfg["symbol"] in markets:
                valid.append(cfg)
            else:
                invalid.append(cfg)
        log(f"🔎 symbol 解析：{len(valid)}/{len(parsed)} 可解析")
        if invalid:
            for cfg in invalid:
                log(f"   ❌ 無法解析：{cfg['symbol']}（{cfg['type']} {cfg['timeframe']}）→ 跳過")
            log("   （若為新上市股票永續解析不到，請升級 requirements.txt 的 ccxt 版本後重跑）")
        else:
            log("   ✅ 全部 symbol 可解析")

        vol_alerts = [VolumeSpikeAlert(c) for c in valid if c["type"] == "volume_spike"]
        ema_alerts = [EmaBreakoutAlert(c) for c in valid if c["type"] == "ema_breakout"]

        # --- ema：依 (symbol,timeframe) 分組，每組抓一次歷史、seed 該組所有警報 ---
        ema_groups = {}
        for a in ema_alerts:
            ema_groups.setdefault((a.cfg["symbol"], a.cfg["timeframe"]), []).append(a)

        if ema_alerts:
            log(f"🌱 開始 seed {len(ema_groups)} 組 ema_breakout 歷史（每組盡量拉 {EMA_SEED_DAYS} 天，分頁）...")
        for (sym, tf), group in ema_groups.items():
            try:
                hist = await _fetch_ohlcv_history(rest, sym, tf, EMA_SEED_DAYS)
            except Exception as e:
                log(f"⚠️ {sym} {tf} seed 歷史抓取失敗：{type(e).__name__}: {e}（此組將以空歷史啟動）")
                hist = []
            closed = hist[:-1] if len(hist) >= 1 else []  # 丟掉最後一根形成中棒
            for a in group:
                a.seed(closed)

        # --- 啟動 Telegram 測試訊息 / 或印未設定提示 ---
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            n_ema_pairs = len({(a.cfg["symbol"], a.cfg["timeframe"]) for a in ema_alerts})
            await telegram_send(
                "✅ 多警報監看系統已啟動\n"
                f"放量突破：{len(vol_alerts)} 則\n"
                f"EMA突破(strict2)：{len(ema_alerts)} 則（{n_ema_pairs} 組品種×框架）\n"
                f"共 {len(valid)} 則警報上線"
            )
        else:
            log("⚠️ 尚未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID：不發 Telegram，只印 log。")
            log("   填好 .env 的兩個值後重跑即可。")

        # --- 啟動所有資料流 ---
        tasks = []
        for a in vol_alerts:
            tasks.append(asyncio.create_task(run_volume_stream(a)))
        for (sym, tf), group in ema_groups.items():
            tasks.append(asyncio.create_task(run_ema_poller(rest, sym, tf, group)))
        if HEALTHCHECK_URL:
            tasks.append(asyncio.create_task(heartbeat_loop()))

        if not tasks:
            log("⚠️ 沒有任何有效警報可啟動。")
            return
        log(f"🚀 啟動 {len(vol_alerts)} 條 volume 串流 + {len(ema_groups)} 條 ema 輪詢，開始監看。")
        await asyncio.gather(*tasks)

    finally:
        try:
            await rest.close()
        except Exception:
            pass
        log("👋 主流程結束。")


# ----------------------------------------------------------------------------
#  進入點
# ----------------------------------------------------------------------------
def _install_signal_handlers() -> None:
    def _handler(signum, frame):
        global STOP
        if not STOP:
            log("🛑 收到中斷訊號，準備優雅退出（最多等下一次行情更新/輪詢）...")
        STOP = True

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


async def main() -> None:
    _install_signal_handlers()

    log("=" * 60)
    log("多警報 即時監看啟動")
    log(f"警報清單（{len(ALERTS)} 則）：")
    for a in ALERTS:
        if a["type"] == "volume_spike":
            log(f"  · 放量突破  {a['symbol']} {a['timeframe']}  "
                f"爆量≥{a.get('vol_mult', VOL_MULT):.1f}x 振幅≥{a.get('range_pct', RANGE_THRESHOLD * 100):.2f}%")
        else:
            log(f"  · EMA突破   {a['symbol']} {a['timeframe']}  "
                f"EMA{a.get('ema_len', EMA_LEN_DEFAULT)} 靜默<P{a.get('quiet_pctile', EMA_QUIET_PCTILE_DEFAULT)}")
    if HEALTHCHECK_URL:
        log(f"存活監控：已啟用，最近有資料時每 {HEARTBEAT_EVERY_SEC}s ping healthchecks.io")
    else:
        log("存活監控：未設定 HEALTHCHECK_URL（停用）")
    log("=" * 60)

    await run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
