"""core/indicators.py — テクニカル指標・急落検出"""
from __future__ import annotations
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    g = d.clip(lower=0).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - df['Close'].shift()).abs(),
        (df['Low']  - df['Close'].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()


def add_h1_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """H1 全指標を付加して返す（元 DF は変更しない）"""
    ind = cfg.get('INDICATOR', {})
    rp  = ind.get('rsi_period',   14)
    ap  = ind.get('atr_period',   14)
    ma  = ind.get('atr_ma_bars',  50)
    bp  = ind.get('bb_period',    20)
    bs  = ind.get('bb_sigma',    3.0)
    ef  = ind.get('ema_fast',     21)
    es  = ind.get('ema_slow',     50)
    sw  = ind.get('swing_period', 20)

    df = df.copy()
    df['RSI']       = calc_rsi(df['Close'], rp)
    df['ATR']       = calc_atr(df, ap)
    df['ATR_MA']    = df['ATR'].rolling(ma).mean()
    df['ATR_ratio'] = df['ATR'] / df['ATR_MA'].replace(0, np.nan)

    bb_ma          = df['Close'].rolling(bp).mean()
    bb_std         = df['Close'].rolling(bp).std()
    df['BB_upper'] = bb_ma + bs * bb_std
    df['BB_lower'] = bb_ma - bs * bb_std
    df['BB_mid']   = bb_ma
    df['BB_pct']   = (df['Close'] - bb_ma) / (df['BB_upper'] - bb_ma).replace(0, np.nan)

    df['EMA21']      = df['Close'].ewm(span=ef, adjust=False).mean()
    df['EMA50']      = df['Close'].ewm(span=es, adjust=False).mean()
    df['Swing_Low']  = df['Low'].rolling(sw).min()
    df['Swing_High'] = df['High'].rolling(sw).max()

    return df.dropna()


def add_m1_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """M1 SMA20・RSI・ATR を付加して返す"""
    ind = cfg.get('INDICATOR', {})
    df  = df.copy()
    df['SMA20'] = df['Close'].rolling(ind.get('sma_m1', 20)).mean()
    df['RSI']   = calc_rsi(df['Close'], ind.get('rsi_period', 14))
    df['ATR']   = calc_atr(df, ind.get('atr_period', 14))
    return df.dropna()


def detect_crash_events(df_h1: pd.DataFrame, df_m1: pd.DataFrame,
                         cfg: dict) -> pd.DataFrame:
    """
    H1足から急落イベントを自動検出

    検出条件（いずれかを満たすバー）:
      a) 1本下落幅 > ATR × crash_atr_multi
      b) オープンギャップダウン > crash_gap_usd
      c) ATR_ratio > vol_spike
    """
    cr       = cfg.get('CRASH', {})
    atr_thr  = cr.get('atr_multi', 2.5)
    gap_thr  = cr.get('gap_usd',   8.0)
    spk_thr  = cr.get('vol_spike', 2.0)

    records = []
    for i in range(1, len(df_h1)):
        row   = df_h1.iloc[i]
        prev  = df_h1.iloc[i - 1]
        atr_v = float(row['ATR']) if not np.isnan(row['ATR']) else 1.0
        ratio = float(row.get('ATR_ratio', np.nan))
        if np.isnan(ratio): ratio = 1.0

        drop = float(prev['Close'] - row['Close'])   # 正=下落
        gap  = float(prev['Close'] - row['Open'])    # 正=ギャップダウン

        cond_drop  = drop > 0 and drop > atr_v * atr_thr
        cond_gap   = gap  > gap_thr
        cond_spike = ratio > spk_thr
        if not (cond_drop or cond_gap or cond_spike):
            continue

        n = len(df_h1)
        records.append({
            'bar':         i,
            'time':        df_h1.index[i],
            'close':       float(row['Close']),
            'drop_usd':    max(drop, 0.0),
            'gap_usd':     max(gap,  0.0),
            'atr':         atr_v,
            'atr_ratio':   ratio,
            'drop_atr':    max(drop, 0.0) / atr_v,
            'cause':       'drop' if cond_drop else ('gap' if cond_gap else 'spike'),
            'recovery_3h':  (df_h1['Close'].iloc[min(i+3,  n-1)] / row['Close'] - 1) * 100,
            'recovery_6h':  (df_h1['Close'].iloc[min(i+6,  n-1)] / row['Close'] - 1) * 100,
            'recovery_12h': (df_h1['Close'].iloc[min(i+12, n-1)] / row['Close'] - 1) * 100,
        })

    df_c = pd.DataFrame(records) if records else pd.DataFrame()

    # M1 ギャップ統計
    m1_gap   = (df_m1['Open'] - df_m1['Close'].shift()).dropna()
    neg_gaps = m1_gap[m1_gap < -gap_thr / 2]

    print(f"[急落検出] H1ベース: {len(df_c)}件", end='')
    if not df_c.empty:
        causes = df_c['cause'].value_counts().to_dict()
        print(f"  {causes}")
        print(f"  下落幅: avg=${df_c.drop_usd.mean():.2f}  max=${df_c.drop_usd.max():.2f}"
              f"  ATR比max={df_c.drop_atr.max():.2f}")
        r6 = df_c['recovery_6h']
        print(f"  6H後回復: avg={r6.mean():+.2f}%  "
              f"反発={(r6>0).sum()}/{len(r6)}件")
    else:
        print()

    print(f"[M1ギャップ] 下方>{gap_thr/2:.1f}USD: {len(neg_gaps)}件  "
          f"avg=${neg_gaps.mean():.2f}  worst=${neg_gaps.min():.2f}"
          if len(neg_gaps) > 0 else "[M1ギャップ] 下方ギャップなし")

    return df_c
