"""
mt5_ea_bridge.py — MT5 EA リアルタイム連携ブリッジ
====================================================
Python でシグナル・SL水準を計算 → signal.json に書き込む
MT5 EA が OnTimer() で読み込み、注文を執行する

実行:
    python mt5_ea_bridge.py           # ポーリングループ（Ctrl+C で終了）
    python mt5_ea_bridge.py --once    # 1回だけ計算して終了（動作確認用）
    python mt5_ea_bridge.py --symbol BTCUSD --lot 0.05

通信プロトコル:
    Python → MT5 EA : output/signal.json   （毎ポーリング更新）
    MT5 EA → Python : output/ea_state.json （EA が書き込む状態）

ルール適用（trading_rules.json）:
    - 買いのみ（売りは構造的損失）
    - 禁止時間帯（UTC 9/16/21）はスキップ
    - 金曜日はスキップ
    - H1/D1 RSI ゾーン + クロスフィルターで品質スコア算出
    - スコア < min_score のシグナルはスキップ
    - EA 連続損失 >= 3 回でその日停止
"""
import sys, json, time, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
import numpy as np


sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import connect_mt5, fetch_ohlcv
from core.indicators import add_h1_indicators, add_d1_indicators, add_m5_indicators
from core.strategy   import check_m5_entry_filter, check_m5_surge, detect_big_move

CFG = {k: getattr(C, k) for k in
       ['MT5','INDICATOR','SIGNAL','EXECUTION','SL','RULES','LOCAL','PLOT','BRIDGE','SCALP','REGIME']}

# ── RulesEngine ロード（なければフィルタなし）────────────────
try:
    from trading_rules import RulesEngine
    _engine = RulesEngine()
    print("[ルール] trading_rules.json 読み込み完了")
except Exception as _e:
    _engine = None
    print(f"[ルール] trading_rules 読み込み失敗: {_e} → フィルタなし")

# ── シグナル状態（クロス検出用）─────────────────────────────
_prev_rsi_h1: float | None = None          # 前回ポーリング時の H1 RSI
_signal_active: dict       = {             # 現在有効なシグナルウィンドウ
    'type':  None,                         # 'dip' / 'momentum_55' / ...
    'until': None,                         # 有効期限 (datetime UTC)
}
_prev_rsi_m1:       float | None    = None    # 前回ポーリング時の M1 RSI（反発クロス検出用）
_rapid_fall_at:     datetime | None = None    # 直近 rapid_fall 発生時刻

# ── レジーム・分散エントリー状態 ─────────────────────────────────
_entry_in_window:      int   = 0      # 現シグナルウィンドウ内の BUY エントリー回数
_last_entry_price:     float = 0.0   # 直近 BUY エントリー価格
_signal_window_key:    tuple = (None, None)  # ウィンドウリセット検出用

_sell_entry_in_window: int   = 0
_sell_last_entry_price:float = 0.0
_sell_window_key:      tuple = (None, None)

# ── スキャルプモード状態 ─────────────────────────────────────
_scalp_prev_rsi: float | None    = None   # 前回足 RSI（クロス検出）
_scalp_last_at:  datetime | None = None   # 直近エントリーシグナル時刻
_scalp_count:       int              = 0      # 当日シグナル発火回数
_scalp_date:        object           = None   # 日付リセット管理
_scalp_last_action: str              = 'none' # 直近スキャルプエントリー方向（大変動継続判定用）
_signal_sell_active: dict          = {        # 下落トレンドフォロー SELL ウィンドウ
    'type':  None,
    'until': None,
}

# ── JPY/USD レートキャッシュ（1時間更新）────────────────────────
_jpy_per_usd_cache: float          = 150.0
_jpy_per_usd_at:    datetime | None = None


def _get_jpy_per_usd(fallback: float = 150.0) -> float:
    """USDJPY レートを MT5 から取得し 1 時間キャッシュする。"""
    global _jpy_per_usd_cache, _jpy_per_usd_at
    now = datetime.now(timezone.utc)
    if _jpy_per_usd_at is not None and (now - _jpy_per_usd_at).total_seconds() < 3600:
        return _jpy_per_usd_cache
    try:
        import MetaTrader5 as mt5
        tick = mt5.symbol_info_tick("USDJPY")
        if tick and tick.bid > 0:
            _jpy_per_usd_cache = float(tick.bid)
            _jpy_per_usd_at    = now
            print(f"[JPY/USD] 更新: {_jpy_per_usd_cache:.3f}")
            return _jpy_per_usd_cache
    except Exception:
        pass
    # MT5 未対応ブローカー (USDJPY 非上場) のフォールバック
    if _jpy_per_usd_at is None:
        print(f"[JPY/USD] USDJPY 取得失敗 → フォールバック {fallback}")
    return fallback


# ── ロットサイズ計算ユーティリティ ──────────────────────────────

def _calc_lot(balance: float, risk_pct: float, sl_dist: float,
              contract_size: float,
              lot_min: float, lot_max: float, lot_step: float,
              fallback: float) -> float:
    """
    残高ベースのロットサイズ計算。
    risk_usd = balance × risk_pct をリスク許容額とし、
    SL距離（価格）× コントラクトサイズ で割ってロットを求める。
    """
    if sl_dist <= 0 or contract_size <= 0:
        return fallback
    risk_usd = balance * risk_pct
    lot = risk_usd / (sl_dist * contract_size)
    lot = round(lot / lot_step) * lot_step
    return max(lot_min, min(lot_max, lot))


