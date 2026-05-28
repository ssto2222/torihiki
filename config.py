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
    bb_tp_sigma              = 2.0,    # H1 BB2σ を TP 初期位置に使う
    bb_tp_near_pct           = 0.85,   # H1 BB2σ 近傍判定（%）
)

# ── H1 シグナル検出（SMA20 + RSI）────────────────────────────
SIGNAL = dict(
    buy_rsi_thr        = 30.0,    # RSI がこの値を下抜け → ディップ買い  ★最適化済み(旧40)
    sell_rsi_thr       = 62.0,    # (旧: 上抜け売り禁止) 現在未使用
    momentum_thrs      = [55.0, 60.0, 65.0, 70.0, 75.0],  # RSI 上抜け → モメンタム買い
    momentum_sell_thrs = [55.0, 50.0, 45.0, 40.0, 35.0],  # RSI 下抜け → 下落トレンド売り
    momentum_buy_max_rsi = 70.0,  # モメンタムBUY時、H1 RSIがこの値以上なら高値掴み防止で禁止
    downtrend_d1_rsi   = 45.0,    # D1 RSI < この値 かつ close < SMA20 → 下落トレンド判定
)

# ── M1 執行 ────────────────────────────────────────────────
EXECUTION = dict(
    touch_margin             = 0.20,    # SMA20 タッチ判定マージン（フォールバック）
    m1_rsi_offset            = 20.0,
    signal_valid_m1          = 240,     # シグナルON有効期限（M1本数）
    m1_exec_buy_thrs         = [57.0, 60.0, 65.0],   # BUY 執行: M1 RSI がいずれかを 2本以上上抜け
    m1_exec_sell_thrs        = [40.0, 35.0, 30.0, 25.0],   # SELL 執行: M1 RSI がいずれかを 2本以上下抜け
    sma20_touch_pct          = 70,      # 過去シグナルの何%をキャッチする touch_margin にするか
    sma20_touch_margin_file  = "./output/sma20_touch_margins.json",  # キャッシュファイル
    split_entry_frac         = 0.5,    # 初回エントリーのロット比率（残りをリミット押し目に充当）
    split_limit_pullback     = 0.4,    # 押し目リミット距離: close ± ATR × この値
)

# ── SL & イグジット ────────────────────────────────────────
SL = dict(
    spread_usd   = 0.30,
    sl_multi     = 1.5,         # SL = Entry ± ATR × sl_multi  ★BT最適(WR52%,SL刈55%,Sharpe26.4)
    tp_atr_multi = {'BTCUSD': 3.0, 'XAUUSD': 4.5},  # TP  = Entry ± ATR × tp_atr_multi  (R:R=2.0)
    tp_atr_multi_above_d1_sma200 = {'BTCUSD': 3.0, 'XAUUSD': 4.5},  # D1 SMA200 上での TP 倍率
    tp_atr_multi_below_d1_sma200 = {'BTCUSD': 2.5, 'XAUUSD': 3.5},  # D1 SMA200 下での TP 倍率
    tp_atr_multi_rsi_high = {'BTCUSD': 1.5, 'XAUUSD': 2.0},  # H1 RSI >= 70 での TP 倍率（強いRSI領域で早期利確）
    tp_atr_multi_rsi_mid  = {'BTCUSD': 2.5, 'XAUUSD': 3.5},  # H1 50 <= RSI < 70 での TP 倍率
    tp_atr_multi_rsi_low  = {'BTCUSD': 3.0, 'XAUUSD': 4.5},  # H1 RSI < 50 での TP 倍率
    hold_max_h1  = 48,
    rsi_exit_thr = 65,          # RSI≥65 でトレーリング起動
    trail_multi  = 2.0,         # トレーリング幅 = ATR × trail_multi (遅らせるため2.5→2.0)
)

# ── ルール分類: general / entry / risk / exit ────────────────────────────
RULES_GENERAL = dict(
    total_risk_pct           = 0.30,  # 全体許容損失（残高比）; max_positions = total_risk_pct / BRIDGE.risk_pct
)

RULES_ENTRY = dict(
    min_score                = 30,    # RulesEngine スコア閾値（以下はエントリースキップ）
)

RULES_RISK = dict(
    max_consecutive_losses   = 5,     # 連続損失この回数でその日の取引停止
    cooldown_large_loss_min  = 1440,  # 大損失後のクールダウン（分）= 翌日まで
    large_loss_threshold_usd = -10000,
)

RULES_EXIT = dict(
    min_hold_minutes         = 15,    # 最低保有時間
)

RULES = dict(
    **RULES_GENERAL,
    **RULES_ENTRY,
    **RULES_RISK,
    **RULES_EXIT,
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
    log_dir          = r"G:\マイドライブ\mt5_log",   # signal_SYMBOL.json のコピー先ディレクトリ（空文字で無効）
    dashboard_mode   = True,         # True: ターミナルで画面クリア + 上書き live 表示 (isatty 時のみ有効)
)

