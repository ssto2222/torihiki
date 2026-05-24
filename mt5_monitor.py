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
import threading
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

# MT5 ターミナル実行ファイルパス（空文字の場合は起動を試みない）
MT5_TERMINAL_EXE  = r"C:\Program Files\MetaTrader 5\terminal64.exe"  # 環境に合わせて修正

# MT5 プロファイル名（/profile: オプションで EA 入りプロファイルを自動ロード）
# 空文字の場合はデフォルトプロファイル（前回終了時の状態）を使用
# 例: MT5_PROFILE = "AutoTrade"  → MT5 の Profiles フォルダ内の "AutoTrade" を使用
MT5_PROFILE       = ""  # 環境に合わせて修正

# MT5 起動後に EA がロードされるまでの待機時間（秒）
# ブローカー接続 + チャート復元 + EA 初期化を含む。環境によって 30〜60 秒が目安
MT5_STARTUP_WAIT  = 40

# ウォッチドッグ設定
RESTART_DELAY_SEC   = 10   # 再起動前の待機秒数
MAX_RESTARTS        = 0    # 最大再起動回数（0 = 無制限）

# ブリッジを別コンソールウィンドウで起動するか（Windows のみ有効）
# True : 独立したウィンドウで起動 → ダッシュボードがそのウィンドウに表示される
#        stdout はブリッジ自身の log_dir ログに書かれるため watchdog 側キャプチャ不要
# False: 従来どおり stdout を _BRIDGE_LOG_FILE にキャプチャ（ダッシュボード不可）
BRIDGE_NEW_CONSOLE  = True

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


