"""bridge/dashboard.py — ポーリング状態のターミナルダッシュボード表示

各ポーリングの結果を色付き・セクション区切りで整形出力する。
ANSI エスケープコードを使用（Windows 10+ / Windows Terminal 対応）。
dashboard_mode=True の場合は毎回画面をクリアして上書き描画（live 表示）。
"""
from __future__ import annotations
import os
import re
import sys
from typing import Any

# ── ANSI カラー ──────────────────────────────────────────────────────────────
_USE_COLOR = (
    hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
    or os.environ.get('FORCE_COLOR', '')
)
# Windows で ANSI を有効化
if os.name == 'nt' and _USE_COLOR:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

# ANSI エスケープ除去用 (recent_logs の幅計算に使用)
_ANSI_RE = re.compile(r'\033\[[0-9;]*[mA-Za-z]')

def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return ''.join(codes) + str(text) + '\033[0m'


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


def activate_dashboard_mode() -> None:
    """ダッシュボードモードを強制有効化する。

    isatty() が False の環境（ランチャー経由・nohup 等）でも動作するよう
    _USE_COLOR を True に上書きし、Windows ANSI を再有効化する。
    run_bridge() の初期化時に一度だけ呼ぶこと。
    """
    global _USE_COLOR
    _USE_COLOR = True
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


_BOLD   = '\033[1m'
_DIM    = '\033[2m'
_RED    = '\033[91m'
_GREEN  = '\033[92m'
_YELLOW = '\033[93m'
_BLUE   = '\033[94m'
_MAGENTA = '\033[95m'
_CYAN   = '\033[96m'
_WHITE  = '\033[97m'
_BG_RED    = '\033[41m'
_BG_GREEN  = '\033[42m'
_BG_YELLOW = '\033[43m'

# ── ヘルパー ─────────────────────────────────────────────────────────────────

def _rsi_color(rsi: float) -> str:
    if rsi <= 25:
        return _RED + _BOLD
    if rsi <= 30:
        return _RED
    if rsi >= 75:
        return _RED + _BOLD
    if rsi >= 70:
        return _RED
    if 45 <= rsi <= 55:
        return _DIM
    return _WHITE


def _action_str(action: str) -> str:
    if action == 'buy':
        return _c(f' ▲ BUY  ', _BG_GREEN, _WHITE, _BOLD)
    if action == 'sell':
        return _c(f' ▼ SELL ', _BG_RED, _WHITE, _BOLD)
    return _c(' ─ NONE ', _DIM)


def _ok(flag: bool) -> str:
    return _c('✓', _GREEN) if flag else _c('✗', _RED)


def _sep(width: int = 60, char: str = '─') -> str:
    return _c(char * width, _DIM)


def _regime_color(regime: str) -> str:
    if 'trend_up' in regime:
        return _GREEN
    if 'trend_down' in regime:
        return _RED
    if 'weak' in regime:
        return _YELLOW
    return _CYAN   # range


def _pending_status(data: dict) -> str:
    """待機状態を日本語で返す。"""
    if data.get('scalp_buy_sma_pending'):
        return _c('BUY SMA20タッチ待ち', _YELLOW)
    if data.get('scalp_buy_confirm_pending'):
        n = data.get('scalp_buy_confirm_count', 0)
        return _c(f'BUY M1確認 {n}/1本', _YELLOW)
    if data.get('scalp_sell_sma_pending'):
        return _c('SELL SMA20タッチ待ち', _YELLOW)
    if data.get('scalp_sell_confirm_pending'):
        n = data.get('scalp_sell_confirm_count', 0)
        return _c(f'SELL M1確認 {n}/1本', _YELLOW)
    skip = data.get('skip_reason', '')
    if skip and skip.startswith('pending_'):
        return _c(skip, _DIM)
    return ''


# ── メイン出力 ───────────────────────────────────────────────────────────────

