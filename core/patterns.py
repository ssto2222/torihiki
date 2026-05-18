"""core/patterns.py — テクニカルパターン検知

対応パターン:
  - Wボトム (ダブルボトム)
  - ダブルトップ (逆W)
  - 三尊 (ヘッド＆ショルダーズ)
  - 逆三尊 (逆ヘッド＆ショルダーズ)

使い方:
    from core.patterns import detect_all_patterns
    patterns = detect_all_patterns(df_h1)   # OHLC DataFrame
    for p in patterns:
        print(p.signal)
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd


@dataclass
class PatternResult:
    name:       str    # 'double_bottom' | 'double_top' | 'head_shoulders' | 'inv_head_shoulders'
    label:      str    # 日本語ラベル
    direction:  str    # 'bullish' | 'bearish'
    confidence: float  # 0.0〜1.0
    neckline:   float  # ネックライン価格
    target:     float  # 理論的な価格目標 (height projection)
    confirmed:  bool   # ネックラインブレイク済み
    bars_ago:   int    # パターン右端から現在までの本数
    key_points: list[tuple[int, float]] = field(default_factory=list)  # (bar_idx, price)

    @property
    def signal(self) -> str:
        status = '確認済' if self.confirmed else '未確認'
        return (f'[{self.label}] {status}  信頼度={self.confidence:.0%}'
                f'  NL={self.neckline:,.1f}  TP={self.target:,.1f}'
                f'  ({self.bars_ago}本前)')


# ── スイングポイント検出 ────────────────────────────────────────────────────────

def find_swing_points(
    df: pd.DataFrame,
    window: int = 5,
    min_prom_atr_ratio: float = 0.15,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """
    スイングハイ・スイングローを検出する。
    window             : 各側に何本確認するか
    min_prom_atr_ratio : ATR × この値 未満の突出は除外（ノイズフィルタ）

    Returns: (swing_highs, swing_lows) — list of (bar_index, price)
    """
    has_hl = 'High' in df.columns and 'Low' in df.columns
    highs  = df['High'].values  if has_hl else df['Close'].values
    lows   = df['Low'].values   if has_hl else df['Close'].values
    closes = df['Close'].values if 'Close' in df.columns else df.iloc[:, 0].values

    # 簡易ATR (終値差の移動平均)
    atr_raw = np.abs(np.diff(closes))
    atr     = float(np.nanmean(atr_raw)) if len(atr_raw) > 0 else 1.0
    min_prom = atr * min_prom_atr_ratio

    swing_highs: list[tuple[int, float]] = []
    swing_lows:  list[tuple[int, float]] = []

    for i in range(window, len(df) - window):
        h = highs[i]
        l = lows[i]

        left_h  = highs[i - window:i]
        right_h = highs[i + 1:i + window + 1]
        left_l  = lows[i - window:i]
        right_l = lows[i + 1:i + window + 1]

        if h >= np.max(left_h) and h >= np.max(right_h):
            prom = h - max(np.min(left_h), np.min(right_h))
            if prom >= min_prom:
                swing_highs.append((i, float(h)))

        if l <= np.min(left_l) and l <= np.min(right_l):
            prom = min(np.max(left_l), np.max(right_l)) - l
            if prom >= min_prom:
                swing_lows.append((i, float(l)))

    return swing_highs, swing_lows


def _get_ohlc(df: pd.DataFrame):
    """(close, high, low) の numpy 配列を返す"""
    close = df['Close'].values if 'Close' in df.columns else df.iloc[:, 0].values
    high  = df['High'].values  if 'High'  in df.columns else close
    low   = df['Low'].values   if 'Low'   in df.columns else close
    return close, high, low


# ── Wボトム ─────────────────────────────────────────────────────────────────

def detect_double_bottom(
    df: pd.DataFrame,
    swing_lows: list[tuple[int, float]],
    *,
    price_tol:  float = 0.03,   # 2谷の価格差許容 (3%)
    min_sep:    int   = 5,      # 谷間の最小バー数
    max_sep:    int   = 120,    # 谷間の最大バー数
    min_height: float = 0.005,  # ネックラインの高さ最小値 (0.5%)
    lookback:   int   = 250,
) -> list[PatternResult]:
    """Wボトム（ダブルボトム）を検知する。"""
    results: list[PatternResult] = []
    close, high, low = _get_ohlc(df)
    n = len(df)

    recent = [(i, p) for i, p in swing_lows if i >= n - lookback]

    for a in range(len(recent) - 1):
        idx1, l1 = recent[a]
        for b in range(a + 1, len(recent)):
            idx2, l2 = recent[b]
            sep = idx2 - idx1
            if not (min_sep <= sep <= max_sep):
                continue

            avg_l = (l1 + l2) / 2
            if abs(l1 - l2) / avg_l > price_tol:
                continue

            # ネックライン: 2谷間の最高値
            neckline = float(np.max(high[idx1:idx2 + 1]))
            height   = neckline - avg_l
            if height / avg_l < min_height:
                continue

            # ネックライン上抜け確認
            confirmed = bool(np.any(close[idx2 + 1:] > neckline))

            sym  = 1.0 - abs(l1 - l2) / avg_l / price_tol
            hgt  = min(height / avg_l / 0.03, 1.0)
            rec  = 1.0 - (n - 1 - idx2) / lookback
            conf = float(np.clip(sym * 0.4 + hgt * 0.3 + rec * 0.3, 0.0, 1.0))

            results.append(PatternResult(
                name='double_bottom', label='Wボトム', direction='bullish',
                confidence=conf,
                neckline=round(neckline, 2),
                target=round(neckline + height, 2),
                confirmed=confirmed,
                bars_ago=n - 1 - idx2,
                key_points=[(idx1, l1), (idx2, l2)],
            ))

    return sorted(results, key=lambda r: r.confidence, reverse=True)


# ── ダブルトップ ──────────────────────────────────────────────────────────────

def detect_double_top(
    df: pd.DataFrame,
    swing_highs: list[tuple[int, float]],
    *,
    price_tol:  float = 0.03,
    min_sep:    int   = 5,
    max_sep:    int   = 120,
    min_height: float = 0.005,
    lookback:   int   = 250,
) -> list[PatternResult]:
    """ダブルトップ（逆W）を検知する。"""
    results: list[PatternResult] = []
    close, high, low = _get_ohlc(df)
    n = len(df)

    recent = [(i, p) for i, p in swing_highs if i >= n - lookback]

    for a in range(len(recent) - 1):
        idx1, h1 = recent[a]
        for b in range(a + 1, len(recent)):
            idx2, h2 = recent[b]
            sep = idx2 - idx1
            if not (min_sep <= sep <= max_sep):
                continue

            avg_h = (h1 + h2) / 2
            if abs(h1 - h2) / avg_h > price_tol:
                continue

            neckline = float(np.min(low[idx1:idx2 + 1]))
            height   = avg_h - neckline
            if height / avg_h < min_height:
                continue

            confirmed = bool(np.any(close[idx2 + 1:] < neckline))

            sym  = 1.0 - abs(h1 - h2) / avg_h / price_tol
            hgt  = min(height / avg_h / 0.03, 1.0)
            rec  = 1.0 - (n - 1 - idx2) / lookback
            conf = float(np.clip(sym * 0.4 + hgt * 0.3 + rec * 0.3, 0.0, 1.0))

            results.append(PatternResult(
                name='double_top', label='ダブルトップ', direction='bearish',
                confidence=conf,
                neckline=round(neckline, 2),
                target=round(neckline - height, 2),
                confirmed=confirmed,
                bars_ago=n - 1 - idx2,
                key_points=[(idx1, h1), (idx2, h2)],
            ))

    return sorted(results, key=lambda r: r.confidence, reverse=True)


# ── 三尊 ─────────────────────────────────────────────────────────────────────

def detect_head_shoulders(
    df: pd.DataFrame,
    swing_highs: list[tuple[int, float]],
    swing_lows:  list[tuple[int, float]],
    *,
    sh_tol:      float = 0.05,   # 左右肩の対称許容 (5%)
    min_head:    float = 0.01,   # 頭が肩より最低これだけ高い
    min_sep:     int   = 3,      # 肩-頭間の最小バー数
    lookback:    int   = 350,
) -> list[PatternResult]:
    """三尊（ヘッド&ショルダーズ）を検知する。"""
    results: list[PatternResult] = []
    close, _, _ = _get_ohlc(df)
    n = len(df)

    recent_h = [(i, p) for i, p in swing_highs if i >= n - lookback]

    for a in range(len(recent_h) - 2):
        idx_ls, h_ls = recent_h[a]
        for b in range(a + 1, len(recent_h) - 1):
            idx_hd, h_hd = recent_h[b]
            if idx_hd - idx_ls < min_sep:
                continue
            if h_hd <= h_ls * (1 + min_head):
                continue                    # 頭が左肩以下
            for c in range(b + 1, len(recent_h)):
                idx_rs, h_rs = recent_h[c]
                if idx_rs - idx_hd < min_sep:
                    continue
                if idx_rs - idx_ls > lookback:
                    continue

                avg_sh = (h_ls + h_rs) / 2
                if abs(h_ls - h_rs) / avg_sh > sh_tol:
                    continue               # 左右肩が非対称
                if h_hd <= h_rs * (1 + min_head):
                    continue               # 頭が右肩以下

                # ネックライン: 左肩-頭 / 頭-右肩 の間の安値平均
                nl1 = [p for i, p in swing_lows if idx_ls < i < idx_hd]
                nl2 = [p for i, p in swing_lows if idx_hd < i < idx_rs]
                if not nl1 or not nl2:
                    continue
                neckline = (min(nl1) + min(nl2)) / 2
                height   = h_hd - neckline
                if height / h_hd < 0.005:
                    continue

                confirmed = bool(np.any(close[idx_rs + 1:] < neckline))

                sym  = 1.0 - abs(h_ls - h_rs) / avg_sh / sh_tol
                hgt  = min(height / h_hd / 0.03, 1.0)
                rec  = 1.0 - (n - 1 - idx_rs) / lookback
                conf = float(np.clip(sym * 0.45 + hgt * 0.3 + rec * 0.25, 0.0, 1.0))

                results.append(PatternResult(
                    name='head_shoulders', label='三尊', direction='bearish',
                    confidence=conf,
                    neckline=round(neckline, 2),
                    target=round(neckline - height, 2),
                    confirmed=confirmed,
                    bars_ago=n - 1 - idx_rs,
                    key_points=[(idx_ls, h_ls), (idx_hd, h_hd), (idx_rs, h_rs)],
                ))

    return sorted(results, key=lambda r: r.confidence, reverse=True)


# ── 逆三尊 ───────────────────────────────────────────────────────────────────

def detect_inv_head_shoulders(
    df: pd.DataFrame,
    swing_highs: list[tuple[int, float]],
    swing_lows:  list[tuple[int, float]],
    *,
    sh_tol:   float = 0.05,
    min_head: float = 0.01,
    min_sep:  int   = 3,
    lookback: int   = 350,
) -> list[PatternResult]:
    """逆三尊（逆ヘッド&ショルダーズ）を検知する。"""
    results: list[PatternResult] = []
    close, _, _ = _get_ohlc(df)
    n = len(df)

    recent_l = [(i, p) for i, p in swing_lows if i >= n - lookback]

    for a in range(len(recent_l) - 2):
        idx_ls, l_ls = recent_l[a]
        for b in range(a + 1, len(recent_l) - 1):
            idx_hd, l_hd = recent_l[b]
            if idx_hd - idx_ls < min_sep:
                continue
            if l_hd >= l_ls * (1 - min_head):
                continue                    # 頭が左肩以上
            for c in range(b + 1, len(recent_l)):
                idx_rs, l_rs = recent_l[c]
                if idx_rs - idx_hd < min_sep:
                    continue
                if idx_rs - idx_ls > lookback:
                    continue

                avg_sh = (l_ls + l_rs) / 2
                if abs(l_ls - l_rs) / avg_sh > sh_tol:
                    continue
                if l_hd >= l_rs * (1 - min_head):
                    continue

                nl1 = [p for i, p in swing_highs if idx_ls < i < idx_hd]
                nl2 = [p for i, p in swing_highs if idx_hd < i < idx_rs]
                if not nl1 or not nl2:
                    continue
                neckline = (max(nl1) + max(nl2)) / 2
                height   = neckline - l_hd
                if height / neckline < 0.005:
                    continue

                confirmed = bool(np.any(close[idx_rs + 1:] > neckline))

                sym  = 1.0 - abs(l_ls - l_rs) / avg_sh / sh_tol
                hgt  = min(height / neckline / 0.03, 1.0)
                rec  = 1.0 - (n - 1 - idx_rs) / lookback
                conf = float(np.clip(sym * 0.45 + hgt * 0.3 + rec * 0.25, 0.0, 1.0))

                results.append(PatternResult(
                    name='inv_head_shoulders', label='逆三尊', direction='bullish',
                    confidence=conf,
                    neckline=round(neckline, 2),
                    target=round(neckline + height, 2),
                    confirmed=confirmed,
                    bars_ago=n - 1 - idx_rs,
                    key_points=[(idx_ls, l_ls), (idx_hd, l_hd), (idx_rs, l_rs)],
                ))

    return sorted(results, key=lambda r: r.confidence, reverse=True)


# ── 全パターン一括検知 ─────────────────────────────────────────────────────────

def detect_all_patterns(
    df: pd.DataFrame,
    window: int = 5,
    top_n: int  = 3,
) -> list[PatternResult]:
    """
    全4パターンを検知し、信頼度上位を返す。
    df: OHLC または Close のみの DataFrame
    window: スイングポイント検出幅
    top_n: 各パターン最大何件を収集するか
    """
    sh, sl = find_swing_points(df, window=window)
    all_r: list[PatternResult] = []
    all_r.extend(detect_double_bottom(df, sl)[:top_n])
    all_r.extend(detect_double_top(df, sh)[:top_n])
    all_r.extend(detect_head_shoulders(df, sh, sl)[:top_n])
    all_r.extend(detect_inv_head_shoulders(df, sh, sl)[:top_n])
    return sorted(all_r, key=lambda r: r.confidence, reverse=True)
