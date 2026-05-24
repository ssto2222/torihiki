"""bridge/signal_scalp.py — スキャルプモード シグナル計算"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import numpy as np

from core.data       import fetch_ohlcv
from core.indicators import add_m5_indicators, add_m1_indicators, add_h1_indicators
from core.strategy   import (detect_big_move, detect_early_surge,
                              should_avoid_entry_during_surge, detect_whipsaw,
                              detect_elliott_w2_buy, detect_elliott_w2_sell,
                              detect_volume_breakout)
from core.patterns   import detect_all_patterns, PatternResult

from bridge.utils    import (_detect_regime, _regime_lot_multi,
                              _position_status, _get_jpy_per_usd,
                              _has_positions_in_direction,
                              detect_bidirectional_loss)
from bridge.signal_normal import compute_signal

if TYPE_CHECKING:
    from bridge.state import ScalpState, SignalState, JpyRateCache, Sma20TouchCache, MacroBiasState

_logger = logging.getLogger('torihiki')


def compute_scalp_signal(symbol: str, cfg: dict,
                         state: 'ScalpState',
                         sig_state: 'SignalState',
                         jpy_cache: 'JpyRateCache',
                         sma20_cache: 'Sma20TouchCache',
                         *, mt5,
                         macro_state: 'MacroBiasState | None' = None) -> dict | None:
    """
    スキャルプモード: M5 RSI がクロスしたら SMA20 タッチ → M1 確認バー 2 本でエントリー。
    state は ScalpState、sig_state は通常モードフォールバック用 SignalState。
    """
    try:
        scalp      = cfg.get('SCALP', {})
        jpy_rate   = _get_jpy_per_usd(jpy_cache, scalp.get('jpy_per_usd', 150.0), mt5=mt5)
        target     = scalp.get('target_profit_jpy',  300)
        sl_ratio   = scalp.get('sl_ratio',           1.5)
        sig_tf     = scalp.get('signal_tf',          'M5')
        buy_thrs   = sorted(scalp.get('rsi_buy_thrs',  [50.0, 55.0, 60.0]))
        sell_thrs  = sorted(scalp.get('rsi_sell_thrs', [45.0, 40.0, 35.0]), reverse=True)
        buy_enabled  = bool(scalp.get('buy_enabled',  True))
        sell_enabled = bool(scalp.get('sell_enabled', True))
        max_day    = scalp.get('max_trades_day',  20)
        cooldown   = scalp.get('cooldown_min',    30)

        # buy/sell が無効になった場合は対応する pending 状態をクリア
        if not buy_enabled and state.buy_sma_pending:
            state.buy_sma_pending = False
            state.buy_sma_at      = None
            state.buy_sma_level   = 0.0
        if not buy_enabled and state.buy_confirm_pending:
            state.buy_confirm_pending  = False
            state.buy_confirm_at       = None
            state.buy_confirm_count    = 0
            state.buy_confirm_bar_time = None
        if not sell_enabled and state.sell_sma_pending:
            state.sell_sma_pending = False
            state.sell_sma_at      = None
            state.sell_sma_level   = 0.0
        if not sell_enabled and state.sell_confirm_pending:
            state.sell_confirm_pending  = False
            state.sell_confirm_at       = None
            state.sell_confirm_count    = 0
            state.sell_confirm_bar_time = None

        now   = datetime.now(timezone.utc)
        today = now.date()

        if state.date != today:
            state.count = 0
            state.date  = today

        df_raw = fetch_ohlcv(symbol, sig_tf, 50)
        if df_raw is None:
            return None

        df = add_m5_indicators(df_raw, cfg)
        if df.empty:
            return None

        bar_time    = df.index[-1]
        bar_changed = (bar_time != state.last_bar_time)
        rsi_prev_bar = float(df['RSI'].iloc[-2]) if len(df) >= 2 else float(state.prev_rsi or 0.0)

        rsi_cur = float(df['RSI'].iloc[-1])
        close_v = float(df['Close'].iloc[-1])
        atr_v   = float(df['ATR'].iloc[-1])
        if atr_v <= 0 or np.isnan(atr_v):
            return None  # ATR 計算不能 → SL/TP が計算できないためスキップ

        # M1 データ取得
        df_m1_raw = fetch_ohlcv(symbol, 'M1', 50)
        df_m1 = None
        rsi_m1_cur = float('nan')
        if df_m1_raw is not None:
            df_m1 = add_m1_indicators(df_m1_raw, cfg)
            if not df_m1.empty:
                rsi_m1_cur = float(df_m1['RSI'].iloc[-1])

        # M1 RSI 追跡（BUY過熱: >65 / SELL過熱: <35）
        if not np.isnan(rsi_m1_cur):
            state.m1_rsi_above_65 = rsi_m1_cur > 65
            state.m1_rsi_below_35 = rsi_m1_cur < 35

        info          = mt5.symbol_info(symbol)
        contract_size = float(info.trade_contract_size) if info else 1.0
        l_min         = float(info.volume_min)          if info else 0.01
        l_max         = float(info.volume_max)          if info else 100.0
        l_step        = float(info.volume_step)         if info else 0.01

        # レジーム判定（M5 ADX）
        regime_cfg = cfg.get('REGIME', {})
        adx_m5_sv  = float(df['ADX'].iloc[-1])     if 'ADX'      in df.columns else float('nan')
        dip_m5_sv  = float(df['DI_plus'].iloc[-1]) if 'DI_plus'  in df.columns else float('nan')
        dim_m5_sv  = float(df['DI_minus'].iloc[-1])if 'DI_minus' in df.columns else float('nan')
        regime_m5s = _detect_regime(adx_m5_sv, dip_m5_sv, dim_m5_sv, regime_cfg)
        r_multi_s  = _regime_lot_multi('weak_trend', regime_m5s, regime_cfg)

        # H1 レジーム取得
        regime_h1s = 'weak_trend'
        dip_h1s    = float('nan')
        dim_h1s    = float('nan')
        # H1 パターン検知用に多めに取得 (200本 ≈ 8日)
        _h1_pats_raw: list[PatternResult] = []
        h1_pattern_bars: list = []
        df_h1_raw  = fetch_ohlcv(symbol, 'H1', 200)
        if df_h1_raw is not None:
            df_h1s = add_h1_indicators(df_h1_raw, cfg)
            if not df_h1s.empty:
                adx_h1s = float(df_h1s['ADX'].iloc[-1])     if 'ADX'      in df_h1s.columns else float('nan')
                dip_h1s = float(df_h1s['DI_plus'].iloc[-1]) if 'DI_plus'  in df_h1s.columns else float('nan')
                dim_h1s = float(df_h1s['DI_minus'].iloc[-1])if 'DI_minus' in df_h1s.columns else float('nan')
                regime_h1s = _detect_regime(adx_h1s, dip_h1s, dim_h1s, regime_cfg)
            # パターン検知 (H1 OHLC, 上位2件のみ, 例外は無視)
            try:
                _h1_pats_raw = detect_all_patterns(df_h1_raw, window=5, top_n=2)
                h1_pattern_bars = [
                    {'name': p.name, 'label': p.label, 'direction': p.direction,
                     'confidence': round(p.confidence, 3), 'neckline': p.neckline,
                     'target': p.target, 'confirmed': p.confirmed,
                     'bars_ago': p.bars_ago}
                    for p in _h1_pats_raw if p.confidence >= 0.40
                ]
            except Exception:
                pass

        # MTF SMA20 傾き + H1 レジーム条件
        # 案A: weak_trend も許可し DI 方向で判断、SMA20 チェックは M5 のみ
        _slope_bars = scalp.get('sma20_slope_bars', 5)
        _slope_thr  = scalp.get('sma20_slope_atr_thr', 0.10)

        def _sma20_ok(tfdf, direction: str) -> bool:
            if tfdf is None or tfdf.empty or 'SMA20' not in tfdf.columns:
                return True
            if len(tfdf) <= _slope_bars:
                return True
            atr_v_tf = float(tfdf['ATR'].iloc[-1]) if 'ATR' in tfdf.columns else float('nan')
            sma_now  = float(tfdf['SMA20'].iloc[-1])
            sma_prev = float(tfdf['SMA20'].iloc[-(_slope_bars + 1)])
            if np.isnan(sma_now) or np.isnan(sma_prev):
                return True
            slope = sma_now - sma_prev
            thr_v = (atr_v_tf * _slope_thr) if not np.isnan(atr_v_tf) else 0.0
            # BUY: 明確な下落でなければOK（上昇必須→フラット許容に緩和）
            # SELL: 明確な上昇でなければOK
            return slope > -thr_v if direction == 'buy' else slope < thr_v

        _di_valid    = not np.isnan(dip_h1s) and not np.isnan(dim_h1s)
        _h1_di_buy   = _di_valid and dip_h1s > dim_h1s   # DI+ > DI-
        _h1_di_sell  = _di_valid and dim_h1s > dip_h1s   # DI- > DI+

        # スキャルプはレンジ相場でも有効 → 'range' も許可
        # h1_di_filter=False(デフォルト): H1レジームのみで判断（DI不要）
        # h1_di_filter=True(厳格): DI方向も必須
        _use_di_filter = scalp.get('h1_di_filter', False)
        mtf_buy_ok  = (regime_h1s != 'trend_down' and
                       (not _use_di_filter or _h1_di_buy) and
                       _sma20_ok(df, 'buy'))
        mtf_sell_ok = (regime_h1s != 'trend_up' and
                       (not _use_di_filter or _h1_di_sell) and
                       _sma20_ok(df, 'sell'))
        # EW2 専用: W2底/天井形成中はM5 SMA20がまだ逆向きのため、スロープチェックを外す
        mtf_ew2_buy_ok  = (regime_h1s != 'trend_down' and (not _use_di_filter or _h1_di_buy))
        mtf_ew2_sell_ok = (regime_h1s != 'trend_up'   and (not _use_di_filter or _h1_di_sell))

        # ── ウィップソー（行ってこい相場）検出 ─────────────────────
        _ws_cfg       = cfg.get('WHIPSAW', {})
        _ws_block     = False
        _ws_ratio     = 0.0
        _is_whipsaw   = False
        _is_bidir     = False

        # ── 極端 RSI 状態の追跡（急落後反発BUY / 急騰後反落SELL） ─────────────
        _ext_os_rsi  = scalp.get('extreme_oversold_rsi',   25.0)
        _ext_ob_rsi  = scalp.get('extreme_overbought_rsi', 75.0)
        if rsi_cur <= _ext_os_rsi:
            state.extreme_oversold = True
        elif rsi_cur > 50.0:
            state.extreme_oversold = False      # RSI 正常圏復帰でクリア
        if rsi_cur >= _ext_ob_rsi:
            state.extreme_overbought = True
        elif rsi_cur < 50.0:
            state.extreme_overbought = False

        # 確定トレンド転換時に逆方向の待機状態をキャンセル
        # M5（短期）ではなく H1（中期）確定トレンドのみでキャンセル
        # → M5 一時的押し目で pending が消えるのを防ぐ
        if regime_h1s == 'trend_up' and (state.sell_sma_pending or state.sell_confirm_pending):
            state.sell_sma_pending      = False
            state.sell_sma_at           = None
            state.sell_confirm_pending  = False
            state.sell_confirm_at       = None
            state.sell_confirm_count    = 0
            state.sell_confirm_bar_time = None
        if regime_h1s == 'trend_down' and (state.buy_sma_pending or state.buy_confirm_pending):
            state.buy_sma_pending      = False
            state.buy_sma_at           = None
            state.buy_confirm_pending  = False
            state.buy_confirm_at       = None
            state.buy_confirm_count    = 0
            state.buy_confirm_bar_time = None

        target_usd    = target / jpy_rate
        _tp_frac_cfg  = scalp.get('tp_atr_fraction', 0.5)
        tp_atr_frac   = (_tp_frac_cfg.get(symbol, 0.5) if isinstance(_tp_frac_cfg, dict) else _tp_frac_cfg)
        tp_move       = atr_v * tp_atr_frac
        sl_move       = tp_move * sl_ratio
        lot_raw       = target_usd / (tp_move * contract_size) if tp_move > 0 else 0
        _lot_max_cfg  = scalp.get('lot_max', {})
        scalp_lot_max = (float(_lot_max_cfg.get(symbol, float('inf')))
                         if isinstance(_lot_max_cfg, dict)
                         else float(_lot_max_cfg))
        lot_base_s    = max(l_min, min(l_max, round(lot_raw / l_step) * l_step))
        lot           = max(l_min, min(l_max, scalp_lot_max,
                            round(lot_base_s * r_multi_s / l_step) * l_step))

        expected_profit_usd = tp_move * contract_size * lot
        expected_profit_jpy = expected_profit_usd * jpy_rate

        # ── 大変動検知: スキャルプ→通常モード自動切換え ──────────
        bm_lookback  = scalp.get('big_move_lookback',  12)
        bm_atr_multi = scalp.get('big_move_atr_multi', 2.0)
        big_move     = detect_big_move(df, bm_lookback, bm_atr_multi)

        if big_move != 'none':
            normal_data = compute_signal(symbol, cfg, sig_state, jpy_cache, mt5=mt5)
            if normal_data is not None:
                state.buy_sma_pending = state.sell_sma_pending = False
                state.buy_sma_at      = state.sell_sma_at      = None
                state.buy_confirm_pending = state.sell_confirm_pending = False
                state.buy_confirm_at      = state.sell_confirm_at      = None
                state.buy_confirm_count   = state.sell_confirm_count   = 0
                state.buy_confirm_bar_time = state.sell_confirm_bar_time = None
                position_aligns = (
                    (big_move == 'up'   and state.last_action == 'buy') or
                    (big_move == 'down' and state.last_action == 'sell')
                )
                if position_aligns:
                    normal_data['trail_multi'] = cfg['SL']['trail_multi']
                normal_data['signal_type'] = f'big_move_{big_move}(was_scalp)'
                normal_data['scalp_mode']  = False
                print(f"[スキャルプ→通常] 大変動={big_move}  "
                      f"last_pos={state.last_action}  "
                      f"trail={'ON' if position_aligns else 'scalp_trail=0'}")
                return normal_data
            _logger.warning("大変動検知中に compute_signal 失敗 → スキャルプスキップ")
            return None

        # ── クールダウン中は通常モードに切換え ─────────────────────
        in_cooldown = (
            state.last_at is not None and
            now < state.last_at + timedelta(minutes=cooldown)
        )
        if in_cooldown:
            normal_data = compute_signal(symbol, cfg, sig_state, jpy_cache, mt5=mt5)
            if normal_data is not None:
                rem = int((state.last_at + timedelta(minutes=cooldown) - now).total_seconds() / 60)
                normal_data['scalp_cooldown_rem'] = rem
                normal_data['scalp_mode']         = False
                return normal_data
            # compute_signal 失敗時はスキャルプロジックにフォールバックし
            # line 488 のクールダウンガードが action='none' を保証する
            _logger.warning("クールダウン中に compute_signal 失敗 → スキャルプロジックにフォールバック")

        # ── 急騰初期検知と中段階回避 ─────────────────────────────
        surge_info = detect_early_surge(df, cfg)

        # BUY 専用: RSI が高すぎる（急騰中段階）はエントリー見送り
        avoid_buy_surge = (should_avoid_entry_during_surge(df, cfg)
                           and not surge_info['is_early_surge']
                           and not in_cooldown)
        if avoid_buy_surge:
            print(f"[急騰回避] RSI={rsi_cur:.1f} が高すぎる → BUY見送り")

        # SELL 専用: RSI が低すぎる（売られすぎ）はエントリー見送り
        sell_oversold_thr = cfg.get('INDICATOR', {}).get('surge_oversold_threshold', 30.0)
        avoid_sell_surge  = rsi_cur < sell_oversold_thr and not in_cooldown
        if avoid_sell_surge:
            print(f"[売られすぎ回避] RSI={rsi_cur:.1f} が低すぎる → SELL見送り")

        # ── M1 待機ロジック ────────────────────────────────────────
        confirmed_signal  = None
        candidate_signal  = None
        crossed_level     = 0.0
        _direct_confirmed = False  # EW2/極端RSI/H1パターン由来: M5レジームチェックをスキップ

        # ── H1 パターン ネックライン突破: スキャルプ直接エントリー ─────────────
        if df_h1_raw is not None and len(df_h1_raw) >= 2 and _h1_pats_raw:
            _prev_h1_close_s = float(df_h1_raw['Close'].iloc[-2])
            _close_h1_cur_s  = float(df_h1_raw['Close'].iloc[-1])
            if len(state.pattern_traded) > 120:
                state.pattern_traded.clear()
            for _pat_s in _h1_pats_raw:
                if _pat_s.confidence < 0.45:
                    continue
                _fp_s = (_pat_s.name, round(_pat_s.neckline, 0))
                if _fp_s in state.pattern_traded:
                    continue
                if _pat_s.direction == 'bullish' and buy_enabled and not avoid_buy_surge:
                    if _prev_h1_close_s <= _pat_s.neckline < _close_h1_cur_s:
                        confirmed_signal  = 'buy'
                        crossed_level     = _pat_s.neckline
                        _direct_confirmed = True
                        state.pattern_traded.add(_fp_s)
                        state.pattern_tp_target = _pat_s.target
                        _logger.info(f'[スキャルプパターンBUY] {_pat_s.label} '
                                     f'NL={_pat_s.neckline:,.2f} 信頼度={_pat_s.confidence:.0%}')
                        break
                elif _pat_s.direction == 'bearish' and sell_enabled and not avoid_sell_surge:
                    if _prev_h1_close_s >= _pat_s.neckline > _close_h1_cur_s:
                        confirmed_signal  = 'sell'
                        crossed_level     = _pat_s.neckline
                        _direct_confirmed = True
                        state.pattern_traded.add(_fp_s)
                        state.pattern_tp_target = _pat_s.target
                        _logger.info(f'[スキャルプパターンSELL] {_pat_s.label} '
                                     f'NL={_pat_s.neckline:,.2f} 信頼度={_pat_s.confidence:.0%}')
                        break

        # ── Elliott Wave2 エントリー ────────────────────────────────────
        _ew2_signal_type = None
        _ew2_tp_price    = None   # Fibonacci extension TP（確定後に上書き）
        _ew2_sl_price    = None   # W2 失効ライン SL
        _ew_cfg = cfg.get('ELLIOTT', {})
        if (_ew_cfg.get('enabled', True)
                and confirmed_signal is None
                and not (state.buy_sma_pending or state.buy_confirm_pending
                         or state.sell_sma_pending or state.sell_confirm_pending)
                and not _ws_block):
            _ew_lb   = _ew_cfg.get('lookback_bars', 40)
            _ew_sw   = _ew_cfg.get('sw_window', 3)
            _ew_fmin = _ew_cfg.get('fib_min', 0.382)
            _ew_fmax = _ew_cfg.get('fib_max', 0.786)
            _ew_w1at = _ew_cfg.get('min_wave1_atr', 1.5)
            _ew_div  = _ew_cfg.get('rsi_div_min', 3.0)
            _ew_bago = _ew_cfg.get('w2_bars_ago_max', 5)
            if buy_enabled and not avoid_buy_surge and mtf_ew2_buy_ok:
                _ew2b = detect_elliott_w2_buy(
                    df, lookback=_ew_lb, sw_window=_ew_sw,
                    fib_min=_ew_fmin, fib_max=_ew_fmax,
                    min_wave1_atr=_ew_w1at, rsi_div_min=_ew_div,
                    w2_rsi_max=_ew_cfg.get('w2_buy_rsi_max', 45.0),
                    w2_bars_ago_max=_ew_bago,
                )
                if _ew2b is not None:
                    _fp_ew2 = ('ew2_buy', round(_ew2b['w2_low'], 0))
                    if len(state.ew2_traded) > 120:
                        state.ew2_traded.clear()
                    _ew2_already = _fp_ew2 in state.ew2_traded
                    _ew2_fib_ext = _ew_cfg.get('fib_tp_ext', 1.618)
                    _ew2_sl_buf  = _ew_cfg.get('sl_buffer_atr', 0.3)
                    _b_tp = _ew2b['w2_low'] + _ew2b['wave1_size'] * _ew2_fib_ext
                    _b_sl = _ew2b['w2_low'] - atr_v * _ew2_sl_buf
                    state.ew2_last_buy = {
                        'w2_price': _ew2b['w2_low'],
                        'fib':      _ew2b['fib_level'],
                        'div':      _ew2b['rsi_div'],
                        'wave1':    _ew2b['wave1_size'],
                        'bars_ago': _ew2b['w2_bars_ago'],
                        'tp':       _b_tp,
                        'sl':       _b_sl,
                        'traded':   _ew2_already,
                    }
                    if not _ew2_already:
                        state.ew2_traded.add(_fp_ew2)
                        confirmed_signal  = 'buy'
                        crossed_level     = _ew2b['w2_low']
                        _direct_confirmed = True
                        _ew2_signal_type  = (f"EW2_buy_fib{_ew2b['fib_level']:.2f}"
                                             f"_div{_ew2b['rsi_div']:.1f}")
                        _ew2_tp_price = _b_tp
                        _ew2_sl_price = _b_sl
                        _logger.info(f'[EW2-BUY] {symbol} W2底={_ew2b["w2_low"]:,.2f} '
                                     f'Fib={_ew2b["fib_level"]:.1%} '
                                     f'RSI_div={_ew2b["rsi_div"]:.1f} '
                                     f'TP={_b_tp:,.2f} SL={_b_sl:,.2f}')
                else:
                    state.ew2_last_buy = None
            if confirmed_signal is None and sell_enabled and not avoid_sell_surge and mtf_ew2_sell_ok:
                _ew2s = detect_elliott_w2_sell(
                    df, lookback=_ew_lb, sw_window=_ew_sw,
                    fib_min=_ew_fmin, fib_max=_ew_fmax,
                    min_wave1_atr=_ew_w1at, rsi_div_min=_ew_div,
                    w2_rsi_min=_ew_cfg.get('w2_sell_rsi_min', 55.0),
                    w2_bars_ago_max=_ew_bago,
                )
                if _ew2s is not None:
                    _fp_ew2 = ('ew2_sell', round(_ew2s['w2_high'], 0))
                    if len(state.ew2_traded) > 120:
                        state.ew2_traded.clear()
                    _ew2_already = _fp_ew2 in state.ew2_traded
                    _ew2_fib_ext = _ew_cfg.get('fib_tp_ext', 1.618)
                    _ew2_sl_buf  = _ew_cfg.get('sl_buffer_atr', 0.3)
                    _s_tp = _ew2s['w2_high'] - _ew2s['wave1_size'] * _ew2_fib_ext
                    _s_sl = _ew2s['w2_high'] + atr_v * _ew2_sl_buf
                    state.ew2_last_sell = {
                        'w2_price': _ew2s['w2_high'],
                        'fib':      _ew2s['fib_level'],
                        'div':      _ew2s['rsi_div'],
                        'wave1':    _ew2s['wave1_size'],
                        'bars_ago': _ew2s['w2_bars_ago'],
                        'tp':       _s_tp,
                        'sl':       _s_sl,
                        'traded':   _ew2_already,
                    }
                    if not _ew2_already:
                        state.ew2_traded.add(_fp_ew2)
                        confirmed_signal  = 'sell'
                        crossed_level     = _ew2s['w2_high']
                        _direct_confirmed = True
                        _ew2_signal_type  = (f"EW2_sell_fib{_ew2s['fib_level']:.2f}"
                                             f"_div{_ew2s['rsi_div']:.1f}")
                        _ew2_tp_price = _s_tp
                        _ew2_sl_price = _s_sl
                        _logger.info(f'[EW2-SELL] {symbol} W2天井={_ew2s["w2_high"]:,.2f} '
                                     f'Fib={_ew2s["fib_level"]:.1%} '
                                     f'RSI_div={_ew2s["rsi_div"]:.1f} '
                                     f'TP={_s_tp:,.2f} SL={_s_sl:,.2f}')
                else:
                    state.ew2_last_sell = None

        # ── 極端売られすぎ/買われすぎ 反発・反落シグナル ───────────────────────
        # 急落でRSI≤25まで下落後、RSIが回復閾値を上抜けた瞬間に直接BUYエントリー
        # SMA20タッチ不要（価格がSMA20から大きく乖離しているため）
        _ext_buy_thr  = scalp.get('extreme_os_buy_thr',  33.0)
        _ext_sell_thr = scalp.get('extreme_ob_sell_thr', 67.0)
        if (confirmed_signal is None and not _ws_block
                and state.extreme_oversold and buy_enabled and not avoid_buy_surge
                and not (state.buy_sma_pending or state.buy_confirm_pending)):
            if rsi_prev_bar <= _ext_buy_thr < rsi_cur and regime_h1s != 'trend_down':
                confirmed_signal       = 'buy'
                crossed_level          = _ext_buy_thr
                _direct_confirmed      = True
                _ew2_signal_type       = f'extreme_os_bounce_{int(_ext_buy_thr)}'
                state.extreme_oversold = False
                print(f"[極端売られすぎ反発BUY] RSI {rsi_prev_bar:.1f}→{rsi_cur:.1f}  閾値={_ext_buy_thr}")
                _logger.info(f'[ExtOS-BUY] {symbol} RSI {rsi_prev_bar:.1f}→{rsi_cur:.1f}')
        if (confirmed_signal is None and not _ws_block
                and state.extreme_overbought and sell_enabled and not avoid_sell_surge
                and not (state.sell_sma_pending or state.sell_confirm_pending)):
            if rsi_prev_bar >= _ext_sell_thr > rsi_cur and regime_h1s != 'trend_up':
                confirmed_signal         = 'sell'
                crossed_level            = _ext_sell_thr
                _direct_confirmed        = True
                _ew2_signal_type         = f'extreme_ob_bounce_{int(_ext_sell_thr)}'
                state.extreme_overbought = False
                print(f"[極端買われすぎ反落SELL] RSI {rsi_prev_bar:.1f}→{rsi_cur:.1f}  閾値={_ext_sell_thr}")
                _logger.info(f'[ExtOB-SELL] {symbol} RSI {rsi_prev_bar:.1f}→{rsi_cur:.1f}')

        # ── SMA20 タッチマージン事前計算 ─────────────────────────────────────────
        # キャッシュあり → キャッシュ値、なし → M5 ATR × 0.15 (BTCで約$150、動的に適正化)
        _touch_atr_frac = scalp.get('sma20_touch_margin_atr', 0.15)
        _cached_margin  = sma20_cache.margins.get(symbol, None)
        touch_margin    = (_cached_margin if _cached_margin is not None
                           else atr_v * _touch_atr_frac)

        # TP/SL ブレイクアウト倍率（デフォルト 1.0 = 変更なし）
        _vb_tp_multi = 1.0
        _vb_sl_multi = 1.0

        # ── ボリュームブレイクアウト: 出来高急増 + 方向性確認でSMA20チェーンをスキップ ──
        # RVOL ≥ threshold + ローソク足が方向性を持つ（騙しフィルター: 実体/レンジ比率）
        if (scalp.get('vol_bo_enabled', True) and confirmed_signal is None
                and not _ws_block and 'RVOL' in df.columns):
            _vol_bo = detect_volume_breakout(df, cfg)
            _vb_rsi_buy_min  = scalp.get('vol_bo_rsi_buy_min',  52.0)
            _vb_rsi_sell_max = scalp.get('vol_bo_rsi_sell_max', 48.0)
            if (_vol_bo['direction'] == 'up'
                    and buy_enabled and not avoid_buy_surge
                    and mtf_buy_ok
                    and rsi_cur >= _vb_rsi_buy_min
                    and bar_time != state.vol_breakout_bar):
                state.vol_breakout_bar = bar_time
                state.vol_breakout_dir = 'up'
                confirmed_signal = 'buy'
                crossed_level    = close_v
                _ew2_signal_type = f'vol_bo_up_rvol{_vol_bo["rvol"]:.1f}'
                _vb_tp_multi = scalp.get('vol_bo_tp_multi', 1.8)
                _vb_sl_multi = scalp.get('vol_bo_sl_multi', 0.8)
                _logger.info(f'[VOL-BO-BUY] {symbol} RVOL={_vol_bo["rvol"]:.1f} '
                             f'body={_vol_bo["body_ratio"]:.2f} RSI={rsi_cur:.1f}')
            elif (_vol_bo['direction'] == 'down'
                    and sell_enabled and not avoid_sell_surge
                    and mtf_sell_ok
                    and rsi_cur <= _vb_rsi_sell_max
                    and bar_time != state.vol_breakout_bar):
                state.vol_breakout_bar = bar_time
                state.vol_breakout_dir = 'down'
                confirmed_signal = 'sell'
                crossed_level    = close_v
                _ew2_signal_type = f'vol_bo_down_rvol{_vol_bo["rvol"]:.1f}'
                _vb_tp_multi = scalp.get('vol_bo_tp_multi', 1.8)
                _vb_sl_multi = scalp.get('vol_bo_sl_multi', 0.8)
                _logger.info(f'[VOL-BO-SELL] {symbol} RVOL={_vol_bo["rvol"]:.1f} '
                             f'body={_vol_bo["body_ratio"]:.2f} RSI={rsi_cur:.1f}')

        # M1 早期執行: M5 RSI が閾値に接近中かつ M1 が先行クロス
        m1_early_margin = scalp.get('m1_early_margin', 2.0)
        if (confirmed_signal is None
                and df_m1 is not None and len(df_m1) >= 2 and m1_early_margin > 0):
            rsi_m1_prev2 = float(df_m1['RSI'].iloc[-2])
            if buy_enabled and not state.buy_sma_pending and not avoid_buy_surge:
                for thr in buy_thrs:
                    if thr - m1_early_margin <= rsi_cur <= thr:
                        if rsi_m1_cur > thr and rsi_m1_prev2 <= thr:
                            if ((not surge_info['is_early_surge'] or surge_info['confidence'] >= 0.3)
                                    and mtf_buy_ok):
                                state.buy_sma_pending  = True
                                state.buy_sma_at       = now
                                state.buy_sma_level    = thr
                                state.sell_sma_pending     = False
                                state.sell_confirm_pending = False
                                label = '急騰兆候BUY' if surge_info['is_early_surge'] else '押し目BUY'
                                print(f"[{label}] Confidence={surge_info['confidence']:.2f}")
                            break
            if confirmed_signal is None and sell_enabled and not state.sell_sma_pending and not avoid_sell_surge:
                for thr in sell_thrs:
                    if thr <= rsi_cur <= thr + m1_early_margin:
                        if rsi_m1_cur < thr and rsi_m1_prev2 >= thr:
                            if mtf_sell_ok:
                                state.sell_sma_pending  = True
                                state.sell_sma_at       = now
                                state.sell_sma_level    = thr
                                state.buy_sma_pending     = False
                                state.buy_confirm_pending = False
                                print(f"[押し戻りSELL] M1 RSI={rsi_m1_cur:.1f} thr={int(thr)}")
                            break

        # SELL SMA20 タッチ待ち
        if confirmed_signal is None and state.sell_sma_pending:
            if not sell_enabled:
                state.sell_sma_pending = False
                state.sell_sma_at      = None
            else:
                timeout_min = 30
                sma20_m1 = (float(df_m1['SMA20'].iloc[-1])
                            if (df_m1 is not None and 'SMA20' in df_m1.columns and not df_m1.empty)
                            else float('nan'))
                if (state.sell_sma_at is not None and
                        (now - state.sell_sma_at).total_seconds() > timeout_min * 60):
                    state.sell_sma_pending = False
                    state.sell_sma_at      = None
                elif not np.isnan(sma20_m1):
                    close_m1 = float(df_m1['Close'].iloc[-1]) if (df_m1 is not None and not df_m1.empty) else close_v
                    # ── SMA20 バイパス: 急落で価格が大きく乖離 → タッチ不要で確認フェーズへ ──
                    _sell_bp  = scalp.get('sell_sma_bypass_atr', 2.0)
                    if _sell_bp > 0 and close_m1 < sma20_m1 - atr_v * _sell_bp:
                        if mtf_sell_ok:
                            state.sell_sma_pending      = False
                            state.sell_sma_at           = None
                            state.sell_confirm_pending  = True
                            state.sell_confirm_at       = now
                            state.sell_confirm_count    = 0
                            state.sell_confirm_bar_time = None
                            state.sell_confirm_level    = state.sell_sma_level
                            print(f"[SELL SMA20バイパス] 乖離={sma20_m1-close_m1:.0f} > ATR×{_sell_bp}={atr_v*_sell_bp:.0f}")
                        else:
                            state.sell_sma_pending = False
                            state.sell_sma_at      = None
                    elif abs(close_m1 - sma20_m1) <= touch_margin:
                        slope_bars = scalp.get('sma20_slope_bars', 5)
                        slope_thr  = scalp.get('sma20_slope_atr_thr', 0.10)
                        atr_m1_v   = float(df_m1['ATR'].iloc[-1]) if ('ATR' in df_m1.columns and len(df_m1) > slope_bars) else float('nan')
                        sma20_prev = float(df_m1['SMA20'].iloc[-(slope_bars + 1)]) if len(df_m1) > slope_bars else float('nan')
                        # 明確な上昇中でなければ OK（上昇は SELL に不利なので弱い上昇も許可）
                        sma20_slope_ok = (
                            np.isnan(atr_m1_v) or np.isnan(sma20_prev) or
                            (sma20_m1 - sma20_prev) < (atr_m1_v * slope_thr)
                        )
                        if sma20_slope_ok:
                            if mtf_sell_ok:
                                state.sell_sma_pending      = False
                                state.sell_sma_at           = None
                                state.sell_confirm_pending  = True
                                state.sell_confirm_at       = now
                                state.sell_confirm_count    = 0
                                state.sell_confirm_bar_time = None
                                state.sell_confirm_level    = state.sell_sma_level
                            else:
                                state.sell_sma_pending = False
                                state.sell_sma_at      = None

        # SELL 下落確認: SMA20タッチ後 M1 下落バー 2 本
        if confirmed_signal is None and state.sell_confirm_pending:
            timeout_min = 30
            if (state.sell_confirm_at is not None and
                    (now - state.sell_confirm_at).total_seconds() > timeout_min * 60):
                state.sell_confirm_pending  = False
                state.sell_confirm_at       = None
                state.sell_confirm_count    = 0
                state.sell_confirm_bar_time = None
            elif df_m1 is not None and not df_m1.empty and len(df_m1) >= 2:
                m1_bar_cur   = df_m1.index[-1]
                close_m1_cur = float(df_m1['Close'].iloc[-1])
                close_m1_prv = float(df_m1['Close'].iloc[-2])
                sma20_m1_c   = (float(df_m1['SMA20'].iloc[-1])
                                if 'SMA20' in df_m1.columns else float('nan'))
                is_down_bar  = close_m1_cur < close_m1_prv
                # M1 上昇トレンド中（close > SMA20）の下落バーはカウントしない
                trend_ok     = np.isnan(sma20_m1_c) or close_m1_cur <= sma20_m1_c

                if is_down_bar and trend_ok and m1_bar_cur != state.sell_confirm_bar_time:
                    state.sell_confirm_count   += 1
                    state.sell_confirm_bar_time = m1_bar_cur
                elif not is_down_bar or not trend_ok:
                    state.sell_confirm_count    = 0
                    state.sell_confirm_bar_time = None

                if state.sell_confirm_count >= 2:
                    confirmed_signal            = 'sell'
                    crossed_level               = state.sell_confirm_level
                    state.sell_confirm_pending  = False
                    state.sell_confirm_at       = None
                    state.sell_confirm_count    = 0
                    state.sell_confirm_bar_time = None

        # BUY SMA20 タッチ待ち
        if confirmed_signal is None and state.buy_sma_pending:
            if not buy_enabled:
                state.buy_sma_pending = False
                state.buy_sma_at      = None
            else:
                timeout_min = 30
                sma20_m1 = (float(df_m1['SMA20'].iloc[-1])
                            if (df_m1 is not None and 'SMA20' in df_m1.columns and not df_m1.empty)
                            else float('nan'))
                if (state.buy_sma_at is not None and
                        (now - state.buy_sma_at).total_seconds() > timeout_min * 60):
                    state.buy_sma_pending = False
                    state.buy_sma_at      = None
                elif not np.isnan(sma20_m1):
                    close_m1 = float(df_m1['Close'].iloc[-1]) if (df_m1 is not None and not df_m1.empty) else close_v
                    # ── SMA20 バイパス: 急騰で価格が大きく乖離 → タッチ不要で確認フェーズへ ──
                    _buy_bp = scalp.get('buy_sma_bypass_atr', 2.0)
                    if _buy_bp > 0 and close_m1 > sma20_m1 + atr_v * _buy_bp:
                        if mtf_buy_ok:
                            state.buy_sma_pending      = False
                            state.buy_sma_at           = None
                            state.buy_confirm_pending  = True
                            state.buy_confirm_at       = now
                            state.buy_confirm_count    = 0
                            state.buy_confirm_bar_time = None
                            state.buy_confirm_level    = state.buy_sma_level
                            print(f"[BUY SMA20バイパス] 乖離={close_m1-sma20_m1:.0f} > ATR×{_buy_bp}={atr_v*_buy_bp:.0f}")
                        else:
                            state.buy_sma_pending = False
                            state.buy_sma_at      = None
                    elif abs(close_m1 - sma20_m1) <= touch_margin:
                        slope_bars = scalp.get('sma20_slope_bars', 5)
                        slope_thr  = scalp.get('sma20_slope_atr_thr', 0.10)
                        atr_m1_v   = float(df_m1['ATR'].iloc[-1]) if ('ATR' in df_m1.columns and len(df_m1) > slope_bars) else float('nan')
                        sma20_prev = float(df_m1['SMA20'].iloc[-(slope_bars + 1)]) if len(df_m1) > slope_bars else float('nan')
                        # 明確な下落中でなければ OK（フラット・上昇中は BUY を許可）
                        sma20_slope_ok = (
                            np.isnan(atr_m1_v) or np.isnan(sma20_prev) or
                            (sma20_m1 - sma20_prev) > -(atr_m1_v * slope_thr)
                        )
                        if sma20_slope_ok:
                            if mtf_buy_ok:
                                state.buy_sma_pending      = False
                                state.buy_sma_at           = None
                                state.buy_confirm_pending  = True
                                state.buy_confirm_at       = now
                                state.buy_confirm_count    = 0
                                state.buy_confirm_bar_time = None
                                state.buy_confirm_level    = state.buy_sma_level
                            else:
                                state.buy_sma_pending = False
                                state.buy_sma_at      = None

        # BUY 上昇確認: SMA20タッチ後 M1 上昇バー 2 本
        if confirmed_signal is None and state.buy_confirm_pending:
            timeout_min = 30
            if (state.buy_confirm_at is not None and
                    (now - state.buy_confirm_at).total_seconds() > timeout_min * 60):
                state.buy_confirm_pending  = False
                state.buy_confirm_at       = None
                state.buy_confirm_count    = 0
                state.buy_confirm_bar_time = None
            elif df_m1 is not None and not df_m1.empty and len(df_m1) >= 2:
                m1_bar_cur   = df_m1.index[-1]
                close_m1_cur = float(df_m1['Close'].iloc[-1])
                close_m1_prv = float(df_m1['Close'].iloc[-2])
                is_up_bar    = close_m1_cur > close_m1_prv
                sma20_m1_c   = (float(df_m1['SMA20'].iloc[-1])
                                if 'SMA20' in df_m1.columns else float('nan'))
                # M1 下落トレンド中（close < SMA20）の上昇バーはカウントしない
                trend_ok     = np.isnan(sma20_m1_c) or close_m1_cur >= sma20_m1_c

                if is_up_bar and trend_ok and m1_bar_cur != state.buy_confirm_bar_time:
                    state.buy_confirm_count   += 1
                    state.buy_confirm_bar_time = m1_bar_cur
                elif not is_up_bar or not trend_ok:
                    state.buy_confirm_count    = 0
                    state.buy_confirm_bar_time = None

                if state.buy_confirm_count >= 2:
                    confirmed_signal           = 'buy'
                    crossed_level              = state.buy_confirm_level
                    state.buy_confirm_pending  = False
                    state.buy_confirm_at       = None
                    state.buy_confirm_count    = 0
                    state.buy_confirm_bar_time = None

        # M5 新規クロス検出（既存の待機状態がない場合のみ）
        if confirmed_signal is None and state.prev_rsi is not None:
            if (buy_enabled
                    and not (state.buy_sma_pending or state.buy_confirm_pending)
                    and not avoid_buy_surge):
                for thr in buy_thrs:
                    if rsi_cur > thr and rsi_prev_bar <= thr:
                        candidate_signal = 'buy'
                        crossed_level    = thr
                        break
            if (candidate_signal is None and sell_enabled
                    and not (state.sell_sma_pending or state.sell_confirm_pending)
                    and not avoid_sell_surge):
                for thr in sell_thrs:
                    if rsi_cur < thr and rsi_prev_bar >= thr:
                        candidate_signal = 'sell'
                        crossed_level    = thr
                        break

        state.prev_rsi      = rsi_cur
        state.last_bar_time = bar_time

        action = 'none'
        skip   = ''

        if confirmed_signal is not None:
            new_cross = confirmed_signal
        elif candidate_signal == 'buy':
            if mtf_buy_ok:
                if _ws_block:
                    skip = _ws_reason
                else:
                    state.buy_sma_pending = True
                    state.buy_sma_at      = now
                    state.buy_sma_level   = crossed_level
                    skip = f'pending_scalp_buy_{int(crossed_level)}_wait_sma20'
            else:
                if regime_h1s == 'trend_down':
                    skip = f'MTF条件NG(buy): H1=trend_down'
                elif _use_di_filter and not _h1_di_buy:
                    _di_str = (f'DI+={dip_h1s:.1f}<DI-={dim_h1s:.1f}'
                               if _di_valid else 'DI=NaN')
                    skip = f'MTF条件NG(buy): {_di_str}'
                else:
                    skip = f'MTF条件NG(buy): M5 SMA20下向き'
            new_cross = None
        elif candidate_signal == 'sell':
            if mtf_sell_ok:
                if _ws_block:
                    skip = _ws_reason
                else:
                    state.sell_sma_pending = True
                    state.sell_sma_at      = now
                    state.sell_sma_level   = crossed_level
                    skip = f'pending_scalp_sell_{int(crossed_level)}_wait_sma20'
            else:
                if regime_h1s == 'trend_up':
                    skip = f'MTF条件NG(sell): H1=trend_up'
                elif _use_di_filter and not _h1_di_sell:
                    _di_str = (f'DI-={dim_h1s:.1f}<DI+={dip_h1s:.1f}'
                               if _di_valid else 'DI=NaN')
                    skip = f'MTF条件NG(sell): {_di_str}'
                else:
                    skip = f'MTF条件NG(sell): M5 SMA20上向き'
            new_cross = None
        else:
            new_cross = None

        # ポジション数チェック
        risk_pct       = cfg['BRIDGE'].get('risk_pct', 0.01)
        total_risk_pct = cfg.get('RULES', {}).get('total_risk_pct', 0.20)
        magic_id       = cfg['MT5'].get('magic', 20240101)
        # scalp はカウントベース管理。r_multi_s で max_positions の有効リスクを補正
        pos_st         = _position_status(risk_pct, total_risk_pct, symbol, magic_id,
                                          r_multi=r_multi_s, mt5=mt5)

        if new_cross:
            hour_utc = now.hour
            eff_hour = (hour_utc + 1) % 24 if now.minute >= 45 else hour_utc

            if _ws_block:
                skip = _ws_reason
            elif state.m1_rsi_above_65 and new_cross == 'buy' and pos_st['total_positions'] > 0:
                skip = 'M1 RSI >65 追加BUY控え'
            elif state.m1_rsi_below_35 and new_cross == 'sell' and pos_st['total_positions'] > 0:
                skip = 'M1 RSI <35 追加SELL控え'
            elif (not _direct_confirmed and
                  ((regime_m5s == 'trend_up'   and new_cross == 'sell') or
                   (regime_m5s == 'trend_down' and new_cross == 'buy'))):
                skip = f'逆トレンドエントリー禁止(regime={regime_m5s})'
            elif eff_hour in {21}:
                skip = f'forbidden_hour={eff_hour}'
            elif state.count >= max_day:
                skip = f'daily_limit={state.count}/{max_day}'
            elif (state.last_at is not None and
                  now < state.last_at + timedelta(minutes=cooldown)):
                rem  = int((state.last_at + timedelta(minutes=cooldown) - now).total_seconds() / 60)
                skip = f'cooldown残{rem}分'
            elif pos_st['available_slots'] <= 0:
                opp_dir = 'sell' if new_cross == 'buy' else 'buy'
                if _has_positions_in_direction(symbol, magic_id, opp_dir, mt5=mt5):
                    # 逆方向にポジションあり → ヘッジ許可
                    action            = new_cross
                    state.last_action = new_cross
                    state.count      += 1
                    state.last_at     = now
                else:
                    skip = (f"max_positions={pos_st['max_positions']}に到達"
                            f"（全{pos_st['total_positions']}本）")
            else:
                action          = new_cross
                state.last_action = new_cross
                state.count    += 1
                state.last_at   = now

        if action == 'buy':
            sl_price = close_v - sl_move
            tp_price = close_v + tp_move
        else:
            sl_price = close_v + sl_move
            tp_price = close_v - tp_move

        # ボリュームブレイクアウト: TP拡大 + SL独立タイト
        # sl_move × vb_sl_multi で SL を通常より短く（方向明確なため）
        if _vb_tp_multi != 1.0 and action in ('buy', 'sell'):
            _vb_tp_move = tp_move * _vb_tp_multi
            _vb_sl_move = sl_move * _vb_sl_multi
            if action == 'buy':
                tp_price = close_v + _vb_tp_move
                sl_price = close_v - _vb_sl_move
            else:
                tp_price = close_v - _vb_tp_move
                sl_price = close_v + _vb_sl_move

        # パターンTP目標で tp_price を上書き（最低 1ATR 確保）
        if state.pattern_tp_target is not None and action in ('buy', 'sell'):
            _pt_s = state.pattern_tp_target
            state.pattern_tp_target = None  # 消費後クリア（次トレードへの汚染防止）
            if action == 'buy':
                tp_price = max(close_v + tp_move, min(close_v + tp_move * 8, _pt_s))
            else:
                tp_price = min(close_v - tp_move, max(close_v - tp_move * 8, _pt_s))

        # EW2 TP/SL 上書き: Fibonacci 拡張TP + W2 失効ラインSL
        # TP = W2底 + Wave1×1.618 (Wave3 目標) — 標準ATR-TPより通常大きい
        # SL = W2底 - ATR×buffer  — W2を割ったら波動構造崩壊 → タイトなSL
        if _ew2_tp_price is not None and action in ('buy', 'sell'):
            if action == 'buy':
                if _ew2_tp_price > close_v:
                    tp_price = _ew2_tp_price
                if _ew2_sl_price is not None and _ew2_sl_price < close_v:
                    sl_price = _ew2_sl_price
            else:
                if _ew2_tp_price < close_v:
                    tp_price = _ew2_tp_price
                if _ew2_sl_price is not None and _ew2_sl_price > close_v:
                    sl_price = _ew2_sl_price

        # マクロバイアス TP/SL 倍率適用（EW2上書き後に適用し最終 TP/SL を決定）
        if macro_state is not None and action in ('buy', 'sell'):
            _mb_tp_m = macro_state.buy_tp_multi  if action == 'buy' else macro_state.sell_tp_multi
            _mb_rm   = macro_state.buy_risk_multi if action == 'buy' else macro_state.sell_risk_multi
            if action == 'buy':
                _tp_dist = tp_price - close_v
                _sl_dist = close_v  - sl_price
                tp_price = close_v + _tp_dist * _mb_tp_m
                sl_price = close_v - _sl_dist * _mb_rm
            else:
                _tp_dist = close_v  - tp_price
                _sl_dist = sl_price - close_v
                tp_price = close_v - _tp_dist * _mb_tp_m
                sl_price = close_v + _sl_dist * _mb_rm

        # ── 最低 SL 距離フロア ────────────────────────────────────────
        # シグナル生成から EA 執行まで価格が動いた場合に SL が約定価格を
        # 跨いで即損切りになるのを防ぐ。EW2 など SL バッファが小さい経路で特に重要。
        _min_sl_atr = scalp.get('min_sl_atr', 0.5)
        _min_sl_dist = atr_v * _min_sl_atr
        if action == 'buy':
            sl_price = min(sl_price, close_v - _min_sl_dist)
        elif action == 'sell':
            sl_price = max(sl_price, close_v + _min_sl_dist)

        # sl_dist / tp_dist: 約定価格からの距離として EA に渡す
        # EA はこれを使って actual_sl = fill - sl_dist (buy) と再計算できる
        _sl_dist_out = round(abs(close_v - sl_price), 2)
        _tp_dist_out = round(abs(tp_price - close_v), 2)

        point  = float(info.point) if info else 0.01
        max_pt = max(1, int(tp_move * 0.5 / point))

        return {
            'timestamp':          now.strftime('%Y.%m.%d %H:%M:%S'),
            'symbol':             symbol,
            'close':              round(close_v, 2),
            'atr':                round(atr_v,   2),
            'rsi_h1':             0.0,
            'rsi_d1':             0.0,
            'rsi_m5':             round(rsi_cur, 1),
            'rsi_m5_prev':        0.0,
            'rsi_m1':             round(rsi_m1_cur, 1) if not np.isnan(rsi_m1_cur) else 0.0,
            'm5_filter_ok':       False,
            'm5_surge':           'none',
            'scalp_type':         'none',
            'sma20':              0.0,
            'sl_multi':           round(sl_ratio, 2),
            'action':             action,
            'signal_type':        (_ew2_signal_type if (_ew2_signal_type and action != 'none')
                                   else (f'scalp_{action}_{int(crossed_level or (state.buy_sma_level if action == "buy" else state.sell_sma_level))}'
                                         if action != 'none' else 'none')),
            'execution_tf':       'm5',
            'signal_valid_until': '',
            'downtrend_ok':       False,
            'sell_signal_type':   'none',
            'sell_valid_until':   '',
            'sell_skip_reason':   '',
            'sl_price':           round(sl_price, 2),
            'tp_price':           round(tp_price, 2),
            'sl_dist':            _sl_dist_out,
            'tp_dist':            _tp_dist_out,
            'score':              100,
            'strength':           'scalp',
            'tp_hold_minutes':    0,
            'skip_reason':        skip,
            'rsi_exit_thr':       cfg['SL']['rsi_exit_thr'],
            'trail_multi':        0.0,
            'max_slip_pt':        max_pt,
            'lot_size':           lot,
            'scalp_mode':         True,
            'scalp_buy_enabled':  buy_enabled,
            'scalp_sell_enabled': sell_enabled,
            'target_profit_jpy':  target,
            'target_profit_usd':  round(target_usd, 4),
            'expected_profit_usd': round(expected_profit_usd, 4),
            'expected_profit_jpy': round(expected_profit_jpy, 0),
            'tp_move_usd':        round(tp_move * contract_size, 4),
            'trades_today':       state.count,
            'cooldown_min':       cooldown,
            'scalp_cooldown_rem': 0,
            'scalp_sell_sma_pending':     state.sell_sma_pending,
            'scalp_sell_confirm_pending': state.sell_confirm_pending,
            'scalp_sell_confirm_count':   state.sell_confirm_count,
            'scalp_buy_sma_pending':      state.buy_sma_pending,
            'scalp_buy_confirm_pending':  state.buy_confirm_pending,
            'scalp_buy_confirm_count':    state.buy_confirm_count,
            'max_positions':      pos_st['max_positions'],
            'total_positions':    pos_st['total_positions'],
            'available_slots':    pos_st['available_slots'],
            'adx_h1':             0.0,
            'adx_m5':             round(adx_m5_sv, 1) if not np.isnan(adx_m5_sv) else 0.0,
            'regime_h1':          regime_h1s,
            'regime_m5':          regime_m5s,
            'regime_lot_multi':   round(r_multi_s, 2),
            'entry_in_window':    0,
            'mtf_buy_ok':         mtf_buy_ok,
            'mtf_sell_ok':        mtf_sell_ok,
            'h1_patterns':        h1_pattern_bars,
            'pattern_tp_target':  state.pattern_tp_target,
            # ダッシュボード表示用
            'extreme_oversold':   state.extreme_oversold,
            'extreme_overbought': state.extreme_overbought,
            'ws_blocked':         _ws_block,
            'ws_ratio':           round(_ws_ratio, 2),
            'rvol': (round(float(df['RVOL'].iloc[-1]), 2)
                     if 'RVOL' in df.columns and not np.isnan(float(df['RVOL'].iloc[-1]))
                     else 0.0),
            'macro_bias':         macro_state.bias       if macro_state else 0.0,
            'macro_bias_label':   macro_state.bias_label if macro_state else 'neutral',
            'macro_summary':      macro_state.summary    if macro_state else '',
            'ew2_last_buy':       state.ew2_last_buy,
            'ew2_last_sell':      state.ew2_last_sell,
        }

    except Exception:
        _logger.exception("[スキャルプ] compute_scalp_signal 例外")
        return None
