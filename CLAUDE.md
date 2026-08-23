# CLAUDE.md

給未來在這個 repo 工作的 Claude Code 的指引。先讀這份，再動手。

## 這是什麼

單一檔案的 Python 即時警報程式，用一份**設定驅動的警報清單**（`alert.py` 最上方的 `ALERTS`）同時跑
多種、多品種、多週期的警報，監看 Bybit USDT 本位線性永續，命中就發 Telegram。只讀公開行情、不下單，
**不需要 Bybit API key**。目前兩種警報：

- **volume_spike（放量突破）**：某商品某週期「形成中」棒**爆量**（≥ 上一根已收完棒的 N 倍）且**漲跌幅**
  （`|close-open|/open` ≥ 門檻）同時成立 → **棒內即發**、**同一根 K 棒只發一次**。走 `ccxt.pro
  watch_ohlcv` 串流。門檻語義是 2026-08-23 使用者指定的修正：原本量 high-low 振幅，會把「上下掃一圈
  又收回開盤價」的假突破發出來（實際案例：振幅 0.505% 但漲跌只 -0.01%，同棒隔 5 分鐘冷卻到期又重發
  一次）——改成量實際漲跌幅＋同棒去重後，舊的 byte-identical 迴歸鎖定已不再適用（訊息多了「漲跌幅」
  行，見 `test_alert.py` 的逐行鎖定）。
- **ema_breakout（EMA20 突破・strict2＋同向站穩）**：只用**已收盤**棒——前一棒實體上穿 EMA、本棒整根
  含影線站上 EMA、**且本棒收盤與訊號同向（多=陽線、空=陰線）**→ 多；空方鏡像。走 **REST 輪詢**（只吃
  已收盤棒，天然容忍股票永續安靜時段：無新棒＝正常，不誤判殭屍）。附「靜默突破」分位判定。基底語義
  逐行對照 quantitive-trading 專案 `ema_breakout/ml/mode_scan.py` 的 `_signal_masks` strict2 分支
  （曾對拍 100% 一致）；「同向站穩」是使用者 2026-08-21 指定的加嚴（Pine 原版不看方向、研究側也未加
  ——與 mode_scan 對拍時記得對齊此條件，訊號數約為原版的 70%）。

使用者（peter）的目的：在自己筆電的 terminal 直接跑，盯多個品種的短線訊號。

## 設計原則（請嚴格遵守，使用者明確要求過）

- **不要過度設計**。維持單一檔案 `alert.py`、`python alert.py` 就能跑。
- **依賴越少越好**：目前只有 `ccxt`（含 `ccxt.pro`），鎖在 `requirements.txt`（`pip install -r requirements.txt`）。Telegram 用標準庫 `urllib`，**不要**改用 `requests` 或加 `python-dotenv`。ccxt 版本鎖死是故意的（它常改、緊跟交易所 API），更新要手動且更新後重跑 `test_alert.py`。
- **不要**加資料庫、Docker、framework、持久化。狀態放記憶體，死掉重啟就好（`logs/alerts.jsonl` 只是
  gitignored 的 append-only 日誌，不是狀態來源）。
- 不要把 `.env` commit 出去（已被 `.gitignore` 排除）。token 是機密。

## 檔案

| 檔案 | 說明 |
|------|------|
| `alert.py` | 主程式。設定區（`ALERTS` + 常數）在最上方；下方依序是 `_load_env` → log/status → telegram → healthcheck → JSONL log → **strict2 偵測（`Strict2Detector` / `strict2_scan` / `ema_series` / `percentile_rank`）** → 訊息 builder → volume state/`handle_candles` → **Alert 類（`VolumeSpikeAlert` / `EmaBreakoutAlert`）** → runner（`run_volume_stream` ws / `run_ema_poller` REST / `heartbeat_loop`）→ `run_forever` → `main` |
| `test_alert.py` | 餵假 K 棒進真實路徑：volume 觸發/冷卻/同棒去重/假突破不誤觸 + 訊息逐行鎖定；strict2 多/空/影線違反/串流==批次；靜默分位三態；ema 訊息格式 |
| `.env` | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `HEALTHCHECK_URL`（不進版控） |
| `logs/alerts.jsonl` | 已發警報 JSONL（runtime、gitignored；寫入失敗不影響發送） |
| `README.md` | 給使用者的操作說明 |

