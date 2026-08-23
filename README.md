# 多警報 即時監看（放量突破 + EMA20 突破）

監看 Bybit 的 **USDT 本位線性永續合約**，用一份**設定驅動的警報清單**（`alert.py` 最上方的
`ALERTS`）同時跑多種、多品種、多週期的警報，命中就立刻發 Telegram 通知。只讀公開行情、
**不下單**，所以不需要任何 Bybit API key。

目前內建兩種警報：

- **放量突破（volume_spike）**：某商品某週期「形成中」K 棒**爆量**（形成中量 ≥ 上一根已收完棒的 N 倍）
  且**漲跌幅**（`|close-open|/open` ≥ 門檻，即現價相對本棒開盤的實際變動）同時成立時，
  **棒內即發**（不等收盤）；**同一根 K 棒只發一次**。
- **EMA20 突破（ema_breakout・兩根K嚴格版 strict2）**：只用**已收盤**K 棒判定——前一棒實體上穿 EMA
  且本棒整根（含影線）站上 EMA → 多；空方鏡像。附「靜默突破」判定。預設監看 BTC、ETH、HYPE、
  TSLA、SPCX、SKHYNIX、MU、SNDK 的 15m 與 1h。

- 單一檔案 `alert.py`，`python alert.py` 直接跑
- 行情：volume_spike 走 `ccxt.pro` 串流（棒內即時）；ema_breakout 走 REST 輪詢（只吃已收盤棒，
  天然容忍股票永續的安靜時段——無新棒＝正常，不是故障）
- 通知用 Telegram Bot API（標準庫 `urllib`，不需 `requests`）
- 每則警報獨立狀態（冷卻 / K 棒追蹤 / warmup / EMA / 振幅分佈），互不干擾；單一品種斷流/停盤
  不影響其他警報
- 狀態全放記憶體，沒有資料庫、沒有 Docker。程式死掉重啟就好（內建斷線自癒）
- 已發警報會追加一行到 `logs/alerts.jsonl`（gitignored；日誌失敗不影響發送）

---

## 訊號規格

### 放量突破（volume_spike）

| 項目 | 值 |
|------|----|
| 商品 | `BTC/USDT:USDT`（Bybit 線性永續） |
| 時間框架 | 15 分鐘 K 棒 |
| 觸發條件①（爆量） | 形成中那根棒的累積量 ≥ 上一根已收完棒完整量的 **2 倍** |
| 觸發條件②（漲跌幅） | `\|close − open\| / open` ≥ **0.5%**（現價相對本棒開盤的實際變動，非 high-low 振幅——上下掃一圈又收回開盤價的棒不算） |
| 觸發時機 | **兩條件同時成立就發，不等收盤** |
| 防洗版 | **同一根 K 棒只發一次**（以棒開盤時戳去重）；另有觸發後 **5 分鐘冷卻** 擋跨棒連發 |

### EMA20 突破（ema_breakout・strict2）

| 項目 | 值 |
|------|----|
| 商品 | BTC / ETH / HYPE / TSLA / SPCX / SKHYNIX / MU / SNDK 的 `X/USDT:USDT` |
| 時間框架 | 15m 與 1h（各一則） |
| EMA | 20（遞迴，seed 於啟動時 REST 拉的歷史，盡量拉滿一年、分頁抓；新上市品種拉到上市日） |
| 多方條件 | 前一棒實體上穿 EMA（`open[-1] < ema[-1]` 且 `close[-1] > ema[-1]`）且本棒整根含影線在 EMA 上（`low > ema`） |
| 空方條件 | 鏡像：`open[-1] > ema[-1]` 且 `close[-1] < ema[-1]` 且 `high < ema` |
| 觸發時機 | **只用已收盤棒**，每棒收盤判定一次；同一棒只發一次（以棒開盤時戳去重） |
| 靜默突破 | 本訊號棒振幅在「該品種×該框架歷史訊號棒振幅分佈」的分位 < 20 → 靜默（歷史訊號 < 30 顯示「資料不足」） |

> strict2 語義逐行對照 quantitive-trading 專案 `ema_breakout/ml/mode_scan.py` 的 `_signal_masks`
> strict2 分支（ground truth）；偵測器已與其在真實 Bybit kline 上對拍 **100% 一致**。

