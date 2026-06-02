"""core/strategy.py — シグナル検出・SL戦略・バックテストエンジン"""
from __future__ import annotations
import logging
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

_logger = logging.getLogger('torihiki.strategy')


# ── H1 シグナル検出（SMA20 + RSI）────────────────────────────

def detect_sma_rsi_signals(df: pd.DataFrame, p: dict, direction: str) -> list[dict]:
    """
    H1 RSI シグナル検出

    買い（2種類）:
      DIP:      RSI が buy_rsi_thr を下抜け（売られすぎ）
      MOMENTUM: RSI が momentum_thrs の各値を上抜け（モメンタム）
                XAU好適ゾーン 55-80・BTC好適ゾーン 60-85 へのエントリー

    売り: RSI が sell_rsi_thr を上抜け（禁止）

    返り値: [{'signal_bar', 'signal_time', 'signal_price', 'atr', 'signal_type'}, ...]
    """
    rsi   = df['RSI'].values
    sma   = df['SMA20'].values
    close = df['Close'].values
    atr   = df['ATR'].values
    n     = len(df)

    buy_th       = p.get('buy_rsi_thr',   45.0)
    sell_th      = p.get('sell_rsi_thr',  62.0)
    mom_thrs     = sorted(p.get('momentum_thrs', [55.0, 60.0, 65.0, 70.0, 75.0]))

    results = []
    for i in range(1, n - 2):
        if np.isnan(sma[i]) or np.isnan(rsi[i]) or np.isnan(rsi[i-1]):
            continue
        if direction == 'buy':
            if rsi[i] < buy_th and rsi[i-1] >= buy_th:
                results.append({
                    'signal_bar':   i,
                    'signal_time':  df.index[i],
                    'signal_price': float(close[i]),
                    'atr':          float(atr[i]) if not np.isnan(atr[i]) else 1.0,
                    'signal_type':  'dip',
                })
            else:
                for m_thr in mom_thrs:
                    if rsi[i] > m_thr and rsi[i-1] <= m_thr:
                        results.append({
                            'signal_bar':   i,
                            'signal_time':  df.index[i],
                            'signal_price': float(close[i]),
                            'atr':          float(atr[i]) if not np.isnan(atr[i]) else 1.0,
                            'signal_type':  f'momentum_{int(m_thr)}',
                        })
                        break  # 1バーで複数閾値を同時超えしない
        else:
            if rsi[i] > sell_th and rsi[i-1] <= sell_th:
                results.append({
                    'signal_bar':   i,
                    'signal_time':  df.index[i],
                    'signal_price': float(close[i]),
                    'atr':          float(atr[i]) if not np.isnan(atr[i]) else 1.0,
                    'signal_type':  'sell',
                })

    # 重複除去（5本以内は最初だけ）
    out, last = [], -99
    for r in sorted(results, key=lambda x: x['signal_bar']):
        if r['signal_bar'] - last > 5:
            out.append(r); last = r['signal_bar']
    return out


def detect_pattern_signals(df: pd.DataFrame,
                           min_conf: float = 0.45,
                           lookback: int = 150,
                           step: int = 12) -> list[dict]:
    """H1 ネックライン突破シグナル（ルックアヘッドなし）。
    detect_sma_rsi_signals と同一フォーマットで返す。"""
    try:
        from core.patterns import detect_all_patterns
    except ImportError:
        return []

    n      = len(df)
    closes = df['Close'].values
    atrs   = df['ATR'].values if 'ATR' in df.columns else np.ones(n)
    times  = df.index
    events:    list[dict] = []
    traded_fp: set        = set()
    active_pats: list     = []

    for k in range(lookback, n):
        if (k - lookback) % step == 0:
            past = df.iloc[k - lookback: k]
            try:
                active_pats = [p for p in detect_all_patterns(past, window=5, top_n=3)
                               if p.confidence >= min_conf]
            except Exception:
                active_pats = []

        prev_c = closes[k - 1]
        cur_c  = closes[k]
        for pat in active_pats:
            fp = (pat.name, round(pat.neckline, 0))
            if fp in traded_fp:
                continue
            direction = None
            if pat.direction == 'bullish' and prev_c <= pat.neckline < cur_c:
                direction = 'buy'
            elif pat.direction == 'bearish' and prev_c >= pat.neckline > cur_c:
                direction = 'sell'
            if direction is not None:
                atr_val = float(atrs[k]) if not np.isnan(atrs[k]) else 1.0
                events.append({
                    'signal_bar':   k,
                    'signal_time':  times[k],
                    'signal_price': float(cur_c),
                    'atr':          atr_val,
                    'signal_type':  f'pattern_{pat.name}',
                    'direction':    direction,
                    'pat_target':   pat.target,
                })
                traded_fp.add(fp)

    return events


# ── M5 エントリーフィルタ ──────────────────────────────────

def check_m5_entry_filter(rsi_m5: float, rsi_m5_prev: float,
                           rsi_d1: float, symbol: str) -> bool:
    """
    M5 RSI エントリータイミングフィルタ。
    RSI が上昇中（rising）かつ指定ゾーンにある場合のみ True を返す。

    BTCUSD:
      - 押し目ゾーン  : 40〜55
      - モメンタムゾーン: 70〜80
      - D1 強い時のみ : >80（rsi_d1 > 70 が必要）
      - 禁止ゾーン    : 60〜70（シグナル反転多発帯）

    XAUUSD:
      - 有効ゾーン    : 50〜70
      - >80 は絶対禁止（急反転リスク）
    """
    if np.isnan(rsi_m5) or np.isnan(rsi_m5_prev):
        return False
    rising = rsi_m5 > rsi_m5_prev
    if not rising:
        return False

    if symbol == 'BTCUSD':
        zone_ok = (
            (40 <= rsi_m5 <= 55) or
            (70 <= rsi_m5 <= 80) or
            (rsi_m5 > 80 and rsi_d1 > 70)
        )
        forbidden = (60 <= rsi_m5 <= 70)
        return zone_ok and not forbidden
    else:  # XAUUSD / default
        return (50 <= rsi_m5 <= 70) and rsi_m5 < 80


def check_m5_surge(df_m5: pd.DataFrame,
                   lookback: int = 5,
                   threshold: float = 20.0) -> str:
    """
    M5 RSI の急変（急騰・急落）を検出する。

    lookback 本前の RSI と現在の RSI の差が threshold 以上なら急変と判定。
    デフォルト: 5本（25分）で 20 ポイント超の変化。

    Returns: 'rapid_rise' / 'rapid_fall' / 'none'
    """
    if df_m5 is None or 'RSI' not in df_m5.columns:
        return 'none'
    rsi = df_m5['RSI'].dropna()
    if len(rsi) < lookback + 1:
        return 'none'
    delta = float(rsi.iloc[-1]) - float(rsi.iloc[-1 - lookback])
    if delta >= threshold:
        return 'rapid_rise'
    if delta <= -threshold:
        return 'rapid_fall'
    return 'none'


