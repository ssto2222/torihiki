"""
mt5_monitor.py — MT5 EA ブリッジ 監視・ウォッチドッグ

使い方:
    # ウォッチドッグモード（推奨）: ブリッジを起動・監視し、異常終了時に自動再起動
    python mt5_monitor.py --watch
    python mt5_monitor.py --watch --mode scalp --symbol BTCUSD

    # ヘルスチェックモード（後方互換）: 1回だけ起動確認して終了
    python mt5_monitor.py

--watch モードの動作:
    1. mt5_ea_bridge.py を subprocess として起動する
    2. プロセスが終了コード 0（正常終了 / Ctrl+C）で終わった場合 → 再起動しない
    3. 終了コード != 0（データ取得 10 回連続失敗など）→ MT5 端末を再起動して再試行
    4. MAX_RESTARTS 回を超えると停止する（0 = 無制限）
"""

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from datetime import datetime

import psutil
import requests
from secret import DISCORD_WEBHOOK_URL

# ─── 設定 ──────────────────────────────────────────────────────────
MAIN_SCRIPT   = "mt5_ea_bridge.py"
LOG_DIR       = r"G:\マイドライブ\mt5_log"   # 環境に合わせて修正
LOG_FILE      = os.path.join(LOG_DIR, "mt5_monitor.log")
FLAG_FILE     = os.path.join(LOG_DIR, "paused.flag")

# ウォッチドッグ設定
RESTART_DELAY_SEC = 10    # 再起動前の待機秒数
MAX_RESTARTS      = 0     # 最大再起動回数（0 = 無制限）

# ブリッジに渡すデフォルト引数（--watch 時に使用）
_DEFAULT_BRIDGE_ARGS = [
    "--mode", "scalp",
    "--symbol", "BTCUSD",
]

# ─── ロギング設定 ───────────────────────────────────────────────────
# root ロガーへの伝播を無効化し、Google Drive ファイルハンドラーの影響を受けない
_logger = logging.getLogger('mt5_monitor')
_logger.setLevel(logging.INFO)
_logger.propagate = False  # root の FileHandler (Google Drive) に伝播させない

# コンソール出力（常に有効）
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))
_logger.addHandler(_ch)

# ローカルファイルへの追記ログ（スクリプトと同じ場所に置くことで Google Drive 問題を回避）
_local_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
try:
    os.makedirs(_local_log_dir, exist_ok=True)
    _fh = logging.handlers.RotatingFileHandler(
        os.path.join(_local_log_dir, 'mt5_monitor.log'),
        maxBytes=2 * 1024 * 1024,  # 2 MB
        backupCount=5,
        encoding='utf-8',
    )
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    _logger.addHandler(_fh)
except OSError:
    pass  # ローカルログが書けない場合はコンソールのみ


def _ts() -> str:
    return datetime.now().strftime('%H:%M:%S')


def send_discord(message: str) -> None:
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        _logger.warning(f"Discord 通知失敗: {e}")


def _kill_mt5_terminal() -> None:
    """MT5 端末プロセスを強制終了する（Windows のみ有効）"""
    try:
        subprocess.run(['taskkill', '/F', '/IM', 'terminal64.exe'], capture_output=True)
        _logger.info("MT5 端末を強制終了しました")
        time.sleep(5)
    except FileNotFoundError:
        pass  # Linux 環境では taskkill は存在しない


def restart_all(bridge_cmd: list[str]) -> None:
    """MT5 端末を再起動してブリッジを再起動する（ヘルスチェックモード用）"""
    _logger.warning("異常検知。MT5 端末を再起動します。")
    send_discord("⚠️ **【警告】** MT5 の異常を検知したため、システムを強制再起動しました。")
    _kill_mt5_terminal()
    subprocess.Popen(bridge_cmd, creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
    _logger.info(f"ブリッジを再起動しました: {' '.join(bridge_cmd)}")


# ─── ウォッチドッグモード ───────────────────────────────────────────

def _build_cmd(extra_args: list[str]) -> list[str]:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), MAIN_SCRIPT)
    return [sys.executable, script] + extra_args


