"""bridge/discord_cmd.py — Discord コマンドボット v2（全設定対応）

バックグラウンドスレッドで asyncio ループを走らせる。
runner.py の run_bridge() から start_discord_bot(cfg) を呼ぶ。

secret.py に以下が必要（なければ起動をスキップ）:
    DISCORD_BOT_TOKEN      = "Bot token here"
    DISCORD_CMD_CHANNEL_ID = 123456789  # int

コマンド一覧:
  !set SECTION.KEY value           全設定を変更        例: !set SCALP.target_profit_jpy 1500
  !set SECTION.KEY.SUBKEY value    dict サブキー変更    例: !set SL.tp_atr_multi.BTCUSD 3.5
  !set shortname value             短縮名（後方互換）   例: !set target 1500  !set buy off
  !get [SECTION[.KEY]]             現在値確認           例: !get SCALP  !get SL.sl_multi
  !list                            セクション一覧
  !help [SECTION[.KEY]]            パラメータ説明       例: !help SCALP  !help SL.sl_multi
  !readme [section_title]          README.md 表示       例: !readme  !readme 起動方法
  !overrides                       現在のオーバーライド一覧
  !reset [SECTION.KEY]             オーバーライド削除   例: !reset  !reset SL.sl_multi
  !params                          !get の短縮エイリアス
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import signal as _signal
import subprocess
import sys
import threading
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Any

from bridge.param_override import (
    PARAMS, CFG_SECTIONS,
    parse_value, set_override, clear_overrides, current_values_text,
    set_override_path, reset_override_path, apply_overrides,
    get_value_text, section_lines, all_overrides_text,
    get_param_desc, _get_docs,
)
from bridge.notify import _build_discord_hourly_msg

_logger = logging.getLogger('torihiki')
_PREFIX       = '!'
_README       = Path(__file__).parent.parent / 'README.md'
_CHUNK_LEN    = 1800   # Discord 2000 char limit にバッファ
_REPO_DIR     = str(Path(__file__).parent.parent)
_BRIDGE_SCRIPT = 'mt5_ea_bridge.py'


def _terminate_pid(pid: int) -> str | None:
    """プロセスを終了する。成功時 None、失敗時エラーメッセージを返す。
    Windows: taskkill /PID /F を使用（os.kill は権限エラーになる場合がある）
    Linux/Mac: SIGTERM を送信
    """
    if os.name == 'nt':
        try:
            r = subprocess.run(
                ['taskkill', '/PID', str(pid), '/F'],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                return None
            err = (r.stderr or r.stdout).decode('cp932', errors='replace').strip()
            return err or f'taskkill failed (code={r.returncode})'
        except FileNotFoundError:
            return 'taskkill コマンドが見つかりません'
        except Exception as e:
            return str(e)
    else:
        try:
            os.kill(pid, _signal.SIGTERM)
            return None
        except ProcessLookupError:
            return f'PID {pid} が見つかりません'
        except PermissionError:
            return f'PID {pid} へのアクセスが拒否されました'


def _find_watchdog_procs() -> list:
    """実行中の mt5_monitor.py --watch プロセスを全て返す [(pid, cmdline), ...]"""
    result = []
    try:
        import psutil
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'python' not in (p.info.get('name') or '').lower():
                    continue
                cl_list = p.info.get('cmdline') or []
                cl_str  = ' '.join(cl_list)
                if 'mt5_monitor.py' in cl_str and '--watch' in cl_str:
                    result.append((p.pid, cl_list))
            except Exception:
                continue
    except Exception:
        pass
    return result


def _make_watchdog_restart_helper(pids: list, cmds: list) -> str:
    """ウォッチドッグ再起動用ヘルパースクリプト文字列を返す。DETACHED_PROCESS で起動し自己削除する。"""
    return f'''\
# Auto-generated restart helper — do not edit
import time, subprocess, os, sys

pids   = {pids!r}
cmds   = {cmds!r}
cwd    = {_REPO_DIR!r}
bridge = {_BRIDGE_SCRIPT!r}

# ブリッジプロセスを全て終了
try:
    import psutil
    for p in psutil.process_iter(['pid', 'cmdline']):
        try:
            cl = ' '.join(p.info.get('cmdline') or [])
            if bridge in cl:
                subprocess.run(['taskkill', '/PID', str(p.pid), '/F'], capture_output=True)
        except Exception:
            pass
except Exception:
    pass

# ウォッチドッグを終了
for pid in pids:
    subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)

time.sleep(3)

# ウォッチドッグを再起動
CREATE_NEW_CONSOLE = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0x00000010)
for cmd in cmds:
    try:
        subprocess.Popen(cmd, cwd=cwd, creationflags=CREATE_NEW_CONSOLE)
    except Exception as e:
        print(f'起動失敗: {{cmd}} -> {{e}}', file=sys.stderr)

time.sleep(1)
try:
    os.unlink(__file__)
except Exception:
    pass
'''


# ── ページネーション ────────────────────────────────────────────────────────

def _paginate(text: str, chunk: int = _CHUNK_LEN) -> list[str]:
    """テキストを Discord 送信可能なサイズに分割する（行単位で分割）。"""
    if len(text) <= chunk:
        return [text]
    pages: list[str] = []
    cur: list[str]   = []
    cur_len = 0
    for line in text.splitlines(keepends=True):
        if cur_len + len(line) > chunk and cur:
            pages.append(''.join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line)
    if cur:
        pages.append(''.join(cur))
    return pages


def _lines_to_pages(lines: list[str], header: str = '') -> list[str]:
    """行リストを Discord ページに変換する（コードブロック付き）。"""
    body  = '\n'.join(lines)
    pages = _paginate(body)
    total = len(pages)
    result = []
    for i, page in enumerate(pages):
        suffix = f'\n（{i+1}/{total}）' if total > 1 else ''
        result.append(f'{header}\n```\n{page}\n```{suffix}' if header else f'```\n{page}\n```{suffix}')
    return result


# ── ヘルプ・README ─────────────────────────────────────────────────────────

def _build_command_help() -> str:
    shorts = ', '.join(f'`{n}`' for n in sorted(PARAMS))
    sects  = ', '.join(f'`{s}`' for s in CFG_SECTIONS)
    return (
        '**torihiki パラメータ制御ボット**\n'
        '```\n'
        '!pull                         git pull して全ウォッチドッグを再起動\n'
        '!status                       現在のシグナル状態を照会\n'
        '!pid                          ブリッジ PID を表示\n'
        '!restart [PID] [SYMBOL]       ブリッジ再起動（ウォッチドッグ経由で完全再起動）\n'
        '!resetlosses [SYMBOL]         連続損失カウントを 0 にリセット（ブリッジ停止不要）\n'
        '!mode [scalp|normal]          動作モード確認/切替（再起動不要）\n'
        '!symbol [SYMBOL]              シンボル確認/切替（再起動不要・MT5再接続）\n'
        '!set SECTION.KEY value        設定変更   例: !set SCALP.cooldown_min 10\n'
        '!set SECTION.KEY.SUB value    dict変更   例: !set SL.tp_atr_multi.BTCUSD 3.5\n'
        '!set shortname value          短縮名変更 例: !set target 1500 / !set buy off\n'
        '!get [SECTION[.KEY]]          値の確認   例: !get SCALP / !get SL.sl_multi\n'
        '!list                         セクション一覧\n'
        '!help [SECTION[.KEY]]         パラメータ説明\n'
        '!readme [キーワード]           README表示\n'
        '!overrides                    変更済み一覧\n'
        '!reset [SECTION.KEY]          オーバーライド削除\n'
        '```\n'
        f'**セクション**: {sects}\n'
        f'**短縮名**: {shorts}'
    )


def _build_section_help(cfg: dict, section: str) -> list[str]:
    """セクションの全パラメータ+説明を Discord ページとして返す。"""
    section = section.upper()
    sec_cfg = cfg.get(section)
    if not isinstance(sec_cfg, dict):
        return [f'セクション `{section}` が見つかりません。利用可能: {", ".join(CFG_SECTIONS)}']
    docs = _get_docs()
    lines: list[str] = []
    for key, base_val in sec_cfg.items():
        desc = get_param_desc(section, key)
        if isinstance(base_val, dict):
            val_str = f'{{{", ".join(f"{k}: {v}" for k, v in base_val.items())}}}'
        elif isinstance(base_val, list):
            val_str = str(base_val)
        else:
            val_str = str(base_val)
        desc_part = f'  # {desc}' if desc else ''
        lines.append(f'{section}.{key} = {val_str}{desc_part}')
    return _lines_to_pages(lines, f'**{section}** パラメータ説明')


def _build_key_help(cfg: dict, dot_path: str) -> str:
    """単一キーの詳細説明。"""
    parts   = dot_path.split('.')
    section = parts[0].upper()
    key     = parts[1] if len(parts) >= 2 else ''
    sec_cfg = cfg.get(section, {})
    if not isinstance(sec_cfg, dict) or key not in sec_cfg:
        return f'`{dot_path}` が見つかりません'
    base_val = sec_cfg[key]
    desc     = get_param_desc(section, key)
    if isinstance(base_val, dict):
        val_str = f'```json\n{__import__("json").dumps(base_val, ensure_ascii=False, indent=2)}\n```'
        sub_hint = '\n'.join(f'  サブキー変更: `!set {section}.{key}.{k} <値>`' for k in base_val)
        return f'**`{dot_path}`**\n{val_str}\n{desc}\n{sub_hint}'
    elif isinstance(base_val, list):
        val_str = f'```json\n{__import__("json").dumps(base_val, ensure_ascii=False)}\n```'
        return f'**`{dot_path}`**\n{val_str}\n{desc}\n変更: `!set {dot_path} [v1, v2, ...]`（JSON配列）'
    else:
        type_name = {bool: 'bool (on/off)', int: '整数', float: '数値', str: '文字列'}.get(type(base_val), str(type(base_val)))
        return (
            f'**`{dot_path}`**\n'
            f'現在値: `{base_val}`　型: {type_name}\n'
            f'説明: {desc or "(なし)"}\n'
            f'変更: `!set {dot_path} <値>`'
        )


def _readme_chunks(keyword: str = '') -> list[str]:
    """README.md を Discord ページに分割して返す。keyword があれば含むセクションのみ。"""
    if not _README.exists():
        return ['README.md が見つかりません']
    text = _README.read_text(encoding='utf-8')

    if keyword:
        # ## セクション単位で keyword を含む箇所を返す
        sections: list[str] = []
        cur: list[str] = []
        for line in text.splitlines():
            if line.startswith('## ') and cur:
                sections.append('\n'.join(cur))
                cur = []
            cur.append(line)
        if cur:
            sections.append('\n'.join(cur))
        matched = [s for s in sections if keyword.lower() in s.lower()]
        if not matched:
            return [f'`{keyword}` を含むセクションが見つかりませんでした']
        text = '\n\n'.join(matched)

    return _paginate(text)


# ── 認証情報取得 ───────────────────────────────────────────────────────────

def _get_credentials() -> tuple[str, int] | tuple[None, None]:
    try:
        import secret
        token   = getattr(secret, 'DISCORD_BOT_TOKEN',      None)
        chan_id = getattr(secret, 'DISCORD_CMD_CHANNEL_ID', None)
        if token and chan_id:
            return token, int(chan_id)
    except ImportError:
        pass
    return None, None


# ── Bot 起動 ──────────────────────────────────────────────────────────────

def start_discord_bot(cfg: dict[str, Any],
                      data_ref: list | None = None,
                      macro_ref: list | None = None,
                      mode_ref: list | None = None,
                      symbol_ref: list | None = None) -> threading.Thread | None:
    """
    バックグラウンドスレッドで Discord ボットを起動する。
    discord.py 未インストール / secret.py に認証情報がなければ何もしない。
    cfg は runner.py の元 cfg dict への参照（apply_overrides でポーリングごとに更新される）。
    data_ref / macro_ref は [None] のような 1 要素リスト（runner.py が最新値を書き込む）。
    mode_ref / symbol_ref は ['scalp'] / ['BTCUSD'] のような 1 要素リスト（ボットが書き込む）。
    """
    token, chan_id = _get_credentials()
    if not token:
        _logger.info('Discord ボット: secret.py に認証情報なし → スキップ')
        return None

    try:
        import discord as _discord
    except ModuleNotFoundError:
        _logger.warning('Discord ボット: discord.py 未インストール → スキップ (pip install discord.py)')
        return None

    class _ParamBot(_discord.Client):
        def __init__(self) -> None:
            intents = _discord.Intents.default()
            intents.message_content = True
            super().__init__(intents=intents)

        async def on_ready(self) -> None:
            _logger.info(f'Discord ボット起動: {self.user}')
            ch = self.get_channel(chan_id)
            if ch:
                await ch.send('【パラメータ制御ボット v2】起動しました。`!help` でコマンド一覧')

        async def on_message(self, message: _discord.Message) -> None:
            if message.author == self.user:
                return
            if message.channel.id != chan_id:
                return
            content = message.content.strip()
            if not content.startswith(_PREFIX):
                return

            parts = content[len(_PREFIX):].split()
            if not parts:
                return
            cmd  = parts[0].lower()
            args = parts[1:]

            try:
                replies = await self._dispatch(cmd, args)
            except Exception as e:
                replies = [f'エラー: {e}']
                _logger.exception(f'Discord コマンド処理エラー: {content}')

            for reply in replies:
                if reply:
                    await message.channel.send(reply)

        async def _dispatch(self, cmd: str, args: list[str]) -> list[str]:
            # ── !pull ──────────────────────────────────────────────────
            if cmd == 'pull':
                _logger.info('Discord [pull] git pull 開始')

                def _do_pull():
                    def _run(git_args):
                        return subprocess.run(
                            ['git'] + git_args, cwd=_REPO_DIR,
                            capture_output=True, text=True, timeout=60,
                        )
                    r = _run(['pull', '--ff-only'])
                    out = (r.stdout + r.stderr).strip()
                    if r.returncode != 0:
                        if 'incorrect old value provided' in out or 'fetching ref' in out:
                            _run(['remote', 'prune', 'origin'])
                            r2 = _run(['pull', '--ff-only'])
                            return r2.returncode, (r2.stdout + r2.stderr).strip()
                        if 'Cannot fast-forward to multiple branches' in out:
                            # fetch してから FETCH_HEAD を明示的にマージ
                            rf = _run(['fetch', 'origin'])
                            if rf.returncode != 0:
                                return rf.returncode, (rf.stdout + rf.stderr).strip()
                            rm = _run(['merge', '--ff-only', 'FETCH_HEAD'])
                            return rm.returncode, (rm.stdout + rm.stderr).strip()
                    return r.returncode, out

                try:
                    ret_code, pull_out = _do_pull()
                except subprocess.TimeoutExpired:
                    return ['❌ git pull タイムアウト (60秒)']
                except Exception as e:
                    return [f'❌ git pull エラー: {e}']

                if ret_code != 0:
                    return [f'❌ git pull 失敗:\n```\n{pull_out[:600]}\n```']

                watchdog_procs = _find_watchdog_procs()
                if not watchdog_procs:
                    return [
                        f'✅ git pull 完了:\n```\n{pull_out[:600]}\n```\n'
                        '⚠️ 実行中のウォッチドッグが見つかりませんでした'
                    ]

                w_pids = [p for p, _ in watchdog_procs]
                w_cmds = [c for _, c in watchdog_procs]
                helper_path = os.path.join(_REPO_DIR, '_restart_helper.py')
                try:
                    with open(helper_path, 'w', encoding='utf-8') as _hf:
                        _hf.write(_make_watchdog_restart_helper(w_pids, w_cmds))
                    _DETACHED = 0x00000008   # DETACHED_PROCESS
                    _NEW_PG   = 0x00000200   # CREATE_NEW_PROCESS_GROUP
                    flags = (_DETACHED | _NEW_PG) if os.name == 'nt' else 0
                    subprocess.Popen(
                        [sys.executable, helper_path],
                        cwd=_REPO_DIR, creationflags=flags, close_fds=True,
                    )
                except Exception as e:
                    return [f'❌ 再起動ヘルパーの起動に失敗しました: {e}']

                pids_str = ', '.join(str(p) for p in w_pids)
                _logger.info(f'Discord [pull] 再起動ヘルパー起動 PIDs={w_pids}')
                return [
                    f'✅ git pull 完了:\n```\n{pull_out[:500]}\n```\n'
                    f'🔄 {len(watchdog_procs)} 個のウォッチドッグを再起動します '
                    f'(PID: {pids_str})\n約3秒後に再起動されます'
                ]

            # ── !status ────────────────────────────────────────────────
            if cmd == 'status':
                if data_ref is None or data_ref[0] is None:
                    return ['データ未取得（ブリッジ起動直後）']
                macro = macro_ref[0] if macro_ref else None
                msg = _build_discord_hourly_msg(data_ref[0], macro)
                return [msg + '\n*(照会応答)*']

            # ── !help ──────────────────────────────────────────────────
            if cmd == 'help':
                if not args:
                    return [_build_command_help()]
                target = args[0]
                if '.' in target and target.count('.') == 1:
                    return [_build_key_help(cfg, target)]
                return _build_section_help(cfg, target.upper())

            # ── !list ──────────────────────────────────────────────────
            if cmd == 'list':
                lines = ['**セクション一覧**  (!help SECTION で詳細)', '```']
                for sec in CFG_SECTIONS:
                    sec_cfg = cfg.get(sec, {})
                    n = len(sec_cfg) if isinstance(sec_cfg, dict) else 0
                    lines.append(f'{sec:<14} ({n}パラメータ)')
                lines.append('```')
                return ['\n'.join(lines)]

            # ── !readme ────────────────────────────────────────────────
            if cmd == 'readme':
                keyword = ' '.join(args) if args else ''
                pages   = _readme_chunks(keyword)
                total   = len(pages)
                result  = []
                for i, page in enumerate(pages):
                    suffix = f'\n（{i+1}/{total}）' if total > 1 else ''
                    result.append(page + suffix)
                return result

            # ── !overrides ─────────────────────────────────────────────
            if cmd == 'overrides':
                return _paginate(all_overrides_text())

            # ── !params / !get ─────────────────────────────────────────
            if cmd in ('params', 'get'):
                if not args:
                    # 全短縮パラメータ（後方互換）
                    return [current_values_text(cfg)]
                target = args[0]
                if '.' in target:
                    # ドット記法: SECTION.KEY or SECTION.KEY.SUBKEY
                    return [get_value_text(cfg, target)]
                # 大文字ならセクション、小文字なら短縮名
                if target.upper() in CFG_SECTIONS or target.isupper():
                    return _lines_to_pages(section_lines(cfg, target.upper()),
                                           f'**{target.upper()}** 現在値')
                spec = PARAMS.get(target.lower())
                if spec:
                    return [get_value_text(cfg, f'{spec.section}.{spec.key}')]
                return [f'不明: `{target}`  例: `!get SCALP` / `!get SCALP.cooldown_min`']

            # ── !set ───────────────────────────────────────────────────
            if cmd == 'set':
                if len(args) < 2:
                    return ['使い方: `!set SECTION.KEY value` または `!set shortname value`']
                name = args[0]
                raw  = ' '.join(args[1:])   # 値にスペースが入る場合を許容

                if '.' in name:
                    # フルパス
                    val, err = set_override_path(name, raw, cfg)
                    if err:
                        return [err]
                    desc = ''
                    parts = name.split('.')
                    if len(parts) >= 2:
                        desc = get_param_desc(parts[0].upper(), parts[1])
                    _logger.info(f'Discord [set] {name} = {val}')
                    return [f'`{name}` を **{val}** に設定しました  {desc}\n次のポーリングから反映されます']
                else:
                    # 短縮名（後方互換）
                    name_l = name.lower()
                    if name_l not in PARAMS:
                        # ドット記法として再試行: 入力ミスで . を忘れた場合の案内
                        known = ', '.join(sorted(PARAMS))
                        return [f'不明な短縮名: `{name}`\n短縮名: {known}\nまたは `!set SECTION.KEY value` 形式で指定してください']
                    val, err = parse_value(name_l, raw)
                    if err:
                        return [err]
                    set_override(name_l, val)
                    spec = PARAMS[name_l]
                    _logger.info(f'Discord [set] {name_l} = {val}')
                    return [f'`{name_l}` を **{val}** に設定しました（{spec.desc}）\n次のポーリングから反映されます']

            # ── !reset ─────────────────────────────────────────────────
            if cmd == 'reset':
                if not args:
                    clear_overrides()
                    _logger.info('Discord [reset] 全オーバーライド削除')
                    return ['全パラメータのオーバーライドを削除しました']
                target = args[0]
                msg = reset_override_path(target if '.' in target else None)
                _logger.info(f'Discord [reset] {target}')
                return [msg]

            # ── !pid ───────────────────────────────────────────────────
            if cmd == 'pid':
                return [f'ブリッジ PID: `{os.getpid()}`']

            # ── !restart ───────────────────────────────────────────────
            if cmd == 'restart':
                # 引数: [PID] [SYMBOL]  順不同
                # 数字のみ → PID、それ以外 → SYMBOL
                target_pid = None
                new_sym    = None
                for a in args:
                    if a.isdigit():
                        target_pid = int(a)
                    else:
                        new_sym = a.upper()
                if target_pid is None:
                    target_pid = os.getpid()

                # symbol_ref を更新して次ポーリングでも反映（ウォッチドッグなし時用）
                if new_sym and symbol_ref is not None:
                    symbol_ref[0] = new_sym

                err = _terminate_pid(target_pid)
                if err:
                    return [f'❌ PID `{target_pid}` の終了に失敗しました: {err}']

                sym_msg = f' → シンボル: `{new_sym}`' if new_sym else ''
                _logger.info(f'Discord [restart] PID={target_pid}{sym_msg}')
                return [
                    f'🔄 PID `{target_pid}` を終了しました{sym_msg}\n'
                    'ウォッチドッグが自動再起動します（シンボル変更は ウォッチドッグの'
                    ' `!restart SYMBOL` で実行するとより確実です）'
                ]

            # ── !mode ──────────────────────────────────────────────────
            if cmd == 'mode':
                current = mode_ref[0] if mode_ref else '不明'
                if not args:
                    return [f'現在のモード: `{current}`\n変更: `!mode scalp` または `!mode normal`']
                new_mode = args[0].lower()
                if new_mode not in ('scalp', 'normal'):
                    return ['モードは `scalp` または `normal` を指定してください']
                if mode_ref is None:
                    return ['モード切替は利用できません（mode_ref 未設定）']
                if new_mode == current:
                    return [f'既にモード `{current}` です']
                mode_ref[0] = new_mode
                _logger.info(f'Discord [mode] {current} → {new_mode}')
                return [f'モードを `{current}` → `{new_mode}` に切り替えます（次のポーリングから反映）']

            # ── !symbol ────────────────────────────────────────────────
            if cmd == 'symbol':
                current = symbol_ref[0] if symbol_ref else '不明'
                if not args:
                    return [f'現在のシンボル: `{current}`\n変更例: `!symbol BTCUSD` / `!symbol XAUUSD`']
                new_sym = args[0].upper()
                if symbol_ref is None:
                    return ['シンボル切替は利用できません（symbol_ref 未設定）']
                if new_sym == current:
                    return [f'既にシンボル `{current}` です']
                symbol_ref[0] = new_sym
                _logger.info(f'Discord [symbol] {current} → {new_sym}')
                return [f'シンボルを `{current}` → `{new_sym}` に切り替えます\n'
                        f'次のポーリングで MT5 再接続・状態リセットを実行します']

            # ── !resetlosses [SYMBOL] ──────────────────────────────────
            if cmd == 'resetlosses':
                sym = args[0].upper() if args else (symbol_ref[0] if symbol_ref else None)
                if not sym:
                    return ['シンボルを指定してください: `!resetlosses BTCUSD`']

                base = Path(cfg.get('BRIDGE', {}).get('status_file',
                    r'C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ea_state.json'))
                state_path = base.with_name(base.stem + f'_{sym}' + base.suffix)
                reset_path = base.with_name(f'ea_reset_{sym}' + base.suffix)

                try:
                    try:
                        ea = json.loads(state_path.read_text(encoding='ascii'))
                    except Exception:
                        ea = {}
                    prev = ea.get('consecutive_losses', 0)
                    reset_ts = int(datetime.now(_tz.utc).timestamp())
                    ea['consecutive_losses'] = 0
                    ea['reset_since'] = reset_ts
                    state_path.write_text(
                        json.dumps(ea, indent=2, ensure_ascii=False), encoding='ascii')
                    reset_path.write_text(
                        json.dumps({'reset_since': reset_ts, 'symbol': sym},
                                   indent=2, ensure_ascii=False), encoding='ascii')
                except Exception as e:
                    return [f'❌ リセット失敗 ({sym}): {e}']

                _logger.info(f'Discord [resetlosses] {sym} consecutive_losses: {prev} → 0')
                return [
                    f'✅ `{sym}` の連続損失カウントをリセットしました\n'
                    f'`consecutive_losses`: {prev} → **0**\n'
                    f'`{reset_path.name}` を作成しました'
                ]

            return [f'不明なコマンド: `!{cmd}`\n`!help` でコマンド一覧']

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = _ParamBot()
        try:
            loop.run_until_complete(client.start(token))
        except Exception:
            _logger.exception('Discord ボット 予期せぬ終了')
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name='discord-bot')
    t.start()
    _logger.info(f'Discord ボット スレッド起動 channel_id={chan_id}')
    return t