def print_poll_status(
    data: dict[str, Any],
    mode: str,
    itr: int,
    bal: Any,
    consec_losses: int,
    effective_cfg: dict | None = None,
    macro_state: Any = None,
    dashboard_mode: bool = False,
    recent_logs: list[str] | None = None,
) -> None:
    """ポーリング1回分の状態を整形して stdout に出力する。

    dashboard_mode=True の場合は画面をクリアして上書き描画する。
    recent_logs が指定されている場合は下部にログ行を表示する。
    """
    # ── ダッシュボードモード: 画面クリア ──────────────────────────────────
    if dashboard_mode:
        # \033[2J: 画面クリア  \033[H: カーソルを左上へ
        sys.stdout.write('\033[2J\033[H')
        sys.stdout.flush()

    eff  = effective_cfg or {}
    scalp_cfg = eff.get('SCALP', {})
    is_scalp  = (mode == 'scalp' and data.get('scalp_mode', True))

    sym    = data.get('symbol', '?')
    ts     = data.get('timestamp', '')[-8:]  # HH:MM:SS
    close  = data.get('close', 0.0)
    atr    = data.get('atr',   0.0)
    rsi_m5 = data.get('rsi_m5', 0.0)
    rsi_m1 = data.get('rsi_m1', 0.0)
    rsi_h1 = data.get('rsi_h1', 0.0)
    action = data.get('action', 'none')

    regime_h1 = data.get('regime_h1', '?')
    regime_m5 = data.get('regime_m5', '?')
    mtf_buy   = data.get('mtf_buy_ok',  False)
    mtf_sell  = data.get('mtf_sell_ok', False)

    adx_h1      = data.get('adx_h1', 0.0)
    adx_m5      = data.get('adx_m5', 0.0)
    di_plus_h1  = data.get('di_plus_h1',  0.0)
    di_minus_h1 = data.get('di_minus_h1', 0.0)
    di_plus_m5  = data.get('di_plus_m5',  0.0)
    di_minus_m5 = data.get('di_minus_m5', 0.0)
    sma20_m5    = data.get('sma20_m5', 0.0)
    sma20_m1    = data.get('sma20_m1', 0.0)
    sma20_slope_buy  = data.get('sma20_slope_buy_ok',  True)
    sma20_slope_sell = data.get('sma20_slope_sell_ok', True)
    cd_cycle    = data.get('trades_cd_cycle', 0)
    cd_trades   = data.get('cooldown_trades', 3)
    cd_rem      = data.get('scalp_cooldown_rem', 0)

    ws_blocked = data.get('ws_blocked', False)
    ws_ratio   = data.get('ws_ratio',   0.0)
    ext_os     = data.get('extreme_oversold',  False)
    ext_ob     = data.get('extreme_overbought', False)
    rvol       = data.get('rvol', 0.0)

    mode_tag = _c(f'[{mode.upper()}]', _CYAN, _BOLD)
    line1 = f" {_c(sym, _WHITE, _BOLD)} {mode_tag} #{itr}  {_c(ts + ' UTC', _DIM)}"

    print()
    print(_sep(64, '━'))
    print(line1)
    print(_sep(64, '─'))

    # ── 価格・指標行 ───────────────────────────────────────────────────────
    price_str = _c(f'${close:>12,.2f}', _WHITE, _BOLD)
    atr_str   = _c(f'ATR ${atr:.2f}', _DIM)
    rsi5_col  = _rsi_color(rsi_m5)
    rsi1_col  = _rsi_color(rsi_m1)
    rsi5_str  = _c(f'{rsi_m5:.1f}', rsi5_col)
    rsi1_str  = _c(f'{rsi_m1:.1f}', rsi1_col)
    # RVOL 色付き（< 1.5: dim、1.5-3.0: yellow、≥ 3.0: red bold）
    _rvol_col = (_RED + _BOLD if rvol >= 3.0 else
                 _YELLOW       if rvol >= 1.5 else _DIM)
    _rvol_str = _c(f'RVOL {rvol:.1f}', _rvol_col)
    print(f" {price_str}  {atr_str}  │  RSI_M5 {rsi5_str}  RSI_M1 {rsi1_str}  {_rvol_str}")

    # ── レジーム行 ─────────────────────────────────────────────────────────
    h1_str = _c(regime_h1, _regime_color(regime_h1))
    m5_str = _c(regime_m5, _regime_color(regime_m5))
    if is_scalp:
        _h1_di = _c(f"ADX{adx_h1:.0f} DI+{di_plus_h1:.0f}/DI-{di_minus_h1:.0f}", _DIM)
        print(f" H1 {h1_str}  {_h1_di}  │  MTF BUY {_ok(mtf_buy)}  SELL {_ok(mtf_sell)}")
        _m5_di = _c(f"ADX{adx_m5:.0f} DI+{di_plus_m5:.0f}/DI-{di_minus_m5:.0f}", _DIM)
        _sma20_dist = close - sma20_m5 if sma20_m5 > 0 else 0.0
        _sma20_str = _c(f'SMA20_M5 ${sma20_m5:,.0f}({_sma20_dist:+,.0f})', _DIM)
        _slope_str  = f'slope BUY{_ok(sma20_slope_buy)} SELL{_ok(sma20_slope_sell)}'
        print(f" M5 {m5_str}  {_m5_di}  │  {_sma20_str}  {_slope_str}")
        if sma20_m1 > 0:
            _sma20_m1_dist = close - sma20_m1
            _sma20_m1_str  = _c(f'SMA20_M1 ${sma20_m1:,.0f}({_sma20_m1_dist:+,.0f})', _DIM)
            print(f" M1 RSI {rsi1_str}  {_sma20_m1_str}")
    else:
        rsi_h1_str = _c(f'{rsi_h1:.1f}', _rsi_color(rsi_h1))
        print(f" H1 {h1_str}(ADX {adx_h1:.0f}) RSI {rsi_h1_str}  M5 {m5_str}")

    # ── 特殊状態行（WS・ExtRSI・EW2・VolBO） ─────────────────────────────
    flags: list[str] = []
    if ws_blocked:
        flags.append(_c(f'WS ブロック (ratio={ws_ratio:.1f})', _YELLOW, _BOLD))
    elif ws_ratio >= 1.5:
        flags.append(_c(f'WS ratio={ws_ratio:.1f}', _YELLOW))
    if ext_os:
        rsi_str = _c(f'RSI={rsi_m5:.1f}', _RED, _BOLD)
        flags.append(_c('⚠ 極端売られすぎ ', _RED, _BOLD) + rsi_str)
    if ext_ob:
        rsi_str = _c(f'RSI={rsi_m5:.1f}', _RED, _BOLD)
        flags.append(_c('⚠ 極端買われすぎ ', _RED, _BOLD) + rsi_str)
    sig_type = data.get('signal_type', '')
    if 'EW2' in sig_type:
        flags.append(_c(f'EW2 {sig_type}', _MAGENTA))
    if 'vol_bo' in sig_type:
        flags.append(_c(f'⚡ VOL-BO {sig_type}', _GREEN if 'up' in sig_type else _RED, _BOLD))
    if flags:
        print(f" {' │ '.join(flags)}")

    # ── H1 パターン ────────────────────────────────────────────────────────
    for pat in data.get('h1_patterns', []):
        ok_sym = _c('✓', _GREEN) if pat['confirmed'] else _c('…', _YELLOW)
        print(f" {_c('▣', _BLUE)} {pat['label']} {ok_sym}"
              f"  {pat['confidence']:.0%}"
              f"  NL=${pat['neckline']:,.0f}"
              f"  TP=${pat['target']:,.0f}"
              f"  ({pat['bars_ago']}本前)")

    # ── Elliott Wave 2 スキャン結果 ─────────────────────────────────────────
    def _ew2_line(label: str, e: dict | None, is_buy: bool) -> str:
        if e is None:
            return f" {_c('EW2', _MAGENTA)} {label} {_c('未検出', _DIM)}"
        w2   = e['w2_price']
        fib  = e['fib']
        wav  = e['wave1']
        div  = e['div']
        tp   = e['tp']
        sl   = e['sl']
        bago = e['bars_ago']
        traded_str = _c(' [済]', _DIM) if e.get('traded') else _c(' [新規]', _GREEN if is_buy else _RED)
        arrow = '▼' if is_buy else '▲'
        col   = _GREEN if is_buy else _RED
        w2_lbl = 'W2底' if is_buy else 'W2天'
        tp_lbl = 'TP↑' if is_buy else 'TP↓'
        sl_lbl = 'SL↓' if is_buy else 'SL↑'
        return (f" {_c('EW2', _MAGENTA)} {_c(arrow + label, col)}"
                f"  {w2_lbl}=${w2:,.0f}  Fib={fib:.1%}  Wave1=${wav:,.0f}"
                f"  div{div:+.1f}  {tp_lbl}=${tp:,.0f}  {sl_lbl}=${sl:,.0f}"
                f"  ({bago}本前){traded_str}")
    _ew2b_data = data.get('ew2_last_buy')
    _ew2s_data = data.get('ew2_last_sell')
    if _ew2b_data is not None or _ew2s_data is not None:
        print(_ew2_line('BUY',  _ew2b_data, True))
        print(_ew2_line('SELL', _ew2s_data, False))
    elif data.get('scalp_mode'):
        print(f" {_c('EW2', _MAGENTA)} {_c('BUY/SELL 未検出', _DIM)}")

    # ── マクロバイアス ──────────────────────────────────────────────────────
    _macro_min = (effective_cfg or {}).get('MACRO', {}).get('min_bias_to_show', 15)
    _mb_bias   = data.get('macro_bias', 0.0)
    _mb_label  = data.get('macro_bias_label', 'neutral')
    if macro_state is not None and macro_state.last_updated_at > 0:
        _mb_bias  = macro_state.bias
        _mb_label = macro_state.bias_label
    if abs(_mb_bias) >= _macro_min or _mb_label != 'neutral':
        _mb_col = (_GREEN if _mb_bias >= 50 else
                   _CYAN  if _mb_bias >= 15 else
                   _DIM   if abs(_mb_bias) < 15 else
                   _YELLOW if _mb_bias > -50 else _RED)
        _mb_str = _c(f'マクロ {_mb_bias:+.0f}[{_mb_label}]', _mb_col)
        _mb_extra = ''
        if macro_state is not None:
            if macro_state.nearest_nl is not None:
                _nl_col = _GREEN if macro_state.nl_dir == 'bullish' else _RED
                _mb_extra += f'  NL={_c(f"${macro_state.nearest_nl:,.0f}", _nl_col)}'
            if macro_state.target_up is not None:
                _mb_extra += f'  ↑TP=${macro_state.target_up:,.0f}'
            if macro_state.target_down is not None:
                _mb_extra += f'  ↓TP=${macro_state.target_down:,.0f}'
        print(f" {_mb_str}{_mb_extra}")

    print(_sep(64, '─'))

    # ── アクション行 ──────────────────────────────────────────────────────
    sl_price = data.get('sl_price', 0.0)
    tp_price = data.get('tp_price', 0.0)
    lot      = data.get('lot_size', 0.0)
    print(f" {_action_str(action)}  "
          f"SL ${sl_price:>10,.2f}  TP ${tp_price:>10,.2f}  lot {lot}")

    sig_type_str = sig_type if sig_type and sig_type != 'none' else ''
    if sig_type_str:
        print(f" signal: {_c(sig_type_str, _CYAN)}")

    # ── 待機・スキップ状態 ────────────────────────────────────────────────
    pending = _pending_status(data)
    skip    = data.get('skip_reason', '')
    if pending:
        print(f" {pending}")
    elif skip and not skip.startswith('pending_'):
        print(f" {_c('skip: ' + skip, _DIM)}")

    if is_scalp:
        ep_usd = data.get('expected_profit_usd', 0.0)
        ep_jpy = int(data.get('expected_profit_jpy', 0))
        tgt    = data.get('target_profit_jpy', 0)
        print(f" 期待収益 +${ep_usd:.2f}(¥{ep_jpy})  目標 ¥{tgt}")

    if not is_scalp:
        score    = data.get('score', 0)
        strength = data.get('strength', '')
        sig_val  = data.get('signal_valid_until', '')
        ep_win   = data.get('entry_in_window', 0)
        max_ep   = eff.get('REGIME', {}).get('max_entry_per_signal', 3)
        print(f" score {score} ({strength})"
              + (f"  window_until {sig_val}" if sig_val else '')
              + (f"  entry #{ep_win}/{max_ep}" if ep_win else ''))
        sell_sig = data.get('sell_signal_type', 'none')
        if sell_sig != 'none':
            print(f" SELL signal: {_c(sell_sig, _RED)}")
        if data.get('scalp_cooldown_rem', 0) > 0:
            _cd_rem = data['scalp_cooldown_rem']
            print(f" {_c(f'scalp cooldown 残{_cd_rem}分', _DIM)}")
        if data.get('skip_reason') and not is_scalp:
            print(f" skip: {_c(data['skip_reason'], _DIM)}")
        if data.get('sell_skip_reason'):
            print(f" sell_skip: {_c(data['sell_skip_reason'], _DIM)}")

    print(_sep(64, '─'))

    # ── ポジション・残高行 ─────────────────────────────────────────────────
    max_p   = data.get('max_positions',   3)
    total_p = data.get('total_positions', 0)
    avail   = data.get('available_slots', max_p)
    today   = data.get('trades_today',    0)
    max_day = scalp_cfg.get('max_trades_day', 20) if is_scalp else None
    bal_str = _c(f'¥{bal}', _WHITE) if bal != 'N/A' else _c('N/A', _DIM)
    pos_str = _c(f'{total_p}/{max_p}', _GREEN if avail > 0 else _RED)
    consec_str = (_c(str(consec_losses), _RED, _BOLD)
                  if consec_losses > 0 else _c('0', _DIM))
    if max_day is not None:
        _cd_prog = _c(f'CD:{cd_cycle}/{cd_trades}', _YELLOW if cd_rem > 0 else _DIM)
        _cd_rem_str = _c(f'(残{cd_rem}分)', _YELLOW) if cd_rem > 0 else ''
        trade_str = f'{today}/{max_day}回  {_cd_prog}{_cd_rem_str}  '
    else:
        trade_str = ''
    print(f" 今日 {trade_str}ポジ {pos_str}(空き{avail})"
          f"  残高 {bal_str}  連続損失 {consec_str}回")

    print(_sep(64, '━'))

    # ── 直近ログ（ダッシュボードモード専用） ──────────────────────────────
    if dashboard_mode and recent_logs:
        _shown = [l for l in recent_logs if l.strip()][-10:]
        if _shown:
            print(_c(' ─ recent log ' + '─' * 50, _DIM))
            for _line in _shown:
                _plain = _strip_ansi(_line)[:100]
                print(_c(f' {_plain}', _DIM))