def watch(bridge_args: list[str]) -> None:
    """ブリッジを起動し、異常終了時に自動再起動するウォッチドッグループ"""
    if os.path.exists(FLAG_FILE):
        _logger.info("一時停止フラグを検出 → 起動しません")
        return

    cmd = _build_cmd(bridge_args)
    restart_count = 0

    send_discord(f"【監視開始】ウォッチドッグを起動しました。 cmd={' '.join(cmd)}")

    while True:
        _logger.info(f"[{_ts()}] ブリッジ起動 (再起動 #{restart_count}回目): {' '.join(cmd)}")
        try:
            ret = subprocess.call(cmd)
        except KeyboardInterrupt:
            _logger.info("Ctrl+C → ウォッチドッグ終了")
            send_discord("【監視終了】Ctrl+C によりウォッチドッグを停止しました。")
            break

        if ret == 0:
            _logger.info("ブリッジが正常終了 (code=0) → 再起動しません")
            send_discord("【監視終了】ブリッジが正常終了しました。再起動しません。")
            break

        restart_count += 1
        _logger.warning(
            f"ブリッジ異常終了 (code={ret}) → {RESTART_DELAY_SEC}秒後に再起動"
            f" (#{restart_count}回目)"
        )
        send_discord(
            f"⚠️ **【再起動】** ブリッジが異常終了 (code={ret})。"
            f"{RESTART_DELAY_SEC}秒後に再起動します。(#{restart_count}回目)"
        )

        if MAX_RESTARTS > 0 and restart_count > MAX_RESTARTS:
            _logger.error(f"最大再起動回数 ({MAX_RESTARTS}回) 到達 → 停止")
            send_discord(f"🛑 **【停止】** 最大再起動回数 ({MAX_RESTARTS}回) に達しました。")
            break

        _kill_mt5_terminal()
        time.sleep(RESTART_DELAY_SEC)

        if os.path.exists(FLAG_FILE):
            _logger.info("一時停止フラグを検出 → 再起動をキャンセル")
            break


# ─── ヘルスチェックモード（後方互換）──────────────────────────────

def health_check() -> None:
    """ブリッジが動いているか確認し、止まっていれば再起動する（後方互換）"""
    if os.path.exists(FLAG_FILE):
        sys.exit(0)

    cmd = _build_cmd(_DEFAULT_BRIDGE_ARGS)

    is_running = any(
        MAIN_SCRIPT in " ".join(p.info.get('cmdline') or [])
        for p in psutil.process_iter(['cmdline'])
    )

    if not is_running:
        restart_all(cmd)
    else:
        try:
            import MetaTrader5 as mt5
            if mt5.initialize():
                if not mt5.terminal_info().connected:
                    restart_all(cmd)
                else:
                    _logger.info("監視：すべて正常です。")
                mt5.shutdown()
        except ImportError:
            _logger.info("MetaTrader5 未インストール → 接続チェックをスキップ")


# ─── エントリーポイント ─────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description='MT5 ブリッジ ウォッチドッグ / ヘルスチェック')
    ap.add_argument('--watch',  action='store_true',
                    help='ウォッチドッグモード: ブリッジを起動・監視・自動再起動')
    ap.add_argument('--mode',   choices=['normal', 'scalp'], default='scalp')
    ap.add_argument('--symbol', default='BTCUSD')
    ap.add_argument('--target', type=int, default=None)
    ap.add_argument('--jpy',    type=float, default=None)
    ap.add_argument('--lot',    type=float, default=None)
    args = ap.parse_args()

    if args.watch:
        bridge_args = ['--mode', args.mode, '--symbol', args.symbol]
        if args.target is not None:
            bridge_args += ['--target', str(args.target)]
        if args.jpy is not None:
            bridge_args += ['--jpy', str(args.jpy)]
        if args.lot is not None:
            bridge_args += ['--lot', str(args.lot)]
        watch(bridge_args)
    else:
        health_check()
