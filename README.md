# BTC 爆量 + 振幅 即時警報

監看 Bybit 的 **BTC USDT 本位線性永續合約**，當「**爆量**」且「**振幅夠大**」同時成立時，
立刻發 Telegram 訊息通知你。只讀公開行情、**不下單**，所以不需要任何 Bybit API key。

- 單一檔案 `alert.py`，`python alert.py` 直接跑
- 行情串流用 `ccxt.pro`（內含在 `ccxt` 套件裡）
- 通知用 Telegram Bot API（標準庫 `urllib`，不需 `requests`）
- 狀態全放記憶體，沒有資料庫、沒有 Docker。程式死掉重啟就好（內建斷線自癒）

---

## 訊號規格

| 項目 | 值 |
|------|----|
| 商品 | `BTC/USDT:USDT`（Bybit 線性永續） |
| 時間框架 | 15 分鐘 K 棒 |
| 觸發條件①（爆量） | 形成中那根 15m 棒的累積量 ≥ 上一根已收完棒完整量的 **2 倍** |
| 觸發條件②（振幅） | `(high − low) / low` ≥ **0.5%** |
| 觸發時機 | **兩條件同時成立就發，不等收盤**（棒內量與振幅都是單調遞增，棒中判斷合理） |
| 防洗版 | 觸發後 **5 分鐘冷卻**，期間不重發 |

門檻都能在 `alert.py` 最上方的「設定區」改。

---

## 安裝（Ubuntu）

```bash
# 0) 取得程式
git clone git@github.com:november295536/btc-price-alert-bot.git
cd btc-price-alert-bot

# 1) 安裝 Python venv 工具（Ubuntu 預設沒裝 venv，要補這個）
sudo apt update && sudo apt install -y python3-venv

# 2) 建立並啟用虛擬環境
python3 -m venv venv
source venv/bin/activate

# 3) 安裝依賴（鎖定版本，到哪裝出來都一致）
pip install -r requirements.txt

# 4) 設定 Telegram：在專案根目錄建一個 .env（兩行，見下方「怎麼拿 token / chat id」）
#    .env 不會進版控（已被 .gitignore 排除），要自己建：
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=你的token
TELEGRAM_CHAT_ID=你的chatid
EOF
chmod 600 .env        # 鎖權限，只有自己讀得到
```

---

## 啟動

```bash
cd btc-price-alert-bot
source venv/bin/activate     # 每次新開終端機都要先啟用 venv
python alert.py
```

啟動後**手機會立刻收到一則「✅ 警報系統已啟動」測試訊息**，確認 token/chat id 設對了。
之後就會持續監看，`Ctrl+C` 優雅停止。`.env` 不用再動。

> 想讓它在 Ubuntu 上開機自啟、崩潰自重啟、SSH 斷線也照跑 → 見下方「在 Ubuntu/VPS 上長期跑（systemd）」。

---

## 終端機會看到什麼

- **互動式終端機**：底部一行行情狀態「原地刷新」（每秒更新但**不洗版**），像這樣：
  ```
  [13:59:00] K[13:45] price=61591.1 vol=407.012 baseline=536.166 vol_ratio=0.76x range_pct=0.278%
  ```
- **重要事件**才會印成獨立一行留著：🌱 seed、🕒 換棒、🚨 觸發、🔄 重連、⚠️ 斷線、🐶 watchdog。
- **輸出導到檔案時**（`nohup` / `> log.txt`）：自動改成每 60 秒印一行（可在設定區 `STATUS_LOG_EVERY_SEC` 調）。

欄位意義：`vol_ratio` ≥ 2.0 算爆量；`range_pct` ≥ 0.5% 算振幅夠；兩者同時成立 → 🚨 + Telegram。

---

## 測試「會不會跳通知」

不用等真盤爆量，直接跑測試程式，它會用假資料走 `alert.py` **真正的**判斷+發送路徑：

```bash
source venv/bin/activate
python test_alert.py
```

預期：手機收到 **1 則 🚨 警報**（第 1 筆）；第 2 筆被冷卻擋下、第 3 筆因振幅不足不發。

---

## 怎麼拿 Telegram bot token / chat id

**Bot token：**
1. Telegram 搜 `@BotFather` → `/newbot` → 取名字、取 username
2. 它回一串 `123456789:AAE...`，就是 token

**Chat id：**
1. 先**對你的 bot 傳一句話**（例如 `hi`），不然拿不到
2. 瀏覽器開 `https://api.telegram.org/bot<你的TOKEN>/getUpdates`
3. 找 `"chat":{"id":...}`，那個數字就是 chat id（個人是正數；群組是負數，連 `-` 一起填）