def _position_status(risk_pct: float, total_risk_pct: float) -> dict:
    """
    全ポジション（手動エントリー含む）をカウントし、最大許容数と空きスロット数を返す。
    max_positions = floor(total_risk_pct / risk_pct)  例: 0.20/0.01 = 20
    """
    max_positions = max(1, int(total_risk_pct / risk_pct))
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get()
        total = len(positions) if positions is not None else 0
    except Exception:
        total = 0
    return {
        'max_positions':   max_positions,
        'total_positions': total,
        'available_slots': max(0, max_positions - total),
    }


def _detect_regime(adx: float, di_plus: float, di_minus: float,
                   regime_cfg: dict) -> str:
    """
    ADX に基づくレジーム判定。
    returns: 'trend_up' | 'trend_down' | 'weak_trend' | 'range'
    """
    if np.isnan(adx):
        return 'range'
    trend_thr = regime_cfg.get('trend_thr', 25.0)
    range_thr = regime_cfg.get('range_thr', 20.0)
    if adx >= trend_thr:
        return 'trend_up' if di_plus > di_minus else 'trend_down'
    if adx >= range_thr:
        return 'weak_trend'
    return 'range'


def _regime_lot_multi(regime_h1: str, regime_m5: str, regime_cfg: dict) -> float:
    """
    H1 / M5 のレジームからロット倍率を返す。
    両方トレンド → lot_multi_trend (1.5×)
    片方トレンド → lot_multi_weak  (1.0×)
    両方レンジ   → lot_multi_range (0.6×)
    """
    t_h1 = regime_h1.startswith('trend')
    t_m5 = regime_m5.startswith('trend')
    if t_h1 and t_m5:
        return float(regime_cfg.get('lot_multi_trend', 1.5))
    if t_h1 or t_m5:
        return float(regime_cfg.get('lot_multi_weak',  1.0))
    return float(regime_cfg.get('lot_multi_range', 0.6))


# ── リアルタイム指標・シグナル計算 ────────────────────────────

