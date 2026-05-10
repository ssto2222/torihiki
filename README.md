# torihiki 自動売買システム

## 概要

MT5 と Python を組み合わせた自動売買システムです。
H1/M5 の RSI・ATR・ADX を使ったシグナル生成、MT5 EA との連携、時間帯バイアス回避、スキャルプモードなどを備えています。

**主な機能**
- H1 RSI クロス戦略（DIP / モメンタム）+ M5/M15/H1 MTF フィルタ
- スキャルプモード: M5 RSI クロス → SMA20 タッチ → M1 確認バー 2 本でエントリー
- マルチタイムフレーム SMA20 傾き + H1 トレンド方向の一致確認
- レジーム判定（ADX）によるロット倍率・分散エントリー制御
- 時間帯バイアス回避: 危険時間帯の自動検出・前後スキップ
- H1 RSI レベル別 TP 倍率（RSI≥70 で早期利確 / RSI<50 でワイド TP）
- RVOL による急騰初期検知とエントリー制御
- 1H BB2σ 近傍 TP 圧縮 + トレーリング

## ディレクトリ構成

```
torihiki/
├── mt5_ea_bridge.py        後方互換シム（bridge/ への委譲）
├── config.py               全設定の一元管理
├── secret.py               Discord Webhook URL（リポジトリ管理外推奨）
├── trading_rules.json      RulesEngine フィルタ設定
│
├── bridge/                 ブリッジ ロジックパッケージ
│   ├── __init__.py         パブリック API まとめ
│   ├── state.py            ポーリング間状態管理（データクラス）
│   │                         SignalState / ScalpState / TimeBiasState / JpyRateCache / Sma20TouchCache
│   ├── io.py               signal.json アトミック書き込み / ea_state.json 読み込み
│   ├── utils.py            ステートレスユーティリティ（ロット計算・レジーム判定など）
│   ├── notify.py           Discord 通知・一時停止フラグ管理
│   ├── time_bias.py        時間帯バイアス分析・ロード
│   ├── sma20.py            SMA20 タッチマージン分析・キャッシュ
│   ├── signal_normal.py    compute_signal (H1 クロス戦略)
│   ├── signal_scalp.py     compute_scalp_signal (M5 スキャルプ)
│   └── runner.py           run_bridge / main（ポーリングループ）
│
├── core/
│   ├── data.py             MT5 データ取得・合成データ生成
│   ├── indicators.py       テクニカル指標（RSI・ATR・ADX・RVOL・BB）
│   ├── plot.py             可視化
│   └── strategy.py         シグナル検出・SL戦略・バックテスト・急騰検知
│
├── mt5/
│   └── XAUUSD_SL_Strategy.mq5   MT5 EA（MQL5）
│
├── mt5_backtest.py         MT5 実データ バックテスト
├── scalp_backtest.py       スキャルプモード バックテスト
├── analyze_time_bias.py    時間帯バイアス分析
├── analyze_sma20_touch.py  SMA20 タッチマージン分析
├── analyze_risk.py         リスク分析・評価
├── local_analysis.py       ローカル分析（MT5不要）
├── monitor_rvol.py         RVOL リアルタイム監視
└── plot_rvol_analysis.py   RVOL 分析プロット
```

## 必要なパッケージ

```bash
pip install pandas numpy matplotlib scipy
pip install MetaTrader5   # Windows のみ
pip install requests      # Discord 通知用
```

## 実行方法

### 1. MT5 EA リアルタイム連携（通常起動）

```bash
# スキャルプモード（推奨）
python mt5_ea_bridge.py --mode scalp

# オプション指定
python mt5_ea_bridge.py --mode scalp --symbol BTCUSD --target 1000 --jpy 150

# 通常モード（H1 クロス戦略）
python mt5_ea_bridge.py --mode normal

# 1回だけ計算して終了（動作確認用）
python mt5_ea_bridge.py --once

# 連続損失カウンタをリセット
python mt5_ea_bridge.py --reset-losses
```

