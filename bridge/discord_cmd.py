"""bridge/discord_cmd.py — Discord コマンドボット（ランタイムパラメータ制御）

バックグラウンドスレッドで asyncio ループを走らせる。
runner.py の run_bridge() から start_discord_bot(cfg) を呼ぶ。

secret.py に以下が必要（なければ起動をスキップ）:
    DISCORD_BOT_TOKEN      = "Bot token here"
    DISCORD_CMD_CHANNEL_ID = 123456789  # int

コマンド:
    !set <param> <value>  パラメータ変更  例: !set target 1500 / !set buy off
    !get [param]          現在値確認      省略で全パラメータ表示
    !params               !get のエイリアス
    !reset                全オーバーライド削除
    !help                 コマンド一覧
"""
from __future__ import annotations
import asyncio
import logging
import threading
from typing import Any

import discord

from bridge.param_override import (
    PARAMS, parse_value, set_override, clear_overrides, current_values_text,
)

_logger = logging.getLogger('torihiki')
_PREFIX = '!'


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


def _build_help() -> str:
    lines = ['**パラメータ制御コマンド一覧**', '```']
    lines.append('!set <param> <value>  パラメータを変更')
    lines.append('!get [param]          現在値を表示（省略で全件）')
    lines.append('!params               全パラメータ表示')
    lines.append('!reset                全オーバーライドを削除')
    lines.append('!help                 このヘルプ')
    lines.append('')
    lines.append('利用可能パラメータ:')
    for name, spec in PARAMS.items():
        type_str = {int: '整数', float: '小数', bool: 'on/off'}[spec.type_]
        rng = '' if spec.type_ is bool else f' [{spec.min_v}〜{spec.max_v}]'
        lines.append(f'  {name:<12} ({type_str}{rng})  {spec.desc}')
    lines.append('```')
    return '\n'.join(lines)


class _ParamBot(discord.Client):
    def __init__(self, channel_id: int, cfg_ref: dict[str, Any]) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self._channel_id = channel_id
        self._cfg_ref    = cfg_ref  # runner.py の元 cfg への参照（表示用）

    async def on_ready(self) -> None:
        _logger.info(f'Discord ボット起動: {self.user}')
        ch = self.get_channel(self._channel_id)
        if ch:
            await ch.send('【パラメータ制御ボット】起動しました。`!help` でコマンド一覧')

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return
        if message.channel.id != self._channel_id:
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
            reply = await self._dispatch(cmd, args)
        except Exception as e:
            reply = f'エラー: {e}'
            _logger.exception(f'Discord コマンド処理エラー: {content}')

        await message.channel.send(reply)

    async def _dispatch(self, cmd: str, args: list[str]) -> str:
        if cmd == 'help':
            return _build_help()

        if cmd in ('params', 'get') and not args:
            return current_values_text(self._cfg_ref)

        if cmd == 'get' and args:
            name = args[0].lower()
            spec = PARAMS.get(name)
            if spec is None:
                known = ', '.join(sorted(PARAMS))
                return f'不明なパラメータ: `{name}`\n使用可能: {known}'
            cur = self._cfg_ref.get(spec.section, {}).get(spec.key, '(未設定)')
            return f'`{name}` = {cur}  ({spec.desc})'

        if cmd == 'set':
            if len(args) < 2:
                return '使い方: `!set <param> <value>`'
            name = args[0].lower()
            raw  = args[1]
            val, err = parse_value(name, raw)
            if err:
                return err
            set_override(name, val)
            spec = PARAMS[name]
            _logger.info(f'Discord: {name} = {val}')
            return f'`{name}` を **{val}** に設定しました（{spec.desc}）\n次のポーリングから反映されます'

        if cmd == 'reset':
            clear_overrides()
            _logger.info('Discord: 全オーバーライドをリセット')
            return '全パラメータのオーバーライドを削除しました'

        return f'不明なコマンド: `!{cmd}`\n`!help` でコマンド一覧'


def start_discord_bot(cfg: dict[str, Any]) -> threading.Thread | None:
    """
    バックグラウンドスレッドで Discord ボットを起動する。
    secret.py に認証情報がなければ何もしない。
    cfg は表示用参照として渡す（runner.py の元 cfg dict）。
    """
    token, chan_id = _get_credentials()
    if not token:
        _logger.info('Discord ボット: secret.py に認証情報なし → スキップ')
        return None

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = _ParamBot(chan_id, cfg)
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
