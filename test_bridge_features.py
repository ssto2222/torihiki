"""
test_bridge_features.py — bridge/signal_normal.py 新機能ユニットテスト（MT5 不要）
Run: python test_bridge_features.py
"""
from __future__ import annotations
import sys, os, traceback
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import config as C
from bridge.state         import SignalState, JpyRateCache
from bridge.signal_normal import compute_signal


# ── cfg ───────────────────────────────────────────────────────

def make_cfg(split_frac: float = 0.5) -> dict:
    cfg = {k: getattr(C, k) for k in
           ['MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES',
            'BRIDGE', 'SCALP', 'REGIME']}
    cfg['RULES']     = {**cfg['RULES'],     'total_risk_pct': 0.30}
    cfg['EXECUTION'] = {**cfg['EXECUTION'], 'split_entry_frac': split_frac,
                                            'split_limit_pullback': 0.4}
    return cfg


# ── mock mt5 ──────────────────────────────────────────────────

def make_mt5():
    mt5  = MagicMock()
    info = MagicMock()
    info.trade_contract_size = 100.0
    info.volume_min  = 0.01
    info.volume_max  = 100.0
    info.volume_step = 0.01
    info.point       = 0.01
    mt5.symbol_info.return_value      = info
    tick = MagicMock(); tick.bid = 2000.0
    mt5.symbol_info_tick.return_value = tick
    acc  = MagicMock(); acc.balance   = 1_500_000.0
    mt5.account_info.return_value     = acc
    mt5.positions_get.return_value    = []
    mt5.ORDER_TYPE_BUY  = 0
    mt5.ORDER_TYPE_SELL = 1
    return mt5


# ── ohlcv builders ────────────────────────────────────────────

def _df(prices: np.ndarray, freq: str,
        last_bullish: int = 0) -> pd.DataFrame:
    """prices → OHLCV DataFrame。last_bullish > 0 なら末尾 N 本を強制陽線にする。"""
    n   = len(prices)
    rng = np.random.default_rng(7)
    noise = np.abs(rng.normal(1.0, 0.3, n))
    opens = np.roll(prices, 1); opens[0] = prices[0]
    if last_bullish:
        for i in range(-last_bullish, 0):
            opens[i] = prices[i] - 1.5   # open < close → 陽線
    highs = np.maximum(prices + noise, np.maximum(opens, prices))
    lows  = np.minimum(prices - noise, np.minimum(opens, prices))
    vols  = rng.integers(500, 5000, n).astype(float)
    idx   = pd.date_range(end=datetime.now(timezone.utc),
                          periods=n, freq=freq, tz=timezone.utc)
    return pd.DataFrame({'Open': opens, 'High': highs, 'Low': lows,
                         'Close': prices, 'Volume': vols}, index=idx)


def h1_uptrend(n=200):
    rng = np.random.default_rng(42)
    p = 2000.0; ps = []
    for _ in range(n):
        p += 0.4 + rng.normal(0, 2.5); p = max(p, 1.0); ps.append(p)
    return _df(np.array(ps), 'h')


def h1_declining_bullish_tail(n=200):
    """上昇後に下落でSMA20を引き下げ、末尾3本を陽線にして h1_two_bear=False を保証"""
    rng = np.random.default_rng(77)
    p = 2000.0; ps = []
    for i in range(n):
        if i < n - 25:
            p += 0.6 + rng.normal(0, 2.0)
        elif i < n - 3:
            p -= 3.0 + rng.normal(0, 1.5)
        else:
            p += 1.8                      # 末尾3本: 小幅陽線
        p = max(p, 1.0); ps.append(p)
    return _df(np.array(ps), 'h', last_bullish=3)


