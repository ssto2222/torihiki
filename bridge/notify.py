"""bridge/notify.py — Discord 通知 + 一時停止フラグ"""
from __future__ import annotations
import os
import requests

try:
    from secret import DISCORD_WEBHOOK_URL
except ImportError:
    DISCORD_WEBHOOK_URL = ''


def send_discord(message: str) -> None:
    """Discord へ通知を送る"""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
    except Exception as e:
        print(f"Discord通知失敗: {e}")


def _build_discord_signal_msg(data: dict, mode: str) -> str:
    """シグナル変化時の Discord メッセージを組み立てる"""
    action = data.get('action', 'none')
    symbol = data.get('symbol', '')

    if action == 'buy':
        head = f'🟢 **[BUY 点灯]** {symbol}'
    elif action == 'sell':
        head = f'🔴 **[SELL 点灯]** {symbol}'
    else:
        head = f'⬜ **[シグナル消灯]** {symbol}'

    close  = data.get('close',  0)
    rsi_m5 = data.get('rsi_m5', 0)
    rsi_m1 = data.get('rsi_m1', 0)
    atr    = data.get('atr',    0)
    sl     = data.get('sl_price', 0)
    tp     = data.get('tp_price', 0)
    h1     = data.get('regime_h1', '?')
    m5r    = data.get('regime_m5', '?')

    lines = [
        head,
        f'close=${close:,.2f}  RSI_M5={rsi_m5:.1f}  RSI_M1={rsi_m1:.1f}  ATR=${atr:.2f}',
        f'SL=${sl:,.2f}  TP=${tp:,.2f}',
    ]

    if data.get('scalp_mode'):
        ep_usd = data.get('expected_profit_usd', 0)
        ep_jpy = int(data.get('expected_profit_jpy', 0))
        tgt    = data.get('target_profit_jpy', 0)
        lines.append(f'期待利益=+${ep_usd:.2f}(¥{ep_jpy})  target=¥{tgt}')

    lines.append(f'H1={h1}  M5={m5r}')

    if data.get('scalp_buy_sma_pending'):
        status = '[BUY] SMA20タッチ待ち'
    elif data.get('scalp_buy_confirm_pending'):
        status = f"[BUY] 確認 {data.get('scalp_buy_confirm_count', 0)}/1本"
    elif data.get('scalp_sell_sma_pending'):
        status = '[SELL] SMA20タッチ待ち'
    elif data.get('scalp_sell_confirm_pending'):
        status = f"[SELL] 確認 {data.get('scalp_sell_confirm_count', 0)}/1本"
    elif data.get('skip_reason'):
        status = f"skip={data['skip_reason']}"
    else:
        b = 'OK' if data.get('mtf_buy_ok',  False) else 'NG'
        s = 'OK' if data.get('mtf_sell_ok', False) else 'NG'
        status = f'[待機中] MTF:BUY={b} SELL={s}'
    lines.append(status)

    return '\n'.join(lines)


def _build_discord_hourly_msg(data: dict, macro_state=None) -> str:
    """1時間ごとのステータスサマリーを Discord メッセージとして組み立てる"""
    symbol    = data.get('symbol', '')
    close     = data.get('close',   0)
    atr       = data.get('atr',     0)
    rsi_m5    = data.get('rsi_m5',  0)
    rsi_m1    = data.get('rsi_m1',  0)
    regime_h1 = data.get('regime_h1', '?')
    regime_m5 = data.get('regime_m5', '?')
    action    = data.get('action', 'none')
    total_p   = data.get('total_positions', 0)
    max_p     = data.get('max_positions',   3)
    avail     = data.get('available_slots', max_p)
    today     = data.get('trades_today',    0)
    max_day   = data.get('cooldown_min',    20)
    skip      = data.get('skip_reason', '')
    mtf_b     = 'OK' if data.get('mtf_buy_ok',  False) else 'NG'
    mtf_s     = 'OK' if data.get('mtf_sell_ok', False) else 'NG'

    act_str = {'buy': '🟢 BUY', 'sell': '🔴 SELL'}.get(action, '⬜ 待機')
    lines = [
        f'📊 **[{symbol}] 1時間ステータス**',
        f'close=${close:,.2f}  RSI M5:{rsi_m5:.1f}  M1:{rsi_m1:.1f}  ATR:${atr:.2f}',
        f'レジーム: H1={regime_h1}  M5={regime_m5}  MTF:BUY={mtf_b}/SELL={mtf_s}',
        f'アクション: {act_str}' + (f'  skip={skip}' if skip else ''),
    ]

    # EW2 スキャン結果
    def _ew2_str(e: dict | None, direction: str) -> str:
        if e is None:
            return f'EW2 {direction}: 未検出'
        traded = '済' if e.get('traded') else '新規'
        return (f'EW2 {direction}: W2=${e["w2_price"]:,.0f}'
                f'  Fib={e["fib"]:.1%}  Wave1=${e["wave1"]:,.0f}'
                f'  div{e["div"]:+.1f}'
                f'  TP→${e["tp"]:,.0f}  SL→${e["sl"]:,.0f}'
                f'  ({e["bars_ago"]}本前)[{traded}]')
    lines.append(_ew2_str(data.get('ew2_last_buy'),  'BUY ▼'))
    lines.append(_ew2_str(data.get('ew2_last_sell'), 'SELL▲'))

    # ペンディング状態
    if data.get('scalp_buy_sma_pending'):
        lines.append('[BUY] SMA20タッチ待ち')
    elif data.get('scalp_buy_confirm_pending'):
        lines.append(f"[BUY] M1確認 {data.get('scalp_buy_confirm_count',0)}/1本")
    elif data.get('scalp_sell_sma_pending'):
        lines.append('[SELL] SMA20タッチ待ち')
    elif data.get('scalp_sell_confirm_pending'):
        lines.append(f"[SELL] M1確認 {data.get('scalp_sell_confirm_count',0)}/1本")

    # マクロバイアス
    if macro_state is not None and macro_state.last_updated_at > 0:
        mb = macro_state.bias
        mb_label = macro_state.bias_label
        lines.append(f'マクロ: {mb:+.0f}[{mb_label}]')

    # ポジション・取引回数
    lines.append(f'ポジション: {total_p}/{max_p}本(空き{avail})  今日: {today}回')

    return '\n'.join(lines)


def check_pause_signal(symbol: str, flag_file: str, *, mt5) -> bool:
    """毎ループ実行：スマホからの Buy Stop (Magic=0) を確認して一時停止フラグを管理する"""
    orders = mt5.orders_get(symbol=symbol)
    has_stop_order = False
    if orders:
        for o in orders:
            if o.magic == 0 and o.type == mt5.ORDER_TYPE_BUY_STOP:
                has_stop_order = True
                break

    if has_stop_order:
        if not os.path.exists(flag_file):
            with open(flag_file, "w") as f:
                f.write("paused")
            send_discord(f"⏸ **【一時停止】** {symbol} スマホからのBuy Stopを検知。待機します。")
        return True
    else:
        if os.path.exists(flag_file):
            os.remove(flag_file)
            send_discord(f"▶️ **【再開】** {symbol} 指値が削除されました。自動売買を再開します。")
        return False
