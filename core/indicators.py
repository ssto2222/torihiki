"""core/indicators.py — テクニカル指標"""
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

    df['SMA20']      = df['Close'].rolling(20).mean()
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


