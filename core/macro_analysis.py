"""core/macro_analysis.py — D1/W1/MN1 マクロパターン分析・バイアス算出

マクロバイアス (-100〜+100) を算出し、TP 倍率・スコア補正・リスク倍率を動的決定する。
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

import numpy as np

from core.data     import fetch_ohlcv
from core.patterns import (
    find_swing_points,
    detect_double_bottom, detect_double_top,
    detect_head_shoulders, detect_inv_head_shoulders,
    PatternResult,
)

if TYPE_CHECKING:
    pass

_logger = logging.getLogger('torihiki')

# バイアスラベルと閾値
_BIAS_LABELS = [
    ('strong_bull', 50),
    ('weak_bull',   15),
    ('neutral',    -15),
    ('weak_bear',  -50),
    ('strong_bear', -999),
]

# タイムフレーム別パターンスコア基底
_TF_BASE = {'MN1': 40, 'W1': 30, 'D1': 20}

# バイアスラベルごとの動的パラメータ
_BIAS_PARAMS: dict[str, dict] = {
    'strong_bull': dict(buy_tp_multi=1.4, sell_tp_multi=0.7,  buy_risk_multi=1.1, sell_risk_multi=0.7,  score_adj_buy=+20, score_adj_sell=-15),
    'weak_bull':   dict(buy_tp_multi=1.2, sell_tp_multi=0.85, buy_risk_multi=1.0, sell_risk_multi=0.85, score_adj_buy=+10, score_adj_sell= -8),
    'neutral':     dict(buy_tp_multi=1.0, sell_tp_multi=1.0,  buy_risk_multi=1.0, sell_risk_multi=1.0,  score_adj_buy=  0, score_adj_sell=  0),
    'weak_bear':   dict(buy_tp_multi=0.85,sell_tp_multi=1.2,  buy_risk_multi=0.85,sell_risk_multi=1.0,  score_adj_buy= -8, score_adj_sell=+10),
    'strong_bear': dict(buy_tp_multi=0.7, sell_tp_multi=1.4,  buy_risk_multi=0.7, sell_risk_multi=1.1,  score_adj_buy=-15, score_adj_sell=+20),
}


def _bias_label(score: float) -> str:
    for label, thr in _BIAS_LABELS:
        if score > thr:
            return label
    return 'strong_bear'


def _score_pattern(p: PatternResult, tf: str) -> float:
    """パターン1件のバイアス寄与スコアを返す（bullish=正 / bearish=負）。"""
    base = _TF_BASE.get(tf, 15)
    conf_bonus = base * p.confidence
    confirmed_bonus = _TF_BASE.get(tf, 15) * 0.4 if p.confirmed else 0.0
    raw = conf_bonus + confirmed_bonus
    return raw if p.direction == 'bullish' else -raw


def _detect_patterns_for_tf(
    df,
    tf: str,
    macro_cfg: dict,
) -> list[PatternResult]:
    """単一TFのパターンを検出してリストで返す。"""
    if df is None or len(df) < 20:
        return []

    # TF に応じたパラメータ（W1/MN1はスイングが長期）
    if tf == 'MN1':
        window, max_sep, lookback = 2, 30, 60
    elif tf == 'W1':
        window, max_sep, lookback = 3, 60, 150
    else:  # D1
        window, max_sep, lookback = 5, 120, 300

    sh, sl = find_swing_points(df, window=window)
    results: list[PatternResult] = []
    top_n = macro_cfg.get('pattern_top_n', 2)

    results.extend(detect_double_bottom(df, sl, max_sep=max_sep, lookback=lookback)[:top_n])
    results.extend(detect_double_top(df, sh, max_sep=max_sep, lookback=lookback)[:top_n])
    results.extend(detect_head_shoulders(df, sh, sl, lookback=lookback)[:top_n])
    results.extend(detect_inv_head_shoulders(df, sh, sl, lookback=lookback)[:top_n])
    return results


def _nearest_neckline(patterns: list[PatternResult], close: float, atr: float
                      ) -> tuple[float | None, str]:
    """close に最も近いアクティブなネックラインと方向を返す。"""
    best_dist = float('inf')
    best_nl: float | None = None
    best_dir = 'none'
    for p in patterns:
        dist = abs(p.neckline - close)
        if dist < best_dist:
            best_dist = dist
            best_nl   = p.neckline
            best_dir  = p.direction
    return best_nl, best_dir


def analyze_macro_bias(
    symbol: str,
    cfg: dict,
    close_v: float,
    atr_v: float,
    *,
    mt5,
) -> dict:
    """
    D1/W1/MN1 パターン・RSI・SMA200 からマクロバイアスを算出する。

    Returns dict with:
        bias          : float  (-100〜+100)
        bias_label    : str    ('strong_bull' / 'weak_bull' / 'neutral' / 'weak_bear' / 'strong_bear')
        d1_patterns   : list[PatternResult]
        w1_patterns   : list[PatternResult]
        mn1_patterns  : list[PatternResult]
        nearest_nl    : float | None   (最近傍ネックライン価格)
        nl_dir        : str            ('bullish'|'bearish'|'none')
        target_up     : float | None   (最大上昇ターゲット)
        target_down   : float | None   (最大下落ターゲット)
        buy_tp_multi  : float
        sell_tp_multi : float
        buy_risk_multi : float
        sell_risk_multi: float
        score_adj_buy : int
        score_adj_sell: int
        d1_rsi        : float
        d1_above_sma200 : bool
        summary       : str
    """
    macro_cfg = cfg.get('MACRO', {})
    d1_bars  = macro_cfg.get('d1_bars',  200)
    w1_bars  = macro_cfg.get('w1_bars',  100)
    mn1_bars = macro_cfg.get('mn1_bars',  60)

    df_d1  = fetch_ohlcv(symbol, 'D1',  d1_bars)
    df_w1  = fetch_ohlcv(symbol, 'W1',  w1_bars)
    df_mn1 = fetch_ohlcv(symbol, 'MN1', mn1_bars)

    # ── パターン検出 ─────────────────────────────────────────
    d1_pats  = _detect_patterns_for_tf(df_d1,  'D1',  macro_cfg)
    w1_pats  = _detect_patterns_for_tf(df_w1,  'W1',  macro_cfg)
    mn1_pats = _detect_patterns_for_tf(df_mn1, 'MN1', macro_cfg)

    # ── パターンスコア ────────────────────────────────────────
    bias = 0.0
    for p in d1_pats:
        bias += _score_pattern(p, 'D1')
    for p in w1_pats:
        bias += _score_pattern(p, 'W1')
    for p in mn1_pats:
        bias += _score_pattern(p, 'MN1')

    # ── D1 RSI 補正 ───────────────────────────────────────────
    d1_rsi = float('nan')
    if df_d1 is not None and len(df_d1) >= 15:
        closes = df_d1['Close'].values
        deltas = np.diff(closes)
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        period = 14
        if len(gains) >= period:
            avg_g = np.mean(gains[-period:])
            avg_l = np.mean(losses[-period:])
            rs    = avg_g / (avg_l + 1e-9)
            d1_rsi = float(100.0 - 100.0 / (1.0 + rs))

    if not np.isnan(d1_rsi):
        if d1_rsi >= 70:
            bias += 15
        elif d1_rsi >= 55:
            bias += 8
        elif d1_rsi <= 30:
            bias -= 15
        elif d1_rsi <= 45:
            bias -= 8

    # ── D1 SMA200 上下 ────────────────────────────────────────
    d1_above_sma200 = False
    if df_d1 is not None and len(df_d1) >= 200:
        sma200 = float(df_d1['Close'].iloc[-200:].mean())
        d1_above_sma200 = close_v > sma200
        bias += 10 if d1_above_sma200 else -10

    # ── ネックライン近傍補正 ──────────────────────────────────
    all_pats = d1_pats + w1_pats + mn1_pats
    nearest_nl, nl_dir = _nearest_neckline(all_pats, close_v, atr_v)
    if nearest_nl is not None and not np.isnan(atr_v) and atr_v > 0:
        if abs(nearest_nl - close_v) <= atr_v * 1.5:
            bias += 10 if nl_dir == 'bullish' else -10

    # ── ターゲット計算 ────────────────────────────────────────
    targets_up   = [p.target for p in all_pats if p.direction == 'bullish' and p.target > close_v]
    targets_down = [p.target for p in all_pats if p.direction == 'bearish' and p.target < close_v]
    target_up   = max(targets_up)   if targets_up   else None
    target_down = min(targets_down) if targets_down else None

    bias = float(np.clip(bias, -100, 100))
    label = _bias_label(bias)
    params = _BIAS_PARAMS[label]

    # サマリー文字列
    pat_summary = []
    for p in sorted(all_pats, key=lambda x: abs(_score_pattern(x, 'D1')), reverse=True)[:3]:
        pat_summary.append(f'{p.label}({"確認" if p.confirmed else "未確認"},{p.confidence:.0%})')
    summary = f'bias={bias:+.0f}[{label}]' + (f' pats:{",".join(pat_summary)}' if pat_summary else '')

    return dict(
        bias=bias,
        bias_label=label,
        d1_patterns=d1_pats,
        w1_patterns=w1_pats,
        mn1_patterns=mn1_pats,
        nearest_nl=nearest_nl,
        nl_dir=nl_dir,
        target_up=target_up,
        target_down=target_down,
        buy_tp_multi=params['buy_tp_multi'],
        sell_tp_multi=params['sell_tp_multi'],
        buy_risk_multi=params['buy_risk_multi'],
        sell_risk_multi=params['sell_risk_multi'],
        score_adj_buy=params['score_adj_buy'],
        score_adj_sell=params['score_adj_sell'],
        d1_rsi=d1_rsi,
        d1_above_sma200=d1_above_sma200,
        summary=summary,
    )
