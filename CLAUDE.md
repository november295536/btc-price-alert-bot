# CLAUDE.md

給未來在這個 repo 工作的 Claude Code 的指引。先讀這份，再動手。

## 這是什麼

單一檔案的 Python 即時警報程式：監看 Bybit `BTC/USDT:USDT`（USDT 本位線性永續）的 15m K 棒，
當「**爆量**（形成中棒累積量 ≥ 上一根已收完棒的 2 倍）」且「**振幅**（`(high-low)/low` ≥ 0.5%）」
**同時成立**時，發 Telegram 通知。只讀公開行情、不下單，**不需要 Bybit API key**。

使用者（peter）的目的：在自己筆電的 terminal 直接跑，盯 BTC 短線爆量。

## 設計原則（請嚴格遵守，使用者明確要求過）

- **不要過度設計**。維持單一檔案 `alert.py`、`python alert.py` 就能跑。
- **依賴越少越好**：目前只有 `ccxt`（含 `ccxt.pro`），鎖在 `requirements.txt`（`pip install -r requirements.txt`）。Telegram 用標準庫 `urllib`，**不要**改用 `requests` 或加 `python-dotenv`。ccxt 版本鎖死是故意的（它常改、緊跟交易所 API），更新要手動且更新後重跑 `test_alert.py`。
- **不要**加資料庫、Docker、framework、持久化。狀態放記憶體，死掉重啟就好。
- 不要把 `.env` commit 出去（已被 `.gitignore` 排除）。token 是機密。

## 檔案

| 檔案 | 說明 |
|------|------|
| `alert.py` | 主程式。設定區在最上方；下方依序是 `_load_env` → log/status → telegram → state → `seed_baseline` → `handle_candles` → `run_forever` → `main` |
| `test_alert.py` | 餵假 K 棒進 `alert.handle_candles` 的真實路徑，驗證觸發/冷卻/不誤觸 |
| `.env` | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（不進版控） |
| `README.md` | 給使用者的操作說明 |

## 關鍵設計（改動前務必理解，否則容易改壞）

- **baseline = 上一根已收完棒的完整量**，是爆量判斷的分母。
- **seed**：啟動與**每次重連**都用 REST `fetch_ohlcv(SYMBOL, '15m', limit=2)` 重新 seed，
  `candles[-2]` 是上一根已收完、`candles[-1]` 是形成中那根。**不可**信記憶體舊值——斷線若跨過棒收盤會算歪。
- **換棒偵測**：`watch_ohlcv` 回傳 list，最後一根的 timestamp 變了就代表換棒；
  此時 baseline 更新成剛收完那根「最後觀測到的量」（`state["cur_vol"]`）。
- **Watchdog**：用 `asyncio.wait_for(exchange.watch_ohlcv(...), timeout=WATCHDOG_TIMEOUT)` 實作。
  超時＝「連線看似還在、其實不吐資料」的殭屍狀態 → 跳出重連並重新 seed。**不要**自己手刻 websocket，
  `ccxt.pro` 的 `watch_*` 已內建重連 + keepalive。
- **冷卻**：用 `time.monotonic()`（不受系統時鐘調整影響），觸發後 `COOLDOWN_SEC` 內不重發。
- **每次更新照算、照判斷觸發**；只有「印出來」被節流，不會漏訊號。
- **log vs status**（在檔案上半部）：
  - `log()`＝事件（seed/換棒/觸發/重連/錯誤），獨立一行留在 scrollback。
  - `status()`＝每秒行情；TTY 用 `\r\033[2K` 原地刷新不洗版，非 TTY 節流成每 `STATUS_LOG_EVERY_SEC` 秒一行。
  - 改 log 時注意維持 `_status_active` 機制：事件印出前要先清掉原地刷新的狀態行。
- **優雅退出**：用 `signal.signal` 設 `STOP` 旗標讓迴圈自然收尾（不靠拋 `CancelledError`），這樣 `await exchange.close()` 能乾淨執行。
- **存活監控（healthchecks.io 死人開關）**：`HEALTHCHECK_URL`（.env，選填）。`healthcheck_ping()` 跑在 executor、失敗只記 log 不影響主程式。**ping 刻意放在「成功收到行情並 handle 完」之後**（每 `HEARTBEAT_EVERY_SEC` 節流），所以心跳代表「真的在運作」而非僅「行程沒死」。改動時別把 ping 移到資料接收前，否則殭屍/斷線狀態也會誤報為健康。

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
