"""core/data.py — データ取得・合成データ生成"""
from __future__ import annotations
import warnings
import numpy as np
import pandas as pd
warnings.filterwarnings('ignore')
from secret import MT5_LOGIN_INFO


# ── MT5 ───────────────────────────────────────────────────

def connect_mt5(symbol: str, mt5_cfg: dict | None = None) -> bool:
    try:
        import MetaTrader5 as mt5
        init_kwargs: dict = {}
        if mt5_cfg:
            login = mt5_cfg.get('login', 0)
            pw    = mt5_cfg.get('password', '')
            srv   = mt5_cfg.get('server', '')
            if login:    init_kwargs['login']    = int(login)
            if pw:       init_kwargs['password'] = str(pw)
            if srv:      init_kwargs['server']   = str(srv)

        if not mt5.initialize(**init_kwargs):
            err = mt5.last_error()
            print(f"[MT5] 初期化失敗: {err}")
            if err[0] == -6:
                print("  → MT5 ターミナルを起動してアカウントにログインするか")
                print("  → config.py の MT5['login'] / MT5['password'] / MT5['server'] を設定してください")
            return False
        info = mt5.symbol_info(symbol)
        if info is None:
            cands = [s.name for s in (mt5.symbols_get() or [])
                     if 'XAU' in s.name.upper() or 'GOLD' in s.name.upper()]
            print(f"[MT5] '{symbol}' 未検出。候補: {cands[:8]}")
            mt5.shutdown(); return False
        if not info.visible:
            mt5.symbol_select(symbol, True)
        print(f"[MT5] 接続OK: {symbol}  digits={info.digits}  spread={info.spread}pt")
        return True
    except ImportError:
        print("[MT5] pip install MetaTrader5 が必要です")
        return False


def fetch_ohlcv(symbol: str, tf_str: str, bars: int) -> pd.DataFrame | None:
    try:
        import MetaTrader5 as mt5
        tf_map = {'M1':mt5.TIMEFRAME_M1,'M5':mt5.TIMEFRAME_M5,
                  'H1':mt5.TIMEFRAME_H1,'H4':mt5.TIMEFRAME_H4,
                  'D1':mt5.TIMEFRAME_D1}
        rates = mt5.copy_rates_from_pos(symbol, tf_map[tf_str], 0, bars)
        if rates is None or len(rates) == 0: return None
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        df.rename(columns={'open':'Open','high':'High','low':'Low',
                           'close':'Close','tick_volume':'Volume'}, inplace=True)
        df = df[['Open','High','Low','Close','Volume']].copy()
        print(f"[MT5] {symbol} {tf_str}: {len(df)}本  "
              f"{df.index[0].strftime('%Y-%m-%d')}〜{df.index[-1].strftime('%Y-%m-%d')}")
        return df
    except Exception as e:
        print(f"[MT5] fetch失敗 {tf_str}: {e}"); return None


def load_mt5(cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame] | tuple[None, None]:
    sym = cfg['MT5']['symbol']
    if not connect_mt5(sym, cfg['MT5']): return None, None
    try:
        import MetaTrader5 as mt5
        h1 = fetch_ohlcv(sym, 'H1', cfg['MT5']['h1_bars'])
        m1 = fetch_ohlcv(sym, 'M1', cfg['MT5']['m1_bars'])
        mt5.shutdown()
    except Exception: return None, None
    if h1 is None or m1 is None: return None, None
    h1 = h1[h1.index >= m1.index[0]]
    print(f"[データ] H1={len(h1)}本 / M1={len(m1)}本（期間一致後）")
    return h1, m1


# ── 合成データ（ローカル分析・テスト用）──────────────────────

def generate_h1(n: int = 3600, seed: int = 42,
                crash_rate: float = 0.008) -> pd.DataFrame:
    """
    XAUUSD H1 合成データ
    ・600bar周期のゼロドリフト対称サイクル（上昇/下降均等）
    ・ATR帯域: $10〜$18（実XAUUSD相当）
    ・急落イベント（crash_rate * n 件）
    ・M1生成が正確になるよう Open を Close に近い値で生成
    """
    rng   = np.random.default_rng(seed)
    dates = pd.date_range('2022-01-01', periods=n, freq='h')

    cycle  = 600
    price  = 1900.0
    prices = []
    n_crash = max(3, int(n * crash_rate))
    crash_set = set(int(b) for b in
                    rng.choice(range(200, n - 100), size=n_crash, replace=False))

    for i in range(n):
        ph = (i % cycle) / cycle
        if   ph < 0.20: drift, vol = +0.0010, 0.009
        elif ph < 0.40: drift, vol = -0.0002, 0.006
        elif ph < 0.60: drift, vol = -0.0010, 0.010
        elif ph < 0.80: drift, vol = +0.0002, 0.006
        else:           drift, vol = -0.00002 * (price - 1900) / max(price, 1), 0.005

        shock = rng.normal(drift, vol)
        if i in crash_set:
            shock -= rng.uniform(0.018, 0.040)      # -1.8%〜-4.0%
        elif rng.random() < 0.003:
            shock -= rng.uniform(0.008, 0.018)      # 軽微急落

        price = max(1300.0, price * np.exp(shock))
        prices.append(price)

    prices = np.array(prices)
    hl     = 0.0035   # 実XAUUSD H1の High-Low 幅比率

    # Open は前Close±小さなギャップ（合成でも現実的な値に）
    opens = np.empty(n)
    opens[0] = prices[0]
    for i in range(1, n):
        opens[i] = prices[i-1] * np.exp(rng.normal(0, 0.0008))

    highs = np.maximum(opens, prices) * (1 + np.abs(rng.normal(hl, 0.002, n)))
    lows  = np.minimum(opens, prices) * (1 - np.abs(rng.normal(hl, 0.002, n)))

    df = pd.DataFrame({'Open': opens, 'High': highs, 'Low': lows,
                       'Close': prices,
                       'Volume': rng.integers(500, 5000, n).astype(float)},
                      index=dates)
    df['High'] = df[['Open','High','Close']].max(axis=1)
    df['Low']  = df[['Open','Low','Close']].min(axis=1)
    df['is_crash'] = [i in crash_set for i in range(n)]

    ret = (prices[-1] / prices[0] - 1) * 100
    print(f"[合成H1] {n}本  ${prices.min():,.0f}〜${prices.max():,.0f}  "
          f"リターン:{ret:+.1f}%  急落:{len(crash_set)}件")
    return df