# ── スキャルプモード（--mode scalp で有効）────────────────────
SCALP = dict(
    jpy_per_usd       = 150.0,   # JPY/USD レート（定期的に手動更新）
    target_profit_jpy = 1000,     # 1トレードあたり目標利益（円）
    sl_ratio          = 3,     # SL幅 = TP幅 × sl_ratio  → 損失 = 目標 × 1.5
    tp_atr_fraction   = {'BTCUSD': 0.5, 'XAUUSD': 0.35},  # TP幅 = M5 ATR × tp_atr_fraction（スプレッド以上を確保）
    signal_tf         = 'M5',    # シグナル生成足（M5クロス→M1で執行確認）
    rsi_buy_thrs      = [50.0, 55.0, 60.0, 65.0],   # RSI 上抜け → BUY
    rsi_sell_thrs     = [50.0, 45.0, 40.0, 35.0],   # RSI 下抜け → SELL
    buy_enabled       = True,     # scalp buy を有効化 / 無効化
    sell_enabled      = True,     # scalp sell を有効化 / 無効化
    max_trades_day    = 20,      # 1日の最大エントリー回数
    cooldown_min      = 15,      # クールダウン時間（分）
    cooldown_trades   = 3,       # この回数トレードするごとにクールダウンを発動
    min_margin_level  = 200.0,   # 証拠金維持率の下限（%）。これを下回らないようlotを縮小
    big_move_lookback  = 12,     # 大変動判定: 過去 N 本（12本=60分）
    big_move_atr_multi = 2.0,   # 大変動判定: 価格変動 > ATR × N で切換え
    m1_early_margin    = 2.0,   # M5 RSI が閾値からこの値以内に接近 + M1 先行クロスで早期執行
    m1_confirm_half_bar = True, # True=半足モード: close>open で確認（バー途中でエントリー）
                                # False=通常モード: close>prev_close で確認（バー確定後）
    lot_max            = {'XAUUSD': 0.05, 'BTCUSD': 0.10},  # シンボル別ロット上限。未設定 = 上限なし
    sma20_slope_bars   = 5,     # SELL SMA20タッチ判定: 傾き計算に使う M1 バー数
    sma20_slope_atr_thr = 0.10, # SELL SMA20タッチ判定: SMA20がATR×この値以上下落していること
    sma20_accel_bars   = 4,     # SMA20 2階微分: 傾きサンプル数（n本の傾きでトレンド判定）
    sma20_accel_tol    = 0.3,   # SMA20 2階微分: 減少率閾値（0.3=ウィンドウ内30%超縮小で禁止）
    # 急落・急騰時 SMA20 バイパス（大きく乖離している場合はタッチ不要）
    sell_sma_bypass_atr  = 1.2,  # SELL: 価格 < SMA20 - ATR×この値 → SMA20タッチスキップ
    buy_sma_bypass_atr   = 1.2,  # BUY:  価格 > SMA20 + ATR×この値 → SMA20タッチスキップ
    # SMA20 タッチマージン（キャッシュ未計算時の ATR ベースフォールバック）
    sma20_touch_margin_atr = 0.4, # タッチマージン = M5_ATR × この値（BTCUSD: ATR1000→$150）
    # MTF H1 フィルター緩和
    h1_di_filter          = False, # True=H1 DI+/DI-方向必須(厳格) / False=H1レジームのみ(推奨)
    # 極端RSI後の反発・反落シグナル
    # RSI ハードゲート: 全BUY/SELLシグナルに適用する絶対下限/上限
    rsi_buy_gate_min      = 40.0,  # BUY: M5 RSIがこの値未満なら全シグナルをブロック
    rsi_sell_gate_max     = 60.0,  # SELL: M5 RSIがこの値超なら全シグナルをブロック
    # M1 RSI 極端値ゲート: 過熱/売られすぎ時の無差別エントリー禁止（EW2免除）
    m1_rsi_ob_gate        = 70.0,  # M1 RSI ≥ この値: 過熱、全方向エントリー禁止
    m1_rsi_os_gate        = 30.0,  # M1 RSI ≤ この値: 売られすぎ、全方向エントリー禁止
    # ── ボリュームブレイクアウト（大変動予兆を検知しSMA20タッチをスキップ） ──────────
    vol_bo_enabled        = True,  # ボリュームブレイクアウト有効/無効
    vol_bo_rvol_thr       = 2.0,   # RVOL ≥ この値でブレイクアウト候補
    vol_bo_body_ratio_min = 0.45,  # ローソク実体/レンジ比率の最小値（騙しフィルター）
    vol_bo_atr_move_min   = 0.3,   # 同バー内価格変動 ≥ ATR × この値（動き確認）
    vol_bo_rsi_buy_min    = 52.0,  # ブレイクアウトBUY: RSI ≥ この値（上昇方向確認）
    vol_bo_rsi_sell_max   = 48.0,  # ブレイクアウトSELL: RSI ≤ この値（下落方向確認）
    vol_bo_tp_multi       = 1.8,   # TP倍率（通常スキャルプTP × この値 → 大きな波に乗る）
    vol_bo_sl_multi       = 0.8,   # SL倍率（通常SL × この値 → エントリー根拠明確ならタイトに）
    # ── SMA 優先エントリー + RSI スケールイン ────────────────────────────
    sma_watch_cooldown_s  = 60,    # 直前エントリーからこの秒数以内は sma_pending 自動再武装しない
    sma_entry_lot_frac    = 1.0,   # SMA優先エントリーのロット倍率（1.0 = 変更なし）
    sma_pending_timeout_min = 10,  # sma_pending / confirm_pending のタイムアウト（分）
    sma_departure_atr     = 1.5,   # pending中に価格がSMA20からこのATR以上乖離したら即エントリー
    rsi_scalein_enabled   = True,  # RSIクロスによるスケールイン有効化
    rsi_scalein_lot_frac  = 0.5,   # スケールインエントリーのロット倍率
    rsi_scalein_max       = 2,     # スケールイン最大回数（per primary entry）
    rsi_scalein_window_min = 30,   # 最終エントリーからこの分数以内のみスケールイン許可
    # ── 通常モード → スキャルプ自動復帰 ─────────────────────────────
    scalp_auto_restore      = True, # True: 通常モード中に大変動が解消したらスキャルプに自動復帰
    scalp_restore_calm_bars = 5,    # N ポール連続で big_move='none' になったら復帰
    # ── ネックライン接近でノーマルモード切替 ──────────────────────────
    neckline_approach_enabled = True,  # True: H1ネックライン付近でノーマルモードに切替
    neckline_approach_atr     = 1.5,   # 接近判定マージン = M5_ATR × この値
    # ── SMA タッチ + H1 ネックライン方向一致時の TP 拡張 ─────────────────
    neckline_tp_extend_enabled = True, # True: SMAタッチがH1パターン方向一致時にTPをtargetへ延長
    neckline_tp_extend_atr     = 3.0,  # 「ネックライン近辺」判定マージン = M5_ATR × この値
    # ── ノーマルバリアント（大変動/ネックライン接近時の拡張パラメーター）──────────
    normal_variant_enabled    = False, # False: NV遷移を無効化（常にスキャルプパラメーターで執行）
    normal_variant_tp_atr     = 1.5,   # NV TP幅 = M5_ATR × この値（スキャルプより広い）
    normal_variant_lot_frac   = 1.0,   # NV ロット倍率（1.0 = 目標利益ベース計算をそのまま使用）
)