def _start_mt5_terminal() -> None:
    """MT5 端末が落ちている場合に起動する（Windows のみ・MT5_TERMINAL_EXE が設定されている場合）

    MT5_PROFILE が設定されている場合は /profile:名前 オプションを渡し、
    EA 入りのプロファイルを自動ロードする。
    """
    if not MT5_TERMINAL_EXE or not os.path.exists(MT5_TERMINAL_EXE):
        return
    # すでに起動中なら何もしない
    for p in psutil.process_iter(['name']):
        if (p.info.get('name') or '').lower() == 'terminal64.exe':
            return
    try:
        cmd = [MT5_TERMINAL_EXE]
        if MT5_PROFILE:
            cmd.append(f'/profile:{MT5_PROFILE}')  # EA 入りプロファイルを指定
        subprocess.Popen(cmd, creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
        _logger.info(
            f"MT5 端末を起動しました: {MT5_TERMINAL_EXE}"
            + (f"  profile={MT5_PROFILE}" if MT5_PROFILE else "")
        )
        _logger.info(f"EA ロード完了まで {MT5_STARTUP_WAIT} 秒待機中...")
        time.sleep(MT5_STARTUP_WAIT)
    except OSError as e:
        _logger.warning(f"MT5 端末の起動に失敗: {e}")


def restart_all(bridge_cmd: list[str]) -> None:
    """MT5 端末を再起動してブリッジを再起動する（ヘルスチェックモード用）"""
    _logger.warning("異常検知。MT5 端末を再起動します。")
    send_discord("⚠️ **【警告】** MT5 の異常を検知したため、システムを強制再起動しました。")
    _kill_mt5_terminal()
    subprocess.Popen(bridge_cmd, creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
    _logger.info(f"ブリッジを再起動しました: {' '.join(bridge_cmd)}")


# ─── Discord ウォッチドッグボット ───────────────────────────────────────

def _is_bridge_running() -> bool:
    """mt5_ea_bridge.py が実行中かどうか psutil で確認する"""
    try:
        for p in psutil.process_iter(['cmdline']):
            if MAIN_SCRIPT in ' '.join(p.info.get('cmdline') or []):
                return True
    except Exception:
        pass
    return False


def _find_bridge_procs(symbol: str) -> list:
    """指定シンボルで動いているブリッジプロセスの一覧を返す"""
    found = []
    try:
        for p in psutil.process_iter(['pid', 'cmdline']):
            cl = ' '.join(p.info.get('cmdline') or [])
            if MAIN_SCRIPT in cl and f'--symbol {symbol}' in cl:
                found.append(p)
    except Exception:
        pass
    return found


def _kill_bridge_procs(symbol: str) -> None:
    """指定シンボルの残留ブリッジプロセスを終了し、コンソールウィンドウが閉じるまで待つ"""
    procs = _find_bridge_procs(symbol)
    if not procs:
        return
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            pass
    _logger.info(f"残留ブリッジ {len(procs)} プロセスを終了しました (symbol={symbol})")
    time.sleep(2)  # コンソールウィンドウが閉じるまで待機


def _is_watch_duplicate(symbol: str) -> bool:
    """同一シンボルの --watch ウォッチドッグが既に動いているか確認する"""
    my_pid    = os.getpid()
    my_script = os.path.basename(__file__)      # 'mt5_monitor.py'
    try:
        for p in psutil.process_iter(['pid', 'cmdline']):
            if p.pid == my_pid:
                continue
            cl = ' '.join(p.info.get('cmdline') or [])
            if my_script in cl and '--watch' in cl and symbol in cl:
                return True
    except Exception:
        pass
    return False


def _load_bot_cfg() -> dict:
    """ボット用の設定辞書（config.py + オーバーライド適用済み）を返す"""
    try:
        import config as C
        from bridge.param_override import apply_overrides
        _KEYS = ['MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES', 'LOCAL',
                 'PLOT', 'BRIDGE', 'SCALP', 'REGIME', 'TIME_BIAS', 'WHIPSAW', 'ELLIOTT', 'MACRO']
        cfg = {k: getattr(C, k) for k in _KEYS if hasattr(C, k)}
        return apply_overrides(cfg)
    except Exception:
        return {}


def start_monitor_bot(shared: dict) -> 'threading.Thread | None':
    """
    ウォッチドッグ用 Discord ボットをバックグラウンドスレッドで起動する。

    shared dict:
        proc          : Popen | None   現在のブリッジプロセス
        paused        : bool           True = 次の再起動をスキップ
        cmd           : list[str]      ブリッジ起動コマンド
        use_new_console: bool          CREATE_NEW_CONSOLE フラグ
        restart_count : int            再起動回数（表示用）

    discord.py 未インストール / secret.py に認証情報なければ None を返す。
    ブリッジ側の discord_cmd.py はボットを起動しないため、同一トークンの競合なし。
    """
    try:
        from secret import DISCORD_BOT_TOKEN as _token, DISCORD_CMD_CHANNEL_ID as _chan_id
        _chan_id = int(_chan_id)
    except (ImportError, AttributeError):
        _logger.info('ウォッチドッグボット: secret.py に認証情報なし → スキップ')
        return None

    try:
        import discord as _discord
    except ModuleNotFoundError:
        _logger.warning('ウォッチドッグボット: discord.py 未インストール → スキップ (pip install discord.py)')
        return None

    # bridge.param_override を使ったパラメータコマンド統合（任意）
    try:
        from bridge.param_override import (
            set_override, set_override_path, reset_override_path,
            clear_overrides, current_values_text, section_lines,
            get_value_text, all_overrides_text,
            parse_value, PARAMS, CFG_SECTIONS,
        )
        from bridge.discord_cmd import (
            _paginate, _lines_to_pages, _build_command_help,
            _build_section_help, _build_key_help, _readme_chunks,
        )
        _has_param_cmds = True
    except ImportError:
        _has_param_cmds = False

    _CREATE_NEW_CONSOLE = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)

    class _MonitorBot(_discord.Client):
        def __init__(self) -> None:
            intents = _discord.Intents.default()
            intents.message_content = True
            super().__init__(intents=intents)

        async def on_ready(self) -> None:
            _logger.info(f'ウォッチドッグボット起動: {self.user}')
            ch = self.get_channel(_chan_id)
            if ch:
                await ch.send('【ウォッチドッグボット】起動しました。`!help` でコマンド一覧')

        async def on_message(self, msg: _discord.Message) -> None:
            if msg.author == self.user or msg.channel.id != _chan_id:
                return
            content = msg.content.strip()
            if not content.startswith('!'):
                return
            parts = content[1:].split()
            if not parts:
                return
            cmd, args = parts[0].lower(), parts[1:]
            try:
                replies = await self._dispatch(cmd, args)
            except Exception as e:
                replies = [f'エラー: {e}']
                _logger.exception(f'ウォッチドッグボット コマンドエラー: {content}')
            for reply in replies:
                if reply:
                    await msg.channel.send(reply)

        async def _dispatch(self, cmd: str, args: list[str]) -> list[str]:
            # ── !start ──────────────────────────────────────────────
            if cmd == 'start':
                if _is_bridge_running():
                    return ['⚠️ ブリッジは既に起動中です。']
                _kill_bridge_procs(shared.get('symbol', 'unknown'))  # 残留プロセスを掃除
                shared['paused'] = False
                return ['✅ 再起動を許可しました。数秒以内に起動します。']

            # ── !stop ────────────────────────────────────────────────
            if cmd == 'stop':
                shared['paused'] = True
                proc = shared.get('proc')
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                        _logger.info(f'Discord [stop] ブリッジを終了 PID={proc.pid}')
                        return [
                            f'🛑 ブリッジを停止しました (PID={proc.pid})。'
                            '再起動しません。`!start` で再開できます。'
                        ]
                    except Exception as e:
                        return [f'❌ 停止に失敗: {e}']
                # psutil でフォールバック
                stopped = []
                for p in psutil.process_iter(['pid', 'cmdline']):
                    if MAIN_SCRIPT in ' '.join(p.info.get('cmdline') or []):
                        try:
                            p.kill()
                            stopped.append(str(p.pid))
                        except Exception:
                            pass
                if stopped:
                    return [
                        f'🛑 ブリッジを停止しました (PID={", ".join(stopped)})。'
                        '`!start` で再開できます。'
                    ]
                return ['⚠️ ブリッジは既に停止しています。']

            # ── !status ──────────────────────────────────────────────
            if cmd == 'status':
                proc    = shared.get('proc')
                running = (proc is not None and proc.poll() is None) or _is_bridge_running()
                paused  = shared.get('paused', False)
                rc      = shared.get('restart_count', 0)
                lines   = [
                    '**【ウォッチドッグ ステータス】**',
                    f'ブリッジ: {"✅ 稼働中" if running else "❌ 停止中"}',
                    f'一時停止: {"はい（`!start` で再開）" if paused else "いいえ"}',
                    f'再起動回数: {rc}',
                ]
                if proc is not None:
                    lines.append(f'PID: {proc.pid}')
                return ['\n'.join(lines)]

            # ── !help ────────────────────────────────────────────────
            if cmd == 'help' and not args:
                proc_help = (
                    '**プロセス管理コマンド**\n'
                    '```\n'
                    '!start    ブリッジを起動（停止中のとき）\n'
                    '!stop     ブリッジを停止（再起動しない）\n'
                    '!status   現在の稼働状態を表示\n'
                    '```\n'
                )
                if _has_param_cmds:
                    return [proc_help + _build_command_help()]
                return [proc_help]

            # ── パラメータコマンド ─────────────────────────────────
            if _has_param_cmds:
                cfg = _load_bot_cfg()

                if cmd == 'help':
                    t = args[0] if args else ''
                    if '.' in t and t.count('.') == 1:
                        return [_build_key_help(cfg, t)]
                    return _build_section_help(cfg, t.upper())

                if cmd == 'list':
                    lines = ['**セクション一覧**  (!help SECTION で詳細)', '```']
                    for sec in CFG_SECTIONS:
                        n = len(cfg.get(sec, {})) if isinstance(cfg.get(sec), dict) else 0
                        lines.append(f'{sec:<14} ({n}パラメータ)')
                    lines.append('```')
                    return ['\n'.join(lines)]

                if cmd == 'readme':
                    keyword = ' '.join(args)
                    pages   = _readme_chunks(keyword)
                    total   = len(pages)
                    return [p + (f'\n（{i+1}/{total}）' if total > 1 else '')
                            for i, p in enumerate(pages)]

                if cmd == 'overrides':
                    return _paginate(all_overrides_text())

                if cmd in ('params', 'get'):
                    if not args:
                        return [current_values_text(cfg)]
                    t = args[0]
                    if '.' in t:
                        return [get_value_text(cfg, t)]
                    if t.upper() in CFG_SECTIONS or t.isupper():
                        return _lines_to_pages(section_lines(cfg, t.upper()),
                                               f'**{t.upper()}** 現在値')
                    spec = PARAMS.get(t.lower())
                    if spec:
                        return [get_value_text(cfg, f'{spec.section}.{spec.key}')]
                    return [f'不明: `{t}`  例: `!get SCALP` / `!get SCALP.cooldown_min`']

                if cmd == 'set':
                    if len(args) < 2:
                        return ['使い方: `!set SECTION.KEY value` または `!set shortname value`']
                    name, raw = args[0], ' '.join(args[1:])
                    if '.' in name:
                        val, err = set_override_path(name, raw, cfg)
                        if err:
                            return [err]
                        _logger.info(f'Discord [set] {name} = {val}')
                        return [f'`{name}` を **{val}** に設定しました\n次のポーリングから反映されます']
                    name_l = name.lower()
                    if name_l not in PARAMS:
                        return [f'不明: `{name}`\n短縮名: {", ".join(sorted(PARAMS))}']
                    val, err = parse_value(name_l, raw)
                    if err:
                        return [err]
                    set_override(name_l, val)
                    _logger.info(f'Discord [set] {name_l} = {val}')
                    return [f'`{name_l}` を **{val}** に設定しました（{PARAMS[name_l].desc}）\n次のポーリングから反映されます']

                if cmd == 'reset':
                    if not args:
                        clear_overrides()
                        _logger.info('Discord [reset] 全オーバーライド削除')
                        return ['全パラメータのオーバーライドを削除しました']
                    target = args[0]
                    msg = reset_override_path(target if '.' in target else None)
                    _logger.info(f'Discord [reset] {target}')
                    return [msg]

            return [f'不明なコマンド: `!{cmd}`\n`!help` でコマンド一覧']

    def _run() -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = _MonitorBot()
        try:
            loop.run_until_complete(client.start(_token))
        except Exception:
            _logger.exception('ウォッチドッグボット 予期せぬ終了')
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name='monitor-discord-bot')
    t.start()
    _logger.info(f'ウォッチドッグボット スレッド起動 channel_id={_chan_id}')
    return t


