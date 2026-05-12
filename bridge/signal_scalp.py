"""bridge/signal_scalp.py — スキャルプモード シグナル計算"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import numpy as np

from core.data       import fetch_ohlcv
from core.indicators import add_m5_indicators, add_m1_indicators, add_h1_indicators
from core.strategy   import detect_big_move, detect_early_surge, should_avoid_entry_during_surge

from bridge.utils    import (_detect_regime, _regime_lot_multi,
                              _position_status, _get_jpy_per_usd,
                              _has_positions_in_direction)
from bridge.signal_normal import compute_signal

if TYPE_CHECKING:
    from bridge.state import ScalpState, SignalState, JpyRateCache, Sma20TouchCache

_logger = logging.getLogger('torihiki')


def compute_scalp_signal(symbol: str, cfg: dict,
                         state: 'ScalpState',
                         sig_state: 'SignalState',
                         jpy_cache: 'JpyRateCache',
                         sma20_cache: 'Sma20TouchCache',
                         *, mt5) -> dict | None:
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

        # M1 RSI 65超え追跡
        if not np.isnan(rsi_m1_cur):
            if rsi_m1_cur > 65:
                state.m1_rsi_above_65 = True
            elif rsi_m1_cur < 65:
                state.m1_rsi_above_65 = False

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

        # M15 SMA20 傾き用データ
        df_m15 = None
        df_m15_raw = fetch_ohlcv(symbol, 'M15', 30)
        if df_m15_raw is not None:
            _df_m15_ind = add_m1_indicators(df_m15_raw, cfg)
            if not _df_m15_ind.empty:
                df_m15 = _df_m15_ind

        # H1 レジーム取得
        regime_h1s = 'weak_trend'
        df_h1_raw  = fetch_ohlcv(symbol, 'H1', 50)
        if df_h1_raw is not None:
            df_h1s = add_h1_indicators(df_h1_raw, cfg)
            if not df_h1s.empty:
                adx_h1s = float(df_h1s['ADX'].iloc[-1])     if 'ADX'      in df_h1s.columns else float('nan')
                dip_h1s = float(df_h1s['DI_plus'].iloc[-1]) if 'DI_plus'  in df_h1s.columns else float('nan')
                dim_h1s = float(df_h1s['DI_minus'].iloc[-1])if 'DI_minus' in df_h1s.columns else float('nan')
                regime_h1s = _detect_regime(adx_h1s, dip_h1s, dim_h1s, regime_cfg)

        # MTF SMA20 傾き + H1 レジーム条件
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
            return slope > thr_v if direction == 'buy' else slope < -thr_v

        mtf_buy_ok  = (regime_h1s == 'trend_up'   and
                       _sma20_ok(df_m1,  'buy') and
                       _sma20_ok(df,     'buy') and
                       _sma20_ok(df_m15, 'buy'))
        mtf_sell_ok = (regime_h1s == 'trend_down' and
                       _sma20_ok(df_m1,  'sell') and
                       _sma20_ok(df,     'sell') and
                       _sma20_ok(df_m15, 'sell'))

        # トレンド転換時に逆方向の待機状態をキャンセル
        if regime_m5s == 'trend_up' and (state.sell_sma_pending or state.sell_confirm_pending):
            state.sell_sma_pending      = False
            state.sell_sma_at           = None
            state.sell_confirm_pending  = False
            state.sell_confirm_at       = None
            state.sell_confirm_count    = 0
            state.sell_confirm_bar_time = None
        if regime_m5s == 'trend_down' and (state.buy_sma_pending or state.buy_confirm_pending):
            state.buy_sma_pending      = False
            state.buy_sma_at           = None
            state.buy_confirm_pending  = False
            state.buy_confirm_at       = None
            state.buy_confirm_count    = 0
            state.buy_confirm_bar_time = None

        target_usd    = target / jpy_rate
        tp_atr_frac   = scalp.get('tp_atr_fraction', 0.5)
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
        surge_info  = detect_early_surge(df, cfg)
        avoid_surge = should_avoid_entry_during_surge(df, cfg)

        if avoid_surge and not surge_info['is_early_surge'] and not in_cooldown:
            print(f"[急騰回避] RSI={rsi_cur:.1f} が高すぎるためエントリー見送り")
            return None

        # ── M1 待機ロジック ────────────────────────────────────────
        confirmed_signal = None
        candidate_signal = None
        crossed_level    = 0.0

        # M1 早期執行: M5 RSI が閾値に接近中かつ M1 が先行クロス
        m1_early_margin = scalp.get('m1_early_margin', 2.0)
        if (confirmed_signal is None
                and df_m1 is not None and len(df_m1) >= 2 and m1_early_margin > 0):
            rsi_m1_prev2 = float(df_m1['RSI'].iloc[-2])
            if buy_enabled and not state.buy_sma_pending:
                for thr in buy_thrs:
                    if thr - m1_early_margin <= rsi_cur <= thr:
                        if rsi_m1_cur > thr and rsi_m1_prev2 <= thr:
                            if ((not surge_info['is_early_surge'] or surge_info['confidence'] >= 0.3)
                                    and mtf_buy_ok):
                                state.buy_sma_pending = True
                                state.buy_sma_at      = now
                                state.buy_sma_level   = thr
                                if surge_info['is_early_surge']:
                                    print(f"[急騰兆候BUY] 急騰初期でもBUY許可 "
                                          f"Confidence={surge_info['confidence']:.2f}")
                                else:
                                    print(f"[押し目BUY] 通常の押し目エントリー "
                                          f"Confidence={surge_info['confidence']:.2f}")
                            break
            if confirmed_signal is None and sell_enabled and not state.sell_sma_pending:
                for thr in sell_thrs:
                    if thr <= rsi_cur <= thr + m1_early_margin:
                        if rsi_m1_cur < thr and rsi_m1_prev2 >= thr:
                            if (surge_info['is_early_surge'] and surge_info['confidence'] > 0.6
                                    and mtf_sell_ok):
                                state.sell_sma_pending = True
                                state.sell_sma_at      = now
                                state.sell_sma_level   = thr
                                print(f"[急騰初期SELL] RVOL={df['RVOL'].iloc[-1]:.2f} "
                                      f"Accel={df['Price_Accel'].iloc[-1]:.2f} "
                                      f"Confidence={surge_info['confidence']:.2f}")
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
                    touch_margin_cfg = sma20_cache.margins.get(
                        symbol, cfg.get('EXECUTION', {}).get('touch_margin', 0.20))
                    close_m1 = float(df_m1['Close'].iloc[-1]) if (df_m1 is not None and not df_m1.empty) else close_v
                    if abs(close_m1 - sma20_m1) <= touch_margin_cfg:
                        slope_bars = scalp.get('sma20_slope_bars', 5)
                        slope_thr  = scalp.get('sma20_slope_atr_thr', 0.10)
                        atr_m1_v   = float(df_m1['ATR'].iloc[-1]) if ('ATR' in df_m1.columns and len(df_m1) > slope_bars) else float('nan')
                        sma20_prev = float(df_m1['SMA20'].iloc[-(slope_bars + 1)]) if len(df_m1) > slope_bars else float('nan')
                        sma20_slope_ok = (
                            not np.isnan(atr_m1_v) and not np.isnan(sma20_prev) and
                            (sma20_m1 - sma20_prev) < -(atr_m1_v * slope_thr)
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
                is_down_bar  = close_m1_cur < close_m1_prv

                if is_down_bar and m1_bar_cur != state.sell_confirm_bar_time:
                    state.sell_confirm_count   += 1
                    state.sell_confirm_bar_time = m1_bar_cur
                elif not is_down_bar:
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
                    touch_margin_cfg = sma20_cache.margins.get(
                        symbol, cfg.get('EXECUTION', {}).get('touch_margin', 0.20))
                    close_m1 = float(df_m1['Close'].iloc[-1]) if (df_m1 is not None and not df_m1.empty) else close_v
                    if abs(close_m1 - sma20_m1) <= touch_margin_cfg:
                        slope_bars = scalp.get('sma20_slope_bars', 5)
                        slope_thr  = scalp.get('sma20_slope_atr_thr', 0.10)
                        atr_m1_v   = float(df_m1['ATR'].iloc[-1]) if ('ATR' in df_m1.columns and len(df_m1) > slope_bars) else float('nan')
                        sma20_prev = float(df_m1['SMA20'].iloc[-(slope_bars + 1)]) if len(df_m1) > slope_bars else float('nan')
                        sma20_slope_ok = (
                            not np.isnan(atr_m1_v) and not np.isnan(sma20_prev) and
                            (sma20_m1 - sma20_prev) > (atr_m1_v * slope_thr)
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

                if is_up_bar and m1_bar_cur != state.buy_confirm_bar_time:
                    state.buy_confirm_count   += 1
                    state.buy_confirm_bar_time = m1_bar_cur
                elif not is_up_bar:
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
            if not (state.buy_sma_pending or state.buy_confirm_pending):
                for thr in buy_thrs:
                    if rsi_cur > thr and rsi_prev_bar <= thr:
                        candidate_signal = 'buy'
                        crossed_level    = thr
                        break
            if (candidate_signal is None and sell_enabled
                    and not (state.sell_sma_pending or state.sell_confirm_pending)):
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
                state.buy_sma_pending = True
                state.buy_sma_at      = now
                state.buy_sma_level   = crossed_level
                skip = f'pending_scalp_buy_{int(crossed_level)}_wait_sma20'
            else:
                skip = f'MTF条件NG(buy): H1={regime_h1s}'
            new_cross = None
        elif candidate_signal == 'sell':
            if mtf_sell_ok:
                state.sell_sma_pending = True
                state.sell_sma_at      = now
                state.sell_sma_level   = crossed_level
                skip = f'pending_scalp_sell_{int(crossed_level)}_wait_sma20'
            else:
                skip = f'MTF条件NG(sell): H1={regime_h1s}'
            new_cross = None
        else:
            new_cross = None

        # ポジション数チェック
        risk_pct       = cfg['BRIDGE'].get('risk_pct', 0.01)
        total_risk_pct = cfg.get('RULES', {}).get('total_risk_pct', 0.20)
        pos_st         = _position_status(risk_pct, total_risk_pct, mt5=mt5)

        if new_cross:
            hour_utc = now.hour
            eff_hour = (hour_utc + 1) % 24 if now.minute >= 45 else hour_utc

            if state.m1_rsi_above_65 and pos_st['total_positions'] > 0:
                skip = 'M1 RSI >65 追加注文控え'
            elif (regime_m5s == 'trend_up'   and new_cross == 'sell') or \
                 (regime_m5s == 'trend_down' and new_cross == 'buy'):
                skip = f'逆トレンドエントリー禁止(regime={regime_m5s})'
            elif eff_hour in {9, 16, 21}:
                skip = f'forbidden_hour={eff_hour}'
            elif state.count >= max_day:
                skip = f'daily_limit={state.count}/{max_day}'
            elif (state.last_at is not None and
                  now < state.last_at + timedelta(minutes=cooldown)):
                rem  = int((state.last_at + timedelta(minutes=cooldown) - now).total_seconds() / 60)
                skip = f'cooldown残{rem}分'
            elif pos_st['available_slots'] <= 0:
                opp_dir = 'sell' if new_cross == 'buy' else 'buy'
                magic_n = cfg['MT5'].get('magic', 20240101)
                if _has_positions_in_direction(symbol, magic_n, opp_dir, mt5=mt5):
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
            'signal_type':        (f'scalp_{action}_{int(crossed_level or (state.buy_sma_level if action == "buy" else state.sell_sma_level))}'
                                   if action != 'none' else 'none'),
            'execution_tf':       'm5',
            'signal_valid_until': '',
            'downtrend_ok':       False,
            'sell_signal_type':   'none',
            'sell_valid_until':   '',
            'sell_skip_reason':   '',
            'sl_price':           round(sl_price, 2),
            'tp_price':           round(tp_price, 2),
            'score':              100,
            'strength':           'scalp',
            'tp_hold_minutes':    5,
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
        }

    except Exception:
        _logger.exception("[スキャルプ] compute_scalp_signal 例外")
        return None