def compute_signal(symbol: str, cfg: dict) -> dict | None:
    """
    バックテストと同じシグナルロジック（クロス検出）でリアルタイムシグナルを生成。

    DIP:      H1 RSI が buy_rsi_thr (40) を下抜けた瞬間
    MOMENTUM: H1 RSI が momentum_thrs (55/60/65/70/75) を上抜けた瞬間
    いずれも signal_valid_m1 分間（デフォルト 240 分）シグナルウィンドウが開く。
    ウィンドウ内かつ RulesEngine=BUY のポーリング時に action='buy' を出力。

    signal.json フォーマット:
      action          : "buy" / "none"
      signal_type     : "dip" / "momentum_55" / ... / "none"
      signal_valid_until: シグナルウィンドウ終了時刻
      score           : RulesEngine スコア (0〜100)
      strength        : "strong" / "normal" / "weak" / "none"
      tp_hold_minutes : TP目安保有時間（分）
      lot_size        : 発注ロット数
      timestamp       : "YYYY.MM.DD HH:MM:SS"（MQL5 StringToTime 互換）
    """
    global _prev_rsi_h1, _signal_active, _signal_sell_active, _prev_rsi_m1, _rapid_fall_at
    global _entry_in_window, _last_entry_price, _signal_window_key
    global _sell_entry_in_window, _sell_last_entry_price, _sell_window_key

    try:
        import MetaTrader5 as mt5

        df_h1_raw = fetch_ohlcv(symbol, 'H1', 200)
        df_d1_raw = fetch_ohlcv(symbol, 'D1', 50)
        df_m5_raw = fetch_ohlcv(symbol, 'M5', 60)
        df_m1_raw = fetch_ohlcv(symbol, 'M1', 30)
        if df_h1_raw is None or df_d1_raw is None:
            return None

        df_h1 = add_h1_indicators(df_h1_raw, cfg)
        df_d1 = add_d1_indicators(df_d1_raw, cfg)
        if df_h1.empty or df_d1.empty or 'SMA20' not in df_h1.columns:
            return None

        last     = df_h1.iloc[-1]
        close_v  = float(last['Close'])
        atr_v    = float(last['ATR'])
        rsi_h1_v = float(last['RSI'])
        sma20    = float(last['SMA20'])
        rsi_d1_v = float(df_d1['RSI'].iloc[-1])

        # H1 ADX（レジーム判定用）
        adx_h1_v  = float(last['ADX'])    if 'ADX'      in df_h1.columns else float('nan')
        dip_h1    = float(last['DI_plus']) if 'DI_plus'  in df_h1.columns else float('nan')
        dim_h1    = float(last['DI_minus'])if 'DI_minus' in df_h1.columns else float('nan')

        # M5 RSI（直近2本で rising 判定 + 急騰急落検出）
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

        # M1 RSI（スキャルプ判定用: RSI(14) を M1 足で計算）
        rsi_m1_cur      = float('nan')
        rsi_m1_bar_prev = float('nan')   # 直前 M1 バーとの比較（rising 判定）
        if df_m1_raw is not None and len(df_m1_raw) >= 3:
            df_m1 = add_m5_indicators(df_m1_raw, cfg)   # RSI(14) ロジック共用
            if not df_m1.empty and len(df_m1) >= 2:
                rsi_m1_cur      = float(df_m1['RSI'].iloc[-1])
                rsi_m1_bar_prev = float(df_m1['RSI'].iloc[-2])

        sl_multi       = cfg['SL']['sl_multi']
        sig_p          = cfg['SIGNAL']
        buy_thr        = sig_p.get('buy_rsi_thr', 40.0)
        mom_thrs       = sorted(sig_p.get('momentum_thrs', [55.0, 60.0, 65.0, 70.0, 75.0]))
        sell_mom_thrs  = sorted(sig_p.get('momentum_sell_thrs', [55.0, 50.0, 45.0, 40.0, 35.0]),
                                reverse=True)  # 大→小 順にチェック
        downtrend_thr  = sig_p.get('downtrend_d1_rsi', 45.0)
        valid_min      = cfg['EXECUTION'].get('signal_valid_m1', 240)

        # 下落トレンド判定: D1 RSI < 45 かつ close が H1 SMA20 を下回る
        downtrend_ok = (rsi_d1_v < downtrend_thr and close_v < sma20)

        now      = datetime.now(timezone.utc)
        hour_utc = now.hour
        dow      = now.weekday()

        # ── クロス検出（BUY + SELL）────────────────────────────
        new_buy_type  = None
        new_sell_type = None
        if _prev_rsi_h1 is not None:
            # BUY: DIP / MOMENTUM
            if rsi_h1_v < buy_thr and _prev_rsi_h1 >= buy_thr:
                new_buy_type = 'dip'
            else:
                for thr in mom_thrs:
                    if rsi_h1_v > thr and _prev_rsi_h1 <= thr:
                        new_buy_type = f'momentum_{int(thr)}'
                        break
            # SELL: 下落トレンドフォロー（D1 RSI < 45 かつ close < SMA20 のときのみ）
            if downtrend_ok:
                for thr in sell_mom_thrs:
                    if rsi_h1_v < thr and _prev_rsi_h1 >= thr:
                        new_sell_type = f'sell_{int(thr)}'
                        break
        _prev_rsi_h1 = rsi_h1_v

        # BUY ウィンドウ
        if new_buy_type:
            _signal_active['type']  = new_buy_type
            _signal_active['until'] = now + timedelta(minutes=valid_min)
        in_window = (
            _signal_active['until'] is not None and
            now <= _signal_active['until']
        )
        sig_type = _signal_active['type'] if in_window else 'none'

        # SELL ウィンドウ（downtrend_ok が継続している間だけ有効）
        if new_sell_type:
            _signal_sell_active['type']  = new_sell_type
            _signal_sell_active['until'] = now + timedelta(minutes=valid_min)
        in_sell_window = (
            _signal_sell_active['until'] is not None and
            now <= _signal_sell_active['until'] and
            downtrend_ok  # ウィンドウ内でもトレンドが反転したら無効
        )
        sell_sig_type = _signal_sell_active['type'] if in_sell_window else 'none'

        # ── RulesEngine フィルタ ────────────────────────────
        active_buy      = in_window
        score           = 0
        strength        = 'none'
        tp_hold_minutes = 0
        skip_reason     = ''

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

            if result.signal != 'BUY':
                active_buy  = False
                skip_reason = ' | '.join(result.reasons[:2])

        # ── 急騰急落スキャルプ判定 ──────────────────────────────
        # surge が検出されたとき H1 ウィンドウ/RulesEngine の結果を上書きして
        # スキャルプシグナルを生成する。連続損失チェックは run_bridge() に委ねる。

        # rapid_fall 発生時刻を記録（反発ウィンドウ管理用）
        if surge == 'rapid_fall':
            _rapid_fall_at = now

        scalp_type = 'none'

        if surge == 'rapid_rise' and not np.isnan(rsi_m1_cur):
            # ① サージ継続スキャルプ
            #    M1 RSI が 55〜70 かつ上昇中 → 勢いが持続、過熱未達
            if 55 <= rsi_m1_cur <= 70 and rsi_m1_cur > rsi_m1_bar_prev:
                scalp_type  = 'surge_scalp'
                sig_type    = 'surge_scalp'
                active_buy  = True
                skip_reason = ''
            else:
                active_buy  = False
                skip_reason = f'm5_surge=rapid_rise(m1={rsi_m1_cur:.1f},要55-70↑)'

        elif surge == 'rapid_fall' and not np.isnan(rsi_m1_cur):
            # ② 急落後反発スキャルプ
            #    rapid_fall から 60 分以内 かつ M1 RSI が 30 を上抜けた瞬間
            in_rebound_window = (
                _rapid_fall_at is not None and
                now <= _rapid_fall_at + timedelta(minutes=60)
            )
            cross_up_30 = (
                _prev_rsi_m1 is not None and
                _prev_rsi_m1 <= 30 and rsi_m1_cur > 30
            )
            if in_rebound_window and cross_up_30:
                scalp_type  = 'rebound_scalp'
                sig_type    = 'rebound_scalp'
                active_buy  = True
                skip_reason = ''
            else:
                active_buy  = False
                skip_reason = (f'm5_surge=rapid_fall'
                               f'(m1={rsi_m1_cur:.1f},待機中)')

        elif surge != 'none' and active_buy:
            # その他の surge（rapid_rise で M1 データ未取得など）はブロック
            active_buy  = False
            skip_reason = f'm5_surge={surge}'

        # M1 RSI をポーリング間クロス検出用に保存
        if not np.isnan(rsi_m1_cur):
            _prev_rsi_m1 = rsi_m1_cur

        # ── SELL アクション決定 ──────────────────────────────────
        # スキャルプが発火していない場合のみ SELL を評価。
        # 禁止時間帯チェックは BUY の RulesEngine 結果を流用（同じ時間ルール）。
        active_sell      = False
        sell_skip_reason = ''
        if scalp_type == 'none' and in_sell_window:
            active_sell = True
            # 禁止時間帯は BUY と共通（RulesEngine が blocked なら SELL も止める）
            if _engine is not None and result.signal != 'BUY':
                # RulesEngine が時間/DOW 起因でブロックしているか確認
                time_blocked = any(
                    kw in r for r in result.reasons
                    for kw in ('hour', 'dow', 'friday', 'weekend', '時', '曜')
                )
                if time_blocked:
                    active_sell      = False
                    sell_skip_reason = ' | '.join(result.reasons[:2])
            if active_sell:
                # SELL が有効なら BUY を抑制（同時ポジション防止）
                active_buy  = False
                skip_reason = 'sell_signal_active'

        # ── アクション / valid_until 文字列 ─────────────────────
        if active_sell:
            action   = 'sell'
            sig_type = sell_sig_type
        elif active_buy:
            action = 'buy'
        else:
            action = 'none'

        valid_until_str = (_signal_active['until'].strftime('%Y.%m.%d %H:%M:%S')
                           if _signal_active['until'] else '')
        sell_valid_until_str = (_signal_sell_active['until'].strftime('%Y.%m.%d %H:%M:%S')
                                if _signal_sell_active['until'] else '')

        # ── SL/TP（方向・種別に応じて切り替え）──────────────────
        if scalp_type == 'surge_scalp':
            sl_price = close_v - atr_v * 0.5
            tp_price = close_v + atr_v * 1.0
        elif scalp_type == 'rebound_scalp':
            sl_price = close_v - atr_v * 0.8
            tp_price = close_v + atr_v * 0.8
        elif action == 'sell':
            # SELL: SL は上、TP は下
            sl_price = close_v + atr_v * sl_multi
            tp_price = close_v - atr_v * cfg['SL']['tp_atr_multi']
        else:
            sl_price = close_v - atr_v * sl_multi
            tp_price = close_v + atr_v * cfg['SL']['tp_atr_multi']

        tick  = mt5.symbol_info(symbol)
        point = tick.point if tick else 0.01
        max_pt = max(1, int(atr_v * 0.5 / point))

        # 残高ベースのロットサイズ計算
        c_sz     = float(tick.trade_contract_size) if tick else 100.0
        l_min    = float(tick.volume_min)          if tick else 0.01
        l_max    = float(tick.volume_max)          if tick else 100.0
        l_step   = float(tick.volume_step)         if tick else 0.01
        account  = mt5.account_info()
        balance_jpy = (float(account.balance) if account
                       else cfg['BRIDGE'].get('fallback_balance', 1_500_000))
        jpy_per_usd = _get_jpy_per_usd(cfg['SCALP'].get('jpy_per_usd', 150.0))
        balance_usd = balance_jpy / jpy_per_usd
        risk_pct    = cfg['BRIDGE'].get('risk_pct', 0.01)

        # ── レジーム判定（H1 + M5 ADX）──────────────────────────
        regime_cfg = cfg.get('REGIME', {})
        regime_h1  = _detect_regime(adx_h1_v, dip_h1, dim_h1, regime_cfg)
        regime_m5  = _detect_regime(adx_m5_v, dip_m5, dim_m5, regime_cfg)
        r_multi    = _regime_lot_multi(regime_h1, regime_m5, regime_cfg)

        lot_base = _calc_lot(balance_usd, risk_pct, atr_v * sl_multi,
                             c_sz, l_min, l_max, l_step,
                             cfg['BRIDGE']['lot_size'])
        lot_size = max(l_min, min(l_max, round(lot_base * r_multi / l_step) * l_step))

        # ── ポジション数チェック（手動エントリー含む全ポジション）──
        total_risk_pct = cfg.get('RULES', {}).get('total_risk_pct', 0.20)
        pos_st = _position_status(risk_pct, total_risk_pct)
        if pos_st['available_slots'] <= 0 and action in ('buy', 'sell'):
            action      = 'none'
            skip_reason = (f"max_positions={pos_st['max_positions']}に到達"
                           f"（全{pos_st['total_positions']}本）")

        # ── 分散エントリーゲート ─────────────────────────────────
        # シグナルウィンドウが変わったらエントリーカウントをリセット
        max_ep   = regime_cfg.get('max_entry_per_signal', 3)
        spacing  = regime_cfg.get('entry_spacing_atr',    0.5)

        if action == 'buy':
            cur_key = (_signal_active['type'], _signal_active['until'])
            if cur_key != _signal_window_key:
                _signal_window_key = cur_key
                _entry_in_window   = 0
                _last_entry_price  = 0.0
            if _entry_in_window >= max_ep:
                action      = 'none'
                skip_reason = f'entry上限{max_ep}回到達'
            elif (_last_entry_price > 0 and
                  close_v > _last_entry_price - spacing * atr_v):
                action      = 'none'
                skip_reason = (f'押し目待ち(現{close_v:.0f}'
                               f'<必要{_last_entry_price - spacing*atr_v:.0f})')
            else:
                _entry_in_window  += 1
                _last_entry_price  = close_v

        elif action == 'sell':
            cur_key = (_signal_sell_active['type'], _signal_sell_active['until'])
            if cur_key != _sell_window_key:
                _sell_window_key        = cur_key
                _sell_entry_in_window   = 0
                _sell_last_entry_price  = 0.0
            if _sell_entry_in_window >= max_ep:
                action      = 'none'
                skip_reason = f'entry上限{max_ep}回到達'
            elif (_sell_last_entry_price > 0 and
                  close_v < _sell_last_entry_price + spacing * atr_v):
                action      = 'none'
                need_v = _sell_last_entry_price + spacing * atr_v
                skip_reason = f'戻り待ち(現{close_v:.0f}需>{need_v:.0f})'
            else:
                _sell_entry_in_window  += 1
                _sell_last_entry_price  = close_v

        return {
            'timestamp':          datetime.now(timezone.utc).strftime('%Y.%m.%d %H:%M:%S'),
            'symbol':             symbol,
            'close':              round(close_v, 2),
            'atr':                round(atr_v,    2),
            'rsi_h1':             round(rsi_h1_v, 1),
            'rsi_d1':             round(rsi_d1_v, 1),
            'rsi_m5':             round(rsi_m5_cur, 1) if not np.isnan(rsi_m5_cur) else 0.0,
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
            'adx_h1':             round(adx_h1_v, 1) if not np.isnan(adx_h1_v) else 0.0,
            'adx_m5':             round(adx_m5_v, 1) if not np.isnan(adx_m5_v) else 0.0,
            'regime_h1':          regime_h1,
            'regime_m5':          regime_m5,
            'regime_lot_multi':   round(r_multi, 2),
            'entry_in_window':    _entry_in_window,
        }
    except Exception as e:
        print(f"[ブリッジ] 計算エラー: {e}")
        return None