def detect_early_surge(df_m5: pd.DataFrame, cfg: dict) -> dict:
    """
    急騰初期検知: RVOLと価格加速を使って急騰の始まりを検知

    Returns: {
        'is_early_surge': bool,
        'surge_strength': float (0-1),
        'confidence': float (0-1)
    }
    """
    if df_m5 is None or len(df_m5) < 20:
        return {'is_early_surge': False, 'surge_strength': 0.0, 'confidence': 0.0}

    # RVOLと価格加速の確認
    has_volume_data = 'RVOL' in df_m5.columns and 'Price_Accel' in df_m5.columns

    if not has_volume_data:
        # 出来高データがない場合は従来のRSI急変検知を使用
        surge_type = check_m5_surge(df_m5)
        is_early = surge_type == 'rapid_rise'
        return {
            'is_early_surge': is_early,
            'surge_strength': 1.0 if is_early else 0.0,
            'confidence': 0.5 if is_early else 0.0
        }

    # RVOLベースの検知
    rvol = df_m5['RVOL'].iloc[-1]
    price_accel = df_m5['Price_Accel'].iloc[-1]
    volume_surge = df_m5['Volume_Surge'].iloc[-1] if 'Volume_Surge' in df_m5.columns else False

    # 急騰初期の条件
    rvol_threshold = cfg.get('INDICATOR', {}).get('early_surge_rvol_threshold', 1.3)
    accel_threshold = cfg.get('INDICATOR', {}).get('early_surge_accel_threshold', 0.5)

    is_early_surge = (
        rvol > rvol_threshold and
        price_accel > accel_threshold and
        volume_surge
    )

    # 強度と信頼度の計算
    surge_strength = min(1.0, (rvol - 1.0) * 0.5 + price_accel * 0.3)
    confidence = min(1.0, rvol * 0.4 + (1.0 if volume_surge else 0.0) * 0.6)

    return {
        'is_early_surge': is_early_surge,
        'surge_strength': surge_strength,
        'confidence': confidence
    }


def should_avoid_entry_during_surge(df_m5: pd.DataFrame, cfg: dict) -> bool:
    """
    急騰中段階でのエントリーを避けるべきかを判定

    急騰がすでに進んでいて、反転リスクが高い場合にTrueを返す
    """
    if df_m5 is None or len(df_m5) < 10:
        return False

    # RSIがすでに高すぎる場合（70以上）は避ける
    rsi_current = df_m5['RSI'].iloc[-1]
    rsi_overbought = cfg.get('INDICATOR', {}).get('surge_overbought_threshold', 70.0)

    if rsi_current > rsi_overbought:
        return True

    # 価格加速が極端に高い場合（すでに急騰が長時間続いている）
    if 'Price_Accel' in df_m5.columns:
        accel_recent = df_m5['Price_Accel'].tail(5).mean()
        accel_threshold = cfg.get('INDICATOR', {}).get('surge_avoid_accel_threshold', 1.5)
        if accel_recent > accel_threshold:
            return True

    return False


def detect_pre_surge(df_m5: pd.DataFrame, cfg: dict) -> dict:
    """
    サージ前兆検出 — BB Squeeze + RVOL 上昇傾向 + ADX 転換の 3 条件スコアリング

    A. BB Squeeze: BB幅が過去 N 本の最小の 120% 以内（圧縮状態）
    B. RVOL 上昇: 直近 4 本中 3 本以上で RVOL が増加傾向
    C. ADX 転換: ADX が低値 (<30) から上昇中 + DI が方向を示している

    Returns:
        pre_surge_up:   bool  上昇前兆スコア >= 2
        pre_surge_down: bool  下落前兆スコア >= 2
        squeeze_on:     bool  BB Squeeze 状態
        rvol_building:  bool  RVOL 上昇トレンド
        score_up:       int   上昇スコア (0-3)
        score_down:     int   下落スコア (0-3)
    """
    _empty = lambda: {
        'pre_surge_up': False, 'pre_surge_down': False,
        'squeeze_on': False, 'rvol_building': False,
        'score_up': 0, 'score_down': 0,
    }
    if df_m5 is None or len(df_m5) < 30:
        return _empty()

    ind = cfg.get('INDICATOR', {})
    squeeze_lookback = ind.get('pre_surge_squeeze_lookback', 30)

    # A. BB Squeeze
    squeeze_on = False
    if 'BB_Width' in df_m5.columns:
        bw_series = df_m5['BB_Width'].dropna()
        if len(bw_series) >= squeeze_lookback:
            bw_cur = float(bw_series.iloc[-1])
            bw_min = float(bw_series.tail(squeeze_lookback).min())
            if not np.isnan(bw_cur) and bw_min > 0:
                squeeze_on = bw_cur <= bw_min * 1.2

    # B. RVOL 上昇傾向
    rvol_building = False
    if 'RVOL' in df_m5.columns:
        rvol_vals = df_m5['RVOL'].dropna().tail(5).values
        if len(rvol_vals) >= 4:
            diffs = np.diff(rvol_vals[-4:])
            rvol_building = int(np.sum(diffs > 0)) >= 3

    # C. ADX 転換 + DI 方向性
    adx_turning_up = adx_turning_down = False
    if all(c in df_m5.columns for c in ['ADX', 'DI_plus', 'DI_minus']):
        adx_vals = df_m5['ADX'].dropna().tail(5).values
        di_plus  = float(df_m5['DI_plus'].iloc[-1])
        di_minus = float(df_m5['DI_minus'].iloc[-1])
        if len(adx_vals) >= 3:
            adx_cur  = float(adx_vals[-1])
            adx_prev = float(adx_vals[-3])
            if adx_cur < 30 and adx_cur > adx_prev:
                adx_turning_up   = di_plus  > di_minus
                adx_turning_down = di_minus > di_plus

    score_up   = int(squeeze_on) + int(rvol_building) + int(adx_turning_up)
    score_down = int(squeeze_on) + int(rvol_building) + int(adx_turning_down)

    return {
        'pre_surge_up':   score_up   >= 2,
        'pre_surge_down': score_down >= 2,
        'squeeze_on':     squeeze_on,
        'rvol_building':  rvol_building,
        'score_up':       score_up,
        'score_down':     score_down,
    }


def detect_big_move(df_m5: pd.DataFrame,
                    lookback: int = 12,
                    atr_multi: float = 2.0) -> str:
    """
    大変動検知: スキャルプ→通常モード自動切換えのトリガー。

    2つの条件どちらかで判定:
      1. 60分（M5×12本）の価格変動が ATR×atr_multi を超える（方向性大変動）
      2. 直近 ATR が 20 本移動平均の 1.8 倍超（ボラスパイク）

    Returns: 'up' / 'down' / 'none'
    """
    if df_m5 is None or 'ATR' not in df_m5.columns or len(df_m5) < lookback + 1:
        return 'none'
    atr = float(df_m5['ATR'].iloc[-1])
    if atr <= 0:
        return 'none'
    close_now  = float(df_m5['Close'].iloc[-1])
    close_prev = float(df_m5['Close'].iloc[-1 - lookback])
    change     = close_now - close_prev

    # 条件①: 方向性変動 > ATR × atr_multi
    if abs(change) > atr * atr_multi:
        return 'up' if change > 0 else 'down'

    # 条件②: ATR スパイク (現在 ATR / 20 本 MA > 1.8)
    atr_ma = df_m5['ATR'].rolling(20, min_periods=10).mean().iloc[-1]
    if not np.isnan(atr_ma) and atr_ma > 0 and atr / atr_ma > 1.8:
        return 'up' if change > 0 else 'down'

    return 'none'


