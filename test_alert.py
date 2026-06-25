#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
測試程式：餵假的 K 棒資料進 alert.py 真正的判斷/發送路徑（handle_candles），
驗證「兩條件成立 -> 真的發 Telegram 警報」以及「5 分鐘冷卻不洗版」。

跑法：
    source venv/bin/activate
    python test_alert.py

注意：這會用 .env 裡的設定，對你的 Telegram 真的發出一則「🚨 警報」訊息，
讓你確認手機會不會跳通知。發的是真實警報格式（測試資料）。
"""

import asyncio

import alert  # 直接用 alert.py 裡的真實邏輯與設定（含 .env 載入）


def make_candle(ts, low, high, close, volume):
    # ccxt OHLCV 格式：[timestamp, open, high, low, close, volume]
    return [ts, low, high, low, close, volume]


async def main():
    if not alert.TELEGRAM_BOT_TOKEN or not alert.TELEGRAM_CHAT_ID:
        alert.log("❌ .env 沒設定 token/chat id，測試無法發 Telegram。")
        return

    alert.log("=" * 60)
    alert.log("測試開始：模擬一根「爆量 + 大振幅」的 15m K 棒")
    alert.log("=" * 60)

    # 準備狀態：上一根已收完量 = 100 BTC（baseline）
    state = alert.new_state()
    ts = 1_700_000_000_000          # 任意固定 timestamp（毫秒）
    state["baseline_vol"] = 100.0
    state["cur_ts"] = ts            # 設成跟測試棒同一根，避免被當成換棒

    # 測試棒：量 250 BTC -> vol_ratio = 2.5x（≥2.0 ✓）
    #          low=61000 high=61500 -> range = 500/61000 ≈ 0.82%（≥0.5% ✓）
    trigger = make_candle(ts, low=61000.0, high=61500.0, close=61480.0, volume=250.0)

    alert.log("→ 第 1 次餵入：兩條件都成立，應該要發出 Telegram 警報")
    await alert.handle_candles([trigger], state)

    # 同一波再餵一次：應該被冷卻擋下，不重複發
    alert.log("→ 第 2 次餵入（同一波）：應該被 5 分鐘冷卻擋下，不再發")
    await alert.handle_candles([trigger], state)

    # 反例：量夠但振幅不足，應該完全不發
    alert.log("→ 第 3 次餵入：量爆但振幅只有 0.1%，不該觸發")
    state2 = alert.new_state()
    state2["baseline_vol"] = 100.0
    state2["cur_ts"] = ts
    small_range = make_candle(ts, low=61000.0, high=61061.0, close=61050.0, volume=300.0)
    await alert.handle_candles([small_range], state2)

    alert.log("=" * 60)
    alert.log("測試結束：你的手機應該只收到 1 則 🚨 警報（第 1 次那筆）。")
    alert.log("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