**`--mode scalp` オプション**
- `--target N` : スキャルプ目標利益（円）　例: `--target 1000`
- `--jpy N`    : JPY/USD レート　例: `--jpy 150`

### 2. バックテスト

```bash
# H1 クロス戦略
python mt5_backtest.py
python mt5_backtest.py --symbol BTCUSD --h1 3000 --m1 50000

# スキャルプ戦略
python scalp_backtest.py
```

### 3. 時間帯バイアス分析

```bash
python analyze_time_bias.py
```

危険時間帯を `output/time_bias.json` に出力します。ブリッジ起動時に自動ロードされます。

### 4. SMA20 タッチマージン分析

```bash
python analyze_sma20_touch.py
```

スキャルプモードの SMA20 タッチ判定マージンを分析・キャッシュします。ブリッジ起動時に自動実行されます。

### 5. ローカル分析（MT5不要）

```bash
python local_analysis.py
python local_analysis.py --optimize
```

### 6. RVOL リアルタイム監視

```bash
python monitor_rvol.py --symbol XAUUSD --minutes 120
python monitor_rvol.py --symbol BTCUSD --minutes 60 --interval 10
```

**表示内容:** 価格チャート / RVOL（1.0×=正常 / 1.3×=急騰初期 / 1.5×=高出来高）/ 価格加速度 / 出来高急増フラグ

### 7. RVOL 分析プロット

```bash
python plot_rvol_analysis.py --symbol XAUUSD --hours 24
```

## config.py の主要設定

### MT5（接続設定）

```python
MT5 = dict(
    symbol    = "BTCUSD",
    h1_bars   = 5000,
    m5_bars   = 20_000,
    m1_bars   = 100_000,
    magic     = 20240101,
    deviation = 10,
    login     = 0,      # 0 = ターミナルが既にログイン済み
    password  = "",
    server    = "",
)
```

### BRIDGE（ブリッジ設定）

```python
BRIDGE = dict(
    signal_file      = "C:/.../Common/Files/signal.json",
    status_file      = "C:/.../Common/Files/ea_state.json",
    poll_sec         = 5,
    lot_size         = 0.05,       # フォールバックロット
    risk_pct         = 0.03,       # 1トレードあたりリスク（残高比）
    fallback_balance = 15_000,
    log_dir          = r"G:\マイドライブ\mt5_log",  # ログ出力先（空文字で無効）
)
```

`log_dir` を設定すると:
- `signal_SYMBOL.json` が `log_dir` にコピーされます
- エラーログが `log_dir/bridge_SYMBOL.log` に記録されます（5MB × 5世代ローテーション）

### SCALP（スキャルプ設定）

```python
SCALP = dict(
    jpy_per_usd       = 150.0,
    target_profit_jpy = 1000,     # 1トレードあたり目標利益（円）
    sl_ratio          = 3,        # SL幅 = TP幅 × sl_ratio
    tp_atr_fraction   = 0.5,      # TP幅 = M5 ATR × tp_atr_fraction
    signal_tf         = 'M5',
    rsi_buy_thrs      = [55.0, 60.0, 65.0],
    rsi_sell_thrs     = [45.0, 40.0, 35.0],
    max_trades_day    = 20,
    cooldown_min      = 15,
    big_move_lookback  = 12,      # 大変動判定: 過去 N 本
    big_move_atr_multi = 5.0,     # 大変動判定: ATR × N 以上の変動
    sma20_slope_bars   = 5,       # MTF SMA20 傾き計算バー数
    sma20_slope_atr_thr = 0.10,   # MTF SMA20 傾き閾値（ATR比）
)
```

**スキャルプエントリーフロー**

```
M5 RSI クロス（閾値超え）
  ↓  MTF 条件チェック: M1/M5/M15 SMA20 傾き + H1 トレンド方向
SMA20 タッチ待ち（30分タイムアウト）
  ↓  傾き確認: SMA20 が ATR×0.10 以上の方向性
M1 確認バー 2 本待ち（上昇/下落）（30分タイムアウト）
  ↓
エントリー
```

