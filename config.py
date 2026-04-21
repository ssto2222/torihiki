"""
config.py — 全設定の一元管理
ここだけ変更すれば全モジュールに反映される
"""

# ── MT5 接続 ──────────────────────────────────────────────
MT5 = dict(
    symbol    = "BTCUSD",       # GOLD / XAUUSDm など要確認
    h1_bars   = 5000,
    m1_bars   = 100_000,
    magic     = 20240101,
    deviation = 10,             # 最大スリッページ(pt)
)

# ── 指標 ──────────────────────────────────────────────────
INDICATOR = dict(
    rsi_period   = 14,
    atr_period   = 14,
    bb_period    = 20,
    bb_sigma     = 3.0,
    sma_m1       = 20,
    swing_period = 20,
    ema_fast     = 21,
    ema_slow     = 50,
    atr_ma_bars  = 50,          # ATR_ratio の分母期間
)

# ── H1 シグナル検出 ────────────────────────────────────────
SIGNAL = dict(
    buy_rsi_thr   = 38.0,
    buy_bb_touch  = 0.80,       # BB_pct ≤ -この値 で -3σ タッチ判定
    db_lookback   = 25,
    db_min_int    = 2,
    db_max_int    = 16,
    db_depth_tol  = 5.0,
    db_neck_rise  = 2.0,
    local_order   = 2,
    sell_rsi_thr  = 62.0,
    sell_bb_touch = 0.80,
    dt_lookback   = 25,
    dt_min_int    = 2,
    dt_max_int    = 16,
    dt_depth_tol  = 5.0,
    dt_neck_drop  = 2.0,
)

# ── M1 執行 ────────────────────────────────────────────────
EXECUTION = dict(
    touch_margin    = 0.20,
    m1_rsi_offset   = 20.0,
    signal_valid_m1 = 240,      # シグナルON有効期限（M1本数）
)

# ── SL & イグジット ────────────────────────────────────────
SL = dict(
    spread_usd      = 0.30,
    hold_max_h1     = 48,
    tp_atr_multi    = 3.0,      # 保険TP = ATR × N
    rsi_exit_thr    = 75,       # RSI≥75 でトレーリング起動（最適値）
    trail_multi     = 1.5,
    # ボラ適応型SL 倍率
    sl_multi_low    = 1.0,      # ATR_ratio < 0.8
    sl_multi_normal = 1.5,      # 0.8 〜 1.5
    sl_multi_medium = 2.5,      # 1.5 〜 2.5
    sl_multi_high   = 4.0,      # > 2.5（急落）
    atr_ratio_medium = 1.5,
    atr_ratio_high   = 2.5,
)

# ── 急落検出 ──────────────────────────────────────────────
CRASH = dict(
    atr_multi = 2.5,            # 1本下落 > ATR×N → 急落
    gap_usd   = 8.0,            # Open-Close ギャップ > N USD
    vol_spike = 2.0,            # ATR_ratio > N
)

# ── 最適化 ────────────────────────────────────────────────
OPTIMIZE = dict(
    n_samples  = 600,
    min_trades = 6,
    seed       = 42,
)

# ── ローカル合成データ ─────────────────────────────────────
LOCAL = dict(
    h1_bars_synth = 3600,
    crash_rate    = 0.008,      # 急落発生率（低めに設定）
    output_dir    = "./output",
)

# ── MT5 EA ブリッジ ────────────────────────────────────────
BRIDGE = dict(
    signal_file = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/signal.json",
    status_file ="C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ea_state.json",
    poll_sec    = 5,
    lot_size    = 0.01,         # 1回の取引ロット数（--lot で上書き可）
)

# ── 可視化 ────────────────────────────────────────────────
PLOT = dict(
    font_family = ['MS Gothic', 'Noto Sans JP', 'DejaVu Sans'],
    dpi         = 150,
    dark_bg     = '#0d1117',
    panel_bg    = '#161b22',
    border      = '#21262d',
    text        = '#c9d1d9',
    muted       = '#8b949e',
    green       = '#3fb950',
    red         = '#f85149',
    blue        = '#58a6ff',
    yellow      = '#e3b341',
    orange      = '#f0883e',
    purple      = '#d2a8ff',
)
