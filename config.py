"""
config.py — 全設定の一元管理
ここだけ変更すれば全モジュールに反映される
"""

# ── MT5 接続 ──────────────────────────────────────────────
MT5 = dict(
    symbol    = "XAUUSD",       # GOLD / XAUUSDm など要確認
    h1_bars   = 5000,
    m5_bars   = 20_000,         # M5 5分足本数（約70日分）
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

# ── H1 シグナル検出（SMA20 + RSI）────────────────────────────
SIGNAL = dict(
    buy_rsi_thr  = 38.0,   # RSI がこの値を下抜け かつ Close > SMA20 → 買い
    sell_rsi_thr = 62.0,   # RSI がこの値を上抜け かつ Close < SMA20 → 売り
)

# ── M1 執行 ────────────────────────────────────────────────
EXECUTION = dict(
    touch_margin    = 0.20,
    m1_rsi_offset   = 20.0,
    signal_valid_m1 = 240,      # シグナルON有効期限（M1本数）
)

# ── SL & イグジット ────────────────────────────────────────
SL = dict(
    spread_usd   = 0.30,
    sl_multi     = 1.5,         # SL = Entry ± ATR × sl_multi
    tp_atr_multi = 3.0,         # TP  = Entry ± ATR × tp_atr_multi
    hold_max_h1  = 48,
    rsi_exit_thr = 75,          # RSI≥75 でトレーリング起動
    trail_multi  = 1.5,         # トレーリング幅 = ATR × trail_multi
)

# ── トレードルール（trading_rules.json から導出）────────────────
RULES = dict(
    min_score                = 30,    # RulesEngine スコア閾値（以下はエントリースキップ）
    max_consecutive_losses   = 3,     # 連続損失この回数でその日の取引停止
    cooldown_large_loss_min  = 1440,  # 大損失後のクールダウン（分）= 翌日まで
    large_loss_threshold_usd = -10000,
    min_hold_minutes         = 15,
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
    output_dir    = "./output",
)

# ── MT5 EA ブリッジ ────────────────────────────────────────
BRIDGE = dict(
    signal_file = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/signal.json",
    status_file ="C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ea_state.json",
    poll_sec    = 5,
    lot_size    = 0.05,         # 1回の取引ロット数（推奨: 0.05〜0.10）（--lot で上書き可）
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