門檻與清單都在 `alert.py` 最上方的「設定區」（`ALERTS` 與各常數）改。

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
# 選填：存活監控（見下方「怎麼知道程式還活著」），不需要可整行刪掉
HEALTHCHECK_URL=https://hc-ping.com/你的-uuid
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
  [13:59:00] K[13:45] price=61591.1 vol=407.012 baseline=536.166 vol_ratio=0.76x move_pct=+0.278%
  ```
- **重要事件**才會印成獨立一行留著：🌱 seed、🕒 換棒、🚨 觸發、🔄 重連、⚠️ 斷線、🐶 watchdog。
- **輸出導到檔案時**（`nohup` / `> log.txt`）：自動改成每 60 秒印一行（可在設定區 `STATUS_LOG_EVERY_SEC` 調）。

欄位意義：`vol_ratio` ≥ 2.0 算爆量；`move_pct`（現價相對本棒開盤的漲跌幅）絕對值 ≥ 0.5% 算動得夠；
兩者同時成立 → 🚨 + Telegram（同一根 K 棒只發一次）。

---

## 測試

直接跑測試程式，它會用假資料走 `alert.py` **真正的**判斷/發送路徑：

```bash
source venv/bin/activate
python test_alert.py
```

涵蓋：

- **volume_spike**：兩條件成立 → 發、冷卻不重發、**同一根 K 棒只發一次**（冷卻過了也不重發）、
  漲跌幅不足不觸發——包含「爆量、high-low 振幅大、但收回開盤價」的假突破，且訊息文字逐行鎖定。
- **strict2 EMA 突破**：多 / 空 / 影線違反不觸發 / 串流==批次一致。
- **靜默突破分位**：`percentile_rank` 與「是 / 否 / 資料不足」三態。
- **ema_breakout 訊息格式**：使用者逐字核准的格式（多 / 空 / 靜默 / 資料不足）。

純邏輯測試不需 `.env`、不連網、一定會跑並斷言；最後一段若 `.env` 有設 token/chat id，會對
Telegram 真的發一則 🚨 warning 讓你確認手機跳通知，沒設就自動略過（測試仍全綠）。

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

## 怎麼知道程式還活著（存活監控 / 死人開關）

危險盲點：這支「沒爆量就不發訊息」，所以它**默默死掉**（被雲端回收、當機、整台 VPS 掛）時，
你不會注意到——沉默看起來跟「行情平靜」一模一樣。解法是用 **healthchecks.io** 當死人開關：
程式**主動往外** ping，一旦心跳停了，healthchecks.io 會**主動通知你**。

設定：
1. 到 [healthchecks.io](https://healthchecks.io) 免費註冊，建一個 check，拿到 ping URL（形如 `https://hc-ping.com/<uuid>`）。
2. 把它填進 `.env` 的 `HEALTHCHECK_URL=`。
3. 在該 check 設定「Period」略大於 `HEARTBEAT_EVERY_SEC`、Grace 給寬一點（如 10 分鐘），避免短暫斷線誤報。
4. 重啟程式。啟動 log 會顯示「存活監控：已啟用」。

運作方式：程式**在成功收到行情後**才 ping（每 `HEARTBEAT_EVERY_SEC` 秒一次），所以心跳代表
「真的有在運作」，而不只是「行程沒死」。被回收/當機/網路死 → 心跳停 → healthchecks.io 通知你。
沒填 `HEALTHCHECK_URL` 就完全停用此功能、不影響其他運作。

> 為什麼推送式（程式往外 ping）而非拉取式（監控戳進來）：這支只對外連線、不開任何 inbound port，
> 推送式不用動防火牆，且能驗證「程式真的在跑」。

---

## 在 Ubuntu/VPS 上長期跑（systemd）

`Ctrl+C` 或關終端機程式就會停，長期跑請用 systemd（開機自啟、崩潰自動重啟、SSH 斷線照跑）。

### 方法 A：一鍵腳本（推薦）

clone 下來後，用**一般帳號**（不要 sudo 整支）執行：

```bash
chmod +x deploy.sh
./deploy.sh
```

它會自動：裝 `python3-venv` → 建 venv、裝依賴 → 檢查 `.env`（沒填會提示你填好再重跑）→
產生並安裝 systemd 服務（路徑/帳號自動填好）→ 啟用並啟動。可重複執行（更新後再跑一次即可）。

### 方法 B：手動

用專案內的 `btc-alert.service` 範本，把 `User` 和三處路徑改成你的實際值，然後：

```bash
sudo cp btc-alert.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btc-alert
```

### 常用指令

