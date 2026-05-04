"""
config.py — 全設定の一元管理
ここだけ変更すれば全モジュールに反映される
"""

# ── MT5 接続 ──────────────────────────────────────────────
MT5 = dict(
    symbol    = "BTCUSD",       # GOLD / XAUUSDm など要確認
    h1_bars   = 5000,
    m5_bars   = 20_000,         # M5 5分足本数（約70日分）
    m1_bars   = 100_000,
    magic     = 20240101,
    deviation = 10,             # 最大スリッページ(pt)
    # ── ログイン認証（-6 Authorization failed 対策）──────────
    # MT5 ターミナルが未ログイン状態の場合はここに認証情報を設定。
    # ターミナルが既にログイン済みなら空欄のままでOK。
    login     = 0,              # 口座番号（int）。0 = 認証情報不使用
    password  = "",             # パスワード
    server    = "",             # ブローカーサーバー名（例: "ICMarketsSC-Demo"）
)

# ── 指標 ──────────────────────────────────────────────────
INDICATOR = dict(
    rsi_period   = 14,
    atr_period   = 14,
    adx_period   = 14,          # ADX / +DI / -DI 期間
    bb_period    = 20,
    bb_sigma     = 3.0,
    sma_m1       = 20,
    swing_period = 20,
    ema_fast     = 21,
    ema_slow     = 50,
    atr_ma_bars  = 50,          # ATR_ratio の分母期間
    # ── 出来高・急騰検知関連 ──────────────────────────────
    rvol_period               = 20,     # RVOL計算期間
    accel_period              = 5,      # 価格加速計算期間
    volume_surge_threshold    = 2.0,    # 出来高急増閾値（倍率）
    rvol_surge_threshold      = 1.5,    # RVOL急増閾値
    early_surge_rvol_threshold = 1.3,   # 急騰初期RVOL閾値
    early_surge_accel_threshold = 0.5,  # 急騰初期価格加速閾値
    surge_overbought_threshold = 70.0,  # 急騰中段階RSI閾値
    surge_avoid_accel_threshold = 1.5,  # 急騰回避価格加速閾値
)

# ── H1 シグナル検出（SMA20 + RSI）────────────────────────────
SIGNAL = dict(
    buy_rsi_thr        = 30.0,    # RSI がこの値を下抜け → ディップ買い  ★最適化済み(旧40)
    sell_rsi_thr       = 62.0,    # (旧: 上抜け売り禁止) 現在未使用
    momentum_thrs      = [55.0, 60.0, 65.0, 70.0, 75.0],  # RSI 上抜け → モメンタム買い
    momentum_sell_thrs = [55.0, 50.0, 45.0, 40.0, 35.0],  # RSI 下抜け → 下落トレンド売り
    downtrend_d1_rsi   = 45.0,    # D1 RSI < この値 かつ close < SMA20 → 下落トレンド判定
)

# ── M1 執行 ────────────────────────────────────────────────
EXECUTION = dict(
    touch_margin             = 0.20,    # SMA20 タッチ判定マージン（フォールバック）
    m1_rsi_offset            = 20.0,
    signal_valid_m1          = 240,     # シグナルON有効期限（M1本数）
    m1_exec_buy_thrs         = [65.0, 70.0, 75.0],   # BUY 執行: M1 RSI がいずれかを 2本以上上抜け
    m1_exec_sell_thrs        = [35.0, 30.0, 25.0],   # SELL 執行: M1 RSI がいずれかを 2本以上下抜け
    sma20_touch_pct          = 70,      # 過去シグナルの何%をキャッチする touch_margin にするか
    sma20_touch_margin_file  = "./output/sma20_touch_margins.json",  # キャッシュファイル
)

# ── SL & イグジット ────────────────────────────────────────
SL = dict(
    spread_usd   = 0.30,
    sl_multi     = 1.5,         # SL = Entry ± ATR × sl_multi  ★BT最適(WR52%,SL刈55%,Sharpe26.4)
    tp_atr_multi = 3.0,         # TP  = Entry ± ATR × tp_atr_multi  (R:R=2.0)
    hold_max_h1  = 48,
    rsi_exit_thr = 65,          # RSI≥65 でトレーリング起動
    trail_multi  = 2.0,         # トレーリング幅 = ATR × trail_multi (遅らせるため2.5→2.0)
)