# ── スキャルプモード シグナル計算 ────────────────────────────

def compute_scalp_signal(symbol: str, cfg: dict) -> dict | None:
    """
    スキャルプモード: M5 RSI が rsi_cross_thr を上/下抜けたらエントリー。
    TP/SL は円建て目標利益から価格幅を逆算。
    トレーリング・TP延長は無効（trail_multi=0）。

    制御:
      - 日次 max_trades_day 回を超えたらスキップ
      - 前回エントリーから cooldown_min 分以内はスキップ
      - 禁止時間帯（UTC 9/16/21h）はスキップ
    """
    global _scalp_prev_rsi, _scalp_last_at, _scalp_count, _scalp_date, _scalp_last_action

    try:
        import MetaTrader5 as mt5

        scalp      = cfg.get('SCALP', {})
        jpy_rate   = _get_jpy_per_usd(scalp.get('jpy_per_usd', 150.0))
        target     = scalp.get('target_profit_jpy',  300)
        sl_ratio   = scalp.get('sl_ratio',           1.5)
        sig_tf     = scalp.get('signal_tf',          'M5')
        buy_thrs   = sorted(scalp.get('rsi_buy_thrs',  [50.0, 55.0, 60.0]))         # 小→大
        sell_thrs  = sorted(scalp.get('rsi_sell_thrs', [45.0, 40.0, 35.0]), reverse=True)  # 大→小
        max_day    = scalp.get('max_trades_day',     20)
        cooldown   = scalp.get('cooldown_min',       30)

        now   = datetime.now(timezone.utc)
        today = now.date()

        # 日付をまたいだらカウントリセット
        if _scalp_date != today:
            _scalp_count = 0
            _scalp_date  = today

        # データ取得（スキャルプは M5 のみ。H1 は ATR_MA rolling(50) の都合で空になるため不使用）
        df_raw = fetch_ohlcv(symbol, sig_tf, 50)   # RSI(14)+ATR(14) に余裕を持たせ 50 本
        if df_raw is None:
            return None

        df = add_m5_indicators(df_raw, cfg)
        if df.empty:
            return None

        rsi_cur = float(df['RSI'].iloc[-1])
        close_v = float(df['Close'].iloc[-1])
        atr_v   = float(df['ATR'].iloc[-1])         # M5 ATR（スキャルプに適切な短期ボラ）

        # シンボル情報取得
        info          = mt5.symbol_info(symbol)
        contract_size = float(info.trade_contract_size) if info else 1.0
        l_min         = float(info.volume_min)          if info else 0.01
        l_max         = float(info.volume_max)          if info else 100.0
        l_step        = float(info.volume_step)         if info else 0.01

        # ── レジーム判定（M5 ADX）→ TP距離・ロット計算 ──────────
        regime_cfg = cfg.get('REGIME', {})
        adx_m5_sv  = float(df['ADX'].iloc[-1])    if 'ADX'      in df.columns else float('nan')
        dip_m5_sv  = float(df['DI_plus'].iloc[-1]) if 'DI_plus'  in df.columns else float('nan')
        dim_m5_sv  = float(df['DI_minus'].iloc[-1])if 'DI_minus' in df.columns else float('nan')
        regime_m5s = _detect_regime(adx_m5_sv, dip_m5_sv, dim_m5_sv, regime_cfg)
        # スキャルプはH1不使用のためM5のみで判定（H1は 'weak_trend' 相当として扱う）
        r_multi_s  = _regime_lot_multi('weak_trend', regime_m5s, regime_cfg)

        # TP幅 = M5 ATR × tp_atr_fraction（先に価格距離を決める）
        # lot  = target_usd / (tp_move × contract_size) × regime_multi
        # SL損失 ≈ target_usd × sl_ratio × regime_multi（レジームで調整）
        target_usd    = target / jpy_rate
        tp_atr_frac   = scalp.get('tp_atr_fraction', 0.5)
        tp_move       = atr_v * tp_atr_frac
        sl_move       = tp_move * sl_ratio
        lot_raw       = target_usd / (tp_move * contract_size) if tp_move > 0 else 0
        lot_base_s    = max(l_min, min(l_max, round(lot_raw / l_step) * l_step))
        lot           = max(l_min, min(l_max,
                            round(lot_base_s * r_multi_s / l_step) * l_step))

        # ── 大変動検知: スキャルプ→通常モード自動切換え ──────────
        bm_lookback   = scalp.get('big_move_lookback',  12)
        bm_atr_multi  = scalp.get('big_move_atr_multi', 2.0)
        big_move      = detect_big_move(df, bm_lookback, bm_atr_multi)

        if big_move != 'none':
            # 通常モードのロジックで再計算
            normal_data = compute_signal(symbol, cfg)
            if normal_data is not None:
                # ポジション方向と大変動方向が一致するならトレーリングを有効化
                position_aligns = (
                    (big_move == 'up'   and _scalp_last_action == 'buy') or
                    (big_move == 'down' and _scalp_last_action == 'sell')
                )
                if position_aligns:
                    normal_data['trail_multi'] = cfg['SL']['trail_multi']
                normal_data['signal_type'] = f'big_move_{big_move}(was_scalp)'
                normal_data['scalp_mode']  = False
                print(f"[スキャルプ→通常] 大変動={big_move}  "
                      f"last_pos={_scalp_last_action}  "
                      f"trail={'ON' if position_aligns else 'scalp_trail=0'}")
                return normal_data
            # 通常モード計算失敗時はスキャルプ継続（フォールスルー）

        # ── クールダウン中は通常モードに切換え ──────────────────
        in_cooldown = (
            _scalp_last_at is not None and
            now < _scalp_last_at + timedelta(minutes=cooldown)
        )
        if in_cooldown:
            normal_data = compute_signal(symbol, cfg)
            if normal_data is not None:
                rem = int((_scalp_last_at + timedelta(minutes=cooldown) - now).total_seconds() / 60)
                normal_data['scalp_cooldown_rem'] = rem
                normal_data['scalp_mode']         = False
                return normal_data
            # compute_signal 失敗時はスキャルプのまま継続

        # tp_move / sl_move はロット計算時に確定済み（上記参照）
        # M5 RSI クロス検出（複数閾値）
        # BUY : 50 / 55 / 60 のいずれかを上抜け
        # SELL: 45 / 40 / 35 のいずれかを下抜け
        new_cross     = None
        crossed_level = 0.0
        if _scalp_prev_rsi is not None:
            for thr in buy_thrs:
                if rsi_cur > thr and _scalp_prev_rsi <= thr:
                    new_cross     = 'buy'
                    crossed_level = thr
                    break
            if new_cross is None:
                for thr in sell_thrs:
                    if rsi_cur < thr and _scalp_prev_rsi >= thr:
                        new_cross     = 'sell'
                        crossed_level = thr
                        break
        _scalp_prev_rsi = rsi_cur

        action = 'none'
        skip   = ''

        # ── ポジション数チェック（手動エントリー含む全ポジション）──
        risk_pct       = cfg['BRIDGE'].get('risk_pct', 0.01)
        total_risk_pct = cfg.get('RULES', {}).get('total_risk_pct', 0.20)
        pos_st         = _position_status(risk_pct, total_risk_pct)

        if new_cross:
            hour_utc = now.hour
            eff_hour = (hour_utc + 1) % 24 if now.minute >= 45 else hour_utc

            if eff_hour in {9, 16, 21}:
                skip = f'forbidden_hour={eff_hour}'
            elif _scalp_count >= max_day:
                skip = f'daily_limit={_scalp_count}/{max_day}'
            elif (_scalp_last_at is not None and
                  now < _scalp_last_at + timedelta(minutes=cooldown)):
                rem  = int((_scalp_last_at + timedelta(minutes=cooldown) - now).total_seconds() / 60)
                skip = f'cooldown残{rem}分'
            elif pos_st['available_slots'] <= 0:
                skip = (f"max_positions={pos_st['max_positions']}に到達"
                        f"（全{pos_st['total_positions']}本）")
            else:
                action              = new_cross
                _scalp_last_action  = new_cross
                _scalp_count       += 1
                _scalp_last_at      = now

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
            'm5_filter_ok':       False,
            'm5_surge':           'none',
            'rsi_m1':             0.0,
            'scalp_type':         'none',
            'sma20':              0.0,
            'sl_multi':           round(sl_ratio, 2),
            'action':             action,
            'signal_type':        (f'scalp_{action}_{int(crossed_level)}'
                                   if action != 'none' else 'none'),
            'signal_valid_until': '',
            'downtrend_ok':       False,
            'sell_signal_type':   'none',
            'sell_valid_until':   '',
            'sell_skip_reason':   '',
            'sl_price':           round(sl_price, 2),
            'tp_price':           round(tp_price, 2),
            'score':              100,        # スキャルプはスコアチェック不要
            'strength':           'scalp',
            'tp_hold_minutes':    5,
            'skip_reason':        skip,
            'rsi_exit_thr':       cfg['SL']['rsi_exit_thr'],
            'trail_multi':        0.0,        # スキャルプはトレーリングなし
            'max_slip_pt':        max_pt,
            'lot_size':           lot,
            # スキャルプ専用フィールド（EA は参照しないが記録用）
            'scalp_mode':         True,
            'target_profit_jpy':  target,
            'tp_move_usd':        round(target_usd, 4),
            'trades_today':       _scalp_count,
            'cooldown_min':       cooldown,
            'scalp_cooldown_rem': 0,
            'max_positions':      pos_st['max_positions'],
            'total_positions':    pos_st['total_positions'],
            'available_slots':    pos_st['available_slots'],
            'adx_h1':             0.0,
            'adx_m5':             round(adx_m5_sv, 1) if not np.isnan(adx_m5_sv) else 0.0,
            'regime_h1':          'n/a',
            'regime_m5':          regime_m5s,
            'regime_lot_multi':   round(r_multi_s, 2),
            'entry_in_window':    0,
        }

    except Exception as e:
        print(f"[スキャルプ] 計算エラー: {e}")
        return None


