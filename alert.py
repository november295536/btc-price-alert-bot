#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 成交量爆量 + 振幅 即時警報（單一檔案、本機 terminal 直接跑）

監看 Bybit 的 BTC USDT 本位線性永續合約，當「爆量」且「振幅夠大」同時成立時，
立刻發 Telegram 訊息通知。只讀公開行情、不下單，因此不需要任何 Bybit API key。

跑法：
    python -m venv venv
    source venv/bin/activate
    pip install ccxt
    # 把下方設定區的 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 填好
    python alert.py

依賴：只有 ccxt（含 ccxt.pro，串流用）。發 Telegram 用 Python 標準庫 urllib，不需要 requests。

⚠️ 本機跑的提醒：筆電「睡眠 / 休眠」會中斷網路連線，websocket 會斷。
   這支程式靠下面的「重連 + 重新 seed baseline + watchdog」邏輯自癒，
   不需要你手動重啟；但若你長時間闔上筆電，喚醒後可能需要幾秒才重新接上。
"""

# =============================================================================
#  設定區（要改的東西都在這裡）
# =============================================================================

# 監看商品：Bybit USDT 本位線性永續。已實測確認此寫法對應 linear / swap 市場。
SYMBOL = "BTC/USDT:USDT"

# 時間框架：15 分鐘 K 棒
TIMEFRAME = "15m"

# 爆量倍數門檻：當前「形成中」棒的累積量 ≥ 上一根「已收完」棒完整量的幾倍
VOL_MULT = 2.0

# 振幅門檻：(high - low) / low ≥ 多少（0.005 = 0.5%）
RANGE_THRESHOLD = 0.005

# 冷卻秒數：觸發後幾秒內不再發，避免同一波洗版（300 = 5 分鐘）
COOLDOWN_SEC = 300

# Watchdog 逾時秒數：超過這麼久沒收到任何行情更新，就判定為殭屍連線並主動重連
WATCHDOG_TIMEOUT = 60

# 重連前等待秒數
RECONNECT_DELAY = 2

# 狀態行印出方式：
#   - 在互動式終端機（TTY）：每秒「原地刷新」同一行，不洗版（此設定不影響它）。
#   - 輸出被導到檔案（nohup / > log.txt 等非 TTY）：每隔這麼多秒才印一行狀態，避免洗爆檔案。
# 不論哪種模式，觸發/換棒/重連/斷線等「事件」一律即時印成獨立一行。
STATUS_LOG_EVERY_SEC = 60

# 存活監控（healthchecks.io 死人開關）：每隔這麼多秒，在「成功收到行情」後對
# HEALTHCHECK_URL（放 .env）ping 一次。心跳一停（被回收/崩潰/網路死），對方會主動通知你。
# healthchecks.io 上把該 check 的 period 設成略大於這個值、grace 給寬一點（如 10 分鐘），
# 才不會被短暫斷線/重連誤報。沒填 HEALTHCHECK_URL 就完全停用此功能。
HEARTBEAT_EVERY_SEC = 300

# Telegram 設定：放在同目錄的 .env 檔，不寫死在程式碼裡（見檔尾說明如何取得）。
#   .env 內容範例：
#     TELEGRAM_BOT_TOKEN=123456789:AAEx.....
#     TELEGRAM_CHAT_ID=123456789
# 也可以用環境變數覆蓋；兩者都沒有就不發 Telegram、只印 log。
# （以下實際值在 import 後、由 _load_env() 從 .env 載入，見下方）
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
# 存活監控 ping URL（healthchecks.io），放 .env 的 HEALTHCHECK_URL；留空＝停用
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
    # 環境變數優先，其次 .env
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
        # 先清掉那行原地刷新的狀態行，事件訊息才不會跟它黏在一起
        sys.stdout.write("\r\033[2K")
        _status_active = False
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def status(msg: str) -> None:
    """每秒的行情狀態：
    - TTY：原地刷新同一行（不洗版）。
    - 非 TTY（導到檔案）：每 STATUS_LOG_EVERY_SEC 秒才印一行。"""
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
    except Exception as e:  # 網路錯誤、token 錯等
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
        # 心跳失敗通常代表網路問題（此時行情多半也收不到）；bot 本身仍在跑
        log(f"⚠️ healthchecks.io 心跳發送失敗：{info}")


# ----------------------------------------------------------------------------
#  狀態（全部放記憶體，程式重啟就重來）
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


# ----------------------------------------------------------------------------
#  用 REST 重新 seed baseline
#  重點：每次（重）連線、或從異常恢復後都要呼叫，不能信記憶體裡可能過期的值，
#  因為斷線若跨過棒收盤，舊 baseline 會算歪。
# ----------------------------------------------------------------------------
async def seed_baseline(exchange, state: dict) -> None:
    # limit=2 -> [上一根已收完, 目前形成中]
    candles = await exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=2)
    if len(candles) < 2:
        raise RuntimeError(f"fetch_ohlcv 只回傳 {len(candles)} 根，無法 seed baseline")
    prev_closed = candles[-2]   # [ts, o, h, l, c, v]
    current = candles[-1]
    state["baseline_vol"] = prev_closed[5]
    state["cur_ts"] = current[0]
    state["cur_vol"] = current[5]
    log(
        f"🌱 seed baseline 完成：上一根已收完量 = {prev_closed[5]:.3f} BTC"
        f"（收盤於 {fmt_ts(prev_closed[0])}）；"
        f"目前形成中棒 {fmt_ts(current[0])} 起始量 = {current[5]:.3f} BTC"
    )


# ----------------------------------------------------------------------------
#  處理每一次 watch_ohlcv 更新：計算 vol_ratio / range_pct，判斷是否觸發
# ----------------------------------------------------------------------------
async def handle_candles(candles, state: dict) -> None:
    if not candles:
        return

    # watch_ohlcv 回傳 list，最後一根就是「正在形成中」的那根
    ts, o, high, low, close, vol = candles[-1][:6]

    # --- 偵測棒收盤：timestamp 變了，代表上一根已收完、新的一根開始 ---
    if state["cur_ts"] is not None and ts != state["cur_ts"]:
        # 把 baseline 更新成「剛收完那根」最後觀測到的完整量
        closed_vol = state["cur_vol"]
        state["baseline_vol"] = closed_vol
        log(
            f"🕒 新的一根 {TIMEFRAME} K 棒開始（{fmt_ts(ts)}）；"
            f"baseline 更新為剛收完那根的量 = {closed_vol:.3f} BTC"
        )
    state["cur_ts"] = ts
    state["cur_vol"] = vol

    baseline = state["baseline_vol"]

    # --- 計算兩個訊號值（都做除零保護；正常 BTC 行情不會為 0）---
    vol_ratio = (vol / baseline) if (baseline and baseline > 0) else float("inf")
    range_pct = ((high - low) / low) if low > 0 else 0.0

    both = vol_ratio >= VOL_MULT and range_pct >= RANGE_THRESHOLD
    now = time.monotonic()
    elapsed = now - state["last_trigger"]
    cooling = state["last_trigger"] > 0 and elapsed < COOLDOWN_SEC

    # --- 每秒的行情狀態：原地刷新一行（不洗版）；冷卻/條件成立只當成這行上的標記 ---
    flag = ""
    if both and cooling:
        flag = f"  [⏳ 冷卻中 {COOLDOWN_SEC - elapsed:.0f}s]"
    elif both:
        flag = "  [⚡ 條件成立]"
    status(
        f"K[{fmt_ts(ts)}] price={close} vol={vol:.3f} baseline={baseline:.3f} "
        f"vol_ratio={vol_ratio:.2f}x range_pct={range_pct * 100:.3f}%{flag}"
    )

    # --- 兩條件同時成立、且不在冷卻中才觸發（棒中即時觸發，不等收盤）---
    if both and not cooling:
        state["last_trigger"] = now
        # 這根形成中棒的漲跌：以現價 close 對開盤 o 判斷（除零保護）。
        # 注意 close 是「形成中棒的當下最新價」而非收盤價，故 close-o 即這根到目前為止的漲跌。
        chg_pct = ((close - o) / o * 100) if o > 0 else 0.0
        diff = close - o  # 現價與開盤的絕對價差（帶正負號）
        # 方向已由 🟢/🔴 + 數字正負號雙重表達，不再寫「上漲/下跌」字；下跌時 chg_pct/diff 本身帶負號
        if close > o:
            emoji, move = "🟢", f"+{chg_pct:.2f}% (+{diff:.1f})"
        elif close < o:
            emoji, move = "🔴", f"{chg_pct:.2f}% ({diff:.1f})"
        else:
            emoji, move = "⚪", "+0.00% (0)"
        text = (
            # 第一行＝手機通知一眼看到的摘要：方向/變動(含價差)、現價、爆量倍數、振幅，單空格分隔
            f"{emoji} BTC {move} {close} {vol_ratio:.2f}x 振幅{range_pct * 100:.2f}%\n"
            f"商品：{SYMBOL}（{TIMEFRAME}）\n"
            f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"K 棒開盤：{fmt_ts(ts)}\n"
            f"現價：{close}\n"
            f"爆量：{vol_ratio:.2f}x（門檻 {VOL_MULT:.1f}x）\n"
            f"  當前量 {vol:.3f} BTC / 上一根 {baseline:.3f} BTC\n"
            f"振幅：{range_pct * 100:.3f}%（門檻 {RANGE_THRESHOLD * 100:.2f}%）\n"
            f"  high {high} / low {low}"
        )
        log(f"🚨 觸發警報！vol_ratio={vol_ratio:.2f}x range_pct={range_pct * 100:.3f}%")
        await telegram_send(text)


# ----------------------------------------------------------------------------
#  主迴圈：(重)連線 -> REST seed -> 串流 watch_ohlcv -> 出錯就重連並重新 seed
# ----------------------------------------------------------------------------
async def run_forever() -> None:
    state = new_state()

    while not STOP:
        # ccxt.pro 的 watch_* 內建自動重連與 keepalive（每 20s ping 等），
        # 所以不自己手刻 websocket。但「重連 ≠ 無縫」，故每次重新建立連線都重新 seed。
        exchange = ccxtpro.bybit({
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        try:
            await exchange.load_markets()
            await seed_baseline(exchange, state)
            state["last_data_ts"] = time.monotonic()
            log(f"📡 開始串流 watch_ohlcv({SYMBOL}, {TIMEFRAME}) ...")

            while not STOP:
                # Watchdog：用 wait_for 包住 watch_ohlcv。
                # 正常情況下行情約每秒更新，timeout 不會觸發；
                # 但若連線「看似還在、其實不再吐資料」（殭屍狀態），
                # 超過 WATCHDOG_TIMEOUT 秒沒有任何更新就丟 TimeoutError，
                # 我們據此跳出去重連並重新 seed —— 這正是最難纏的失效模式。
                try:
                    candles = await asyncio.wait_for(
                        exchange.watch_ohlcv(SYMBOL, TIMEFRAME),
                        timeout=WATCHDOG_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    log(
                        f"🐶 Watchdog：超過 {WATCHDOG_TIMEOUT}s 沒收到任何行情更新，"
                        f"判定為殭屍連線，主動重啟 watch 迴圈"
                    )
                    break  # 跳出內層 -> 重連 + 重新 seed

                now = time.monotonic()
                state["last_data_ts"] = now
                await handle_candles(candles, state)

                # 存活心跳：成功收到行情後（節流）對 healthchecks.io ping 一次。
                # 放在「收到資料」之後，所以 ping 代表的是「真的有在運作」而非只是行程沒死。
                if HEALTHCHECK_URL and (now - state["last_heartbeat"]) >= HEARTBEAT_EVERY_SEC:
                    state["last_heartbeat"] = now
                    await healthcheck_ping()

        except Exception as e:
            # 任何連線 / 串流錯誤（含斷線 1006 等）都在這裡接住，不讓整支程式死掉
            log(f"⚠️ 連線/串流發生錯誤：{type(e).__name__}: {e}")
        finally:
            try:
                await exchange.close()
            except Exception:
                pass

        if not STOP:
            await asyncio.sleep(RECONNECT_DELAY)
            log("🔄 重新連線並重新 seed baseline ...")

    log("👋 主迴圈結束。")


# ----------------------------------------------------------------------------
#  進入點：先發 Telegram 啟動測試訊息，再開始監看；處理 Ctrl+C 優雅退出
# ----------------------------------------------------------------------------
def _install_signal_handlers() -> None:
    """攔截 SIGINT / SIGTERM，設定 STOP 旗標讓迴圈自然收尾（不靠拋例外）。"""
    def _handler(signum, frame):
        global STOP
        if not STOP:
            log("🛑 收到中斷訊號，準備優雅退出（最多等下一次行情更新，約 1 秒）...")
        STOP = True

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


async def main() -> None:
    _install_signal_handlers()

    log("=" * 60)
    log("BTC 爆量 + 振幅 即時警報啟動")
    log(
        f"設定：{SYMBOL} {TIMEFRAME} | 爆量≥{VOL_MULT:.1f}x | "
        f"振幅≥{RANGE_THRESHOLD * 100:.2f}% | 冷卻{COOLDOWN_SEC}s | watchdog {WATCHDOG_TIMEOUT}s"
    )
    if HEALTHCHECK_URL:
        log(f"存活監控：已啟用，每 {HEARTBEAT_EVERY_SEC}s ping healthchecks.io")
    else:
        log("存活監控：未設定 HEALTHCHECK_URL（停用）")
    log("=" * 60)

    # 啟動測試訊息：讓你立刻確認 token / chat id 設定正確，不用等真訊號
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        await telegram_send(
            "✅ BTC 爆量警報系統已啟動\n"
            f"監看 {SYMBOL} {TIMEFRAME}\n"
            f"爆量≥{VOL_MULT:.1f}x 且 振幅≥{RANGE_THRESHOLD * 100:.2f}% 時通知你"
        )
    else:
        log("⚠️ 尚未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID：")
        log("   不會發 Telegram，但仍會在這裡印出 vol_ratio / range_pct 供你觀察。")
        log("   填好設定區的兩個值後重跑即可。")

    await run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 理論上 signal handler 會先處理；這裡是保險
        pass