def detect_volume_breakout(df: pd.DataFrame, cfg: dict) -> dict:
    """出来高急増 + 方向性確認によるブレイクアウト検出。

    騙し防止: ローソク足実体/レンジ比率チェック（ヒゲ多い = 方向性なし = スキップ）。
    RVOL 列がない場合（出来高データ未提供ブローカー）は direction='none' を返す。

    Returns: {'direction': 'up'/'down'/'none', 'rvol': float,
              'strength': float 0-1, 'body_ratio': float, 'rsi': float}
    """
    _empty = lambda r=0.0: {'direction': 'none', 'rvol': r, 'strength': 0.0,
                             'body_ratio': 0.0, 'rsi': 50.0}
    if df is None or len(df) < 5:
        return _empty()
    if 'RVOL' not in df.columns:
        return _empty()

    scalp        = cfg.get('SCALP', {})
    rvol_thr     = scalp.get('vol_bo_rvol_thr',       2.0)
    body_min     = scalp.get('vol_bo_body_ratio_min',  0.45)
    atr_move_min = scalp.get('vol_bo_atr_move_min',    0.3)

    rvol = float(df['RVOL'].iloc[-1])
    if np.isnan(rvol):
        return _empty()
    if rvol < rvol_thr:
        return _empty(round(rvol, 2))

    o   = float(df['Open'].iloc[-1])
    h   = float(df['High'].iloc[-1])
    l   = float(df['Low'].iloc[-1])
    c   = float(df['Close'].iloc[-1])
    rng = h - l
    body = abs(c - o)
    body_ratio = body / rng if rng > 0 else 0.0
    atr  = float(df['ATR'].iloc[-1]) if ('ATR' in df.columns
           and not np.isnan(df['ATR'].iloc[-1])) else 0.0
    rsi  = float(df['RSI'].iloc[-1]) if 'RSI' in df.columns else 50.0

    rv_r  = round(rvol, 2)
    br_r  = round(body_ratio, 2)
    rsi_r = round(rsi, 1)

    # 騙しフィルター: ヒゲが大きい（実体が小さい）＝ マーケットメーカーのストップ狩り
    if body_ratio < body_min:
        return {'direction': 'none', 'rvol': rv_r, 'strength': 0.0,
                'body_ratio': br_r, 'rsi': rsi_r}

    # 実際の価格移動確認: 小さな動きで出来高だけ多い（スプレッド拡大等）を除外
    atr_move = body / atr if atr > 0 else 0.0
    if atr_move < atr_move_min:
        return {'direction': 'none', 'rvol': rv_r, 'strength': 0.0,
                'body_ratio': br_r, 'rsi': rsi_r}

    direction = 'up' if c > o else 'down'
    strength  = round(min(1.0, (rvol / rvol_thr - 1.0) * 0.4 + body_ratio * 0.6), 2)
    return {
        'direction':  direction,
        'rvol':       rv_r,
        'strength':   strength,
        'body_ratio': br_r,
        'rsi':        rsi_r,
    }


def detect_whipsaw(df: pd.DataFrame, n: int = 20,
                   threshold: float = 2.0) -> tuple[bool, float]:
    """ATR合計 / 実効レンジ比でウィップソー（行ってこい相場）を検出。

    ratio = Σ ATR(N本) / (N本の最高値 - N本の最安値)
    ratio >= threshold → 価格が往復していてトレンドが出ていない

    Returns: (is_whipsaw, ratio)
    """
    if df is None or len(df) < n or 'ATR' not in df.columns:
        return False, 0.0
    atr_sum    = float(df['ATR'].iloc[-n:].sum())
    high_n     = float(df['High'].iloc[-n:].max())
    low_n      = float(df['Low'].iloc[-n:].min())
    true_range = high_n - low_n
    if true_range <= 0 or np.isnan(atr_sum) or np.isnan(true_range):
        return False, 0.0
    ratio = atr_sum / true_range
    return ratio >= threshold, round(ratio, 2)


def detect_d1_trendlines(
    df_d1: pd.DataFrame,
    close: float,
    prev_close: float,
    cfg: dict,
) -> dict:
    """D1 スウィング高値/安値にトレンドラインを当て、現在バーへ延長した重要価格と
    近接・ブレイク・バウンスシグナルを返す。

    極値は現在バー(最終行)を除いた履歴から検出。
    2点以上の極値を線形回帰で結び、現在バー位置へ投影した価格を重要価格とする。

    Returns:
        resistance / support: {'price', 'slope', 'n_points', 'valid'}
        near_resistance / near_support  : 近接フラグ
        break_resistance               : 切り上げ (抵抗線を上抜け → BUY候補)
        break_support                  : 割り込み (支持線を下抜け → SELL候補)
        bounce_buy                     : 支持線反発 (BUY候補)
        bounce_sell                    : 抵抗線反落 (SELL候補)
        res_dist_atr / sup_dist_atr    : ライン距離 / D1_ATR
        d1_atr                         : D1 ATR 値
    """
    _nan = float('nan')
    _empty = dict(
        resistance=dict(price=_nan, slope=0.0, n_points=0, valid=False),
        support=   dict(price=_nan, slope=0.0, n_points=0, valid=False),
        d1_atr=1.0,
        near_resistance=False, near_support=False,
        break_resistance=False, break_support=False,
        bounce_buy=False, bounce_sell=False,
        res_dist_atr=_nan, sup_dist_atr=_nan,
    )
    if df_d1 is None or len(df_d1) < 5:
        return _empty

    tl_cfg    = cfg.get('D1_TRENDLINE', {})
    enabled   = tl_cfg.get('enabled', True)
    if not enabled:
        return _empty

    sw_window  = tl_cfg.get('sw_window',  3)
    min_points = tl_cfg.get('min_points', 2)
    max_pts    = tl_cfg.get('max_lookback_pts', 5)
    near_mult  = tl_cfg.get('near_atr_mult', 0.5)

    # 現在バー(最終行)を除いた履歴から極値を検出
    df_hist = df_d1.iloc[:-1]
    if len(df_hist) < sw_window * 2 + 3:
        return _empty

    has_hl = 'High' in df_hist.columns and 'Low' in df_hist.columns
    highs  = df_hist['High'].values  if has_hl else df_hist['Close'].values
    lows   = df_hist['Low'].values   if has_hl else df_hist['Close'].values
    cls    = df_hist['Close'].values

    # D1 ATR（近接判定用）
    tr = np.maximum(highs[1:] - lows[1:],
         np.maximum(np.abs(highs[1:] - cls[:-1]),
                    np.abs(lows[1:]  - cls[:-1])))
    window_atr = min(14, len(tr))
    d1_atr = float(np.mean(tr[-window_atr:])) if window_atr > 0 else 1.0
    if d1_atr <= 0 or np.isnan(d1_atr):
        d1_atr = 1.0

    # スウィング高値・安値を検出（patterns.find_swing_points を再利用）
    from core.patterns import find_swing_points
    sw_highs, sw_lows = find_swing_points(df_hist, window=sw_window)

    # 現在バーの時系列インデックス位置（投影先）
    current_idx = float(len(df_d1) - 1)

    def _fit_line(pts: list) -> tuple | None:
        """最新側 max_pts 個を線形回帰して (slope, projected_price, n) を返す"""
        if len(pts) < min_points:
            return None
        use = pts[-max_pts:]
        xs  = np.array([p[0] for p in use], dtype=float)
        ys  = np.array([p[1] for p in use], dtype=float)
        if len(xs) == 2:
            slope = (ys[1] - ys[0]) / (xs[1] - xs[0]) if xs[1] != xs[0] else 0.0
            projected = ys[0] + slope * (current_idx - xs[0])
        else:
            coef = np.polyfit(xs, ys, 1)
            slope = float(coef[0])
            projected = slope * current_idx + float(coef[1])
        return slope, projected, len(use)

    res_fit = _fit_line(sw_highs)
    sup_fit = _fit_line(sw_lows)

    res_info = dict(price=_nan, slope=0.0, n_points=0, valid=False)
    if res_fit is not None:
        res_info = dict(price=res_fit[1], slope=res_fit[0], n_points=res_fit[2], valid=True)

    sup_info = dict(price=_nan, slope=0.0, n_points=0, valid=False)
    if sup_fit is not None:
        sup_info = dict(price=sup_fit[1], slope=sup_fit[0], n_points=sup_fit[2], valid=True)

    res_p = res_info['price']
    sup_p = sup_info['price']
    near_thr = d1_atr * near_mult

    def _dist_atr(p):
        return abs(close - p) / d1_atr if not np.isnan(p) else _nan

    near_resistance = (res_info['valid'] and not np.isnan(res_p)
                       and abs(close - res_p) <= near_thr)
    near_support    = (sup_info['valid'] and not np.isnan(sup_p)
                       and abs(close - sup_p) <= near_thr)

    # 切り上げ: 前足が抵抗線以下 → 現足が抵抗線以上（上抜けブレイク）
    break_resistance = (res_info['valid'] and not np.isnan(res_p)
                        and prev_close < res_p <= close)
    # 割り込み: 前足が支持線以上 → 現足が支持線以下（下抜けブレイク）
    break_support    = (sup_info['valid'] and not np.isnan(sup_p)
                        and prev_close > sup_p >= close)

    # 支持線バウンス: 直前に支持線近辺まで下落し現足が上昇回復
    bounce_buy  = (sup_info['valid'] and not np.isnan(sup_p)
                   and prev_close <= sup_p + near_thr
                   and close > prev_close and close > sup_p)
    # 抵抗線バウンス: 直前に抵抗線近辺まで上昇し現足が下落
    bounce_sell = (res_info['valid'] and not np.isnan(res_p)
                   and prev_close >= res_p - near_thr
                   and close < prev_close and close < res_p)

    return dict(
        resistance=res_info,
        support=sup_info,
        d1_atr=d1_atr,
        near_resistance=near_resistance,
        near_support=near_support,
        break_resistance=break_resistance,
        break_support=break_support,
        bounce_buy=bounce_buy,
        bounce_sell=bounce_sell,
        res_dist_atr=_dist_atr(res_p),
        sup_dist_atr=_dist_atr(sup_p),
    )