# ─── ウォッチドッグモード ───────────────────────────────────────────

# ブリッジの stdout/stderr を書き出すファイル（ローカルログディレクトリ）
_BRIDGE_LOG_FILE = os.path.join(_local_log_dir, 'mt5_bridge_console.log')
# RotatingFileHandler と同等の上限（超えたら切り詰めて再オープン）
_BRIDGE_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _open_bridge_log():
    """ブリッジ用ログファイルを追記モードで開く。10 MB 超なら切り詰める。"""
    if os.path.exists(_BRIDGE_LOG_FILE) and os.path.getsize(_BRIDGE_LOG_FILE) > _BRIDGE_LOG_MAX_BYTES:
        # 古いファイルをローテート
        rotated = _BRIDGE_LOG_FILE + '.1'
        if os.path.exists(rotated):
            os.remove(rotated)
        os.rename(_BRIDGE_LOG_FILE, rotated)
    return open(_BRIDGE_LOG_FILE, 'a', encoding='utf-8', buffering=1)


def _build_cmd(extra_args: list[str]) -> list[str]:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), MAIN_SCRIPT)
    # -X utf8: Windows cp932 コンソールでも ¥ × などを正しく出力するため UTF-8 モード強制
    return [sys.executable, '-X', 'utf8', '-u', script] + extra_args


