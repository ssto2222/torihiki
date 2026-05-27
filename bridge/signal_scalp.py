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
        max_day         = scalp.get('max_trades_day',  20)
        cooldown        = scalp.get('cooldown_min',    15)
        cooldown_trades = int(scalp.get('cooldown_trades', 3))

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
        rsi_m1_cur   = float('nan')
        sma20_m1_val = float('nan')
        if df_m1_raw is not None:
            df_m1 = add_m1_indicators(df_m1_raw, cfg)
            if not df_m1.empty:
                rsi_m1_cur   = float(df_m1['RSI'].iloc[-1])
                sma20_m1_val = (float(df_m1['SMA20'].iloc[-1])
                                if 'SMA20' in df_m1.columns else float('nan'))

        # M15 データ取得（マルチTF SMA20 傾きチェック用）
        df_m15_raw    = fetch_ohlcv(symbol, 'M15', 30)
        df_m15        = None
        sma20_m15_val = float('nan')
        if df_m15_raw is not None:
            df_m15 = add_m5_indicators(df_m15_raw, cfg)  # SMA20/ATR 計算
            if df_m15.empty:
                df_m15 = None
            elif 'SMA20' in df_m15.columns:
                sma20_m15_val = float(df_m15['SMA20'].iloc[-1])

        # EW2 専用 M5 データ（多めに取得してパターン探索精度を上げる）
        # signal用 df は50本のみ → EW2は独立して200本のM5足を使用
        _ew2_bars    = cfg.get('ELLIOTT', {}).get('lookback_bars', 100) + 30
        df_m5_ew2_raw = fetch_ohlcv(symbol, 'M5', _ew2_bars)
        df_m5_ew2     = None
        if df_m5_ew2_raw is not None:
            df_m5_ew2 = add_m5_indicators(df_m5_ew2_raw, cfg)
            if df_m5_ew2.empty:
                df_m5_ew2 = None

        # D1 データ取得（SMA20 方向チェック用: EW2以外は逆方向エントリー禁止）
        df_d1_raw    = fetch_ohlcv(symbol, 'D1', 30)
        df_d1        = None
        sma20_d1_val = float('nan')
        if df_d1_raw is not None:
            df_d1 = add_m5_indicators(df_d1_raw, cfg)
            if df_d1.empty:
                df_d1 = None
            elif 'SMA20' in df_d1.columns:
                sma20_d1_val = float(df_d1['SMA20'].iloc[-1])

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
        adx_m5_sv    = float(df['ADX'].iloc[-1])     if 'ADX'      in df.columns else float('nan')
        dip_m5_sv    = float(df['DI_plus'].iloc[-1]) if 'DI_plus'  in df.columns else float('nan')
        dim_m5_sv    = float(df['DI_minus'].iloc[-1])if 'DI_minus' in df.columns else float('nan')
        sma20_m5_val = float(df['SMA20'].iloc[-1])   if 'SMA20'    in df.columns else float('nan')
        regime_m5s   = _detect_regime(adx_m5_sv, dip_m5_sv, dim_m5_sv, regime_cfg)
        r_multi_s  = _regime_lot_multi('weak_trend', regime_m5s, regime_cfg)

        # H1 レジーム取得
        regime_h1s = 'weak_trend'
        adx_h1s    = float('nan')
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

        def _sma20_accel_ok(tfdf, direction: str) -> bool:
            """SMA20 2階微分チェック: |傾き|がウィンドウ内で accel_tol 以上減少していれば False。
            線形回帰で傾きのトレンドを計算し、相対減少率が閾値を超えたら減速と判定する。
            """
            _accel_n   = scalp.get('sma20_accel_bars', 4)
            _accel_tol = scalp.get('sma20_accel_tol',  0.3)
            if tfdf is None or tfdf.empty or 'SMA20' not in tfdf.columns:
                return True
            sma = tfdf['SMA20'].dropna()
            if len(sma) < _accel_n + 2:
                return True
            slopes     = np.diff(sma.values[-(_accel_n + 1):])  # _accel_n 本の1階差分
            abs_slopes = np.abs(slopes)
            mean_abs   = float(abs_slopes.mean())
            if mean_abs < 1e-10:
                return True  # ほぼフラットなSMA20 → 減速チェック不要
            x    = np.arange(len(abs_slopes), dtype=float)
            coef = np.polyfit(x, abs_slopes, 1)
            # ウィンドウ全体での相対減少率: (trend_per_bar × n_steps) / mean_abs
            relative_decline = -float(coef[0]) * (len(abs_slopes) - 1) / mean_abs
            return relative_decline < _accel_tol

        _di_valid    = not np.isnan(dip_h1s) and not np.isnan(dim_h1s)
        _h1_di_buy   = _di_valid and dip_h1s > dim_h1s   # DI+ > DI-
        _h1_di_sell  = _di_valid and dim_h1s > dip_h1s   # DI- > DI+

        # スキャルプはレンジ相場でも有効 → 'range' も許可
        # h1_di_filter=False(デフォルト): H1レジームのみで判断（DI不要）
        # h1_di_filter=True(厳格): DI方向も必須
        _use_di_filter       = scalp.get('h1_di_filter', False)
        _sma20_slope_buy_ok  = _sma20_ok(df,    'buy')   # M5
        _sma20_slope_sell_ok = _sma20_ok(df,    'sell')
        _sma20_m1_buy_ok     = _sma20_ok(df_m1, 'buy')   # M1
        _sma20_m1_sell_ok    = _sma20_ok(df_m1, 'sell')
        _sma20_m15_buy_ok    = _sma20_ok(df_m15, 'buy')  # M15
        _sma20_m15_sell_ok   = _sma20_ok(df_m15, 'sell')
        _sma20_d1_buy_ok     = _sma20_ok(df_d1,  'buy')  # D1
        _sma20_d1_sell_ok    = _sma20_ok(df_d1,  'sell')
        # M1 は絶対ゲート（エントリーゲートで別途チェック）
        # M5/M15 が両方とも逆向き → BUY/SELL禁止（M1クリア後の二次フィルター）
        _sma20_consensus_buy  = (_sma20_slope_buy_ok  or _sma20_m15_buy_ok)
        _sma20_consensus_sell = (_sma20_slope_sell_ok or _sma20_m15_sell_ok)
        # SMA20 2階微分フラグ: |傾き|が減少トレンドかどうか
        # シグナル時はM5、執行時はM1でチェック（EW2はW2形成中に正常減速するため免除）
        _sma20_m5_accel_buy_ok  = _sma20_accel_ok(df,    'buy')
        _sma20_m5_accel_sell_ok = _sma20_accel_ok(df,    'sell')
        _sma20_m1_accel_buy_ok  = _sma20_accel_ok(df_m1, 'buy')
        _sma20_m1_accel_sell_ok = _sma20_accel_ok(df_m1, 'sell')
        mtf_buy_ok  = (regime_h1s != 'trend_down' and
                       (not _use_di_filter or _h1_di_buy) and
                       _sma20_slope_buy_ok)
        mtf_sell_ok = (regime_h1s != 'trend_up' and
                       (not _use_di_filter or _h1_di_sell) and
                       _sma20_slope_sell_ok)
        # EW2 専用: W2底/天井形成中はM5 SMA20がまだ逆向きのため、スロープチェックを外す
        mtf_ew2_buy_ok  = (regime_h1s != 'trend_down' and (not _use_di_filter or _h1_di_buy))
        mtf_ew2_sell_ok = (regime_h1s != 'trend_up'   and (not _use_di_filter or _h1_di_sell))

        # ── ウィップソー（行ってこい相場）検出 ─────────────────────
        _ws_cfg       = cfg.get('WHIPSAW', {})
        _ws_block     = False
        _ws_ratio     = 0.0
        _is_whipsaw   = False
        _is_bidir     = False

        # 確定トレンド転換時に逆方向の待機状態をキャンセル
        # M5（短期）ではなく H1（中期）確定トレンドのみでキャンセル
        # → M5 一時的押し目で pending が消えるのを防ぐ
        if regime_h1s == 'trend_up' and (state.sell_sma_pending or state.sell_confirm_pending):
            state.sell_sma_pending      = False
            state.sell_sma_at           = None
            state.sell_sma_level        = 0.0
            state.sell_confirm_pending  = False
            state.sell_confirm_at       = None
            state.sell_confirm_count    = 0
            state.sell_confirm_bar_time = None
            state.sell_confirm_level    = 0.0
        if regime_h1s == 'trend_down' and (state.buy_sma_pending or state.buy_confirm_pending):
            state.buy_sma_pending      = False
            state.buy_sma_at           = None
            state.buy_sma_level        = 0.0
            state.buy_confirm_pending  = False
            state.buy_confirm_at       = None
            state.buy_confirm_count    = 0
            state.buy_confirm_bar_time = None
            state.buy_confirm_level    = 0.0

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

        # ── ノーマルバリアント: 大変動/ネックライン接近時の拡張パラメーター ──
        # 同じ執行条件でTP拡大+トレーリング+ロット調整を適用する
        _normal_variant  = False   # True になると NV パラメーターで上書き
        _nv_tp_frac      = scalp.get('normal_variant_tp_atr', 1.5)
        _nv_lot_frac_cfg = scalp.get('normal_variant_lot_frac', 1.0)
        _nv_tp_move      = atr_v * _nv_tp_frac
        _nv_sl_move      = _nv_tp_move * sl_ratio
        _nv_lot_raw      = target_usd / (_nv_tp_move * contract_size) if _nv_tp_move > 0 else 0
        _nv_lot_base     = max(l_min, min(l_max, round(_nv_lot_raw / l_step) * l_step))
        _nv_lot          = max(l_min, min(l_max, scalp_lot_max,
                               round(_nv_lot_base * r_multi_s * _nv_lot_frac_cfg / l_step) * l_step))

        # ── 大変動検知: スキャルプ→通常モード自動切換え ──────────
        bm_lookback  = scalp.get('big_move_lookback',  12)
        bm_atr_multi = scalp.get('big_move_atr_multi', 2.0)
        big_move     = detect_big_move(df, bm_lookback, bm_atr_multi)

        # 大変動検知: ノーマルバリアントパラメーターに切替（同じ執行条件を使用）
        if big_move != 'none':
            if not state.in_big_move_normal:
                print(f"[スキャルプ→NVモード] 大変動={big_move} last_pos={state.last_action}"
                      f" → 同条件+拡張TP/トレーリング")
                _logger.info(f'[スキャルプ→NVモード] 大変動={big_move}')
            state.in_big_move_normal = True
        elif state.in_big_move_normal:
            # 大変動解消: スキャルプパラメーターに復帰
            state.in_big_move_normal = False
            print(f"[NV→スキャルプ復帰] 大変動解消")
            _logger.info('[NV→スキャルプ復帰] 大変動解消')
        _normal_variant = state.in_big_move_normal

        # ── クールダウン中 ─────────────────────────────────────────
        # cooldown_trades 回トレードするごとに cooldown_min 分間のクールダウン
        # 同じ執行条件でスキャルプロジックを継続（クールダウンゲートで入場抑制）
        in_cooldown = (
            state.cooldown_start_at is not None and
            now < state.cooldown_start_at + timedelta(minutes=cooldown)
        )

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

        # ── ネックライン接近: ノーマルモードへ切替 ───────────────────────
        # H1 パターンのネックライン付近（ATR×neckline_approach_atr 以内）に接近したら
        # ノーマルモードに切り替えて大変動エントリーに備える。
        # ポジション保有中はトレーリング継続のため決済までスキャルプを待機させる。
        _nl_enabled = scalp.get('neckline_approach_enabled', True)
        _nl_margin  = atr_v * scalp.get('neckline_approach_atr', 1.5) if _nl_enabled else 0.0
        _near_neckline = False
        if _nl_margin > 0 and _h1_pats_raw:
            for _pnl in _h1_pats_raw:
                if _pnl.confidence < 0.45:
                    continue
                _fp_nl = (_pnl.name, round(_pnl.neckline, 0))
                if _fp_nl in state.pattern_traded:
                    continue
                if _pnl.direction == 'bullish' and buy_enabled and not avoid_buy_surge:
                    if 0 < _pnl.neckline - close_v <= _nl_margin:
                        _near_neckline = True
                        break
                elif _pnl.direction == 'bearish' and sell_enabled and not avoid_sell_surge:
                    if 0 < close_v - _pnl.neckline <= _nl_margin:
                        _near_neckline = True
                        break

        if _near_neckline and not state.near_neckline_normal:
            print(f"[ネックライン接近] NVモード切替 "
                  f"close={close_v:.2f} margin={_nl_margin:.2f}")
            _logger.info(f'[ネックライン接近] NVモード切替 close={close_v:.2f}')
            state.near_neckline_normal = True

        if state.near_neckline_normal:
            _magic_id = cfg['MT5'].get('magic', 20240101)
            try:
                _all_pos = mt5.positions_get(symbol=symbol) or []
                _has_pos = any(getattr(p, 'magic', 0) == _magic_id for p in _all_pos)
            except Exception:
                _has_pos = True  # 取得失敗時は安全のためポジションありと仮定

            if _has_pos or _near_neckline:
                # ノーマルバリアントパラメーターでスキャルプロジックを継続
                _normal_variant = True
            else:
                # ポジションなし + ネックライン付近でもない → スキャルプ復帰
                state.near_neckline_normal = False
                print(f"[ネックライン解消] スキャルプモードに復帰")
                _logger.info('[ネックライン解消] スキャルプモードに復帰')

        # ── M1 待機ロジック ────────────────────────────────────────
        confirmed_signal  = None
        candidate_signal  = None
        crossed_level     = 0.0
        _direct_confirmed  = False  # EW2/H1パターン由来: 一部ゲートをスキップ
        _is_ew2_signal     = False  # EW2専用フラグ: RSIゲートをバイパスする
        _is_scalein_signal = False  # RSIスケールイン由来: M1 RSI極端値ゲートをスキップ
        _lot_frac          = 1.0    # ロット倍率（SMA優先=1.0、スケールイン=rsi_scalein_lot_frac）
        # _normal_variant は big_move/neckline セクションで既にセット済み

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
            _ew_lb   = _ew_cfg.get('lookback_bars', 100)
            _ew_sw   = _ew_cfg.get('sw_window', 3)
            _ew_fmin = _ew_cfg.get('fib_min', 0.382)
            _ew_fmax = _ew_cfg.get('fib_max', 0.786)
            _ew_w1at = _ew_cfg.get('min_wave1_atr', 1.5)
            _ew_div  = _ew_cfg.get('rsi_div_min', 3.0)
            _ew_bago = _ew_cfg.get('w2_bars_ago_max', 5)
            _df_ew2  = df_m5_ew2 if df_m5_ew2 is not None else df  # M5専用df優先
            if buy_enabled and not avoid_buy_surge and mtf_ew2_buy_ok:
                _ew2b = detect_elliott_w2_buy(
                    _df_ew2, lookback=_ew_lb, sw_window=_ew_sw,
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
                        _is_ew2_signal    = True
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
                    _df_ew2, lookback=_ew_lb, sw_window=_ew_sw,
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
                        _is_ew2_signal    = True
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

        # ── SMA 優先自動待機セット ─────────────────────────────────────────
        # RSI クロスを待たずに MTF 条件 + RSI50 方向フィルターで sma_pending を自動セット
        _sma_cooldown_s = scalp.get('sma_watch_cooldown_s', 60)
        _just_entered   = (state.last_at is not None and
                           (now - state.last_at).total_seconds() < _sma_cooldown_s)
        if (confirmed_signal is None and not _just_entered and not in_cooldown
                and not state.buy_sma_pending  and not state.sell_sma_pending
                and not state.buy_confirm_pending and not state.sell_confirm_pending):
            if buy_enabled and not avoid_buy_surge and mtf_buy_ok and rsi_cur >= 50.0:
                state.buy_sma_pending = True
                state.buy_sma_at      = now
                state.buy_sma_level   = 50.0
                print(f"[SMA優先AUTO-WATCH/BUY] RSI={rsi_cur:.1f} MTF=OK")
            elif sell_enabled and not avoid_sell_surge and mtf_sell_ok and rsi_cur < 50.0:
                state.sell_sma_pending = True
                state.sell_sma_at      = now
                state.sell_sma_level   = 50.0
                print(f"[SMA優先AUTO-WATCH/SELL] RSI={rsi_cur:.1f} MTF=OK")

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
            if confirmed_signal is None and sell_enabled and not state.sell_sma_pending and not state.buy_sma_pending and not avoid_sell_surge:
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
                    close_m1  = float(df_m1['Close'].iloc[-1]) if (df_m1 is not None and not df_m1.empty) else close_v
                    _open_m1  = (float(df_m1['Open'].iloc[-1])
                                 if (df_m1 is not None and 'Open' in df_m1.columns) else close_m1)
                    _bar_down = close_m1 < _open_m1   # 現在M1バーが陰線（方向確認済み）
                    # ── SMA20 バイパス: 価格が SMA20 より touch_margin 以上下方 ──
                    if close_m1 < sma20_m1 - touch_margin:
                        if mtf_sell_ok:
                            state.sell_sma_pending = False
                            state.sell_sma_at      = None
                            if _bar_down:
                                # 現在バーが陰線 → confirm_pending をスキップして即エントリー
                                confirmed_signal = 'sell'
                                crossed_level    = state.sell_sma_level
                                state.sell_scalein_rsi_done.clear()
                                print(f"[SELL 即エントリー/bypass] 乖離={sma20_m1-close_m1:.1f}")
                            else:
                                # 現在バーが陽線 → confirm_pending で次バー待ち
                                state.sell_confirm_pending  = True
                                state.sell_confirm_at       = now
                                state.sell_confirm_count    = 0
                                state.sell_confirm_bar_time = None
                                state.sell_confirm_level    = state.sell_sma_level
                                print(f"[SELL SMA20バイパス] 乖離={sma20_m1-close_m1:.1f} > マージン={touch_margin:.1f}")
                        # mtf_sell_ok=False でもペンディング継続（次ポールで再チェック）
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
                        if sma20_slope_ok and mtf_sell_ok:
                            state.sell_sma_pending = False
                            state.sell_sma_at      = None
                            if _bar_down:
                                # SMA20タッチ + 陰線 → 即エントリー（最良ポイント）
                                confirmed_signal = 'sell'
                                crossed_level    = state.sell_sma_level
                                state.sell_scalein_rsi_done.clear()
                                print(f"[SELL 即エントリー/touch] SMA20={sma20_m1:.1f}")
                            else:
                                # 陽線中 → confirm_pending で方向反転待ち
                                state.sell_confirm_pending  = True
                                state.sell_confirm_at       = now
                                state.sell_confirm_count    = 0
                                state.sell_confirm_bar_time = None
                                state.sell_confirm_level    = state.sell_sma_level
                        # sma20_slope_ok=False or mtf_sell_ok=False → ペンディング継続

        # SELL 下落確認: SMA20タッチ後 M1 下落バー 1 本
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
                open_m1_cur  = (float(df_m1['Open'].iloc[-1])
                                if 'Open' in df_m1.columns else close_m1_prv)
                sma20_m1_c   = (float(df_m1['SMA20'].iloc[-1])
                                if 'SMA20' in df_m1.columns else float('nan'))
                # M1 上昇トレンド中（close > SMA20）の下落バーはカウントしない
                trend_ok     = np.isnan(sma20_m1_c) or close_m1_cur <= sma20_m1_c
                # 半足モード: 現在バーが自身のオープンより下 → 早期検出
                # 通常モード: 前バーの終値より下 → バー確定後に検出
                _half_bar    = scalp.get('m1_confirm_half_bar', True)
                is_down_bar  = (close_m1_cur < open_m1_cur) if _half_bar else (close_m1_cur < close_m1_prv)

                # mtf_sell_ok を confirm ループでも再チェック（confirm_pending 設定後に
                # M5 SMA20 傾きが変化した場合のゲートバイパスを防ぐ）
                if is_down_bar and trend_ok and mtf_sell_ok and m1_bar_cur != state.sell_confirm_bar_time:
                    state.sell_confirm_count   += 1
                    state.sell_confirm_bar_time = m1_bar_cur
                elif not is_down_bar or not trend_ok or not mtf_sell_ok:
                    state.sell_confirm_count    = 0
                    state.sell_confirm_bar_time = None

                if state.sell_confirm_count >= 1:
                    confirmed_signal            = 'sell'
                    crossed_level               = state.sell_confirm_level
                    state.sell_scalein_rsi_done.clear()
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
                    close_m1  = float(df_m1['Close'].iloc[-1]) if (df_m1 is not None and not df_m1.empty) else close_v
                    _open_m1  = (float(df_m1['Open'].iloc[-1])
                                 if (df_m1 is not None and 'Open' in df_m1.columns) else close_m1)
                    _bar_up   = close_m1 > _open_m1   # 現在M1バーが陽線（方向確認済み）
                    # ── SMA20 バイパス: 価格が SMA20 より touch_margin 以上上方 ──
                    if close_m1 > sma20_m1 + touch_margin:
                        if mtf_buy_ok:
                            state.buy_sma_pending = False
                            state.buy_sma_at      = None
                            if _bar_up:
                                # 現在バーが陽線 → confirm_pending をスキップして即エントリー
                                confirmed_signal = 'buy'
                                crossed_level    = state.buy_sma_level
                                state.buy_scalein_rsi_done.clear()
                                print(f"[BUY 即エントリー/bypass] 乖離={close_m1-sma20_m1:.1f}")
                            else:
                                # 現在バーが陰線 → confirm_pending で次バー待ち
                                state.buy_confirm_pending  = True
                                state.buy_confirm_at       = now
                                state.buy_confirm_count    = 0
                                state.buy_confirm_bar_time = None
                                state.buy_confirm_level    = state.buy_sma_level
                                print(f"[BUY SMA20バイパス] 乖離={close_m1-sma20_m1:.1f} > マージン={touch_margin:.1f}")
                        # mtf_buy_ok=False でもペンディング継続（次ポールで再チェック）
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
                        if sma20_slope_ok and mtf_buy_ok:
                            state.buy_sma_pending = False
                            state.buy_sma_at      = None
                            if _bar_up:
                                # SMA20タッチ + 陽線 → 即エントリー（最良ポイント）
                                confirmed_signal = 'buy'
                                crossed_level    = state.buy_sma_level
                                state.buy_scalein_rsi_done.clear()
                                print(f"[BUY 即エントリー/touch] SMA20={sma20_m1:.1f}")
                            else:
                                # 陰線中 → confirm_pending で方向反転待ち
                                state.buy_confirm_pending  = True
                                state.buy_confirm_at       = now
                                state.buy_confirm_count    = 0
                                state.buy_confirm_bar_time = None
                                state.buy_confirm_level    = state.buy_sma_level
                        # sma20_slope_ok=False or mtf_buy_ok=False → ペンディング継続

        # BUY 上昇確認: SMA20タッチ後 M1 上昇バー 1 本
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
                open_m1_cur  = (float(df_m1['Open'].iloc[-1])
                                if 'Open' in df_m1.columns else close_m1_prv)
                sma20_m1_c   = (float(df_m1['SMA20'].iloc[-1])
                                if 'SMA20' in df_m1.columns else float('nan'))
                # M1 下落トレンド中（close < SMA20）の上昇バーはカウントしない
                trend_ok     = np.isnan(sma20_m1_c) or close_m1_cur >= sma20_m1_c
                # 半足モード: 現在バーが自身のオープンより上 → 早期検出
                # 通常モード: 前バーの終値より上 → バー確定後に検出
                _half_bar    = scalp.get('m1_confirm_half_bar', True)
                is_up_bar    = (close_m1_cur > open_m1_cur) if _half_bar else (close_m1_cur > close_m1_prv)

                # mtf_buy_ok を confirm ループでも再チェック（confirm_pending 設定後に
                # M5 SMA20 傾きが変化した場合のゲートバイパスを防ぐ）
                if is_up_bar and trend_ok and mtf_buy_ok and m1_bar_cur != state.buy_confirm_bar_time:
                    state.buy_confirm_count   += 1
                    state.buy_confirm_bar_time = m1_bar_cur
                elif not is_up_bar or not trend_ok or not mtf_buy_ok:
                    state.buy_confirm_count    = 0
                    state.buy_confirm_bar_time = None

                if state.buy_confirm_count >= 1:
                    confirmed_signal           = 'buy'
                    crossed_level              = state.buy_confirm_level
                    state.buy_scalein_rsi_done.clear()
                    state.buy_confirm_pending  = False
                    state.buy_confirm_at       = None
                    state.buy_confirm_count    = 0
                    state.buy_confirm_bar_time = None

        # ── RSI スケールイン ──────────────────────────────────────────────
        # SMA優先エントリー後、RSI が方向継続でクロスしたら追加エントリー
        _si_enabled     = scalp.get('rsi_scalein_enabled', True)
        _si_lot_frac    = scalp.get('rsi_scalein_lot_frac', 0.5)
        _si_max         = scalp.get('rsi_scalein_max', 2)
        _si_window_min  = scalp.get('rsi_scalein_window_min', 30)
        if (_si_enabled and confirmed_signal is None and not in_cooldown
                and state.last_at is not None
                and (now - state.last_at).total_seconds() <= _si_window_min * 60
                and state.last_action in ('buy', 'sell')):
            if (state.last_action == 'buy' and buy_enabled and not avoid_buy_surge
                    and mtf_buy_ok):
                for thr in buy_thrs:
                    if (thr not in state.buy_scalein_rsi_done
                            and rsi_cur > thr and rsi_prev_bar <= thr
                            and len(state.buy_scalein_rsi_done) < _si_max):
                        confirmed_signal   = 'buy'
                        crossed_level      = thr
                        _lot_frac          = _si_lot_frac
                        _is_scalein_signal = True
                        state.buy_scalein_rsi_done.add(thr)
                        print(f"[BUY スケールイン] RSI={rsi_cur:.1f} thr={thr:.0f}"
                              f" 済={sorted(state.buy_scalein_rsi_done)}")
                        break
            elif (state.last_action == 'sell' and sell_enabled and not avoid_sell_surge
                    and mtf_sell_ok):
                for thr in sell_thrs:
                    if (thr not in state.sell_scalein_rsi_done
                            and rsi_cur < thr and rsi_prev_bar >= thr
                            and len(state.sell_scalein_rsi_done) < _si_max):
                        confirmed_signal   = 'sell'
                        crossed_level      = thr
                        _lot_frac          = _si_lot_frac
                        _is_scalein_signal = True
                        state.sell_scalein_rsi_done.add(thr)
                        print(f"[SELL スケールイン] RSI={rsi_cur:.1f} thr={thr:.0f}"
                              f" 済={sorted(state.sell_scalein_rsi_done)}")
                        break

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
            # M1 RSI 極端値ゲート: EW2・スケールイン免除（RSI継続上昇/下落でM1が極端になりうる）
            elif (not _is_ew2_signal and not _is_scalein_signal and not np.isnan(rsi_m1_cur)
                  and rsi_m1_cur >= scalp.get('m1_rsi_ob_gate', 70.0)):
                skip = f'M1 RSI{rsi_m1_cur:.1f}≥{scalp.get("m1_rsi_ob_gate", 70.0):.0f} 過熱 エントリー禁止'
            elif (not _is_ew2_signal and not _is_scalein_signal and not np.isnan(rsi_m1_cur)
                  and rsi_m1_cur <= scalp.get('m1_rsi_os_gate', 30.0)):
                skip = f'M1 RSI{rsi_m1_cur:.1f}≤{scalp.get("m1_rsi_os_gate", 30.0):.0f} 売られすぎ エントリー禁止'
            # M1 SMA20 絶対ゲート: EW2は免除（W2形成中はM1下落が正常）
            elif new_cross == 'buy' and not _is_ew2_signal and not _sma20_m1_buy_ok:
                skip = 'M1 SMA20下落中 BUY絶対禁止'
            elif new_cross == 'sell' and not _is_ew2_signal and not _sma20_m1_sell_ok:
                skip = 'M1 SMA20上昇中 SELL絶対禁止'
            # M5 SMA20 2階微分ゲート（シグナル確認）: EW2免除（W2形成中の減速は正常）
            elif new_cross == 'buy' and not _is_ew2_signal and not _sma20_m5_accel_buy_ok:
                skip = 'M5 SMA20傾き減速中 BUY禁止'
            elif new_cross == 'sell' and not _is_ew2_signal and not _sma20_m5_accel_sell_ok:
                skip = 'M5 SMA20傾き減速中 SELL禁止'
            # M1 SMA20 2階微分ゲート（執行確認）: EW2免除
            elif new_cross == 'buy' and not _is_ew2_signal and not _sma20_m1_accel_buy_ok:
                skip = 'M1 SMA20傾き減速中 BUY禁止'
            elif new_cross == 'sell' and not _is_ew2_signal and not _sma20_m1_accel_sell_ok:
                skip = 'M1 SMA20傾き減速中 SELL禁止'
            # M5 SMA20 価格位置ゲート: EW2 + _direct_confirmed 免除
            # （H1パターンはネックライン突破時にSMA20を跨ぐ形で発動するため）
            elif (new_cross == 'buy' and not _is_ew2_signal and not _direct_confirmed
                  and not np.isnan(sma20_m5_val) and sma20_m5_val > 0
                  and close_v < sma20_m5_val):
                skip = f'M5 SMA20下({close_v:,.0f}<{sma20_m5_val:,.0f}) BUY禁止'
            elif (new_cross == 'sell' and not _is_ew2_signal and not _direct_confirmed
                  and not np.isnan(sma20_m5_val) and sma20_m5_val > 0
                  and close_v > sma20_m5_val):
                skip = f'M5 SMA20上({close_v:,.0f}>{sma20_m5_val:,.0f}) SELL禁止'
            elif new_cross == 'buy' and not _is_ew2_signal and not _sma20_d1_buy_ok:
                skip = 'D1 SMA20下落中 BUY禁止(EW2除外)'
            elif new_cross == 'sell' and not _is_ew2_signal and not _sma20_d1_sell_ok:
                skip = 'D1 SMA20上昇中 SELL禁止(EW2除外)'
            # M5/M15 SMA20 コンセンサス: EW2は免除（W2押し目でM5下落は正常）
            elif new_cross == 'buy' and not _is_ew2_signal and not _sma20_consensus_buy:
                skip = 'SMA20下落(M5/M15両方負) BUY禁止'
            elif new_cross == 'sell' and not _is_ew2_signal and not _sma20_consensus_sell:
                skip = 'SMA20上昇(M5/M15両方正) SELL禁止'
            elif new_cross == 'buy' and not _is_ew2_signal and rsi_cur < scalp.get('rsi_buy_gate_min', 40.0):
                skip = f'RSI{rsi_cur:.1f}<BUY最低閾値{scalp.get("rsi_buy_gate_min", 40.0):.0f} 禁止'
            elif new_cross == 'sell' and not _is_ew2_signal and rsi_cur > scalp.get('rsi_sell_gate_max', 60.0):
                skip = f'RSI{rsi_cur:.1f}>SELL最高閾値{scalp.get("rsi_sell_gate_max", 60.0):.0f} 禁止'
            elif (not _direct_confirmed and
                  ((regime_m5s == 'trend_up'   and new_cross == 'sell') or
                   (regime_m5s == 'trend_down' and new_cross == 'buy'))):
                skip = f'逆トレンドエントリー禁止(regime={regime_m5s})'
            elif eff_hour in {21}:
                skip = f'forbidden_hour={eff_hour}'
            elif state.count >= max_day:
                skip = f'daily_limit={state.count}/{max_day}'
            elif (not _normal_variant and
                  state.cooldown_start_at is not None and
                  now < state.cooldown_start_at + timedelta(minutes=cooldown)):
                rem  = int((state.cooldown_start_at + timedelta(minutes=cooldown) - now).total_seconds() / 60)
                skip = f'cooldown残{rem}分({cooldown_trades}回毎)'
            elif pos_st['available_slots'] <= 0:
                opp_dir = 'sell' if new_cross == 'buy' else 'buy'
                if _has_positions_in_direction(symbol, magic_id, opp_dir, mt5=mt5):
                    # 逆方向にポジションあり → ヘッジ許可
                    action            = new_cross
                    state.last_action = new_cross
                    state.count      += 1
                    state.last_at     = now
                    if state.count % cooldown_trades == 0:
                        state.cooldown_start_at = now
                else:
                    skip = (f"max_positions={pos_st['max_positions']}に到達"
                            f"（全{pos_st['total_positions']}本）")
            else:
                action          = new_cross
                state.last_action = new_cross
                state.count    += 1
                state.last_at   = now
                if state.count % cooldown_trades == 0:
                    state.cooldown_start_at = now

        # ── ロット倍率適用（スケールイン= rsi_scalein_lot_frac）──────────
        if _lot_frac != 1.0 and action in ('buy', 'sell'):
            lot = max(l_min, round(lot * _lot_frac / l_step) * l_step)
            expected_profit_usd = tp_move * contract_size * lot
            expected_profit_jpy = expected_profit_usd * jpy_rate

        # ── ノーマルバリアント: 拡張TP/SL/ロット上書き ────────────────────
        if _normal_variant and action in ('buy', 'sell'):
            # lot は NV 基準（スケールイン倍率も適用）
            lot     = max(l_min, round(_nv_lot * _lot_frac / l_step) * l_step) if _lot_frac != 1.0 else _nv_lot
            tp_move = _nv_tp_move
            sl_move = _nv_sl_move
            expected_profit_usd = tp_move * contract_size * lot
            expected_profit_jpy = expected_profit_usd * jpy_rate

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

        # ── 証拠金維持率チェック: lot を上限調整 ────────────────────────
        _min_ml     = scalp.get('min_margin_level', 200.0)
        _acc_equity = 0.0
        _acc_margin = 0.0
        _acc_ml     = 0.0   # 現在の維持率（表示用）
        try:
            _acc = mt5.account_info()
            if _acc is not None:
                _acc_equity = float(_acc.equity)
                _acc_margin = float(_acc.margin)
                _acc_ml     = (_acc_equity / _acc_margin * 100.0
                               if _acc_margin > 0 else 999.9)
        except Exception as _ml_err:
            _logger.warning(f'[証拠金] account_info 失敗: {_ml_err}')

        if action in ('buy', 'sell') and _min_ml > 0 and _acc_equity > 0:
            try:
                _order_type = (mt5.ORDER_TYPE_BUY if action == 'buy'
                               else mt5.ORDER_TYPE_SELL)
                _req_margin = mt5.order_calc_margin(_order_type, symbol, lot, close_v)
                if _req_margin is not None and _req_margin > 0:
                    _ml_after = _acc_equity / (_acc_margin + _req_margin) * 100.0
                    if _ml_after < _min_ml:
                        _max_new = _acc_equity * 100.0 / _min_ml - _acc_margin
                        if _max_new > 0:
                            _margin_per_lot = _req_margin / lot
                            _lot_adj = _max_new / _margin_per_lot
                            lot      = max(l_min, round(_lot_adj / l_step) * l_step)
                            _acc_ml  = _acc_equity / (_acc_margin + _margin_per_lot * lot) * 100.0
                            expected_profit_usd = tp_move * contract_size * lot
                            expected_profit_jpy = expected_profit_usd * jpy_rate
                            print(f"  [証拠金] lot縮小→{lot} (ML推定{_acc_ml:.0f}%≥{_min_ml:.0f}%)")
                            _logger.info(f'[証拠金] lot縮小={lot} ML={_acc_ml:.0f}%')
                        else:
                            skip   = (f'証拠金不足(ML現在{_acc_ml:.0f}%'
                                      f'→追加後{_ml_after:.0f}%<{_min_ml:.0f}%)')
                            action = 'none'
                            print(f"  [証拠金] {skip}")
                            _logger.warning(f'[証拠金] {skip}')
            except Exception as _ml_err:
                _logger.warning(f'[証拠金チェック] 失敗: {_ml_err}')

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
            'rsi_m5_prev':        round(rsi_prev_bar, 1),
            'rsi_m1':             round(rsi_m1_cur, 1) if not np.isnan(rsi_m1_cur) else 0.0,
            'm5_filter_ok':       False,
            'm5_surge':           'none',
            'scalp_type':         'none',
            'sma20':              round(sma20_m5_val, 2) if not np.isnan(sma20_m5_val) else 0.0,
            'sl_multi':           round(sl_ratio, 2),
            'action':             action,
            'signal_type':        (_ew2_signal_type if (_ew2_signal_type and action != 'none')
                                   else (f'{"normal" if _normal_variant else "scalp"}_{action}_scalein_{int(crossed_level or 0)}'
                                         if (_is_scalein_signal and action != 'none')
                                         else (f'{"normal" if _normal_variant else "scalp"}_{action}_{int(crossed_level or (state.buy_sma_level if action == "buy" else state.sell_sma_level))}'
                                               if action != 'none' else 'none'))),
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
            'trail_multi':        cfg['SL']['trail_multi'] if (_normal_variant and action != 'none') else 0.0,
            'max_slip_pt':        max_pt,
            'lot_size':           lot,
            'scalp_mode':         not _normal_variant,
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
            'adx_h1':             round(adx_h1s, 1) if not np.isnan(adx_h1s) else 0.0,
            'adx_m5':             round(adx_m5_sv, 1) if not np.isnan(adx_m5_sv) else 0.0,
            'di_plus_h1':         round(dip_h1s, 1) if not np.isnan(dip_h1s) else 0.0,
            'di_minus_h1':        round(dim_h1s, 1) if not np.isnan(dim_h1s) else 0.0,
            'di_plus_m5':         round(dip_m5_sv, 1) if not np.isnan(dip_m5_sv) else 0.0,
            'di_minus_m5':        round(dim_m5_sv, 1) if not np.isnan(dim_m5_sv) else 0.0,
            'sma20_m5':            round(sma20_m5_val,  0) if not np.isnan(sma20_m5_val)  else 0.0,
            'sma20_m1':            round(sma20_m1_val,  0) if not np.isnan(sma20_m1_val)  else 0.0,
            'sma20_m15':           round(sma20_m15_val, 0) if not np.isnan(sma20_m15_val) else 0.0,
            'sma20_d1':            round(sma20_d1_val,  0) if not np.isnan(sma20_d1_val)  else 0.0,
            'sma20_slope_buy_ok':  _sma20_slope_buy_ok,
            'sma20_slope_sell_ok': _sma20_slope_sell_ok,
            'sma20_m1_buy_ok':     _sma20_m1_buy_ok,
            'sma20_m1_sell_ok':    _sma20_m1_sell_ok,
            'sma20_m15_buy_ok':    _sma20_m15_buy_ok,
            'sma20_m15_sell_ok':   _sma20_m15_sell_ok,
            'sma20_d1_buy_ok':        _sma20_d1_buy_ok,
            'sma20_d1_sell_ok':       _sma20_d1_sell_ok,
            'sma20_m5_accel_buy_ok':  _sma20_m5_accel_buy_ok,
            'sma20_m5_accel_sell_ok': _sma20_m5_accel_sell_ok,
            'sma20_m1_accel_buy_ok':  _sma20_m1_accel_buy_ok,
            'sma20_m1_accel_sell_ok': _sma20_m1_accel_sell_ok,
            'regime_h1':          regime_h1s,
            'regime_m5':          regime_m5s,
            'regime_lot_multi':   round(r_multi_s, 2),
            'entry_in_window':    0,
            'mtf_buy_ok':         mtf_buy_ok,
            'mtf_sell_ok':        mtf_sell_ok,
            'cooldown_trades':    cooldown_trades,
            'trades_cd_cycle':    state.count % max(1, cooldown_trades),
            'h1_patterns':        h1_pattern_bars,
            'pattern_tp_target':  state.pattern_tp_target,
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
            # 証拠金情報
            'margin_level':       round(_acc_ml, 1),
            'account_equity':     round(_acc_equity, 2),
            'account_margin':     round(_acc_margin, 2),
            'min_margin_level':   _min_ml,
        }

    except Exception:
        _logger.exception("[スキャルプ] compute_scalp_signal 例外")
        return None