def _ew_swing(df: pd.DataFrame, window: int = 3):
    """EW2 検出用スイングポイント（遅延インポートで循環参照を回避）"""
    from core.patterns import find_swing_points
    return find_swing_points(df, window=window)


def detect_elliott_w2_buy(
    df: pd.DataFrame,
    lookback:        int   = 40,
    sw_window:       int   = 3,
    fib_min:         float = 0.382,
    fib_max:         float = 0.786,
    min_wave1_atr:   float = 1.5,
    rsi_div_min:     float = 3.0,
    w2_rsi_max:      float = 45.0,
    w2_bars_ago_max: int   = 5,
) -> dict | None:
    """エリオット波動 Wave2 第2底 BUY シグナル検出（M5 推奨）。

    検出条件:
      1. 直近 w2_bars_ago_max 本以内に確定スイングロー（第2底）が存在する
      2. 第2底の RSI ≤ w2_rsi_max（売られすぎ圏）
      3. 現在の RSI が第2底より高い（反転上昇開始）
      4. 第2底と第1底の間にスイングハイ（Wave1 ピーク）が存在する
      5. Wave1 の高さ ≥ ATR × min_wave1_atr
      6. 第2底が Wave1 の Fibonacci fib_min〜fib_max 押し戻し範囲
      7. 第2底 RSI > 第1底 RSI（強気ダイバージェンス）≥ rsi_div_min
    """
    _tag = '[EW2-BUY]'
    if df is None or len(df) < lookback + sw_window + 2:
        _logger.info(f'{_tag} データ不足 len={len(df) if df is not None else 0} '
                      f'必要={lookback + sw_window + 2}')
        return None
    if not {'RSI', 'ATR', 'High', 'Low', 'Close'}.issubset(df.columns):
        _logger.info(f'{_tag} 必要カラム不足 cols={list(df.columns)}')
        return None

    df_w = df.iloc[-lookback:]
    rsi  = df_w['RSI'].values
    atr  = float(df['ATR'].iloc[-1])
    n    = len(df_w)

    if atr <= 0 or np.isnan(atr):
        _logger.info(f'{_tag} ATR無効 atr={atr}')
        return None

    sh, sl = _ew_swing(df_w, window=sw_window)
    _logger.debug(f'{_tag} スイング検出 swing_high={len(sh)}個 swing_low={len(sl)}個 '
                  f'lookback={lookback}本 cur_rsi={float(rsi[-1]):.1f}')

    # ① 第2底候補: 直近 w2_bars_ago_max 本以内の確定スイングロー
    w2_cands = [(i, p) for i, p in sl if i >= n - 1 - w2_bars_ago_max]
    if not w2_cands:
        _latest_sl = sl[-1] if sl else None
        _ago = (n - 1 - _latest_sl[0]) if _latest_sl else '―'
        _logger.info(f'{_tag} ①失敗: 直近{w2_bars_ago_max}本以内にスイングロー無し '
                      f'最新スイングロー={_latest_sl} ({_ago}本前)')
        return None
    w2_idx, w2_low = w2_cands[-1]
    w2_rsi = float(rsi[w2_idx])

    if np.isnan(w2_rsi) or w2_rsi > w2_rsi_max:
        _logger.info(f'{_tag} ②失敗: W2底RSI={w2_rsi:.1f} > 上限{w2_rsi_max} '
                      f'W2底={w2_low:,.2f} ({n-1-w2_idx}本前)')
        return None

    # ③ RSI が第2底から反転上昇中
    cur_rsi = float(rsi[-1])
    if np.isnan(cur_rsi) or cur_rsi <= w2_rsi:
        _logger.info(f'{_tag} ③失敗: RSI未反転 cur={cur_rsi:.1f} ≤ W2底RSI={w2_rsi:.1f}')
        return None

    # ④ Wave1 ピーク: 第2底より前のスイングハイ
    prev_highs = [(i, p) for i, p in sh if i < w2_idx]
    if not prev_highs:
        _logger.info(f'{_tag} ④失敗: W2底(idx={w2_idx})より前にスイングハイ無し')
        return None
    w1_peak_idx, w1_peak = prev_highs[-1]

    # ④ 第1底: Wave1 ピークより前のスイングロー
    first_lows = [(i, p) for i, p in sl if i < w1_peak_idx]
    if not first_lows:
        _logger.info(f'{_tag} ④失敗: W1ピーク(idx={w1_peak_idx})より前にスイングロー無し')
        return None
    w1_idx, w1_low = first_lows[-1]
    w1_rsi = float(rsi[w1_idx])

    # ⑤ Wave1 の高さチェック
    wave1_size = w1_peak - w1_low
    if wave1_size < atr * min_wave1_atr or wave1_size <= 0:
        _logger.info(f'{_tag} ⑤失敗: Wave1サイズ={wave1_size:.2f} < ATR×{min_wave1_atr}={atr*min_wave1_atr:.2f}')
        return None

    # ⑥ フィボナッチ リトレースメント
    fib_level = (w1_peak - w2_low) / wave1_size
    if not (fib_min <= fib_level <= fib_max):
        _logger.info(f'{_tag} ⑥失敗: Fib={fib_level:.3f} 範囲外[{fib_min},{fib_max}] '
                      f'W1底={w1_low:,.2f} W1峰={w1_peak:,.2f} W2底={w2_low:,.2f}')
        return None

    # ⑦ 強気ダイバージェンス: 第2底 RSI > 第1底 RSI
    if np.isnan(w1_rsi) or (w2_rsi - w1_rsi) < rsi_div_min:
        _logger.info(f'{_tag} ⑦失敗: RSIダイバ差={w2_rsi-w1_rsi:.1f} < 閾値{rsi_div_min} '
                      f'(W2_RSI={w2_rsi:.1f} W1_RSI={w1_rsi:.1f})')
        return None

    return {
        'signal':      'elliott_w2_buy',
        'w1_low':      round(w1_low, 2),
        'w1_peak':     round(w1_peak, 2),
        'w2_low':      round(w2_low, 2),
        'wave1_size':  round(wave1_size, 2),
        'fib_level':   round(fib_level, 3),
        'rsi_div':     round(w2_rsi - w1_rsi, 1),
        'w2_bars_ago': n - 1 - w2_idx,
        'fib_38':      round(w1_peak - wave1_size * 0.382, 2),
        'fib_50':      round(w1_peak - wave1_size * 0.500, 2),
        'fib_62':      round(w1_peak - wave1_size * 0.618, 2),
    }