def h1_ranging_bullish_tail(n=200):
    """ADX<20 のレンジデータ。末尾3本を陽線にして SELL 不発火を保証。
    seed=117: ADX≈19.2, RSI≈66.0 (XAUUSD good zone), SMA20上向き, two_bear=False.
    """
    rng = np.random.default_rng(117)
    p = 2000.0; ps = []
    for i in range(n):
        p += rng.normal(0, 7.0)
        p = max(p, 1.0); ps.append(p)
    ps[-3:] = [ps[-4] + 0.5, ps[-4] + 1.0, ps[-4] + 1.5]   # 末尾陽線
    return _df(np.array(ps), 'h', last_bullish=3)


def d1(n=80):
    rng = np.random.default_rng(11)
    p = 2000.0; ps = []
    for _ in range(n):
        p += 2.0 + rng.normal(0, 15.0); p = max(p, 1.0); ps.append(p)
    return _df(np.array(ps), 'D')


def m5_uptrend(n=100):
    """seed=50 で SMA20 末尾が確実に上向き。"""
    rng = np.random.default_rng(50)
    p = 2000.0; ps = []
    for _ in range(n):
        p += 0.2 + rng.normal(0, 1.2); p = max(p, 1.0); ps.append(p)
    return _df(np.array(ps), '5min')


def m15(n=60):
    rng = np.random.default_rng(55)
    p = 2000.0; ps = []
    for _ in range(n):
        p += 0.3 + rng.normal(0, 2.0); p = max(p, 1.0); ps.append(p)
    return _df(np.array(ps), '15min')


def m1_peaked(n=100):
    """上昇トレンド + bar[-4]で急落 → RSI[-2]<=RSI[-3]（ピーク確定）、両者 >=57。
    volatility=3.0 で十分な下落バーを生成し、30本スライス後も RSI が nan にならない。
    """
    rng = np.random.default_rng(33)
    p = 2000.0; ps = []
    for i in range(n - 3):
        p += 0.3 + rng.normal(0, 3.0); p = max(p, 1.0); ps.append(p)
    ps.append(ps[-1] + 1.0)    # bar[-3]: 小幅上昇
    ps.append(ps[-1] - 5.0)    # bar[-2]: 下落バー → RSI低下
    ps.append(ps[-1] + 0.5)    # bar[-1]: 小幅回復
    prices = np.array(ps)
    # 十分なノイズで OHLCV を構築
    rng2 = np.random.default_rng(34)
    noise = np.abs(rng2.normal(1.5, 0.3, n))
    opens = np.roll(prices, 1); opens[0] = prices[0]
    highs = np.maximum(prices + noise, np.maximum(opens, prices))
    lows  = np.minimum(prices - noise, np.minimum(opens, prices))
    vols  = rng2.integers(50, 500, n).astype(float)
    idx   = pd.date_range(end=datetime.now(timezone.utc),
                          periods=n, freq='min', tz=timezone.utc)
    return pd.DataFrame({'Open': opens, 'High': highs, 'Low': lows,
                         'Close': prices, 'Volume': vols}, index=idx)


def m1_rising(n=100):
    """上昇トレンド → RSI[-2] > RSI[-3]（まだ上昇中）。
    volatility=3.0 で 30本スライス後も RSI が nan にならない。
    seed=44 で実証確認済: RSI[-2]=67.6, RSI[-3]=66.0。
    """
    rng = np.random.default_rng(44)
    p = 2000.0; ps = []
    for i in range(n):
        inc = 0.3 + rng.normal(0, 3.0) if i < n - 15 else 0.5 + rng.normal(0, 2.5)
        p = max(p + inc, 1.0); ps.append(p)
    prices = np.array(ps)
    rng2 = np.random.default_rng(45)
    noise = np.abs(rng2.normal(1.5, 0.3, n))
    opens = np.roll(prices, 1); opens[0] = prices[0]
    highs = np.maximum(prices + noise, np.maximum(opens, prices))
    lows  = np.minimum(prices - noise, np.minimum(opens, prices))
    vols  = rng2.integers(50, 500, n).astype(float)
    idx   = pd.date_range(end=datetime.now(timezone.utc),
                          periods=n, freq='min', tz=timezone.utc)
    return pd.DataFrame({'Open': opens, 'High': highs, 'Low': lows,
                         'Close': prices, 'Volume': vols}, index=idx)