```bash
journalctl -u btc-alert -f                # 即時看 log
sudo systemctl restart btc-alert          # 改設定後重啟
sudo systemctl status btc-alert           # 看運行狀態
sudo systemctl stop btc-alert             # 停止
```

systemd 跑時輸出不是終端機 → 程式自動切成節流模式（每 60 秒寫一行進 journald，不洗爆硬碟）。

> ⚠️ 機房地區：Bybit 對部分地區（如美國）封鎖、Telegram 在少數國家被擋。建議選日本/新加坡/香港等機房，開好後先手動 `python alert.py` 確認收得到啟動訊息。

### 更新既有部署（改完程式後）

在本機 commit + push 到 GitHub 後，SSH 進 server：

```bash
cd ~/btc-price-alert-bot          # 你 clone 的專案目錄（路徑不同就改掉）
git pull                          # 拉最新程式
sudo systemctl restart btc-alert  # 重啟套用新版
journalctl -u btc-alert -f        # 確認起來了（會看到啟動訊息與行情）
```

重點：

- **只改程式（沒動 `requirements.txt`）→ `git pull` + `restart` 就夠**，不必重跑 `deploy.sh`。
- **改了依賴**（`requirements.txt`）→ 改跑 `./deploy.sh`，它會順便更新 venv 依賴、重產服務檔再重啟。
- **`.env` 不受影響**：它沒進版控，`git pull` 不會覆蓋 server 上的 token。
- server 重啟後會**發一則「✅ 已啟動」測試訊息**到手機，收到就代表新版跑起來了。
- 萬一 `git pull` 因 server 上有雜散改動卡住：`git stash` 或 `git checkout -- .` 清掉再 pull。

---

## VPS 安全加固（開公網 IP 的機器建議做）

核心觀念：這支只對外連線、**不需要任何 inbound，唯一要開的門就是 SSH(22)**。
所以「安全」＝把那扇門縮到最小、鎖好；別再開其他 port。

### 1. 鎖 SSH 來源 IP（最重要，OCI Security List）

先在機器上看自己家的公網 IP：

```bash
echo $SSH_CONNECTION | awk '{print $1}'
```

然後 Console → VCN → 該 subnet 的 **Security List → Ingress Rules**，把 22 那條的
`Source` 從 `0.0.0.0/0` 改成 `你家IP/32`。其餘 inbound 全刪（OCI 預設沒允許就拒絕）。

| 做法 | 安全性 | 風險 |
|------|--------|------|
| 22 只開給你家 IP `/32` | 最高 | 家裡 IP 浮動時可能把自己鎖在外（可用 OCI Console / Cloud Shell 改回來）|
| 22 對全世界開、只靠金鑰登入 | 夠用 | 幾乎沒有（金鑰登入暴力破解無解），只是 log 有掃描噪音 |

> IP 固定 → 鎖 `/32`；IP 會變 → 維持開放但務必只用金鑰登入 + 裝 fail2ban。

### 2. SSH 只准金鑰、禁密碼與 root

```bash
# 確認設定（看到 PasswordAuthentication no 就對了）
sudo grep -Ei 'passwordauth|permitrootlogin' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/* 2>/dev/null
# 若是 yes 才需改成 no，然後：
sudo systemctl restart ssh
```

### 3. fail2ban（自動封鎖狂試登入的 IP）

```bash
sudo apt update && sudo apt install -y fail2ban
sudo systemctl enable --now fail2ban
```

