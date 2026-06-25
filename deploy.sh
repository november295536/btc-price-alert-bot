#!/usr/bin/env bash
#
# 一鍵部署到 Ubuntu：裝 venv 工具 + 建虛擬環境 + 裝依賴 + 產生並啟用 systemd 服務。
# 自動把服務檔裡的「執行帳號」和「路徑」填成目前環境，不用手改。
#
# 用法（在 clone 下來的專案目錄裡，用『一般帳號』執行，不要 sudo 整支）：
#   chmod +x deploy.sh
#   ./deploy.sh
#
# 重跑安全：可重複執行（更新依賴、重產服務檔、重啟服務）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- 決定執行身分與是否需要 sudo ---
if [ "$(id -u)" -eq 0 ]; then
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    echo "✗ 請不要用 sudo 執行整支腳本。"
    echo "  改用一般帳號直接跑 ./deploy.sh，需要 root 的步驟腳本會自己呼叫 sudo。"
    echo "  （用 sudo 跑整支會讓 venv/.env 變成 root 擁有，服務帳號讀不到。）"
    exit 1
  fi
  SUDO=""            # 已是 root，特權指令直接執行
  RUN_USER="root"
else
  SUDO="sudo"
  RUN_USER="$USER"
fi
echo "服務將以帳號 [$RUN_USER] 執行，工作目錄 [$SCRIPT_DIR]"

# --- 1) 安裝 python3-venv（Ubuntu 預設沒有）---
echo "==> 1/5 安裝 python3-venv（需要 sudo）"
$SUDO apt-get update
$SUDO apt-get install -y python3-venv

# --- 2) 建 venv + 裝依賴 ---
echo "==> 2/5 建立 venv 並安裝依賴"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --upgrade pip >/dev/null
./venv/bin/pip install -r requirements.txt

# --- 3) 檢查 .env（沒有就建空白範本並中止，請填好再重跑）---
echo "==> 3/5 檢查 .env"
if [ ! -f .env ]; then
  printf 'TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n# 選填：healthchecks.io 存活監控 ping URL（不需要可留空）\nHEALTHCHECK_URL=\n' > .env
  chmod 600 .env
  echo "✗ 已建立空白 .env，請填入 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 後重跑 ./deploy.sh"
  echo "  （HEALTHCHECK_URL 為選填，要存活監控再填）"
  exit 1
fi
chmod 600 .env
if ! grep -Eq '^TELEGRAM_BOT_TOKEN=.+' .env || ! grep -Eq '^TELEGRAM_CHAT_ID=.+' .env; then
  echo "✗ .env 裡的 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 還沒填，填好再重跑 ./deploy.sh"
  exit 1
fi

# --- 4) 產生並安裝 systemd 服務（路徑/帳號自動填）---
echo "==> 4/5 產生並安裝 systemd 服務 btc-alert.service（需要 sudo）"
$SUDO tee /etc/systemd/system/btc-alert.service >/dev/null <<EOF
[Unit]
Description=BTC volume-spike + range alert (Bybit perp -> Telegram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/venv/bin/python $SCRIPT_DIR/alert.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# --- 5) 啟用並啟動 ---
echo "==> 5/5 啟用並啟動服務"
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now btc-alert
$SUDO systemctl restart btc-alert    # 重跑此腳本時確保載入最新程式/設定

echo
echo "✅ 完成，服務已啟動。常用指令："
echo "  journalctl -u btc-alert -f        # 即時看 log（應很快看到啟動訊息與行情）"
echo "  sudo systemctl status btc-alert   # 看運行狀態"
echo "  sudo systemctl restart btc-alert  # 改門檻/設定後重啟"
echo "  sudo systemctl stop btc-alert     # 停止"