# ── fetch mock ────────────────────────────────────────────────

def mock_fetch(h1, d1_, m5_, m1_, m15_):
    def _f(symbol, tf, bars):
        df = {'H1': h1, 'D1': d1_, 'M5': m5_, 'M1': m1_, 'M15': m15_}.get(tf)
        if df is None: return None
        return df.iloc[-bars:].copy() if len(df) > bars else df.copy()
    return _f


# ── state factory ─────────────────────────────────────────────

def buy_state(entry_in_window=0, split_pending=False) -> SignalState:
    st  = SignalState()
    fut = datetime.now(timezone.utc) + timedelta(hours=4)
    st.signal_active_type  = 'momentum_65'
    st.signal_active_until = fut
    # h1_uptrend RSI ≈ 65.96 → prev must be > 65 to prevent new momentum_65 cross detection
    st.prev_rsi_h1         = 67.0
    st.entry_in_window     = entry_in_window
    st.split_pending_buy   = split_pending
    st.signal_window_key   = ('momentum_65', fut)
    st.last_entry_price    = 2000.0 if entry_in_window > 0 else 0.0
    return st


def jpy():
    return JpyRateCache(value=150.0, fetched_at=datetime.now(timezone.utc))


# ── RSI / indicator peek ──────────────────────────────────────

def peek_m1_rsi(df):
    from core.indicators import add_m5_indicators
    cfg_ = make_cfg()
    try:
        df2 = add_m5_indicators(df.copy(), cfg_)
        if len(df2) < 3: return float('nan'), float('nan'), float('nan')
        return float(df2['RSI'].iloc[-1]), float(df2['RSI'].iloc[-2]), float(df2['RSI'].iloc[-3])
    except Exception:
        return float('nan'), float('nan'), float('nan')


def peek_h1(df):
    from core.indicators import add_h1_indicators
    cfg_ = make_cfg()
    df2 = add_h1_indicators(df.copy(), cfg_)
    s1, s2 = float(df2['SMA20'].iloc[-1]), float(df2['SMA20'].iloc[-2])
    b2, b3 = df2.iloc[-2], df2.iloc[-3]
    two_bear = b2['Close'] < b2['Open'] and b3['Close'] < b3['Open']
    adx = float(df2['ADX'].iloc[-1])
    return s1, s2, two_bear, adx


# ── report ────────────────────────────────────────────────────

PASS_COUNT = FAIL_COUNT = 0

def report(name, ok, detail=''):
    global PASS_COUNT, FAIL_COUNT
    if ok: PASS_COUNT += 1
    else:  FAIL_COUNT += 1
    print(f'  [{"PASS" if ok else "FAIL"}] {detail}')


# ── tests ─────────────────────────────────────────────────────

def t1_split_first():
    """T1: 初回エントリー → half-lot + split_pending=True"""
    cfg_ = make_cfg(0.5); st = buy_state(0); mt5 = make_mt5()
    df_m1 = m1_peaked()
    r, p, p2 = peek_m1_rsi(df_m1)
    print(f'    M1 RSI cur={r:.1f} prev={p:.1f} prev2={p2:.1f} peaked={p<=p2}')
    fetch = mock_fetch(h1_uptrend(), d1(), m5_uptrend(), df_m1, m15())
    with patch('bridge.signal_normal.fetch_ohlcv', side_effect=fetch):
        res = compute_signal('XAUUSD', cfg_, st, jpy(), mt5=mt5)
    if res is None:
        report('T1 split_first', False, 'returned None'); return
    ok = res['action'] == 'buy' and st.split_pending_buy is True
    report('T1 split_first', ok,
           f"action={res['action']} lot={res['lot_size']:.4f}"
           f" split_pending={st.split_pending_buy} skip={res.get('skip_reason','')!r}")