# ── エリオット波動 Wave2 エントリー ───────────────────────────────
ELLIOTT = dict(
    enabled          = True,
    lookback_bars    = 100,   # スイング探索バー数（M5足: 100本=500分≈8時間）
    sw_window        = 3,     # スイングポイント確定ウィンドウ（両側 N 本）
    fib_min          = 0.382, # Fibonacci 押し戻し下限
    fib_max          = 0.786, # Fibonacci 押し戻し上限
    min_wave1_atr    = 1.5,   # Wave1 の最小サイズ（ATR 倍）
    rsi_div_min      = 3.0,   # 強気/弱気ダイバージェンス最小差分
    w2_buy_rsi_max   = 50.0,  # BUY: 第2底の RSI 上限（中立圏まで許容）
    w2_sell_rsi_min  = 50.0,  # SELL: 第2天井の RSI 下限（中立圏まで許容）
    w2_bars_ago_max  = 8,     # 第2底/天井が直近 N 本以内であること
    fib_tp_ext       = 1.618, # TP = W2底 + Wave1_size × fib_tp_ext（Wave3 黄金比目標）
    sl_buffer_atr    = 0.3,   # SL = W2底 - ATR × sl_buffer_atr（波動失効ライン）
)

# ── ウィップソー（行ってこい相場）対策 ────────────────────────────
WHIPSAW = dict(
    ratio_n            = 10,    # ATR集計本数（M5: 50分 / H1: 10時間）直近相場に絞る
    ratio_thr          = 2.5,   # ATR合計÷実効レンジ がこの値以上でウィップソー判定（旧2.0）
    bidir_lookback_h   = 1,     # 双方向損失チェック: 過去N時間の約定を参照（旧2）
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
    skip_before_min      = 15,      # 危険時間帯の N 分前からスキップ開始
    skip_after_min       = 0,      # 危険時間帯終了後 N 分までスキップ継続
    rebias_interval_hours = 24,     # この間隔（時間）で自動再分析。0 = 起動時1回のみ
    bias_file             = "./output/time_bias.json",
    time_ref              = 'exit', # 'exit': 決済時刻基準（ノーマル向き）/ 'entry': エントリー時刻基準
)

# ── マクロバイアス分析（D1/W1/MN1 パターン・RSI・SMA200）────────────
MACRO = dict(
    enabled             = True,      # マクロ分析を有効化
    update_interval_h   = 4,         # 再分析間隔（時間）。0 = 起動時1回のみ
    d1_bars             = 200,       # D1 取得バー数
    w1_bars             = 100,       # W1 取得バー数
    mn1_bars            = 60,        # MN1 取得バー数
    pattern_top_n       = 2,         # TFごとのパターン最大取得数
    min_bias_to_show    = 15,        # ダッシュボード表示閾値（|bias| >= この値で表示）
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