---

## 斷線韌性（重點）

筆電睡眠/休眠、網路抖動都會斷 websocket。這支靠以下機制自癒，**不用手動重啟**：

- `ccxt.pro` 的 `watch_ohlcv` 內建自動重連 + keepalive（每 20s ping）
- **每次（重）連線都用 REST 重新 seed baseline**，不信記憶體裡可能過期的值（斷線若跨過棒收盤，舊 baseline 會算歪）
- watch 迴圈用 try/except 包住，斷線睡 2 秒再重連，不會弄死程式
- **Watchdog**：用 `asyncio.wait_for(watch_ohlcv, timeout=60)` 包住——超過 60 秒沒收到任何更新就判定為「殭屍連線」（連線看似還在、其實不吐資料），主動重連並重新 seed

驗證自癒：跑起來後把 Wi-Fi 關約 1 分鐘再開，會看到 ⚠️ 斷線 → 🔄 重連 → 🌱 重新 seed，不崩潰、不用過期 baseline。

---

## 在 Ubuntu/VPS 上長期跑（systemd）

`Ctrl+C` 或關終端機程式就會停，長期跑請用 systemd（開機自啟、崩潰自動重啟、SSH 斷線照跑）。
專案內附 `btc-alert.service` 範本，把裡面的 `User` 和三處路徑改成你的實際值，然後：

```bash
sudo cp btc-alert.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btc-alert     # 開機自啟 + 立刻啟動

journalctl -u btc-alert -f                # 即時看 log
sudo systemctl restart btc-alert          # 改設定後重啟
sudo systemctl status btc-alert           # 看運行狀態
```

systemd 跑時輸出不是終端機 → 程式自動切成節流模式（每 60 秒寫一行進 journald，不洗爆硬碟）。

> ⚠️ 機房地區：Bybit 對部分地區（如美國）封鎖、Telegram 在少數國家被擋。建議選日本/新加坡/香港等機房，開好後先手動 `python alert.py` 確認收得到啟動訊息。

---

## 檔案說明

| 檔案 | 用途 |
|------|------|
| `alert.py` | 主程式（設定區在最上方） |
| `test_alert.py` | 測試程式：餵假資料驗證觸發/冷卻/發送 |
| `requirements.txt` | 依賴清單（鎖定 ccxt 版本）；`pip install -r requirements.txt` |
| `btc-alert.service` | VPS 用的 systemd 服務檔範本（開機自啟、崩潰自重啟） |
| `.env` | Telegram token 與 chat id（**不要 commit**，已被 `.gitignore` 排除） |
| `.gitignore` | 排除 `.env` / `venv/` |
| `venv/` | 虛擬環境 |

---

## 設定區（`alert.py` 最上方）

| 變數 | 預設 | 意義 |
|------|------|------|
| `SYMBOL` | `BTC/USDT:USDT` | 監看商品 |
| `TIMEFRAME` | `15m` | K 棒週期 |
| `VOL_MULT` | `2.0` | 爆量倍數門檻 |
| `RANGE_THRESHOLD` | `0.005` | 振幅門檻（0.5%） |
| `COOLDOWN_SEC` | `300` | 冷卻秒數（5 分鐘） |
| `WATCHDOG_TIMEOUT` | `60` | 多久沒資料就判定殭屍連線重連 |
| `RECONNECT_DELAY` | `2` | 重連前等待秒數 |
| `STATUS_LOG_EVERY_SEC` | `60` | 非 TTY（導到檔案）時狀態行印出間隔 |

Telegram 的 token / chat id 從 `.env` 讀（環境變數可覆蓋）。

---

## 疑難排解

- **沒收到啟動訊息**：token/chat id 填錯，或你還沒先對 bot 傳過話。重看「怎麼拿 chat id」。
- **`getUpdates` 一直空的**：通常是訊息傳到別隻同名 bot；確認 bot username 完全一致。
- **一直重連**：檢查網路；Bybit 偶發斷線是正常的，程式會自己接回來。
- **想背景長跑不佔終端機**：用上面的 systemd（推薦）；臨時測試也可 `nohup python alert.py > alert.log 2>&1 &`，再 `tail -f alert.log`。

> ⚠️ 安全：`.env` 裡的 bot token 等於這隻 bot 的密碼。別把 `.env` 上傳或外流；要換用 `@BotFather` 的 `/revoke` 重生一組再更新 `.env`。