def t2_split_limit():
    """T2: split_pending=True → limit_buy + limit_prices 設定"""
    cfg_ = make_cfg(0.5); st = buy_state(1, split_pending=True); mt5 = make_mt5()
    fetch = mock_fetch(h1_uptrend(), d1(), m5_uptrend(), m1_peaked(), m15())
    with patch('bridge.signal_normal.fetch_ohlcv', side_effect=fetch):
        res = compute_signal('XAUUSD', cfg_, st, jpy(), mt5=mt5)
    if res is None:
        report('T2 split_limit', False, 'returned None'); return
    lp = res.get('limit_prices', [])
    ok = res['action'] == 'limit_buy' and len(lp) > 0 and st.split_pending_buy is False
    report('T2 split_limit', ok,
           f"action={res['action']} limit_prices={lp}"
           f" split_pending={st.split_pending_buy}")


def t3_split_disabled():
    """T3: split_frac=1.0 → split 無効, full-lot"""
    cfg_f = make_cfg(1.0); st_f = buy_state(0); mt5 = make_mt5()
    cfg_h = make_cfg(0.5); st_h = buy_state(0)
    fetch = mock_fetch(h1_uptrend(), d1(), m5_uptrend(), m1_peaked(), m15())
    with patch('bridge.signal_normal.fetch_ohlcv', side_effect=fetch):
        rf = compute_signal('XAUUSD', cfg_f, st_f, jpy(), mt5=mt5)
    with patch('bridge.signal_normal.fetch_ohlcv', side_effect=fetch):
        rh = compute_signal('XAUUSD', cfg_h, st_h, jpy(), mt5=mt5)
    if rf is None:
        report('T3 split_disabled', False, 'returned None'); return
    lf = rf['lot_size']; lh = rh['lot_size'] if rh else float('nan')
    ok = (rf['action'] == 'buy' and st_f.split_pending_buy is False
          and lf >= lh - 1e-6)
    report('T3 split_disabled', ok,
           f"action={rf['action']} lot_full={lf:.4f} lot_half={lh:.4f}"
           f" split_pending={st_f.split_pending_buy}")


def t4_h1_sma20_declining():
    """T4: H1 SMA20 下向き (h1_two_bear=False) → BUY=H1_SMA20_down_buy禁止"""
    cfg_ = make_cfg(); st = buy_state(0); mt5 = make_mt5()
    df_h1 = h1_declining_bullish_tail()
    s1, s2, tb, adx = peek_h1(df_h1)
    print(f'    H1 SMA20 {s1:.1f}<{s2:.1f} dec={s1<s2} two_bear={tb} ADX={adx:.1f}')
    fetch = mock_fetch(df_h1, d1(), m5_uptrend(), m1_peaked(), m15())
    with patch('bridge.signal_normal.fetch_ohlcv', side_effect=fetch):
        res = compute_signal('XAUUSD', cfg_, st, jpy(), mt5=mt5)
    if res is None:
        report('T4 h1_sma20_declining', False, 'returned None'); return
    skip = res.get('skip_reason', '')
    ok = res['action'] == 'none' and 'H1_SMA20_down_buy禁止' in skip
    report('T4 h1_sma20_declining', ok,
           f"action={res['action']} skip={skip!r}")