def detect_elliott_w2_sell(
    df: pd.DataFrame,
    lookback:        int   = 40,
    sw_window:       int   = 3,
    fib_min:         float = 0.382,
    fib_max:         float = 0.786,
    min_wave1_atr:   float = 1.5,
    rsi_div_min:     float = 3.0,
    w2_rsi_min:      float = 55.0,
    w2_bars_ago_max: int   = 5,
) -> dict | None:
    """エリオット波動 Wave2 第2天井 SELL シグナル検出（M5 推奨）。

    下落 Wave1 → 反発 Wave2（第2天井）→ Wave3 下落へのエントリー。
    RSI 弱気ダイバージェンス: 第2天井の RSI < 第1天井の RSI。
    """
    _tag = '[EW2-SELL]'
    if df is None or len(df) < lookback + sw_window + 2:
        _logger.info(f'{_tag} データ不足 len={len(df) if df is not None else 0} '
                      f'必要={lookback + sw_window + 2}')
        return None
    if not {'RSI', 'ATR', 'High', 'Low', 'Close'}.issubset(df.columns):
        _logger.info(f'{_tag} 必要カラム不足 cols={list(df.columns)}')
        return None

    df_w = df.iloc[-lookback:]
    rsi  = df_w['RSI'].values
    atr  = float(df['ATR'].iloc[-1])
    n    = len(df_w)

    if atr <= 0 or np.isnan(atr):
        _logger.info(f'{_tag} ATR無効 atr={atr}')
        return None

    sh, sl = _ew_swing(df_w, window=sw_window)
    _logger.debug(f'{_tag} スイング検出 swing_high={len(sh)}個 swing_low={len(sl)}個 '
                  f'lookback={lookback}本 cur_rsi={float(rsi[-1]):.1f}')

    # ① 第2天井候補: 直近 w2_bars_ago_max 本以内の確定スイングハイ
    w2_cands = [(i, p) for i, p in sh if i >= n - 1 - w2_bars_ago_max]
    if not w2_cands:
        _latest_sh = sh[-1] if sh else None
        _ago = (n - 1 - _latest_sh[0]) if _latest_sh else '―'
        _logger.info(f'{_tag} ①失敗: 直近{w2_bars_ago_max}本以内にスイングハイ無し '
                      f'最新スイングハイ={_latest_sh} ({_ago}本前)')
        return None
    w2_idx, w2_high = w2_cands[-1]
    w2_rsi = float(rsi[w2_idx])

    if np.isnan(w2_rsi) or w2_rsi < w2_rsi_min:
        _logger.info(f'{_tag} ②失敗: W2天井RSI={w2_rsi:.1f} < 下限{w2_rsi_min} '
                      f'W2天井={w2_high:,.2f} ({n-1-w2_idx}本前)')
        return None

    # ③ RSI が第2天井から反転下落中
    cur_rsi = float(rsi[-1])
    if np.isnan(cur_rsi) or cur_rsi >= w2_rsi:
        _logger.info(f'{_tag} ③失敗: RSI未反転 cur={cur_rsi:.1f} ≥ W2天井RSI={w2_rsi:.1f}')
        return None

    # ④ Wave1 ボトム: 第2天井より前のスイングロー
    prev_lows = [(i, p) for i, p in sl if i < w2_idx]
    if not prev_lows:
        _logger.info(f'{_tag} ④失敗: W2天井(idx={w2_idx})より前にスイングロー無し')
        return None
    w1_valley_idx, w1_valley = prev_lows[-1]

    # ④ 第1天井: Wave1 ボトムより前のスイングハイ
    first_highs = [(i, p) for i, p in sh if i < w1_valley_idx]
    if not first_highs:
        _logger.info(f'{_tag} ④失敗: W1ボトム(idx={w1_valley_idx})より前にスイングハイ無し')
        return None
    w1_idx, w1_high = first_highs[-1]
    w1_rsi = float(rsi[w1_idx])

    # ⑤ Wave1 の高さ
    wave1_size = w1_high - w1_valley
    if wave1_size < atr * min_wave1_atr or wave1_size <= 0:
        _logger.info(f'{_tag} ⑤失敗: Wave1サイズ={wave1_size:.2f} < ATR×{min_wave1_atr}={atr*min_wave1_atr:.2f}')
        return None

    # ⑥ フィボナッチ
    fib_level = (w2_high - w1_valley) / wave1_size
    if not (fib_min <= fib_level <= fib_max):
        _logger.info(f'{_tag} ⑥失敗: Fib={fib_level:.3f} 範囲外[{fib_min},{fib_max}] '
                      f'W1天井={w1_high:,.2f} W1谷={w1_valley:,.2f} W2天井={w2_high:,.2f}')
        return None

    # ⑦ 弱気ダイバージェンス: 第1天井 RSI > 第2天井 RSI
    if np.isnan(w1_rsi) or (w1_rsi - w2_rsi) < rsi_div_min:
        _logger.info(f'{_tag} ⑦失敗: RSIダイバ差={w1_rsi-w2_rsi:.1f} < 閾値{rsi_div_min} '
                      f'(W1_RSI={w1_rsi:.1f} W2_RSI={w2_rsi:.1f})')
        return None

    return {
        'signal':      'elliott_w2_sell',
        'w1_high':     round(w1_high, 2),
        'w1_valley':   round(w1_valley, 2),
        'w2_high':     round(w2_high, 2),
        'wave1_size':  round(wave1_size, 2),
        'fib_level':   round(fib_level, 3),
        'rsi_div':     round(w1_rsi - w2_rsi, 1),
        'w2_bars_ago': n - 1 - w2_idx,
        'fib_38':      round(w1_valley + wave1_size * 0.382, 2),
        'fib_50':      round(w1_valley + wave1_size * 0.500, 2),
        'fib_62':      round(w1_valley + wave1_size * 0.618, 2),
    }