## 關鍵設計（改動前務必理解，否則容易改壞）

### volume_spike（ws 串流）
- **baseline = 上一根已收完棒的完整量**，是爆量判斷的分母。
- **門檻量「漲跌幅」不是「振幅」**（2026-08-23 使用者指定）：`move_pct = (close-open)/open`，取絕對值
  與 `move_threshold` 比。`range_pct`（high-low 振幅）仍計算，但只在訊息裡當參考資訊。不要改回振幅。
- **seed**：啟動與**每次重連**都用 REST `fetch_ohlcv(symbol, tf, limit=2)` 重新 seed，
  `candles[-2]` 是上一根已收完、`candles[-1]` 是形成中那根。**不可**信記憶體舊值——斷線若跨過棒收盤會算歪。
- **換棒偵測**：`watch_ohlcv` 回傳 list（實測 Bybit 只回**單一形成中棒**），最後一根的 timestamp 變了就代表換棒；
  此時 baseline 更新成剛收完那根「最後觀測到的量」（`state["cur_vol"]`）。
- **Watchdog（volume 專用，安靜容忍）**：用 `asyncio.wait_for(watch_ohlcv, timeout=WATCHDOG_TIMEOUT)`。超時後
  **先 REST 探測**：探測正常且無新活動＝市場安靜 → 容忍、維持連線（不誤判殭屍）；探測顯示有新活動或連不上＝
  真殭屍/斷線 → 重連並重新 seed。**不要**自己手刻 websocket，`ccxt.pro` 的 `watch_*` 已內建重連 + keepalive。
- **同棒去重 + 冷卻**：同一根 K 棒只發一次（`state["last_trigger_bar_ts"]` 記棒開盤時戳；冷卻到期也
  不重發——15m 棒 > 5 分鐘冷卻，沒有去重會同棒連發）。冷卻用 `time.monotonic()`（不受系統時鐘調整
  影響），觸發後 `cooldown_sec` 內不重發，擋跨棒連發。
- **每次更新照算、照判斷觸發**；只有「印出來」被節流，不會漏訊號。

### ema_breakout（REST 輪詢，strict2 凍結語義）
- **只用已收盤棒**：輪詢回來的 list 最後一根是形成中棒，只取 `candles[:-1]`；時戳前進（> `detector.last_ts`）
  的才是新收盤棒，逐棒餵 `Strict2Detector.feed_bar`。同一棒只發一次（以棒開盤時戳去重）。
- **strict2 語義（逐行對照 `mode_scan._signal_masks`，不得自創變體）**：
  `long = (o[-1] < e[-1]) and (c[-1] > e[-1]) and (low > e)`；`short` 鏡像（注意是**嚴格** `<`/`>`，非 `≤`）。
  gate = 訊號棒 index ≥ 4（`max(window_len=3, SL_LOOKBACK=5, 3)-1`）。
- **EMA**：遞迴 `ema = c*k + prev*(1-k)`, `k=2/(len+1)`，**seed 於第一根收盤價**（與 `decision_bars._ema_seeded`
  逐步一致）。啟動時 REST 分頁抓約一年歷史 seed（帶重試+退避；新上市品種自動拉到上市日）。
- **靜默突破**：本訊號棒振幅 `(high-low)/low` 對「該品種×該框架歷史訊號棒振幅分佈」的分位 `<` `quiet_pctile`
  → 靜默；歷史訊號 < `EMA_MIN_SIGNALS_FOR_QUIET`(30) → 顯示「資料不足」、不硬判。seed 時收集歷史分佈，之後
  每個新訊號滾動加入。