### 4. 自動安全更新

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades   # 問你時選 Yes
```

### 5. 其他

- **不要為這支 bot 開任何其他 inbound port**（我們用 healthchecks.io 推送式心跳就是為了不開 port）。
- bot 用**非 root 帳號**跑（`deploy.sh` 已處理）、`.env` 設 `chmod 600`（已處理）。
- ⭐ **去 Oracle 帳號開 MFA**：Console 帳號被盜的話什麼防火牆都繞過，比 VM 本身更該保護。

> 最小組合：①（IP 固定就）鎖 22 給你家 IP ② 確認金鑰登入 + fail2ban ③ Oracle 帳號開 MFA。

---

## 檔案說明

| 檔案 | 用途 |
|------|------|
| `alert.py` | 主程式（設定區在最上方） |
| `test_alert.py` | 測試程式：餵假資料驗證觸發/冷卻/發送 |
| `requirements.txt` | 依賴清單（鎖定 ccxt 版本）；`pip install -r requirements.txt` |
| `deploy.sh` | Ubuntu 一鍵部署腳本（裝環境 + 起 systemd 服務） |
| `btc-alert.service` | systemd 服務檔範本（手動部署用；`deploy.sh` 會自動產生對應版本） |
| `.env` | Telegram token 與 chat id（**不要 commit**，已被 `.gitignore` 排除） |
| `.gitignore` | 排除 `.env` / `venv/` / `logs/` |
| `logs/alerts.jsonl` | 已發警報的 JSONL 日誌（runtime，gitignored） |
| `venv/` | 虛擬環境 |

---

## 設定區（`alert.py` 最上方）

### 警報清單 `ALERTS`

一份 list，每個 dict 是一則警報。`type` 決定偵測器，其餘欄位是該警報的參數：

```python
ALERTS = [
    {"type": "volume_spike", "symbol": "BTC/USDT:USDT", "timeframe": "15m",
     "vol_mult": 2.0, "move_pct": 0.5, "cooldown_sec": 300},
    {"type": "ema_breakout", "symbol": "BTC/USDT:USDT", "timeframe": "1h",
     "ema_len": 20, "quiet_pctile": 20},
    # ... 其餘品種 × {15m, 1h}
]
```

- `volume_spike`：`vol_mult`（爆量倍數）、`move_pct`（漲跌幅門檻 `|close-open|/open`，**百分比**，
  0.5 = 0.5%；舊 key 名 `range_pct` 仍接受）、`cooldown_sec`。
- `ema_breakout`：`ema_len`（預設 20）、`quiet_pctile`（靜默突破分位門檻，預設 20）。
- 同一 `(symbol, timeframe)` 的多則警報會共用一條資料流；每則警報狀態獨立、互不干擾。
- 啟動時會 `load_markets` 驗證清單裡每個 symbol 可解析，並列出解析失敗者。
- `SKHY/USDT:USDT` 與 `SKHYNIX/USDT:USDT` 疑似重複（兩者在 Bybit 皆可解析），預設把 `SKHY` 那兩則
  **註解掉**，留給你確認。

### 全域常數

| 變數 | 預設 | 意義 |
|------|------|------|
| `SYMBOL` / `TIMEFRAME` / `VOL_MULT` / `RANGE_THRESHOLD` / `COOLDOWN_SEC` | BTC/15m/2.0/0.005/300 | 舊版單一 volume 警報的相容預設（`handle_candles` 用；不影響 `ALERTS`） |
| `EMA_LEN_DEFAULT` | `20` | EMA 週期預設 |
| `EMA_QUIET_PCTILE_DEFAULT` | `20` | 靜默突破分位門檻（振幅分位 < 此值 → 靜默） |
| `EMA_MIN_SIGNALS_FOR_QUIET` | `30` | 歷史訊號 < 此數 → 顯示「資料不足」、不硬判 |
| `EMA_SEED_DAYS` | `365` | 啟動時 seed 歷史天數（分頁抓；新上市品種自動拉到上市日） |
| `EMA_POLL_SEC` | `15` | ema_breakout 輪詢間隔（已收棒偵測延遲 ≤ 此值） |
| `WATCHDOG_TIMEOUT` | `60` | volume 串流多久沒資料就 REST 探測是否殭屍（安靜市場會被容忍、不誤判） |
| `RECONNECT_DELAY` | `2` | 重連前等待秒數 |
| `STATUS_LOG_EVERY_SEC` | `60` | 非 TTY（導到檔案）時狀態行印出間隔 |
| `HEARTBEAT_EVERY_SEC` | `300` | 每隔多久對 healthchecks.io ping 一次（需最近有任一資料流仍在吐資料） |

Telegram 的 token / chat id、healthchecks.io 的 `HEALTHCHECK_URL` 都從 `.env` 讀（環境變數可覆蓋）。

---

## 疑難排解

- **沒收到啟動訊息**：token/chat id 填錯，或你還沒先對 bot 傳過話。重看「怎麼拿 chat id」。
- **`getUpdates` 一直空的**：通常是訊息傳到別隻同名 bot；確認 bot username 完全一致。
- **一直重連**：檢查網路；Bybit 偶發斷線是正常的，程式會自己接回來。
- **想背景長跑不佔終端機**：用上面的 systemd（推薦）；臨時測試也可 `nohup python alert.py > alert.log 2>&1 &`，再 `tail -f alert.log`。

> ⚠️ 安全：`.env` 裡的 bot token 等於這隻 bot 的密碼。別把 `.env` 上傳或外流；要換用 `@BotFather` 的 `/revoke` 重生一組再更新 `.env`。