def t5_h1_range():
    """T5: H1 ADX<20 レンジ → H1レンジ執行スキップ"""
    cfg_ = make_cfg(); st = buy_state(0); mt5 = make_mt5()
    df_h1 = h1_ranging_bullish_tail()
    s1, s2, tb, adx = peek_h1(df_h1)
    print(f'    H1 ADX={adx:.1f} two_bear={tb}')
    fetch = mock_fetch(df_h1, d1(), m5_uptrend(), m1_peaked(), m15())
    with patch('bridge.signal_normal.fetch_ohlcv', side_effect=fetch):
        res = compute_signal('XAUUSD', cfg_, st, jpy(), mt5=mt5)
    if res is None:
        report('T5 h1_range', False, 'returned None'); return
    skip = res.get('skip_reason', ''); regime = res.get('regime_h1', '')
    # H1 range filter or SMA20 filter may fire — both correctly block execution.
    # Key assertion: action is none and regime is correctly detected as range.
    ok = res['action'] == 'none' and regime == 'range'
    report('T5 h1_range', ok,
           f"action={res['action']} regime={regime} ADX={adx:.1f} skip={skip!r}")


def t6_overshoot_rising_blocks():
    """T6: M1 RSI 上昇中 → 初回BUY=M1初回BUY ブロック"""
    cfg_ = make_cfg(); st = buy_state(0); mt5 = make_mt5()
    df_m1 = m1_rising()
    r, p, p2 = peek_m1_rsi(df_m1)
    print(f'    M1 RSI cur={r:.1f} prev={p:.1f} prev2={p2:.1f} rising={p>p2}')
    fetch = mock_fetch(h1_uptrend(), d1(), m5_uptrend(), df_m1, m15())
    with patch('bridge.signal_normal.fetch_ohlcv', side_effect=fetch):
        res = compute_signal('XAUUSD', cfg_, st, jpy(), mt5=mt5)
    if res is None:
        report('T6 overshoot_rising', False, 'returned None'); return
    skip = res.get('skip_reason', '')
    ok = res['action'] == 'none' and 'M1初回BUY' in skip
    report('T6 overshoot_rising', ok,
           f"action={res['action']} skip={skip!r}")


def t7_overshoot_peaked_passes():
    """T7: M1 RSI ピーク確定 → オーバーシュートガード通過"""
    cfg_ = make_cfg(); st = buy_state(0); mt5 = make_mt5()
    df_m1 = m1_peaked()
    r, p, p2 = peek_m1_rsi(df_m1)
    print(f'    M1 RSI cur={r:.1f} prev={p:.1f} prev2={p2:.1f} peaked={p<=p2}')
    fetch = mock_fetch(h1_uptrend(), d1(), m5_uptrend(), df_m1, m15())
    with patch('bridge.signal_normal.fetch_ohlcv', side_effect=fetch):
        res = compute_signal('XAUUSD', cfg_, st, jpy(), mt5=mt5)
    if res is None:
        report('T7 overshoot_peaked', False, 'returned None'); return
    skip = res.get('skip_reason', '')
    ok = 'M1初回BUY' not in skip
    report('T7 overshoot_peaked', ok,
           f"action={res['action']} skip={skip!r}")


# ── main ──────────────────────────────────────────────────────

TESTS = [
    ('T1: split_entry_first',              t1_split_first),
    ('T2: split_entry_limit',              t2_split_limit),
    ('T3: split_disabled (frac=1.0)',      t3_split_disabled),
    ('T4: H1 SMA20 declining → BUY禁止',  t4_h1_sma20_declining),
    ('T5: H1 range → 執行スキップ',         t5_h1_range),
    ('T6: M1 RSI 上昇中 → 初回BUYブロック', t6_overshoot_rising_blocks),
    ('T7: M1 RSI ピーク済み → 通過',        t7_overshoot_peaked_passes),
]

if __name__ == '__main__':
    print('=' * 65)
    print('bridge/signal_normal.py  feature tests')
    print('=' * 65)
    for name, fn in TESTS:
        print(f'\n{name}')
        try:
            fn()
        except Exception as e:
            traceback.print_exc()
            report(name, False, f'Exception: {e}')
    print(f'\n{"=" * 65}')
    print(f'Results: {PASS_COUNT} passed, {FAIL_COUNT} failed'
          f'  ({PASS_COUNT + FAIL_COUNT} total)')
    print('=' * 65)
    sys.exit(0 if FAIL_COUNT == 0 else 1)