- **輪詢天然容忍安靜**：無新棒＝正常（不是故障、沒有 watchdog 誤判）；單一品種拉取失敗只重試、不影響他人。
- **改 strict2 前先跑對拍**：`Strict2Detector` / `strict2_scan` 需與 `mode_scan.detect_signals` 在同一 ccxt
  OHLCV 上逐筆一致（BTC 15m/1h 各近 60 天已驗 100%）。

### 共用
- **每則警報獨立狀態、互不干擾**；`ALERTS` 依 `(symbol,timeframe)` 分流（volume 一條 ws / 每組 ema 一條輪詢）。
- **log vs status**（在檔案上半部）：
  - `log()`＝事件（seed/換棒/觸發/重連/錯誤），獨立一行留在 scrollback。
  - `status()`＝每秒行情；TTY 用 `\r\033[2K` 原地刷新不洗版，非 TTY 節流成每 `STATUS_LOG_EVERY_SEC` 秒一行。
  - 改 log 時注意維持 `_status_active` 機制：事件印出前要先清掉原地刷新的狀態行。
- **優雅退出**：用 `signal.signal` 設 `STOP` 旗標讓迴圈自然收尾（不靠拋 `CancelledError`），這樣 `await exchange.close()` 能乾淨執行。
- **存活監控（healthchecks.io 死人開關）**：`HEALTHCHECK_URL`（.env，選填）。`heartbeat_loop()` 只在「最近有
  任一資料流仍在吐資料」（`_LAST_DATA_TS` 新鮮）時才 ping，所以心跳代表「真的在運作」而非僅「行程沒死」。
  預設清單含永遠活躍的 BTC 當基準，安靜的股票永續不會拉低整體心跳。
- **backward-compat**：`new_state()` / `seed_baseline()` / `handle_candles()` 與模組全域 `SYMBOL/TIMEFRAME/
  VOL_MULT/RANGE_THRESHOLD/COOLDOWN_SEC` 保留，讓舊測試/呼叫仍可用；live bot 走 `ALERTS`。

## 已實測確認的 ccxt 事實（4.5.x，不要憑記憶推翻）

- `import ccxt.pro as ccxtpro`；`ccxtpro.bybit()` 的 `watch_ohlcv` / `fetch_ohlcv` 都是 coroutine。
- 兩者回傳格式都是 `[[timestamp_ms, open, high, low, close, volume], ...]`，**volume 是基礎幣（BTC）量**，全程一致使用。
- `watch_ohlcv(SYMBOL, '15m')` 回傳的 list，**最後一個元素是「正在形成中」的那根**，volume 逐秒遞增。
- `BTC/USDT:USDT` 確為 linear / swap 市場（`ex.market(sym)['linear'] is True`）。

## 怎麼跑 / 怎麼驗證

```bash
source venv/bin/activate
python alert.py            # 真實監看（會發啟動測試訊息到 Telegram）
python test_alert.py       # 不必等真盤，驗證觸發/冷卻/不誤觸
```

驗證程式碼改動但**不想真的發 Telegram / 不想連真盤**時的技巧：
- 用 env 蓋掉憑證跑短程，會走「未設定」分支、不發訊：
  `TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= timeout --signal=SIGTERM 10 python alert.py 2>&1 | head -20`
- 純驗證 `handle_candles` 邏輯：import `alert`，自己組 `[ts,o,h,l,c,v]` 餵進去（見 `test_alert.py`）。
- 連真盤確認 API 行為時，記得 `await exchange.close()`，否則會有 unclosed connector 警告。

## 改東西時的注意

- 改門檻/商品/週期：動設定區常數即可。
- 換商品要先確認新 symbol 在 `ex.load_markets()` 裡存在、型別正確（別憑記憶假設 symbol 寫法）。
- 任何宣稱「修好斷線/重連」的改動，請實際模擬斷線（關網路再恢復）或用 watchdog 路徑驗證，別只看程式碼。
