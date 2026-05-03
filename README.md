# torihiki 自動売買システム

## 概要

このリポジトリは、MT5 と Python を組み合わせた自動売買システムです。
H1/M5 の RSI・ATR・ADX を使ったシグナル生成、MT5 EA との連携、時間帯バイアス回避、スキャルプモードなどを備えています。

**新機能 (2026-05): 急騰初期検知システム**
- RVOL（相対出来高）を使った急騰初期検知
- 急騰中段階でのエントリー回避
- 急騰時のリスク調整ロットサイズ制御
- 急騰初期SELL / 押し目BUY の戦略最適化

## ディレクトリ構成

```
 torihiki/
 ├── analyze_risk.py      リスク分析・評価
 ├── analyze_time_bias.py 時間帯バイアス分析
 ├── config.py            全設定の一元管理
 ├── examples.py          実行例・テスト用コード
 ├── local_analysis.py    ローカル分析（MT5不要）
 ├── mt5_backtest.py      MT5 実データ バックテスト
 ├── mt5_ea_bridge.py     MT5 EA リアルタイム連携ブリッジ
 ├── secret.py            機密情報・認証用（未管理推奨）
 ├── trading_rules.json   ルールエンジン用フィルタ
 ├── output/              分析・バックテスト出力
 ├── core/
 │   ├── data.py          データ取得・合成データ生成
 │   ├── indicators.py    テクニカル指標・急落検出・RVOL計算
 │   ├── plot.py          可視化
 │   └── strategy.py      シグナル検出・SL戦略・バックテスト・急騰検知
 └── mt5/
     └── XAUUSD_SL_Strategy.mq5  MT5 EA（MQL5）
```

## 必要なパッケージ

```bash
pip install pandas numpy matplotlib scipy
pip install MetaTrader5
```

## 実行方法

### 1. ローカル分析（MT5不要）

```bash
python local_analysis.py
python local_analysis.py --optimize
python local_analysis.py --output ./result
```

### 2. 時間帯バイアス分析

```bash
python analyze_time_bias.py
```

### 3. リスク分析

```bash
python analyze_risk.py
```

### 4. MT5 実データ バックテスト

```bash
python mt5_backtest.py
python mt5_backtest.py --symbol BTCUSD --h1 3000 --m1 50000
```

### 5. MT5 EA リアルタイム連携

1. `config.py` の `MT5['symbol']` を使用するシンボルに合わせる
2. MT5 EA の `InpSignalFile` を `signal.json` の書き込み先と一致させる
3. Python ブリッジを起動

```bash
python mt5_ea_bridge.py
python mt5_ea_bridge.py --once
python mt5_ea_bridge.py --mode scalp --symbol BTCUSD --lot 0.05 --target 1000 --jpy 150
```

**急騰初期検知機能:**
- RVOL（相対出来高）1.3倍以上 + 価格加速0.5%以上で急騰初期と判定
- 急騰初期ではSELLエントリーを優先（信頼度60%以上）
- 急騰中段階（RSI>70）ではエントリー回避
- 急騰時はロットサイズを最大50%削減してリスク制御

`--mode` の選択:
- `normal`: H1/M5 クロス戦略
- `scalp` : M5 RSI 閾値クロス / 円建て TP でエントリー

`--target` はスキャルプモードの目標利益（円）、`--jpy` は JPY/USD レートです。

## config.py の主要設定

### MT5

```python
MT5 = dict(
    symbol = "BTCUSD",
    h1_bars = 5000,
    m5_bars = 20000,
    m1_bars = 100000,
    magic = 20240101,
    deviation = 10,
)
```

### BRIDGE

```python
BRIDGE = dict(
    signal_file      = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/signal.json",
    status_file      = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ea_state.json",
    poll_sec         = 5,
    lot_size         = 0.05,
    risk_pct         = 0.03,
    fallback_balance = 15_000,
    scalp_lot_multi  = 2.0,
)
```

### SCALP

```python
SCALP = dict(
    jpy_per_usd       = 150.0,
    target_profit_jpy = 1000,
    sl_ratio          = 1.5,
    tp_atr_fraction   = 0.5,
    signal_tf         = 'M5',
    rsi_buy_thrs      = [55.0, 60.0, 65.0],
    rsi_sell_thrs     = [45.0, 40.0, 35.0],
    max_trades_day    = 20,
    cooldown_min      = 15,
    big_move_lookback = 12,
    big_move_atr_multi= 2.0,
)
```

### REGIME

```python
REGIME = dict(
    trend_thr            = 25.0,
    range_thr            = 20.0,
    lot_multi_trend      = 1.5,
    lot_multi_weak       = 1.0,
    lot_multi_range      = 0.6,
    max_entry_per_signal = 3,
    entry_spacing_atr    = 0.5,
    scalp_reserve_slots  = 1,
)
```

### TIME_BIAS

```python
TIME_BIAS = dict(
    enabled              = True,
    danger_win_rate_thr  = 0.40,    # win_rate < 40% → 危険時間帯
    danger_avg_pnl       = 0.0,     # avg_pnl <= 0 → 危険時間帯（OR条件）
    min_trades_per_hour  = 5,       # サンプル不足の時間帯は判定スキップ
    skip_before_min      = 30,      # 危険時間帯の 30分前からスキップ開始
    skip_after_min       = 15,      # 危険時間帯終了後 15分までスキップ継続
    rebias_interval_hours= 24,      # 時間帯分析の再実行間隔（時間）
    bias_file            = "./output/time_bias.json",
)
```

**スキップ対象時間帯**
- 危険時間帯の開始時刻から30分前 ～ 危険時間帯終了後15分
- 例：危険時間帯が 13:00 UTC なら 12:30～14:15 がスキップ対象
- スキップ対象時間帯中は新規エントリー禁止

## 主要機能

- H1/M5 の RSI・ATR・ADX を使ったシグナル生成
- `trading_rules.json` によるルールエンジンフィルタ
- MT5 EA との `signal.json` / `ea_state.json` 双方向連携
- スキャルプモード: M5 RSI 閾値クロス + 1本待機ロジック
- 時間帯バイアス回避: 危険時間帯で新規エントリー停止 / 事前決済
- レジーム判定による分散エントリー・スキャルプ枠確保

## MT5 EA 側設定

- EA を `MQL5/Experts/` に配置しコンパイル
- チャートにアタッチ
- `InpSignalFile` を `config.py` の `BRIDGE['signal_file']` に合わせる
- EA は `signal.json` を読み込み、新規エントリー・SL/TP を更新します

## 注意

- 現在 `signal.json` の出力先は `config.py` の `BRIDGE['signal_file']` で指定されています。
- `output/` フォルダは主に分析出力用で、MT5 EA ブリッジのワークディレクトリとは別です。
- `trading_rules.json` がある場合、`RulesEngine` を読み込んでフィルタを適用します。