# ── トレードルール（trading_rules.json から導出）────────────────
RULES = dict(
    min_score                = 30,    # RulesEngine スコア閾値（以下はエントリースキップ）
    max_consecutive_losses   = 3,     # 連続損失この回数でその日の取引停止
    cooldown_large_loss_min  = 1440,  # 大損失後のクールダウン（分）= 翌日まで
    large_loss_threshold_usd = -10000,
    min_hold_minutes         = 15,
    total_risk_pct           = 0.30,  # 全体許容損失（残高比）; max_positions = total/risk_pct = 10
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
    signal_file      = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/signal.json",
    status_file      = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ea_state.json",
    poll_sec         = 5,
    lot_size         = 0.05,         # フォールバックロット（残高取得失敗時）
    risk_pct         = 0.03,         # 1トレードあたりリスク = 残高 × 3%  ★Quarter-Kelly(7.1%)の保守側上限
    fallback_balance = 15_000,    # MT5残高取得失敗時のデフォルト残高（円）
    scalp_lot_multi  = 1.0,          # スキャルプモードのロット倍率
)

# ── スキャルプモード（--mode scalp で有効）────────────────────
SCALP = dict(
    jpy_per_usd       = 150.0,   # JPY/USD レート（定期的に手動更新）
    target_profit_jpy = 1000,     # 1トレードあたり目標利益（円）
    sl_ratio          = 3,     # SL幅 = TP幅 × sl_ratio  → 損失 = 目標 × 1.5
    tp_atr_fraction   = 0.5,     # TP幅 = M5 ATR × tp_atr_fraction（スプレッド以上を確保）
    signal_tf         = 'M5',    # シグナル生成足（M5クロス→M1で執行確認）
    rsi_buy_thrs      = [55.0, 60.0, 65.0],   # RSI 上抜け → BUY
    rsi_sell_thrs     = [45.0, 40.0, 35.0],   # RSI 下抜け → SELL
    buy_enabled       = True,     # scalp buy を有効化 / 無効化
    sell_enabled      = False,     # scalp sell を有効化 / 無効化
    max_trades_day    = 20,      # 1日の最大エントリー回数
    cooldown_min      = 15,      # 前回エントリーからのクールダウン（分）
    big_move_lookback  = 12,     # 大変動判定: 過去 N 本（12本=60分）
    big_move_atr_multi = 5.0,   # 大変動判定: 価格変動 > ATR × N で切換え
    m1_early_margin    = 2.0,   # M5 RSI が閾値からこの値以内に接近 + M1 先行クロスで早期執行
    lot_max            = {'XAUUSD': 0.05},  # シンボル別ロット上限。未設定 = 上限なし
    sma20_slope_bars   = 5,     # SELL SMA20タッチ判定: 傾き計算に使う M1 バー数
    sma20_slope_atr_thr = 0.10, # SELL SMA20タッチ判定: SMA20がATR×この値以上下落していること
)

# ── レジーム判定・分散エントリー ──────────────────────────────────
REGIME = dict(
    # ADX によるトレンド/レンジ判定（H1・M5 それぞれ独立評価）
    trend_thr            = 25.0,   # ADX ≥ この値 → トレンド
    range_thr            = 20.0,   # ADX < この値 → レンジ
    # レジーム別ロット倍率（_calc_lot の結果に乗算）
    lot_multi_trend      = 1.5,    # H1・M5 両方トレンド時
    lot_multi_weak       = 1.0,    # 片方のみトレンド時
    lot_multi_range      = 0.6,    # 両方レンジ時
    # 分散エントリー（レンジ・弱トレンド時）
    max_entry_per_signal = 3,      # 1シグナルウィンドウ内の最大エントリー回数
    entry_spacing_atr    = 0.5,    # 追加エントリーの最小間隔（ATR × この値 の押し目/戻り）
    # トレンド一括エントリー
    scalp_reserve_slots  = 1,      # スキャルプ用に残す空きスロット数（トレンド一括時も確保）
)

# ── 時間帯バイアス回避 ─────────────────────────────────────────
TIME_BIAS = dict(
    enabled              = True,
    danger_win_rate_thr  = 0.40,    # win_rate がこの値未満 → 危険
    danger_avg_pnl       = 0.0,     # avg_pnl がこの値以下 → 危険（OR条件）
    min_trades_per_hour  = 5,       # サンプル不足の時間帯は判定スキップ
    skip_before_min      = 30,      # 危険時間帯の N 分前からスキップ開始
    skip_after_min       = 0,      # 危険時間帯終了後 N 分までスキップ継続
    rebias_interval_hours = 24,     # この間隔（時間）で自動再分析。0 = 起動時1回のみ
    bias_file             = "./output/time_bias.json",
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