def watch(bridge_args: list[str]) -> None:
    """ブリッジを起動し、異常終了時に自動再起動するウォッチドッグループ"""
    # シンボルを bridge_args から取得（重複チェックと残留プロセス管理に使用）
    _watch_symbol = next(
        (bridge_args[i + 1] for i, a in enumerate(bridge_args) if a == '--symbol'),
        'unknown',
    )

    # ── 同一シンボルの重複起動を防止 ────────────────────────────
    if _is_watch_duplicate(_watch_symbol):
        _logger.error(
            f"シンボル {_watch_symbol} のウォッチドッグは既に起動中です → 終了します。"
            "同一シンボルは 1 プロセスのみ許可されます。"
        )
        return

    # ── 前回の残留ブリッジプロセスを終了 ────────────────────────
    # 旧ウォッチドッグが kill されてブリッジだけ生き残っている場合や
    # 別ウィンドウが閉じきれていない場合に一掃する
    _kill_bridge_procs(_watch_symbol)

    cmd = _build_cmd(bridge_args)
    restart_count = 0

    _CREATE_NEW_CONSOLE = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
    _use_new_console = os.name == 'nt' and BRIDGE_NEW_CONSOLE and bool(_CREATE_NEW_CONSOLE)

    # ボットスレッドとウォッチドッグループで proc と paused 状態を共有する
    shared: dict = {
        'proc':            None,
        'paused':          os.path.exists(FLAG_FILE),  # FLAG_FILE があれば最初から一時停止
        'cmd':             cmd,
        'symbol':          _watch_symbol,
        'use_new_console': _use_new_console,
        'restart_count':   0,
    }
    start_monitor_bot(shared)

    if _use_new_console:
        _logger.info("ブリッジを別コンソールウィンドウで起動します（ダッシュボードモード）")
        _logger.info("ブリッジのログ → BRIDGE.log_dir で設定されたフォルダ")
    else:
        _logger.info(f"ブリッジコンソールログ → {_BRIDGE_LOG_FILE}")
    if shared['paused']:
        _logger.info("一時停止フラグを検出 → 一時停止状態で起動します（`!start` で開始）")
    send_discord(f"【監視開始】ウォッチドッグを起動しました。 cmd={' '.join(cmd)}")

    STATUS_INTERVAL = 60
    while True:
        # ── 一時停止中: Discord !start を待つ ──────────────────────
        if shared['paused'] or os.path.exists(FLAG_FILE):
            time.sleep(5)
            continue

        _logger.info(f"[{_ts()}] ブリッジ起動 (再起動 #{restart_count}回目): {' '.join(cmd)}")
        # 残留プロセス・ウィンドウを確実に閉じてから起動
        _kill_bridge_procs(_watch_symbol)
        proc = None
        ret  = -1  # Popen 失敗時のデフォルト（異常終了として扱う）
        try:
            if _use_new_console:
                proc = subprocess.Popen(cmd, creationflags=_CREATE_NEW_CONSOLE)
            else:
                with _open_bridge_log() as bf:
                    proc = subprocess.Popen(cmd, stdout=bf, stderr=bf)

            shared['proc'] = proc

            while True:
                try:
                    ret = proc.wait(timeout=STATUS_INTERVAL)
                    break
                except subprocess.TimeoutExpired:
                    _logger.info(f"[{_ts()}] ✓ 正常運転中  PID={proc.pid}")
        except KeyboardInterrupt:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            _logger.info("Ctrl+C → ウォッチドッグ終了")
            send_discord("【監視終了】Ctrl+C によりウォッチドッグを停止しました。")
            break
        except Exception as e:
            _logger.error(f"ブリッジ起動エラー: {e}")
            time.sleep(RESTART_DELAY_SEC)
        finally:
            shared['proc'] = None

        # ── プロセス終了後の処理 ────────────────────────────────────
        # !stop による意図的な終了、または正常終了 → 一時停止して !start 待ち
        if ret == 0 or shared['paused']:
            reason = "正常終了 (code=0)" if ret == 0 else f"停止コマンドにより終了 (code={ret})"
            _logger.info(f"ブリッジが{reason} → 一時停止モード（`!start` で再起動）")
            send_discord(f"【監視】ブリッジが停止しました。`!start` で再起動できます。")
            shared['paused'] = True
            continue

        # 異常終了 → 再起動
        restart_count += 1
        shared['restart_count'] = restart_count
        _logger.warning(
            f"ブリッジ異常終了 (code={ret}) → {RESTART_DELAY_SEC}秒後に再起動"
            f" (#{restart_count}回目)"
        )
        send_discord(
            f"⚠️ **【再起動】** ブリッジが異常終了 (code={ret})。"
            f"{RESTART_DELAY_SEC}秒後に再起動します。(#{restart_count}回目)"
        )

        if MAX_RESTARTS > 0 and restart_count > MAX_RESTARTS:
            _logger.error(f"最大再起動回数 ({MAX_RESTARTS}回) 到達 → 一時停止")
            send_discord(
                f"🛑 **【停止】** 最大再起動回数 ({MAX_RESTARTS}回) に達しました。"
                "`!start` で再開できます。"
            )
            shared['paused'] = True
            continue

        if ret == 2:
            # exit(2) = MT5 接続失敗: MT5 は kill せずブリッジのみ再起動
            _logger.info("MT5 接続失敗による終了 → MT5 は維持してブリッジのみ再起動")
            _start_mt5_terminal()
        else:
            # exit(1) = 稼働中の実行時エラー: MT5 を強制終了して再起動
            _kill_mt5_terminal()
            _start_mt5_terminal()

        time.sleep(RESTART_DELAY_SEC)

        if shared['paused'] or os.path.exists(FLAG_FILE):
            _logger.info("一時停止フラグを検出 → 再起動をキャンセル")
            shared['paused'] = True


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
