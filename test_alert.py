#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
測試程式：餵假的 K 棒資料進 alert.py 真正的判斷/發送路徑，驗證：
  1. volume_spike：兩條件成立 → 發 Telegram 警報；5 分鐘冷卻不重發；振幅不足不觸發，
     且訊息文字與舊版 byte-identical（迴歸鎖定）。
  2. ema_breakout strict2：多/空偵測、影線違反不觸發、串流==批次一致。
  3. 靜默突破分位：percentile_rank 與「是/否/資料不足」三態判定。

跑法：
    source venv/bin/activate
    python test_alert.py

純邏輯測試（strict2 / 靜默 / 訊息格式）不需 .env、不連網、一定會跑並斷言。
最後的 volume_spike 情境若 .env 有設 token/chat id，會對你的 Telegram 真的發一則
「🚨 警報」讓你確認手機跳通知；沒設就自動略過（測試仍全綠）。
"""

import asyncio

import alert  # 直接用 alert.py 裡的真實邏輯與設定（含 .env 載入）


def make_candle(ts, low, high, close, volume):
    # ccxt OHLCV 格式：[timestamp, open, high, low, close, volume]
    return [ts, low, high, low, close, volume]


def _ohlc(ts, o, h, l, c, v=1.0):
    return [ts, o, h, l, c, v]


# ----------------------------------------------------------------------------
#  1. volume_spike：訊息 byte-identical + 觸發/冷卻/不誤觸（不需 telegram）
# ----------------------------------------------------------------------------
async def test_volume_message_and_trigger():
    ts = 1_700_000_000_000
    sent = []

    orig_send = alert.telegram_send

    async def capture(text):
        sent.append(text)
        return True

    orig_log = alert._append_alert_log
    alert.telegram_send = capture
    alert._append_alert_log = lambda rec: None  # 測試時不落地 JSONL
    try:
        # 情境1：兩條件成立 → 應發 1 則
        state = alert.new_state()
        state["baseline_vol"] = 100.0
        state["cur_ts"] = ts
        trigger = make_candle(ts, low=61000.0, high=61500.0, close=61480.0, volume=250.0)
        await alert.handle_candles([trigger], state)
        assert len(sent) == 1, f"應發 1 則，實際 {len(sent)}"

        # 情境2：同一波再餵 → 冷卻擋下、不重發
        await alert.handle_candles([trigger], state)
        assert len(sent) == 1, "冷卻期間不應重發"

        # 情境3：量爆但振幅不足 → 不觸發
        state2 = alert.new_state()
        state2["baseline_vol"] = 100.0
        state2["cur_ts"] = ts
        small = make_candle(ts, low=61000.0, high=61061.0, close=61050.0, volume=300.0)
        await alert.handle_candles([small], state2)
        assert len(sent) == 1, "振幅不足不應觸發"

        # 訊息文字逐行鎖定（時間行因含牆鐘時間而排除；K棒開盤行用 fmt_ts 保持時區無關）
        lines = sent[0].split("\n")
        expected = [
            "🟢 BTC +0.79% (+480.0)|61480.0|2.50x|振幅0.82%",
            "商品：BTC/USDT:USDT（15m）",
            None,  # 時間：...（略過比對）
            f"K 棒開盤：{alert.fmt_ts(ts)}",
            "現價：61480.0",
            "爆量：2.50x（門檻 2.0x）",
            "  當前量 250.000 BTC / 上一根 100.000 BTC",
            "振幅：0.820%（門檻 0.50%）",
            "  high 61500.0 / low 61000.0",
        ]
        assert len(lines) == len(expected), f"訊息行數不符：{len(lines)} vs {len(expected)}"
        for got, exp in zip(lines, expected):
            if exp is None:
                assert got.startswith("時間："), f"第3行應為時間行：{got!r}"
            else:
                assert got == exp, f"訊息行不符：\n  got: {got!r}\n  exp: {exp!r}"
        alert.log("✅ volume_spike：訊息 byte-identical + 觸發/冷卻/不誤觸 全部通過")
    finally:
        alert.telegram_send = orig_send
        alert._append_alert_log = orig_log


# ----------------------------------------------------------------------------
#  2. strict2 EMA 突破偵測（多 / 空 / 影線違反不觸發 / 串流==批次）
# ----------------------------------------------------------------------------
def _build_long_scenario(low_confirm):
    """12 根平盤(100) → 實體上穿棒 → 確認棒。回傳 (bars, confirm_idx)。"""
    ts = 1_700_000_000_000
    step = 900_000
    bars = [_ohlc(ts + i * step, 100.0, 100.0, 100.0, 100.0) for i in range(12)]
    bars.append(_ohlc(ts + 12 * step, 99.5, 100.7, 99.4, 100.5))   # 實體上穿（o<ema, c>ema）
    bars.append(_ohlc(ts + 13 * step, 100.6, 101.3, low_confirm, 101.0))  # 確認棒
    return bars, 13


def _build_short_scenario(high_confirm):
    ts = 1_700_000_000_000
    step = 900_000
    bars = [_ohlc(ts + i * step, 100.0, 100.0, 100.0, 100.0) for i in range(12)]
    bars.append(_ohlc(ts + 12 * step, 100.5, 100.6, 99.3, 99.5))   # 實體下穿（o>ema, c<ema）
    bars.append(_ohlc(ts + 13 * step, 99.4, high_confirm, 99.1, 99.0))  # 確認棒
    return bars, 13


def _detect(bars):
    det = alert.Strict2Detector(ema_len=20, quiet_pctile=20, min_signals=0)
    out = []
    for b in bars:
        s = det.feed_bar(b, silent=False)
        if s is not None:
            out.append(s)
    return out


def test_strict2_detection():
    # --- 多方：確認棒整根在 EMA 上 → 觸發 long ---
    bars, ci = _build_long_scenario(low_confirm=100.5)
    closes = [b[4] for b in bars]
    emas = alert.ema_series(closes, 20)
    # 前置條件（自我驗證這個情境確實符合 strict2 多方定義）
    assert bars[ci - 1][1] < emas[ci - 1] and bars[ci - 1][4] > emas[ci - 1], "breakout 棒應實體上穿"
    assert bars[ci][3] > emas[ci], "確認棒 low 應 > ema"
    sigs = _detect(bars)
    assert len(sigs) == 1 and sigs[0]["side"] == "long" and sigs[0]["idx"] == ci, f"應偵測到 long@{ci}：{sigs}"
    exp_amp = (bars[ci][2] - bars[ci][3]) / bars[ci][3]
    assert abs(sigs[0]["amplitude"] - exp_amp) < 1e-12, "振幅計算應為 (high-low)/low"

    # --- 影線違反：確認棒 low 跌破 EMA → 不觸發 ---
    bars_v, ci_v = _build_long_scenario(low_confirm=99.9)  # low 在 EMA 下
    assert bars_v[ci_v][3] < emas[ci_v], "此變體確認棒 low 應在 ema 下（違反 strict2）"
    assert _detect(bars_v) == [], "確認棒影線跌破 EMA 不應觸發"

    # --- 同向站穩違反（2026-08-21 加嚴）：確認棒整根在 EMA 上但收陰 → 不觸發多方 ---
    bars_d, ci_d = _build_long_scenario(low_confirm=100.5)
    o_d = bars_d[ci_d][1]
    bars_d[ci_d][4] = o_d - 0.05  # close < open（陰線），但 low=100.5 仍在 EMA 上
    closes_d = [b[4] for b in bars_d]
    emas_d = alert.ema_series(closes_d, 20)
    assert bars_d[ci_d][3] > emas_d[ci_d], "此變體確認棒 low 仍應 > ema（只違反方向）"
    assert _detect(bars_d) == [], "確認棒收陰（與多方訊號反向）不應觸發"
    # 空方鏡像：確認棒整根在 EMA 下但收陽 → 不觸發空方
    sbars_d, sci_d = _build_short_scenario(high_confirm=99.5)
    sbars_d[sci_d][4] = sbars_d[sci_d][1] + 0.05  # close > open（陽線）
    scloses_d = [b[4] for b in sbars_d]
    semas_d = alert.ema_series(scloses_d, 20)
    assert sbars_d[sci_d][2] < semas_d[sci_d], "此變體確認棒 high 仍應 < ema（只違反方向）"
    assert _detect(sbars_d) == [], "確認棒收陽（與空方訊號反向）不應觸發"

    # --- 空方鏡像 ---
    sbars, sci = _build_short_scenario(high_confirm=99.5)
    scloses = [b[4] for b in sbars]
    semas = alert.ema_series(scloses, 20)
    assert sbars[sci - 1][1] > semas[sci - 1] and sbars[sci - 1][4] < semas[sci - 1], "breakout 棒應實體下穿"
    assert sbars[sci][2] < semas[sci], "確認棒 high 應 < ema"
    ssigs = _detect(sbars)
    assert len(ssigs) == 1 and ssigs[0]["side"] == "short" and ssigs[0]["idx"] == sci, f"應偵測到 short@{sci}：{ssigs}"

    # --- 串流 == 批次（gate 一致；用會實際產生訊號的確定性正弦路徑）---
    import math
    ts = 1_700_000_000_000
    walk = []
    for i in range(400):
        px = 100.0 + 8.0 * math.sin(i / 9.0)
        nxt = 100.0 + 8.0 * math.sin((i + 1) / 9.0)
        o = px
        c = nxt  # 讓實體帶方向，貼近真實 K 棒
        walk.append(_ohlc(ts + i * 900_000, o, max(o, c) + 0.05, min(o, c) - 0.05, c))
    wcloses = [b[4] for b in walk]
    wemas = alert.ema_series(wcloses, 20)
    batch = set(alert.strict2_scan([b[1] for b in walk], [b[2] for b in walk],
                                   [b[3] for b in walk], wcloses, wemas))
    stream = set((s["idx"], s["side"]) for s in _detect(walk))
    assert len(batch) >= 5, f"正弦路徑應產生數個訊號供交叉比對，實際 {len(batch)}"
    sides = {side for _, side in batch}
    assert sides == {"long", "short"}, f"正弦路徑應多空皆有：{sides}"
    assert stream == batch, f"串流與批次不一致：僅串流{stream - batch} 僅批次{batch - stream}"
    alert.log(f"✅ strict2：多/空/影線違反/串流==批次 全部通過（sine 訊號 {len(batch)} 筆，多空皆有）")


# ----------------------------------------------------------------------------
#  3. 靜默突破分位（percentile_rank + 是/否/資料不足 三態）
# ----------------------------------------------------------------------------
def test_quiet_percentile():
    dist = [0.01 * i for i in range(1, 101)]  # 0.01 .. 1.00（100 筆）
    assert alert.percentile_rank(dist, 0.005) == 0.0, "低於全部 → P0"
    assert alert.percentile_rank(dist, 0.105) == 10.0, "小於 0.105 的有 10 筆 → P10"
    assert alert.percentile_rank(dist, 1.5) == 100.0, "高於全部 → P100"
    assert alert.percentile_rank([], 0.5) == 0.0, "空分佈保護"

    # 是：本次振幅落在歷史低分位（< quiet_pctile）
    bars, ci = _build_long_scenario(low_confirm=100.5)
    det = alert.Strict2Detector(ema_len=20, quiet_pctile=20, min_signals=30)
    det.amp_dist = [0.02] * 40  # 注入 40 筆歷史振幅（皆大於本訊號振幅≈0.008）
    sig = None
    for b in bars:
        s = det.feed_bar(b, silent=False)
        if s:
            sig = s
    assert sig is not None and sig["insufficient"] is False
    assert sig["amp_pctile"] == 0.0 and sig["quiet"] is True, f"應為靜默(P0)：{sig}"

    # 否：本次振幅高於歷史 → 不靜默
    det2 = alert.Strict2Detector(ema_len=20, quiet_pctile=20, min_signals=30)
    det2.amp_dist = [0.001] * 40  # 歷史皆極小
    sig2 = None
    for b in bars:
        s = det2.feed_bar(b, silent=False)
        if s:
            sig2 = s
    assert sig2["amp_pctile"] == 100.0 and sig2["quiet"] is False, f"應為不靜默(P100)：{sig2}"

    # 資料不足：歷史訊號 < 30
    det3 = alert.Strict2Detector(ema_len=20, quiet_pctile=20, min_signals=30)
    det3.amp_dist = [0.02] * 10
    sig3 = None
    for b in bars:
        s = det3.feed_bar(b, silent=False)
        if s:
            sig3 = s
    assert sig3["insufficient"] is True and sig3["amp_pctile"] is None and sig3["quiet"] is None
    assert sig3["n_hist"] == 10, f"n_hist 應為 10：{sig3}"
    alert.log("✅ 靜默突破分位：是 / 否 / 資料不足 三態 + percentile_rank 全部通過")


# ----------------------------------------------------------------------------
#  4. ema_breakout 訊息格式（使用者逐字核准）
# ----------------------------------------------------------------------------
def test_ema_message_format():
    from datetime import datetime
    cfg = {"base": "BTC", "symbol": "BTC/USDT:USDT", "timeframe": "1h",
           "ema_len": 20, "tf_ms": alert._timeframe_ms("1h")}
    ts = 1_700_000_000_000
    now = datetime(2026, 8, 21, 3, 0, 2)
    sig = {"side": "long", "bar_open_ts": ts, "open": 1, "high": 2, "low": 1, "close": 43180.5,
           "ema": 1, "amplitude": 0.01, "amp_pctile": 12.0, "quiet": True, "insufficient": False,
           "n_hist": 57, "idx": 9}
    msg = alert.build_ema_message(cfg, sig, now=now)
    lines = msg.split("\n")
    assert lines[0] == "🟢 BTC 突破多（1h）|43180.5|靜默:是", lines[0]
    assert lines[1] == "商品：BTC/USDT:USDT（1h・EMA20突破）", lines[1]
    assert lines[2] == "時間：2026-08-21 03:00:02", lines[2]
    assert lines[3] == f"訊號K棒：{alert.fmt_ts(ts)} 開 / {alert.fmt_ts(ts + cfg['tf_ms'])} 收", lines[3]
    assert lines[4] == "收盤價：43180.5", lines[4]
    assert lines[5] == "靜默突破：是（振幅分位 P12）", lines[5]

    # 空方 + 不靜默
    sig_s = dict(sig, side="short", quiet=False, amp_pctile=63.4)
    ms2 = alert.build_ema_message(cfg, sig_s, now=now).split("\n")
    assert ms2[0] == "🔴 BTC 突破空（1h）|43180.5|靜默:否", ms2[0]
    assert ms2[5] == "靜默突破：否（振幅分位 P63）", ms2[5]

    # 資料不足
    sig_i = dict(sig, quiet=None, amp_pctile=None, insufficient=True, n_hist=12)
    ms3 = alert.build_ema_message(cfg, sig_i, now=now).split("\n")
    assert ms3[0] == "🟢 BTC 突破多（1h）|43180.5|靜默:資料不足", ms3[0]
    assert ms3[5] == "靜默突破：資料不足（歷史訊號 n=12）", ms3[5]
    alert.log("✅ ema_breakout 訊息格式（多/空/靜默/資料不足）全部通過")


# ----------------------------------------------------------------------------
#  5. （選配）真的對 Telegram 發一則 volume_spike 警報 —— 有 .env 才跑
# ----------------------------------------------------------------------------
async def test_volume_telegram_optional():
    if not alert.TELEGRAM_BOT_TOKEN or not alert.TELEGRAM_CHAT_ID:
        alert.log("→ 未設定 .env token/chat id，略過『真的發 Telegram』情境（純邏輯測試已全綠）")
        return
    alert.log("→ .env 已設定：對 Telegram 真的發一則 🚨 警報（確認手機會跳通知）")
    state = alert.new_state()
    ts = 1_700_000_000_000
    state["baseline_vol"] = 100.0
    state["cur_ts"] = ts
    trigger = make_candle(ts, low=61000.0, high=61500.0, close=61480.0, volume=250.0)
    await alert.handle_candles([trigger], state)
    if alert.HEALTHCHECK_URL:
        alert.log("→ 對 healthchecks.io ping 一次（確認存活監控）")
        await alert.healthcheck_ping()


async def main():
    alert.log("=" * 60)
    alert.log("測試開始")
    alert.log("=" * 60)
    await test_volume_message_and_trigger()
    test_strict2_detection()
    test_quiet_percentile()
    test_ema_message_format()
    await test_volume_telegram_optional()
    alert.log("=" * 60)
    alert.log("✅ 全部測試通過")
    alert.log("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
