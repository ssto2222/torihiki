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


def calc_rvol(volume: pd.Series, period: int = 20) -> pd.Series:
    """
    Relative Volume (RVOL) を計算
    現在の出来高を過去 period 本の平均出来高で割った値
    """
    return volume / volume.rolling(period).mean()


def calc_price_acceleration(close: pd.Series, period: int = 5) -> pd.Series:
    """
    価格加速指標: 短期移動平均の変化率
    急騰初期検知に使用
    """
    sma = close.rolling(period).mean()
    return sma.pct_change(periods=1) * 100


def detect_volume_surge(volume: pd.Series, rvol: pd.Series,
                        volume_threshold: float = 2.0,
                        rvol_threshold: float = 1.5) -> pd.Series:
    """
    出来高急増検知
    volume_threshold: 直近出来高 vs 過去平均の倍率
    rvol_threshold: RVOLの閾値
    """
    vol_surge = volume > volume.rolling(20).mean() * volume_threshold
    rvol_surge = rvol > rvol_threshold
    return vol_surge & rvol_surge


def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX / +DI / -DI を計算して返す (Wilder smoothing)"""
    high, low, close = df['High'], df['Low'], df['Close']

    up   = high.diff()
    down = -low.diff()
    plus_dm  = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)

    alpha   = 1 / period
    kw      = dict(alpha=alpha, min_periods=period, adjust=False)
    atr_s   = tr.ewm(**kw).mean()
    plus_di = (pd.Series(plus_dm,  index=df.index).ewm(**kw).mean() / atr_s * 100)
    minus_di= (pd.Series(minus_dm, index=df.index).ewm(**kw).mean() / atr_s * 100)
    dx      = ((plus_di - minus_di).abs()
               / (plus_di + minus_di).replace(0, np.nan) * 100)
    adx     = dx.ewm(**kw).mean()

    return pd.DataFrame(
        {'ADX': adx, 'DI_plus': plus_di, 'DI_minus': minus_di},
        index=df.index,
    )


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

    adx_p = ind.get('adx_period', 14)
    adx_df = calc_adx(df, adx_p)
    df['ADX']      = adx_df['ADX']
    df['DI_plus']  = adx_df['DI_plus']
    df['DI_minus'] = adx_df['DI_minus']

    return df.dropna()


def add_m1_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """M1 SMA20・RSI・ATR を付加して返す"""
    ind = cfg.get('INDICATOR', {})
    df  = df.copy()
    df['SMA20'] = df['Close'].rolling(ind.get('sma_m1', 20)).mean()
    df['RSI']   = calc_rsi(df['Close'], ind.get('rsi_period', 14))
    df['ATR']   = calc_atr(df, ind.get('atr_period', 14))
    return df.dropna()


def add_m5_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """M5 RSI・ATR・ADX・RVOL・価格加速 を付加して返す（M5 エントリーフィルタ・レジーム判定用）"""
    ind = cfg.get('INDICATOR', {})

    # 出来高データがある場合のみRVOL計算
    if 'Volume' in df.columns and df['Volume'].notna().any():
        df = df.copy()
        df['RVOL'] = calc_rvol(df['Volume'], ind.get('rvol_period', 20))
        df['Price_Accel'] = calc_price_acceleration(df['Close'], ind.get('accel_period', 5))
        df['Volume_Surge'] = detect_volume_surge(
            df['Volume'],
            df['RVOL'],
            volume_threshold=ind.get('volume_surge_threshold', 2.0),
            rvol_threshold=ind.get('rvol_surge_threshold', 1.5)
        )
    else:
        df = df.copy()

    df['RSI'] = calc_rsi(df['Close'], ind.get('rsi_period', 14))
    df['ATR'] = calc_atr(df, ind.get('atr_period', 14))

    adx_df     = calc_adx(df, ind.get('adx_period', 14))
    df['ADX']      = adx_df['ADX']
    df['DI_plus']  = adx_df['DI_plus']
    df['DI_minus'] = adx_df['DI_minus']

    return df.dropna()


def add_d1_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """D1 データに RSI を付加して返す（MT5 D1取得データ用）"""
    ind = cfg.get('INDICATOR', {})
    df  = df.copy()
    df['RSI'] = calc_rsi(df['Close'], ind.get('rsi_period', 14))
    return df.dropna()


def add_d1_rsi_to_h1(df_h1: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    H1 DataFrame を D1 にリサンプルして RSI を計算し、
    RSI_D1 カラムとして H1 に forward-fill でマージして返す。
    バックテスト・ローカル分析で D1 RSI を使う際に呼ぶ。
    """
    ind = cfg.get('INDICATOR', {})
    rp  = ind.get('rsi_period', 14)
    d1_close = df_h1['Close'].resample('1D').last().dropna()
    d1_rsi   = calc_rsi(d1_close, rp)
    df_h1    = df_h1.copy()
    df_h1['RSI_D1'] = d1_rsi.reindex(df_h1.index, method='ffill')
    return df_h1