def generate_m5_from_h1(df_h1: pd.DataFrame, seed: int = 456) -> pd.DataFrame:
    """
    H1 → M5 変換（各H1バーを12本のM5バーに分解）
    M5 RSI フィルタ動作確認・バックテスト用合成データ
    """
    rng  = np.random.default_rng(seed)
    rows = []
    n_h1 = len(df_h1)

    for i in range(n_h1):
        row  = df_h1.iloc[i]
        o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
        ts   = df_h1.index[i]
        span = abs(h - l)

        drift = (c - o) / 12
        path  = [o]
        for _ in range(11):
            step = drift + rng.normal(0, span * 0.10)
            path.append(path[-1] + step)
        path = np.array(path)
        if abs(path[-1] - c) > 1e-6:
            path = path + (c - path[-1])
        path = np.clip(path, l, h)
        path[-1] = c

        for j in range(12):
            po = path[j]
            pc = path[j] if j == 11 else path[j+1]
            nb = abs(rng.normal(0, span * 0.03))
            rows.append({
                'time':   ts + pd.Timedelta(minutes=j * 5),
                'Open':   po,
                'High':   min(max(po, pc) + nb, h),
                'Low':    max(min(po, pc) - nb, l),
                'Close':  pc,
                'Volume': float(rng.integers(10, 200)),
            })

    df = pd.DataFrame(rows).set_index('time')
    print(f"[合成M5] {len(df)}本完了")
    return df


def generate_m1_from_h1(df_h1: pd.DataFrame, seed: int = 123) -> pd.DataFrame:
    """
    H1 → M1 変換（各H1バーを60本のM1バーに分解）
    ・OHLC整合性を保持
    ・H1の HL レンジ内でランダムウォーク
    ・ギャップは H1 Open-前Close差を1本目M1のOpenに反映
    """
    rng  = np.random.default_rng(seed)
    rows = []
    n_h1 = len(df_h1)

    for i in range(n_h1):
        row  = df_h1.iloc[i]
        o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
        ts   = df_h1.index[i]
        span = abs(h - l)
        vol  = span / (o * 60 + 1e-9)   # 1分足相当のボラ

        # 60本のClosepathを生成（o→c に向かうドリフト付きRW）
        drift = (c - o) / 60
        path  = [o]
        for _ in range(59):
            step = drift + rng.normal(0, span * 0.04)
            path.append(path[-1] + step)
        path = np.array(path)

        # cに正規化してHLレンジ内にクリップ
        if abs(path[-1] - c) > 1e-6:
            path = path + (c - path[-1])
        path = np.clip(path, l, h)
        path[-1] = c

        for j in range(60):
            po = path[j]
            pc = path[j] if j == 59 else path[j+1]
            nb = abs(rng.normal(0, span * 0.015))
            rows.append({
                'time':   ts + pd.Timedelta(minutes=j),
                'Open':   po,
                'High':   min(max(po, pc) + nb, h),
                'Low':    max(min(po, pc) - nb, l),
                'Close':  pc,
                'Volume': float(rng.integers(5, 80)),
            })

    df = pd.DataFrame(rows).set_index('time')
    print(f"[合成M1] {len(df)}本完了")
    return df


# ── 統合ロード ─────────────────────────────────────────────

def load_data(cfg: dict, force_synthetic: bool = False
              ) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    """
    (df_h1, df_m1, is_real) を返す
    force_synthetic=True → MT5 を試みず合成データ
    """
    if not force_synthetic:
        h1, m1 = load_mt5(cfg)
        if h1 is not None and m1 is not None:
            return h1, m1, True

    print("[フォールバック] 合成データを使用")
    loc = cfg.get('LOCAL', {})
    h1  = generate_h1(n=loc.get('h1_bars_synth', 3600),
                      crash_rate=loc.get('crash_rate', 0.008))
    m1  = generate_m1_from_h1(h1)
    return h1, m1, False