def find_m5_entry(df_m5: pd.DataFrame, signal_time: pd.Timestamp,
                  direction: str, cfg: dict,
                  rsi_d1: float = 50.0,
                  symbol: str = 'BTCUSD') -> dict | None:
    """
    H1 シグナル発火後、最初に RSI が上昇している M5 バーをエントリーに使う。
    M5 RSI ゾーン条件は m5_bonus フラグとして記録（ゲートではなくボーナス）。
    """
    exe    = cfg.get('EXECUTION', {})
    sl_cfg = cfg.get('SL', {})
    valid  = max(exe.get('signal_valid_m1', 240) // 5, 12)
    spread = sl_cfg.get('spread_usd', 0.30)

    slc = df_m5[df_m5.index >= signal_time].head(valid)
    if len(slc) < 3:
        return None

    sma   = slc['SMA20'].values
    rsi   = slc['RSI'].values
    close = slc['Close'].values
    idx   = slc.index

    for i in range(1, len(close)):
        if np.isnan(rsi[i]) or np.isnan(rsi[i - 1]):
            continue
        if direction == 'buy' and rsi[i] > rsi[i - 1]:  # rising のみ（ゾーン制限なし）
            if not np.isnan(sma[i]) and not np.isnan(sma[i-1]) and sma[i] < sma[i-1]:
                continue
            ep = float(close[i]) + spread
            return {
                'entry_time':      idx[i],
                'entry_price':     ep,
                'sma_at_entry':    float(close[i]),
                'rsi_at_entry':    float(rsi[i]),
                'm5_bonus':        check_m5_entry_filter(rsi[i], rsi[i-1], rsi_d1, symbol),
            }
    return None


# ── M1 執行ロジック ────────────────────────────────────────

def find_m1_entry(df_m1: pd.DataFrame, signal_time: pd.Timestamp,
                  direction: str, cfg: dict,
                  rsi_thr: float) -> dict | None:
    """
    SMAプルバック + RSIゲートで M1 エントリーを探す

    Step1: Close が SMA20 を クロス
    Step2: Low/High が SMA±margin にタッチ
    Step3: Close > SMA(買い) / Close < SMA(売り)  かつ  RSI < thr(買い) / RSI > thr(売り)
    """
    exe    = cfg.get('EXECUTION', {})
    sl_cfg = cfg.get('SL', {})
    margin = exe.get('touch_margin',    0.20)
    valid  = exe.get('signal_valid_m1', 240)
    spread = sl_cfg.get('spread_usd',   0.30)

    slc = df_m1[df_m1.index >= signal_time].head(valid)
    if len(slc) < 25: return None

    sma   = slc['SMA20'].values
    rsi   = slc['RSI'].values   if 'RSI'  in slc.columns else np.full(len(slc), 50.0)
    close = slc['Close'].values
    high  = slc['High'].values
    low   = slc['Low'].values
    idx   = slc.index

    # Step1: SMA クロス
    cross = None
    for i in range(1, len(close)):
        if np.isnan(sma[i]): continue
        if direction == 'buy'  and close[i] > sma[i] and close[i-1] <= sma[i-1]:
            cross = i; break
        if direction == 'sell' and close[i] < sma[i] and close[i-1] >= sma[i-1]:
            cross = i; break
    if cross is None: return None

    # Step2: タッチ
    touch = None
    for i in range(cross + 1, len(close)):
        if np.isnan(sma[i]): continue
        if direction == 'buy'  and low[i]  <= sma[i] + margin: touch = i; break
        if direction == 'sell' and high[i] >= sma[i] - margin: touch = i; break
    if touch is None: return None

    # Step3: RSI ゲート（タッチ後20本以内）
    for i in range(touch, min(touch + 20, len(close))):
        if np.isnan(sma[i]) or np.isnan(rsi[i]): continue
        if direction == 'buy'  and close[i] > sma[i] and rsi[i] < rsi_thr:
            return {'entry_time':   idx[i],
                    'entry_price':  float(close[i]) + spread,
                    'sma_at_entry': float(sma[i]),
                    'rsi_at_entry': float(rsi[i])}
        if direction == 'sell' and close[i] < sma[i] and rsi[i] > rsi_thr:
            return {'entry_time':   idx[i],
                    'entry_price':  float(close[i]) - spread,
                    'sma_at_entry': float(sma[i]),
                    'rsi_at_entry': float(rsi[i])}
    return None


# ── SL 戦略クラス ──────────────────────────────────────────

class SLStrategy:
    name    = ''
    name_ja = ''
    color   = '#58a6ff'

    def calc_sl(self, ep: float, direction: str,
                bar: int, df: pd.DataFrame) -> float:
        raise NotImplementedError

    def update_sl(self, sl: float, direction: str,
                  bar: int, df: pd.DataFrame, ep: float) -> float:
        return sl   # デフォルト: 固定


class FixedSL(SLStrategy):
    """A. 固定SL（ベースライン）"""
    name = 'fixed'; name_ja = 'A. 固定SL ($15)'; color = '#8b949e'
    def __init__(self, usd=15.0): self.usd = usd
    def calc_sl(self, ep, d, b, df):
        return ep - self.usd if d == 'buy' else ep + self.usd


class AtrSL(SLStrategy):
    """B. ATR×1.5 SL"""
    name = 'atr'; name_ja = 'B. ATR×1.5 SL'; color = '#58a6ff'
    def __init__(self, multi=1.5): self.m = multi
    def calc_sl(self, ep, d, b, df):
        atr = float(df['ATR'].iloc[b])
        return ep - atr * self.m if d == 'buy' else ep + atr * self.m


class StructuralSL(SLStrategy):
    """C. 構造的SL（スイング安値/高値ベース）"""
    name = 'struct'; name_ja = 'C. 構造的SL'; color = '#e3b341'
    def __init__(self, buf=0.3): self.buf = buf
    def calc_sl(self, ep, d, b, df):
        atr = float(df['ATR'].iloc[b])
        if d == 'buy':
            return float(df['Swing_Low'].iloc[b])  - atr * self.buf
        else:
            return float(df['Swing_High'].iloc[b]) + atr * self.buf


class TwoStageSL(SLStrategy):
    """
    D. 二段階SL（急落バッファ型）
    通常時 ATR×1.0、ATR_ratio ≥ 1.8 で ATR×2.5 に自動拡大
    """
    name = 'two_stage'; name_ja = 'D. 二段階SL'; color = '#3fb950'
    def calc_sl(self, ep, d, b, df):
        atr = float(df['ATR'].iloc[b])
        return ep - atr * 1.0 if d == 'buy' else ep + atr * 1.0
    def update_sl(self, sl, d, b, df, ep):
        atr   = float(df['ATR'].iloc[b])
        ratio = float(df['ATR_ratio'].iloc[b]) if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        m     = 2.5 if ratio >= 1.8 else 1.0
        return ep - atr * m if d == 'buy' else ep + atr * m


class VolAdaptiveSL(SLStrategy):
    """
    E. ボラ適応型SL ★推奨★
    ATR_ratio に応じて SL 幅を動的調整
      < 0.8  → ×1.0（低ボラ）
      〜1.5  → ×1.5（通常）
      〜2.5  → ×2.5（高ボラ）
      > 2.5  → ×4.0（急落）
    """
    name = 'vol_adapt'; name_ja = 'E. ボラ適応型SL★'; color = '#f85149'

    def __init__(self, cfg: dict | None = None):
        sl = (cfg or {}).get('SL', {})
        self.low    = sl.get('sl_multi_low',    1.0)
        self.normal = sl.get('sl_multi_normal', 1.5)
        self.medium = sl.get('sl_multi_medium', 2.5)
        self.high   = sl.get('sl_multi_high',   4.0)
        self.thr_m  = sl.get('atr_ratio_medium', 1.5)
        self.thr_h  = sl.get('atr_ratio_high',   2.5)

    def _m(self, ratio: float) -> float:
        if np.isnan(ratio): return self.normal
        if ratio > self.thr_h:  return self.high
        elif ratio > self.thr_m: return self.medium
        elif ratio > 0.8:       return self.normal
        else:                   return self.low

    def calc_sl(self, ep, d, b, df):
        atr = float(df['ATR'].iloc[b])
        r   = float(df['ATR_ratio'].iloc[b]) if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        return ep - atr * self._m(r) if d == 'buy' else ep + atr * self._m(r)

    def update_sl(self, sl, d, b, df, ep):
        atr = float(df['ATR'].iloc[b])
        r   = float(df['ATR_ratio'].iloc[b]) if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        new = ep - atr * self._m(r) if d == 'buy' else ep + atr * self._m(r)
        return max(sl, new) if d == 'buy' else min(sl, new)


def get_all_strategies(cfg: dict | None = None) -> list[SLStrategy]:
    return [FixedSL(), AtrSL(), StructuralSL(), TwoStageSL(), VolAdaptiveSL(cfg)]


# ── バックテストエンジン ────────────────────────────────────

def run_backtest(df_h1: pd.DataFrame, df_m1: pd.DataFrame,
                 strategy: SLStrategy, sig_p: dict, cfg: dict,
                 direction: str = 'buy',
                 crash_bar_set: set | None = None,
                 df_m5: pd.DataFrame | None = None) -> dict:
    """
    H1 シグナル → M1 エントリー → H1 バーで SL/TP 判定
    M1 の Open でスリッページを精密再現
    RSI≥75（買い）/ RSI≤25（売り）でトレーリング起動
    """
    sl_c     = cfg.get('SL', {})
    exe      = cfg.get('EXECUTION', {})
    symbol   = cfg.get('MT5', {}).get('symbol', 'XAUUSD')
    spread   = sl_c.get('spread_usd',    0.30)
    hold_max = sl_c.get('hold_max_h1',   48)
    rsi_exit = sl_c.get('rsi_exit_thr',  75.0)
    trail_m  = sl_c.get('trail_multi',   1.5)
    _tp_raw  = sl_c.get('tp_atr_multi',  3.0)
    tp_m     = float(_tp_raw.get(symbol, next(iter(_tp_raw.values()))) if isinstance(_tp_raw, dict) else _tp_raw)
    rsi_off  = exe.get('m1_rsi_offset',  20.0)

    crash_bars = crash_bar_set or set()
    signals    = detect_sma_rsi_signals(df_h1, sig_p, direction)

    # trading_rules フィルタ（buy のみ有効、import 失敗時はスキップ）
    try:
        from trading_rules import RulesEngine as _RE
        _rules_engine = _RE()
        symbol    = cfg.get('MT5', {}).get('symbol', 'BTCUSD')
        min_score = cfg.get('RULES', {}).get('min_score', 0)
        rsi_d1    = df_h1.get('RSI_D1') if hasattr(df_h1, 'get') else df_h1['RSI_D1'] if 'RSI_D1' in df_h1.columns else None

        filtered = []
        for sig in signals:
            st         = sig['signal_time']
            hour_utc   = st.hour
            minute_utc = st.minute
            dow        = st.dayofweek
            rsi_h1_v = float(df_h1['RSI'].iloc[sig['signal_bar']])
            rsi_d1_v = float(rsi_d1.iloc[sig['signal_bar']]) if rsi_d1 is not None and not np.isnan(rsi_d1.iloc[sig['signal_bar']]) else 50.0
            res = _rules_engine.evaluate(
                symbol=symbol, rsi_h1=rsi_h1_v, rsi_d1=rsi_d1_v,
                direction=direction, hour_utc=hour_utc, dow=dow,
                minute_utc=minute_utc,
            )
            if res.signal in ('BUY', 'SELL') and res.score >= min_score:
                filtered.append(sig)
        signals = filtered
    except Exception:
        pass  # trading_rules 未インストール / 使用不可の場合はフィルタなし

    # パターン ネックライン突破シグナルを追加（RulesEngine フィルタの外）
    try:
        _pat_sigs = [s for s in detect_pattern_signals(df_h1)
                     if s.get('direction') == direction]
        if _pat_sigs:
            signals = sorted(signals + _pat_sigs, key=lambda s: s['signal_bar'])
    except Exception:
        pass

    m1_rsi_thr = (sig_p.get('buy_rsi_thr',  38.0) + rsi_off if direction == 'buy'
                  else sig_p.get('sell_rsi_thr', 62.0) - rsi_off)

    close_h1 = df_h1['Close'].values
    high_h1  = df_h1['High'].values
    low_h1   = df_h1['Low'].values
    atr_h1   = df_h1['ATR'].values
    rsi_h1   = df_h1['RSI'].values
    idx_h1   = df_h1.index
    n_h1     = len(df_h1)

    trades     = []
    used_until = pd.Timestamp.min

    for sig in signals:
        if sig['signal_time'] < used_until:
            continue

        # D1 RSI（M5フィルタ + rules フィルタ共用）
        sb       = sig['signal_bar']
        rsi_d1_v = (float(df_h1['RSI_D1'].iloc[sb])
                    if 'RSI_D1' in df_h1.columns and not np.isnan(df_h1['RSI_D1'].iloc[sb])
                    else 50.0)
        symbol   = cfg.get('MT5', {}).get('symbol', 'BTCUSD')

        # M5 優先、なければ M1 フォールバック
        if df_m5 is not None and not df_m5.empty:
            info = find_m5_entry(df_m5, sig['signal_time'], direction, cfg, rsi_d1_v, symbol)
        else:
            info = find_m1_entry(df_m1, sig['signal_time'], direction, cfg, m1_rsi_thr)
        if info is None:
            continue

        ep  = info['entry_price']
        sb  = sig['signal_bar']
        atr = float(atr_h1[sb]) if not np.isnan(atr_h1[sb]) else sig['atr']

        sl      = strategy.calc_sl(ep, direction, sb, df_h1)
        sl_dist = abs(ep - sl)

        # SL 距離フィルタ
        if sl_dist < atr * 0.3 or sl_dist > atr * 6:
            continue

        tp         = ep + atr * tp_m if direction == 'buy' else ep - atr * tp_m
        # パターンTP目標があれば上書き（1〜8×ATR 範囲に制限）
        if sig.get('pat_target') is not None:
            _pt_dist = abs(float(sig['pat_target']) - ep)
            if atr * tp_m < _pt_dist < atr * tp_m * 8:
                tp = ep + _pt_dist if direction == 'buy' else ep - _pt_dist
        trail_sl   = None
        rsi_trig   = False
        best_price = ep
        was_crash  = False

        try:
            epos = df_h1.index.searchsorted(info['entry_time'])
        except Exception:
            continue

        xp, xt, reason, slip_usd = None, None, 'timeout', 0.0

        for b in range(epos + 1, min(epos + hold_max, n_h1)):
            h_b  = high_h1[b]
            l_b  = low_h1[b]
            a_b  = float(atr_h1[b]) if not np.isnan(atr_h1[b]) else atr
            r_b  = float(rsi_h1[b])

            if b in crash_bars: was_crash = True

            # SL 更新（戦略依存）
            sl = strategy.update_sl(sl, direction, b, df_h1, ep)

            # RSI 連動トレーリング起動
            if not rsi_trig:
                trig = ((direction == 'buy'  and r_b >= rsi_exit) or
                        (direction == 'sell' and r_b <= 100 - rsi_exit))
                if trig:
                    rsi_trig = True
                    trail_sl = (h_b - a_b * trail_m if direction == 'buy'
                                else l_b + a_b * trail_m)
                    trail_sl = (max(trail_sl, sl) if direction == 'buy'
                                else min(trail_sl, sl))

            if trail_sl is not None:
                if direction == 'buy':
                    if h_b > best_price: best_price = h_b
                    nt = best_price - a_b * trail_m
                    if nt > trail_sl: trail_sl = nt
                else:
                    if l_b < best_price: best_price = l_b
                    nt = best_price + a_b * trail_m
                    if nt < trail_sl: trail_sl = nt

            eff_sl = (max(sl, trail_sl) if trail_sl is not None and direction == 'buy'
                      else min(sl, trail_sl) if trail_sl is not None else sl)

            # SL 到達: M1 で精密なスリッページ計算
            sl_hit = ((direction == 'buy'  and l_b <= eff_sl) or
                      (direction == 'sell' and h_b >= eff_sl))
            if sl_hit:
                h1_time  = idx_h1[b]
                m1_slice = df_m1[(df_m1.index >= h1_time) &
                                  (df_m1.index <  h1_time + pd.Timedelta(hours=1))]
                actual   = eff_sl
                slip_usd = 0.0
                if len(m1_slice) > 0:
                    for _, mr in m1_slice.iterrows():
                        mo = float(mr['Open'])
                        if (direction == 'buy'  and mo < eff_sl) or \
                           (direction == 'sell' and mo > eff_sl):
                            actual   = mo
                            slip_usd = abs(eff_sl - mo)
                            break
                xp     = actual
                xt     = idx_h1[b]
                reason = 'sl_slip' if slip_usd > 0.5 else 'sl'
                break

            # TP 到達
            if ((direction == 'buy'  and h_b >= tp) or
                (direction == 'sell' and l_b <= tp)):
                xp = tp; xt = idx_h1[b]; reason = 'tp'; break

        if xp is None:
            eb = min(epos + hold_max, n_h1 - 1)
            xp = float(close_h1[eb]); xt = idx_h1[eb]; reason = 'timeout'
            slip_usd = 0.0

        pnl  = ((xp - ep) if direction == 'buy' else (ep - xp)) * 100
        trades.append({
            'direction':     direction,
            'entry_time':    info['entry_time'],
            'exit_time':     xt,
            'entry_price':   ep,
            'exit_price':    xp,
            'sl_dist':       sl_dist,
            'slippage_usd':  slip_usd,
            'pnl':           pnl,
            'reason':        reason,
            'was_crash':     was_crash,
            'rsi_triggered': rsi_trig,
        })
        used_until = xt

    return _metrics(trades, strategy)


def _metrics(trades: list, strategy: SLStrategy) -> dict:
    empty = dict(strategy=strategy.name_ja, color=strategy.color,
                 trades=[], n_trades=0, total_pnl=0.0, win_rate=0.0,
                 profit_factor=0.0, max_dd=0.0, sharpe=0.0,
                 sl_hit_rate=0.0, slip_rate=0.0,
                 avg_sl_dist=0.0, avg_slip_usd=0.0,
                 crash_survival=0.0, max_consec_loss=0,
                 equity=[], pnls=[], reason_counts={})
    if not trades: return empty

    pnls   = np.array([t['pnl'] for t in trades])
    equity = np.cumsum(pnls)
    peak   = np.maximum.accumulate(equity)
    wins   = pnls[pnls > 0]; losses = pnls[pnls <= 0]
    gp     = wins.sum()    if len(wins)   > 0 else 0.0
    gl     = -losses.sum() if len(losses) > 0 else 1e-9

    sl_h  = [t for t in trades if 'sl' in t.get('reason', '')]
    sl_s  = [t for t in trades if t.get('reason') == 'sl_slip']
    cr_t  = [t for t in trades if t.get('was_crash')]
    c_sur = sum(1 for t in cr_t if t['pnl'] > -30) / max(len(cr_t), 1)
    max_cl = cur_cl = 0
    for p in pnls:
        if p < 0: cur_cl += 1; max_cl = max(max_cl, cur_cl)
        else:     cur_cl = 0

    sharpe = (pnls.mean() / (pnls.std() + 1e-9)
              * np.sqrt(252 * 24)) if len(pnls) > 1 else 0.0

    return dict(
        strategy        = strategy.name_ja,
        color           = strategy.color,
        trades          = trades,
        n_trades        = len(trades),
        total_pnl       = float(pnls.sum()),
        win_rate        = float(len(wins) / max(len(pnls), 1)),
        profit_factor   = float(gp / gl),
        max_dd          = float((peak - equity).max()),
        sharpe          = float(sharpe),
        sl_hit_rate     = float(len(sl_h) / max(len(trades), 1)),
        slip_rate       = float(len(sl_s) / max(len(trades), 1)),
        avg_sl_dist     = float(np.mean([t['sl_dist'] for t in trades])),
        avg_slip_usd    = float(np.mean([t['slippage_usd'] for t in trades])),
        crash_survival  = float(c_sur),
        max_consec_loss = int(max_cl),
        equity          = equity.tolist(),
        pnls            = pnls.tolist(),
        reason_counts   = {r: sum(1 for t in trades if t.get('reason') == r)
                           for r in set(t.get('reason', '') for t in trades)},
    )