# ── ファイル I/O ───────────────────────────────────────────

def write_signal(data: dict, path: str):
    """signal.json をアトミックに書き込む (Windows ファイルロック対応)"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='ascii') as f:
        json.dump(data, f, ensure_ascii=True, indent=2)

    retries = 5
    for attempt in range(retries):
        try:
            Path(tmp).replace(Path(path))
            return
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(0.1)
            else:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except OSError:
                    pass
                raise


def read_ea_state(path: str) -> dict:
    try:
        with open(path, encoding='ascii') as f:
            return json.load(f)
    except Exception:
        return {}


# ── ポーリングループ ─────────────────────────────────────────

def run_bridge(cfg: dict, once: bool = False, mode: str = 'normal'):
    symbol     = cfg['MT5']['symbol']
    sig_path   = cfg['BRIDGE']['signal_file']
    state_path = cfg['BRIDGE']['status_file']
    poll_sec   = cfg['BRIDGE']['poll_sec']
    lot_size   = cfg['BRIDGE']['lot_size']
    max_consec = cfg.get('RULES', {}).get('max_consecutive_losses', 3)
    min_score  = cfg.get('RULES', {}).get('min_score', 30)
    scalp_cfg  = cfg.get('SCALP', {})

    Path(sig_path).parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  MT5 EA ブリッジ  [{symbol}]  モード: {mode.upper()}")
    print(f"  signal.json  → {sig_path}")
    print(f"  ea_state.json← {state_path}")
    print(f"  ポーリング   : {poll_sec}秒  （Ctrl+C で終了）")
    if mode == 'scalp':
        print(f"  目標利益     : {scalp_cfg.get('target_profit_jpy', 300)}円"
              f"  クールダウン : {scalp_cfg.get('cooldown_min', 30)}分"
              f"  日次上限     : {scalp_cfg.get('max_trades_day', 20)}回")
    else:
        print(f"  ロット数     : {lot_size}  最小スコア: {min_score}")
        print(f"  連続損失上限 : {max_consec}回")
    print("=" * 60)

    if not connect_mt5(symbol, cfg['MT5']):
        print("\n[エラー] MT5 接続失敗。ターミナルを起動して再実行してください。")
        return

    try:
        itr = 0
        while True:
            itr += 1
            t_s  = time.time()

            if mode == 'scalp':
                data = compute_scalp_signal(symbol, cfg)
            else:
                data = compute_signal(symbol, cfg)

            if data:
                # 連続損失チェック（EA state から読む）
                ea            = read_ea_state(state_path)
                consec_losses = ea.get('consecutive_losses', 0)
                pos           = ea.get('positions', 0)
                bal           = ea.get('balance', 'N/A')

                if consec_losses >= max_consec and data['action'] in ('buy', 'sell'):
                    data['action']      = 'none'
                    data['skip_reason'] = f'consecutive_losses={consec_losses}>={max_consec}'

                write_signal(data, sig_path)
                ts = datetime.now().strftime('%H:%M:%S')

                if mode == 'scalp' and data.get('scalp_mode', True):
                    # スキャルプ専用ログ
                    print(f"\n[{ts}] #{itr} [SCALP]  "
                          f"close=${data['close']:,.2f}  "
                          f"RSI_M5={data['rsi_m5']:.1f}  "
                          f"ATR=${data['atr']:.2f}  "
                          f"残高=¥{bal}  "
                          f"lot={data['lot_size']}(TP={scalp_cfg.get('tp_atr_fraction',0.5)}×ATR)  "
                          f"今日={data['trades_today']}/{scalp_cfg.get('max_trades_day',20)}回")
                    print(f"  action={data['action'].upper():4s}  "
                          f"signal={data['signal_type']}  "
                          f"TP=+${data.get('tp_move_usd',0):.2f}"
                          f"(¥{scalp_cfg.get('target_profit_jpy',300)})  "
                          f"SL=${data['sl_price']:,.2f}  TP=${data['tp_price']:,.2f}")
                else:
                    # 通常モードログ
                    surge_tag = f"[{data['m5_surge']}]" if data['m5_surge'] != 'none' else ''
                    scalp_tag = f"[SCALP:{data['scalp_type']}]" if data['scalp_type'] != 'none' else ''
                    trend_tag = '[SELL↓]' if data['downtrend_ok'] else ''
                    print(f"\n[{ts}] #{itr}  "
                          f"close=${data['close']:,.2f}  "
                          f"RSI_H1={data['rsi_h1']:.1f}  RSI_D1={data['rsi_d1']:.1f}{trend_tag}  "
                          f"RSI_M5={data['rsi_m5']:.1f}({'↑' if data['m5_filter_ok'] else '↓/NG'})  "
                          f"RSI_M1={data['rsi_m1']:.1f}{surge_tag}  "
                          f"ATR=${data['atr']:.2f}")
                    rg_tag = (f"[ADX_H1={data.get('adx_h1',0):.0f}/{data.get('regime_h1','?')}"
                              f" M5={data.get('adx_m5',0):.0f}/{data.get('regime_m5','?')}"
                              f" ×{data.get('regime_lot_multi',1.0)}]")
                    ep_tag = (f" entry#{data.get('entry_in_window',0)}/{cfg.get('REGIME',{}).get('max_entry_per_signal',3)}"
                              if data.get('entry_in_window', 0) > 0 else '')
                    print(f"  action={data['action'].upper():4s}  "
                          f"signal={data['signal_type']}{scalp_tag}  "
                          f"SL=${data['sl_price']:,.2f}  TP=${data['tp_price']:,.2f}  "
                          f"score={data['score']}({data['strength']})  "
                          f"lot={data['lot_size']}{ep_tag}")
                    print(f"  {rg_tag}")
                    if data['signal_valid_until']:
                        print(f"  buy_window_until={data['signal_valid_until']}")
                    if data['sell_signal_type'] != 'none':
                        print(f"  sell_signal={data['sell_signal_type']}  "
                              f"sell_window_until={data['sell_valid_until']}")
                    if data.get('scalp_cooldown_rem', 0) > 0:
                        print(f"  [SCALP cooldown残{data['scalp_cooldown_rem']}分 → 通常モード中]")

                if data['skip_reason']:
                    print(f"  skip: {data['skip_reason']}")
                if data.get('sell_skip_reason'):
                    print(f"  sell_skip: {data['sell_skip_reason']}")
                max_p  = data.get('max_positions',   20)
                total_p = data.get('total_positions', pos)
                avail  = data.get('available_slots',  max_p - pos)
                print(f"  残高=¥{bal}  ポジション={total_p}/{max_p}件(空き{avail})  連続損失={consec_losses}回")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] #{itr}  データ取得失敗")

            if once: break
            time.sleep(max(0, poll_sec - (time.time() - t_s)))

    except KeyboardInterrupt:
        print("\n[ブリッジ] 終了")
    finally:
        try:
            import MetaTrader5 as mt5; mt5.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='MT5 EA リアルタイムブリッジ')
    ap.add_argument('--once',   action='store_true', help='1回だけ計算して終了')
    ap.add_argument('--symbol', default=C.MT5['symbol'])
    ap.add_argument('--output', default='./output')
    ap.add_argument('--lot',    type=float, default=None,
                    help=f'1回の取引ロット数（省略時: {C.BRIDGE["lot_size"]}）')
    ap.add_argument('--mode',   choices=['normal', 'scalp'], default='normal',
                    help='normal: H1クロス戦略（デフォルト）/ scalp: M5 RSI50クロス, 円建てTP')
    ap.add_argument('--target', type=int, default=None,
                    help='スキャルプモード目標利益（円）（省略時: config.py の値）')
    ap.add_argument('--jpy',    type=float, default=None,
                    help='スキャルプモード JPY/USD レート（省略時: config.py の値）')
    args = ap.parse_args()

    CFG['MT5']['symbol']          = args.symbol
    CFG['BRIDGE']['signal_file']  = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/signal.json"
    CFG['BRIDGE']['status_file']  = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ea_state.json"
    if args.lot    is not None:
        CFG['BRIDGE']['lot_size']          = args.lot
    if args.target is not None:
        CFG['SCALP']['target_profit_jpy']  = args.target
    if args.jpy    is not None:
        CFG['SCALP']['jpy_per_usd']        = args.jpy

    run_bridge(CFG, once=args.once, mode=args.mode)
