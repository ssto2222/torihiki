import MetaTrader5 as mt5
import time
import os
import sys
import subprocess
import psutil
import logging
import requests
from secret import DISCORD_WEBHOOK_URL

# --- 設定 ---
MAIN_SCRIPT = "mt5_ea_bridge.py"
LOG_DIR = r"G:\マイドライブ\mt5_log" # 環境に合わせて修正
LOG_FILE = os.path.join(LOG_DIR, "mt5_monitor.log")
FLAG_FILE = os.path.join(LOG_DIR, "paused.flag")

# ここで引数を自由にカスタマイズ
COMMAND_TO_RUN = [
    sys.executable,                          # Python実行ファイルのパス
    os.path.join(os.path.dirname(__file__), MAIN_SCRIPT), # スクリプト名
    "--mode", "scalp",                       # ★ここでモード指定
    "--symbol", "BTCUSD",                    # ★ここで通貨ペア指定
    # 必要に応じて追加可能
    # "--lot", "0.1",
    # "--magic", "123456"
]

# ログの設定（Googleドライブに保存）
logging.basicConfig(
    level=logging.INFO,
    filename=LOG_FILE,
    filemode='a',
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)

def send_discord(message):
    requests.post(DISCORD_WEBHOOK_URL, json={"content": message})

def restart_all():
    logging.warning("異常検知。再起動実行。")
    send_discord("⚠️ **【警告】** MT5の異常を検知したため、システムを強制再起動しました。")
    subprocess.run(['taskkill', '/F', '/IM', 'terminal64.exe'], capture_output=True)
    time.sleep(5)
    
    script_path = os.path.join(os.path.dirname(__file__), MAIN_SCRIPT)
    if mt5.initialize():
        subprocess.Popen(COMMAND_TO_RUN, creationflags=subprocess.CREATE_NEW_CONSOLE)
    mt5.shutdown()

if __name__ == "__main__":
    if os.path.exists(FLAG_FILE):
        sys.exit()

    # メインが動いているかチェック
    is_running = any(MAIN_SCRIPT in " ".join(p.info['cmdline'] or []) 
                     for p in psutil.process_iter(['cmdline']))

    if not is_running:
        restart_all()
    elif mt5.initialize():
        if not mt5.terminal_info().connected:
            restart_all()
        else:
            logging.info("監視：すべて正常です。")
        mt5.shutdown()