### REGIME（レジーム設定）

```python
REGIME = dict(
    trend_thr            = 25.0,   # ADX ≥ 25 → トレンド
    range_thr            = 20.0,   # ADX < 20 → レンジ
    lot_multi_trend      = 1.5,    # H1・M5 両方トレンド時
    lot_multi_weak       = 1.0,    # 片方のみトレンド時
    lot_multi_range      = 0.6,    # 両方レンジ時
    max_entry_per_signal = 3,
    entry_spacing_atr    = 0.5,
    scalp_reserve_slots  = 1,
)
```

### TIME_BIAS（時間帯バイアス設定）

```python
TIME_BIAS = dict(
    enabled              = True,
    danger_win_rate_thr  = 0.40,   # win_rate < 40% → 危険時間帯
    danger_avg_pnl       = 0.0,    # avg_pnl <= 0 → 危険時間帯（OR条件）
    min_trades_per_hour  = 5,
    skip_before_min      = 30,     # 危険時間帯 30分前からスキップ
    skip_after_min       = 0,      # 危険時間帯終了後スキップ延長
    rebias_interval_hours = 24,    # 自動再分析間隔（時間）
    bias_file            = "./output/time_bias.json",
)
```

## ルール設定

`config.py` のトレードルール分類:

| 設定グループ | 主な設定 | 説明 |
|---|---|---|
| `RULES_GENERAL` | `total_risk_pct` | 全体最大リスク。`total_risk_pct / risk_pct` が最大ポジション数 |
| `RULES_ENTRY` | `min_score` | RulesEngine スコア閾値。未満はエントリースキップ |
| `RULES_RISK` | `max_consecutive_losses` | 連続損失上限。超えたらその日の取引停止 |
| `RULES_EXIT` | `min_hold_minutes` | 最低保有時間 |

## MT5 EA 側設定

1. `mt5/XAUUSD_SL_Strategy.mq5` を `MQL5/Experts/` に配置してコンパイル
2. チャートにアタッチ
3. `InpSignalFile` を `config.py` の `BRIDGE['signal_file']` に合わせる
4. EA が `signal_SYMBOL.json` を読み込み、新規エントリー・SL/TP を自動更新

**通信プロトコル**
- Python → MT5 EA : `signal_SYMBOL.json`（毎ポーリング更新）
- MT5 EA → Python : `ea_state_SYMBOL.json`（EA が書き込む）

## アーキテクチャ概要

```
mt5_ea_bridge.py（シム）
    ↓ 委譲
bridge/
  runner.py ──→ signal_normal.py ──→ compute_signal()
             └→ signal_scalp.py  ──→ compute_scalp_signal()
                                          ↓
                                    core/data.py（MT5 データ取得）
                                    core/indicators.py（指標計算）
                                    core/strategy.py（シグナル判定）
```

**状態管理（bridge/state.py）**

ポーリング間の状態はすべてデータクラスで管理されます:

| クラス | 役割 |
|---|---|
| `SignalState` | compute_signal のクロス検出・分散エントリー・BB2σタッチ状態 |
| `ScalpState` | compute_scalp_signal の RSI クロス・SMA20 タッチ・確認バー状態 |
| `TimeBiasState` | 時間帯バイアス判定・クールダウン管理 |
| `JpyRateCache` | USDJPY レート 1 時間キャッシュ |
| `Sma20TouchCache` | シンボル別 SMA20 タッチマージンキャッシュ |

## 注意

- `secret.py` には `DISCORD_WEBHOOK_URL` を記述します（Git 管理外推奨）
- `trading_rules.json` がある場合、`RulesEngine` を読み込んでフィルタを適用します
- MetaTrader5 パッケージは Windows 専用です。Linux/macOS では MT5 接続機能は利用できません
- `output/` フォルダは分析出力用。MT5 EA ブリッジの signal ファイルは `BRIDGE['signal_file']` で別途指定します
