"""bridge/signal_normal.py — H1 クロス戦略シグナル計算"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import numpy as np

from core.data       import fetch_ohlcv
from core.indicators import (add_h1_indicators, add_d1_indicators,
                              add_m5_indicators)
from core.strategy   import check_m5_entry_filter, check_m5_surge

from bridge.utils    import (_detect_regime, _regime_lot_multi,
                              _calc_lot, _position_status, _get_jpy_per_usd)

if TYPE_CHECKING:
    from bridge.state import SignalState, JpyRateCache

_logger = logging.getLogger('torihiki')

try:
    from trading_rules import RulesEngine
    _engine = RulesEngine()
    print("[ルール] trading_rules.json 読み込み完了")
except Exception as _e:
    _engine = None
    print(f"[ルール] trading_rules 読み込み失敗: {_e} → フィルタなし")


def compute_signal(symbol: str, cfg: dict,
                   state: 'SignalState',
                   jpy_cache: 'JpyRateCache',
                   *, mt5) -> dict | None:
    """
    バックテストと同じシグナルロジック（クロス検出）でリアルタイムシグナルを生成。
    state は呼び出しをまたいで保持される SignalState インスタンス。
    """
    try:
        df_h1_raw  = fetch_ohlcv(symbol, 'H1',  200)
        df_d1_raw  = fetch_ohlcv(symbol, 'D1',   50)
        df_m5_raw  = fetch_ohlcv(symbol, 'M5',   60)
        df_m1_raw  = fetch_ohlcv(symbol, 'M1',   30)
        df_m15_raw = fetch_ohlcv(symbol, 'M15',  40)

        # M15 SMA20 傾き判定 + XAUUSD/GOLD 用タッチ値
        sma20_m15_is_down = False
        m15_sma20_cur     = float('nan')
        m15_bb_upper2     = float('nan')
        m15_close         = float('nan')
        if df_m15_raw is not None and len(df_m15_raw) >= 21:
            df_m15_raw['SMA20'] = df_m15_raw['Close'].rolling(window=20).mean()
            sma20_m15_last = df_m15_raw['SMA20'].iloc[-1]
            sma20_m15_prev = df_m15_raw['SMA20'].iloc[-2]
            if not np.isnan(sma20_m15_last) and not np.isnan(sma20_m15_prev):
                if sma20_m15_last < sma20_m15_prev:
                    sma20_m15_is_down = True
            m15_sma20_cur = float(sma20_m15_last)
            m15_close     = float(df_m15_raw['Close'].iloc[-1])
            _m15_std = df_m15_raw['Close'].rolling(20).std().iloc[-1]
            if not np.isnan(_m15_std) and not np.isnan(m15_sma20_cur):
                m15_bb_upper2 = m15_sma20_cur + 2.0 * float(_m15_std)

        if df_h1_raw is None or df_d1_raw is None:
            return None

        df_h1 = add_h1_indicators(df_h1_raw, cfg)
        df_d1 = add_d1_indicators(df_d1_raw, cfg)
        if df_h1.empty or df_d1.empty or 'SMA20' not in df_h1.columns:
            return None

        last       = df_h1.iloc[-1]
        close_v    = float(last['Close'])
        atr_v      = float(last['ATR'])
        rsi_h1_v   = float(last['RSI'])
        sma20      = float(last['SMA20'])
        h1_sma20_prev = (float(df_h1['SMA20'].iloc[-2])
                         if len(df_h1) >= 2 and 'SMA20' in df_h1.columns
                         else float('nan'))

        # H1 SELL トリガー: SMA20 下向き + 直近2本の確定陰線
        h1_sma20_declining = (not np.isnan(sma20) and not np.isnan(h1_sma20_prev)
                               and sma20 < h1_sma20_prev)
        h1_two_bear = (
            len(df_h1) >= 3 and
            float(df_h1['Close'].iloc[-2]) < float(df_h1['Open'].iloc[-2]) and
            float(df_h1['Close'].iloc[-3]) < float(df_h1['Open'].iloc[-3])
        )

        h1_bb_mid    = float(last['BB_mid'])    if 'BB_mid'    in df_h1.columns else float('nan')
        h1_bb_upper2 = float(last['BB_upper2']) if 'BB_upper2' in df_h1.columns else float('nan')
        h1_bb_lower2 = float(last['BB_lower2']) if 'BB_lower2' in df_h1.columns else float('nan')
        h1_bb2_pct   = float(last['BB2_pct'])   if 'BB2_pct'   in df_h1.columns else float('nan')
        rsi_d1_v     = float(df_d1['RSI'].iloc[-1])
        d1_sma200    = (float(df_d1['Close'].rolling(200).mean().iloc[-1])
                        if len(df_d1) >= 200 else float('nan'))
        d1_above_sma200 = (False if np.isnan(d1_sma200)
                           else float(df_d1['Close'].iloc[-1]) > d1_sma200)

        adx_h1_v = float(last['ADX'])     if 'ADX'      in df_h1.columns else float('nan')
        dip_h1   = float(last['DI_plus']) if 'DI_plus'  in df_h1.columns else float('nan')
        dim_h1   = float(last['DI_minus'])if 'DI_minus' in df_h1.columns else float('nan')

        # M5 RSI・急騰急落検出
        df_m5_raw['SMA20'] = df_m5_raw['Close'].rolling(20).mean()
        rsi_m5_cur  = float('nan')
        rsi_m5_prev = float('nan')
        m5_ok       = False
        surge       = 'none'
        adx_m5_v    = float('nan')
        dip_m5      = float('nan')
        dim_m5      = float('nan')
        if df_m5_raw is not None and len(df_m5_raw) >= 3:
            df_m5 = add_m5_indicators(df_m5_raw, cfg)
            if not df_m5.empty and len(df_m5) >= 2:
                rsi_m5_cur  = float(df_m5['RSI'].iloc[-1])
                rsi_m5_prev = float(df_m5['RSI'].iloc[-2])
                m5_ok = check_m5_entry_filter(rsi_m5_cur, rsi_m5_prev, rsi_d1_v, symbol)
                surge = check_m5_surge(df_m5)
                if 'ADX' in df_m5.columns:
                    adx_m5_v = float(df_m5['ADX'].iloc[-1])
                    dip_m5   = float(df_m5['DI_plus'].iloc[-1])
                    dim_m5   = float(df_m5['DI_minus'].iloc[-1])

        # M1 RSI
        rsi_m1_cur       = float('nan')
        rsi_m1_bar_prev  = float('nan')
        rsi_m1_bar_prev2 = float('nan')
        if df_m1_raw is not None and len(df_m1_raw) >= 3:
            df_m1 = add_m5_indicators(df_m1_raw, cfg)
            if not df_m1.empty and len(df_m1) >= 3:
                rsi_m1_cur       = float(df_m1['RSI'].iloc[-1])
                rsi_m1_bar_prev  = float(df_m1['RSI'].iloc[-2])
                rsi_m1_bar_prev2 = float(df_m1['RSI'].iloc[-3])

        # M5 SMA20 押し目判定
        sma20_m5_current = float('nan')
        sma20_m5_prev    = float('nan')
        close_m5_current = float('nan')
        close_m5_prev    = float('nan')
        if df_m5_raw is not None and len(df_m5_raw) >= 21:
            sma20_m5_current = float(df_m5_raw['SMA20'].iloc[-1])
            sma20_m5_prev    = float(df_m5_raw['SMA20'].iloc[-2])
            close_m5_current = float(df_m5_raw['Close'].iloc[-1])
            close_m5_prev    = float(df_m5_raw['Close'].iloc[-2])

        sl_multi      = cfg['SL']['sl_multi']
        sig_p         = cfg['SIGNAL']
        buy_thr       = sig_p.get('buy_rsi_thr', 40.0)
        mom_thrs      = sorted(sig_p.get('momentum_thrs', [55.0, 60.0, 65.0, 70.0, 75.0]))
        momentum_buy_max_rsi = sig_p.get('momentum_buy_max_rsi', 80.0)
        sell_mom_thrs = sorted(sig_p.get('momentum_sell_thrs', [55.0, 50.0, 45.0, 40.0, 35.0]),
                               reverse=True)
        downtrend_thr = sig_p.get('downtrend_d1_rsi', 45.0)
        valid_min     = cfg['EXECUTION'].get('signal_valid_m1', 240)

        downtrend_ok = (rsi_d1_v < downtrend_thr and close_v < sma20)

        now      = datetime.now(timezone.utc)
        hour_utc = now.hour
        hour_jst = (hour_utc + 9) % 24
        dow      = now.weekday()

        # ── BB2σ タッチ検出（now 定義後に実行）─────────────────────
        if not np.isnan(h1_bb_upper2) and close_v >= h1_bb_upper2:
            if not state.bb2_touched_buy:
                state.bb2_touched_buy    = True
                state.bb2_touched_at_buy = now
        if not np.isnan(h1_bb_lower2) and close_v <= h1_bb_lower2:
            if not state.bb2_touched_sell:
                state.bb2_touched_sell    = True
                state.bb2_touched_at_sell = now

        # BB2σタッチ状態を valid_min 経過後にリセット
        if state.bb2_touched_buy and state.bb2_touched_at_buy:
            if now > state.bb2_touched_at_buy + timedelta(minutes=valid_min):
                state.bb2_touched_buy = False
        if state.bb2_touched_sell and state.bb2_touched_at_sell:
            if now > state.bb2_touched_at_sell + timedelta(minutes=valid_min):
                state.bb2_touched_sell = False

        # 慎重分散エントリー条件（2本連続陽線後3本目BB2σタッチ）
        careful_entry = False
        if len(df_h1) >= 3 and not np.isnan(h1_bb_upper2) and close_v >= h1_bb_upper2:
            prev1 = df_h1.iloc[-2]
            prev2 = df_h1.iloc[-3]
            if prev2['Close'] > prev2['Open'] and prev1['Close'] > prev1['Open']:
                careful_entry = True

        # ── クロス検出 BUY ────────────────────────────────────────
        new_buy_type  = None
        new_sell_type = None
        if state.prev_rsi_h1 is not None:
            if rsi_h1_v < buy_thr and state.prev_rsi_h1 >= buy_thr:
                new_buy_type = 'dip'
            else:
                for thr in mom_thrs:
                    if rsi_h1_v > thr and state.prev_rsi_h1 <= thr and rsi_h1_v <= momentum_buy_max_rsi:
                        new_buy_type = f'momentum_{int(thr)}'
                        break
        state.prev_rsi_h1 = rsi_h1_v

        # ── SELL 検出: H1 SMA20 下向き + 直近2本確定陰線 ─────────
        if h1_sma20_declining and h1_two_bear:
            new_sell_type = 'sma20_2bear'

        # BUY ウィンドウ（新規クロスで逆方向ウィンドウをキャンセル）
        if new_buy_type:
            state.signal_active_type       = new_buy_type
            state.signal_active_until      = now + timedelta(minutes=valid_min)
            state.signal_sell_active_until = None  # SELL ウィンドウを無効化
        in_window = (state.signal_active_until is not None and
                     now <= state.signal_active_until)
        sig_type = state.signal_active_type if in_window else 'none'

        # SELL ウィンドウ（新規クロスで逆方向ウィンドウをキャンセル）
        if new_sell_type:
            state.signal_sell_active_type  = new_sell_type
            state.signal_sell_active_until = now + timedelta(minutes=valid_min)
            state.signal_active_until      = None  # BUY ウィンドウを無効化
        in_sell_window = (state.signal_sell_active_until is not None and
                          now <= state.signal_sell_active_until and
                          h1_sma20_declining)  # SMA20が上向きに転じたら即キャンセル
        sell_sig_type = state.signal_sell_active_type if in_sell_window else 'none'

        # ── RulesEngine フィルタ ─────────────────────────────────
        active_buy      = in_window
        score           = 0
        strength        = 'none'
        tp_hold_minutes = 0
        skip_reason     = ''
        result          = None

        if _engine is not None:
            result = _engine.evaluate(
                symbol     = symbol,
                rsi_h1     = rsi_h1_v,
                rsi_d1     = rsi_d1_v,
                direction  = 'buy',
                hour_utc   = hour_utc,
                dow        = dow,
                minute_utc = now.minute,
            )
            score           = result.score
            strength        = result.strength or 'none'
            tp_hold_minutes = result.tp_hold_minutes or 0

        if m5_ok:
            score = min(100, score + 10)

        if result is not None and result.signal != 'BUY':
            active_buy  = False
            skip_reason = ' | '.join(result.reasons[:2])

        # M15 SMA20 下向きは BUY 禁止
        if active_buy and sma20_m15_is_down:
            active_buy  = False
            skip_reason = "M15 SMA20 is downward"

        # ── 急騰急落スキャルプ判定 ─────────────────────────────────
        if surge == 'rapid_fall':
            state.rapid_fall_at = now

        scalp_type = 'none'

        if surge == 'rapid_rise' and not np.isnan(rsi_m1_cur):
            if 55 <= rsi_m1_cur <= 70 and rsi_m1_cur > rsi_m1_bar_prev:
                scalp_type  = 'surge_scalp'
                sig_type    = 'surge_scalp'
                active_buy  = True
                skip_reason = ''
            else:
                active_buy  = False
                skip_reason = f'm5_surge=rapid_rise(m1={rsi_m1_cur:.1f},要55-70↑)'

        elif surge == 'rapid_fall' and not np.isnan(rsi_m1_cur):
            in_rebound_window = (
                state.rapid_fall_at is not None and
                now <= state.rapid_fall_at + timedelta(minutes=60)
            )
            cross_up_30 = (
                state.prev_rsi_m1 is not None and
                state.prev_rsi_m1 <= 30 and rsi_m1_cur > 30
            )
            if in_rebound_window and cross_up_30:
                scalp_type  = 'rebound_scalp'
                sig_type    = 'rebound_scalp'
                active_buy  = True
                skip_reason = ''
            else:
                active_buy  = False
                skip_reason = f'm5_surge=rapid_fall(m1={rsi_m1_cur:.1f},待機中)'

        elif surge != 'none' and active_buy:
            active_buy  = False
            skip_reason = f'm5_surge={surge}'

        if not np.isnan(rsi_m1_cur):
            state.prev_rsi_m1 = rsi_m1_cur

        # ── SELL アクション決定 ─────────────────────────────────
        active_sell      = False
        sell_skip_reason = ''
        if scalp_type == 'none' and in_sell_window:
            active_sell = True
            if result is not None and result.signal != 'BUY':
                time_blocked = any(
                    kw in r for r in result.reasons
                    for kw in ('hour', 'dow', 'friday', 'weekend', '時', '曜')
                )
                if time_blocked:
                    active_sell      = False
                    sell_skip_reason = ' | '.join(result.reasons[:2])
            if active_sell:
                active_buy  = False
                skip_reason = 'sell_signal_active'

        if active_sell:
            action   = 'sell'
            sig_type = sell_sig_type
        elif active_buy:
            action = 'buy'
        else:
            action = 'none'

        valid_until_str = (state.signal_active_until.strftime('%Y.%m.%d %H:%M:%S')
                           if state.signal_active_until else '')
        sell_valid_until_str = (state.signal_sell_active_until.strftime('%Y.%m.%d %H:%M:%S')
                                if state.signal_sell_active_until else '')

        # ── M1 執行フィルタ ───────────────────────────────────────
        exec_cfg = cfg.get('EXECUTION', {})
        m1_buy_thrs  = exec_cfg.get('m1_exec_buy_thrs',  [65.0, 70.0, 75.0])
        m1_sell_thrs = exec_cfg.get('m1_exec_sell_thrs', [40.0, 35.0, 30.0])
        _is_gold = symbol.upper().rstrip('.') in {'XAUUSD', 'GOLD'}
        # SELL は M1 RSI フィルタを適用しない（短期執行はシグナル条件で担保）
        if (action in ('buy', 'limit_buy') and scalp_type == 'none'
                and not np.isnan(rsi_m1_bar_prev) and not np.isnan(rsi_m1_bar_prev2)):
            orig_action = action
            if orig_action in ('buy', 'limit_buy'):
                m1_exec_ok = any(
                    rsi_m1_bar_prev >= thr and rsi_m1_bar_prev2 >= thr
                    for thr in m1_buy_thrs
                )
                thr_str = '/'.join(str(int(t)) for t in m1_buy_thrs) + '↑'
            else:
                m1_exec_ok = any(
                    rsi_m1_bar_prev <= thr and rsi_m1_bar_prev2 <= thr
                    for thr in m1_sell_thrs
                )
                thr_str = '/'.join(str(int(t)) for t in m1_sell_thrs) + '↓'
            if not m1_exec_ok:
                action      = 'none'
                skip_reason = (f'M1執行待機: RSI_M1={rsi_m1_cur:.1f}'
                               f'(要{thr_str} 2本以上)')

        # ── M1 初回エントリー オーバーシュートフェールセーフ ────────
        # 1回目エントリー時にRSIがまだ進行方向へ動いていればピーク/底反転まで待機。
        # bar_prev > bar_prev2 (BUY) = 直近完成バーでもRSI上昇中 = 高値オーバーシュート継続中
        # bar_prev < bar_prev2 (SELL)= 直近完成バーでもRSI下落中 = 安値オーバーシュート継続中
        if (action in ('buy', 'limit_buy') and state.entry_in_window == 0
                and not np.isnan(rsi_m1_bar_prev) and not np.isnan(rsi_m1_bar_prev2)
                and rsi_m1_bar_prev > rsi_m1_bar_prev2):
            action      = 'none'
            skip_reason = (f'M1初回BUY: RSI上昇中'
                           f'({rsi_m1_bar_prev2:.1f}→{rsi_m1_bar_prev:.1f})'
                           f' ピーク反転後に執行')
        if (action == 'sell' and state.sell_entry_in_window == 0
                and not np.isnan(rsi_m1_bar_prev) and not np.isnan(rsi_m1_bar_prev2)
                and rsi_m1_bar_prev < rsi_m1_bar_prev2):
            action      = 'none'
            skip_reason = (f'M1初回SELL: RSI下落中'
                           f'({rsi_m1_bar_prev2:.1f}→{rsi_m1_bar_prev:.1f})'
                           f' 底反転後に執行')

        # ── XAUUSD/GOLD SELL 専用: M15 SMA20 OR BB2σ タッチで執行 ─
        if action == 'sell' and _is_gold:
            touch_frac    = exec_cfg.get('m15_touch_atr_frac', 0.15)
            touch_margin  = atr_v * touch_frac
            m15_sma_touch = (not np.isnan(m15_sma20_cur) and not np.isnan(m15_close)
                             and abs(m15_close - m15_sma20_cur) <= touch_margin)
            m15_bb2_touch = (not np.isnan(m15_bb_upper2) and not np.isnan(m15_close)
                             and m15_close >= m15_bb_upper2 - touch_margin)
            if not (m15_sma_touch or m15_bb2_touch):
                action      = 'none'
                skip_reason = (f'GOLD M15待機: SMA20={m15_sma20_cur:.1f}'
                               f' BB2σ={m15_bb_upper2:.1f}'
                               f' 現値={m15_close:.1f}')

        # M5 SMA20 下向きは BUY 禁止、上向きは SELL 禁止
        if action == 'buy' and not np.isnan(sma20_m5_current) and not np.isnan(sma20_m5_prev):
            if sma20_m5_current < sma20_m5_prev:
                action      = 'none'
                skip_reason = 'M5_SMA20_down_buy禁止'
        if action == 'sell' and not np.isnan(sma20_m5_current) and not np.isnan(sma20_m5_prev):
            if sma20_m5_current > sma20_m5_prev:
                action      = 'none'
                skip_reason = 'M5_SMA20_up_sell禁止'

        # H1 SMA20 下向きは BUY 禁止、上向きは SELL 禁止
        if action in ('buy', 'limit_buy') and h1_sma20_declining:
            action      = 'none'
            skip_reason = 'H1_SMA20_down_buy禁止'
        h1_sma20_rising = (not np.isnan(sma20) and not np.isnan(h1_sma20_prev)
                           and sma20 > h1_sma20_prev)
        if action == 'sell' and h1_sma20_rising:
            action      = 'none'
            skip_reason = 'H1_SMA20_up_sell禁止'

        if action == 'buy' and _engine is None and hour_jst == 14:
            action      = 'none'
            skip_reason = 'JST14-15_buy禁止'

        # ── BB2σ タッチ後押し目待ちフィルタ ─────────────────────
        limit_prices = []
        if action == 'buy' and state.bb2_touched_buy and not np.isnan(sma20_m5_current):
            spacing = atr_v * 0.1
            limit_prices = [
                round(sma20_m5_current - spacing, 2),
                round(sma20_m5_current,            2),
                round(sma20_m5_current + spacing,  2),
            ]
            action      = 'limit_buy'
            skip_reason = ''
        elif action == 'sell' and state.bb2_touched_sell and not np.isnan(sma20_m5_current):
            if not (close_m5_prev > sma20_m5_prev and close_m5_current < sma20_m5_current):
                action      = 'none'
                skip_reason = 'BB2σタッチ後SMA20戻り待ち'

        # ── SL/TP ────────────────────────────────────────────────
        if scalp_type == 'surge_scalp':
            sl_price = close_v - atr_v * 0.5
            tp_price = close_v + atr_v * 1.0
        elif scalp_type == 'rebound_scalp':
            sl_price = close_v - atr_v * 0.8
            tp_price = close_v + atr_v * 0.8
        else:
            tp_multi = cfg['SL'].get('tp_atr_multi', 3.0)
            if not np.isnan(d1_sma200):
                tp_multi = (cfg['SL'].get('tp_atr_multi_above_d1_sma200', tp_multi)
                            if d1_above_sma200 else
                            cfg['SL'].get('tp_atr_multi_below_d1_sma200', tp_multi))
            if rsi_h1_v >= 70.0:
                tp_multi = min(tp_multi, cfg['SL'].get('tp_atr_multi_rsi_high', 2.0))
            elif rsi_h1_v >= 50.0:
                tp_multi = min(tp_multi, cfg['SL'].get('tp_atr_multi_rsi_mid',  2.5))
            else:
                tp_multi = min(tp_multi, cfg['SL'].get('tp_atr_multi_rsi_low',  3.0))

            if action == 'sell':
                sl_price = close_v + atr_v * sl_multi
                tp_price = close_v - atr_v * tp_multi
            else:
                sl_price = close_v - atr_v * sl_multi
                tp_price = close_v + atr_v * tp_multi

            bb_near_pct = cfg['INDICATOR'].get('bb_tp_near_pct', 0.85)
            if action == 'buy' and not np.isnan(h1_bb_upper2) and h1_bb_upper2 > close_v:
                if h1_bb2_pct >= bb_near_pct or (h1_bb_upper2 - close_v) <= atr_v * 0.5:
                    tp_price = min(tp_price, h1_bb_upper2)
            elif action == 'sell' and not np.isnan(h1_bb_lower2) and h1_bb_lower2 < close_v:
                sell_bb2_pct = ((h1_bb_mid - close_v) / (h1_bb_mid - h1_bb_lower2)
                                if h1_bb_mid != h1_bb_lower2 else 0.0)
                if sell_bb2_pct >= bb_near_pct or (close_v - h1_bb_lower2) <= atr_v * 0.5:
                    tp_price = max(tp_price, h1_bb_lower2)

        tick  = mt5.symbol_info(symbol)
        point = tick.point if tick else 0.01
        max_pt = max(1, int(atr_v * 0.5 / point))

        c_sz     = float(tick.trade_contract_size) if tick else 100.0
        l_min    = float(tick.volume_min)          if tick else 0.01
        l_max    = float(tick.volume_max)          if tick else 100.0
        l_step   = float(tick.volume_step)         if tick else 0.01
        account  = mt5.account_info()
        balance_jpy = (float(account.balance) if account
                       else cfg['BRIDGE'].get('fallback_balance', 1_500_000))
        jpy_per_usd = _get_jpy_per_usd(jpy_cache,
                                        cfg['SCALP'].get('jpy_per_usd', 150.0), mt5=mt5)
        balance_usd = balance_jpy / jpy_per_usd
        risk_pct    = cfg['BRIDGE'].get('risk_pct', 0.01)

        regime_cfg = cfg.get('REGIME', {})
        regime_h1  = _detect_regime(adx_h1_v, dip_h1, dim_h1, regime_cfg)
        regime_m5  = _detect_regime(adx_m5_v, dip_m5, dim_m5, regime_cfg)
        r_multi    = _regime_lot_multi(regime_h1, regime_m5, regime_cfg)

        lot_base = _calc_lot(balance_usd, risk_pct, atr_v * sl_multi,
                             c_sz, l_min, l_max, l_step, cfg['BRIDGE']['lot_size'])
        lot_size = max(l_min, min(l_max, round(lot_base * r_multi / l_step) * l_step))

        total_risk_pct = cfg.get('RULES', {}).get('total_risk_pct', 0.20)
        magic_id       = cfg['MT5'].get('magic', 20240101)
        pos_st = _position_status(risk_pct, total_risk_pct, symbol, magic_id,
                                   balance_usd=balance_usd, contract_size=c_sz,
                                   sl_dist=atr_v * sl_multi, r_multi=r_multi, mt5=mt5)
        if pos_st['available_slots'] <= 0 and action in ('buy', 'sell', 'limit_buy'):
            action      = 'none'
            skip_reason = (f"max_positions={pos_st['max_positions']}に到達"
                           f"（全{pos_st['total_positions']}本"
                           f" open_risk={pos_st['open_risk_pct']:.1%}）")

        # H1 レンジ中は執行スキップ
        if action in ('buy', 'sell', 'limit_buy') and regime_h1 == 'range':
            action      = 'none'
            skip_reason = f'H1レンジ({regime_h1})執行スキップ'

        # ── スプリットエントリー（初回半ロット + 2回目押し目リミット）────
        # split_entry_frac < 1.0 の場合のみ有効
        split_frac    = exec_cfg.get('split_entry_frac',     0.5)
        pullback_frac = exec_cfg.get('split_limit_pullback', 0.4)
        if 0.0 < split_frac < 1.0:
            if action == 'buy' and state.entry_in_window == 0:
                # 初回: ロットを split_frac に縮小し、残りを押し目リミット待機へ
                lot_size = max(l_min, round(lot_size * split_frac / l_step) * l_step)
                state.split_pending_buy = True
            elif action == 'buy' and state.split_pending_buy:
                # 2回目: 残りロットを押し目リミット注文に変換
                pullback_px             = round(close_v - atr_v * pullback_frac, 2)
                limit_prices            = [pullback_px]
                lot_size                = max(l_min, round(lot_size * (1.0 - split_frac) / l_step) * l_step)
                action                  = 'limit_buy'
                state.split_pending_buy = False
                state.entry_in_window  += 1   # リミット枠を消費してゲートをスキップ

            if action == 'sell' and state.sell_entry_in_window == 0:
                # SELL 初回: ロットを split_frac に縮小（limit_sell 未実装のため残りは spacing 機構に委ねる）
                lot_size = max(l_min, round(lot_size * split_frac / l_step) * l_step)
                state.split_pending_sell = True
            elif action == 'sell' and state.split_pending_sell:
                # SELL 2回目: フラグ解除 → 通常エントリーゲートへ（戻り目は spacing で管理）
                state.split_pending_sell = False

        # ── 分散エントリーゲート ─────────────────────────────────
        max_ep        = regime_cfg.get('max_entry_per_signal', 3)
        spacing       = regime_cfg.get('entry_spacing_atr',    0.5)
        scalp_reserve = regime_cfg.get('scalp_reserve_slots',  1)
        if careful_entry:
            max_ep  = 1
            spacing = 1.0
        is_full_trend   = (regime_h1 in ('trend_up', 'trend_down') and
                           regime_m5 in ('trend_up', 'trend_down'))
        avail_for_trend = max(0, pos_st['available_slots'] - scalp_reserve)
        trend_max_ep    = max(1, avail_for_trend)

        if action == 'buy':
            cur_key = (state.signal_active_type, state.signal_active_until)
            if cur_key != state.signal_window_key:
                state.signal_window_key  = cur_key
                state.entry_in_window    = 0
                state.last_entry_price   = 0.0
                state.split_pending_buy  = False   # 旧ウィンドウのスプリット待機をキャンセル
            if is_full_trend:
                if state.entry_in_window >= trend_max_ep:
                    action      = 'none'
                    skip_reason = f'トレンドentry上限{trend_max_ep}回到達'
                else:
                    state.entry_in_window  += 1
                    state.last_entry_price  = close_v
            else:
                if state.entry_in_window >= max_ep:
                    action      = 'none'
                    skip_reason = f'entry上限{max_ep}回到達'
                elif (state.last_entry_price > 0 and
                      close_v > state.last_entry_price - spacing * atr_v):
                    action      = 'none'
                    skip_reason = (f'押し目待ち(現{close_v:.0f}'
                                   f'<必要{state.last_entry_price - spacing*atr_v:.0f})')
                else:
                    state.entry_in_window  += 1
                    state.last_entry_price  = close_v

        elif action == 'sell':
            cur_key = (state.signal_sell_active_type, state.signal_sell_active_until)
            if cur_key != state.sell_window_key:
                state.sell_window_key        = cur_key
                state.sell_entry_in_window   = 0
                state.sell_last_entry_price  = 0.0
                state.split_pending_sell     = False  # 旧ウィンドウのスプリット待機をキャンセル
            if is_full_trend:
                if state.sell_entry_in_window >= trend_max_ep:
                    action      = 'none'
                    skip_reason = f'トレンドentry上限{trend_max_ep}回到達'
                else:
                    state.sell_entry_in_window  += 1
                    state.sell_last_entry_price  = close_v
            else:
                if state.sell_entry_in_window >= max_ep:
                    action      = 'none'
                    skip_reason = f'entry上限{max_ep}回到達'
                elif (state.sell_last_entry_price > 0 and
                      close_v < state.sell_last_entry_price + spacing * atr_v):
                    action      = 'none'
                    need_v = state.sell_last_entry_price + spacing * atr_v
                    skip_reason = f'戻り待ち(現{close_v:.0f}需>{need_v:.0f})'
                else:
                    state.sell_entry_in_window  += 1
                    state.sell_last_entry_price  = close_v

        return {
            'timestamp':          datetime.now(timezone.utc).strftime('%Y.%m.%d %H:%M:%S'),
            'symbol':             symbol,
            'close':              round(close_v, 2),
            'atr':                round(atr_v,    2),
            'rsi_h1':             round(rsi_h1_v, 1),
            'rsi_d1':             round(rsi_d1_v, 1),
            'rsi_m5':             round(rsi_m5_cur,  1) if not np.isnan(rsi_m5_cur)  else 0.0,
            'rsi_m5_prev':        round(rsi_m5_prev, 1) if not np.isnan(rsi_m5_prev) else 0.0,
            'm5_filter_ok':       m5_ok,
            'm5_surge':           surge,
            'rsi_m1':             round(rsi_m1_cur, 1) if not np.isnan(rsi_m1_cur) else 0.0,
            'scalp_type':         scalp_type,
            'sma20':              round(sma20,    2),
            'sl_multi':           round(sl_multi,  2),
            'action':                action,
            'signal_type':           sig_type,
            'signal_valid_until':    valid_until_str,
            'downtrend_ok':          downtrend_ok,
            'sell_signal_type':      sell_sig_type,
            'sell_valid_until':      sell_valid_until_str,
            'sell_skip_reason':      sell_skip_reason,
            'sl_price':              round(sl_price,  2),
            'tp_price':              round(tp_price,  2),
            'score':                 score,
            'strength':              strength,
            'tp_hold_minutes':       tp_hold_minutes,
            'skip_reason':           skip_reason,
            'rsi_exit_thr':       cfg['SL']['rsi_exit_thr'],
            'trail_multi':        cfg['SL']['trail_multi'],
            'max_slip_pt':        max_pt,
            'lot_size':           lot_size,
            'max_positions':      pos_st['max_positions'],
            'total_positions':    pos_st['total_positions'],
            'available_slots':    pos_st['available_slots'],
            'open_risk_pct':      round(pos_st['open_risk_pct'], 4),
            'adx_h1':             round(adx_h1_v, 1) if not np.isnan(adx_h1_v) else 0.0,
            'adx_m5':             round(adx_m5_v, 1) if not np.isnan(adx_m5_v) else 0.0,
            'regime_h1':          regime_h1,
            'regime_m5':          regime_m5,
            'regime_lot_multi':   round(r_multi, 2),
            'entry_in_window':    state.entry_in_window,
            'is_full_trend':      is_full_trend,
            'scalp_reserve':      scalp_reserve,
            'careful_entry':      careful_entry,
            'limit_prices':       limit_prices,
        }
    except Exception:
        _logger.exception("[ブリッジ] compute_signal 例外")
        return None
