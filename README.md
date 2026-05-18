# torihiki 自動売買システム

## 概要

MT5 と Python を組み合わせた自動売買システムです。H1/M5 RSI・ATR・ADX によるシグナル生成、MT5 EA との JSON 連携、スキャルプモード、H1 テクニカルパターン認識、Discord によるリモートパラメータ制御を備えています。

### 主な機能

| カテゴリ | 機能 |
|---|---|
| **通常モード** | H1 RSI クロス（DIP / モメンタム）+ M5/M15 MTF フィルタ |
| **スキャルプモード** | M5 RSI クロス → SMA20 タッチ → M1 確認 2 本でエントリー |
| **パターン認識** | Wボトム / ダブルトップ / 三尊 / 逆三尊をH1で検知しネックライン突破でエントリー |
| **MTF フィルタ** | H1 レジーム（weak_trend + DI 方向） + M5/M15 SMA20 傾き |
| **レジーム制御** | ADX によるロット倍率・分散エントリー・レンジ回避 |
| **リスク管理** | BB2σ 圧縮 TP / スプリットエントリー / トレーリング SL |
| **急騰対応** | RVOL 急騰初期検知・中段階回避・大変動時の通常モード自動切替 |
| **Discord 制御** | Webhook 監視通知 + Bot コマンドによるリアルタイムパラメータ変更 |
| **ウォッチドッグ** | 異常終了時の MT5 自動再起動・再起動回数管理 |

---

## ディレクトリ構成

```
torihiki/
├── mt5_ea_bridge.py        後方互換シム（bridge/ への委譲）
├── mt5_monitor.py          ウォッチドッグ / ヘルスチェック
├── config.py               全設定の一元管理
├── secret.py               認証情報（Git 管理外推奨）
├── trading_rules.json      RulesEngine フィルタ設定
│
├── bridge/                 ブリッジ ロジックパッケージ
│   ├── __init__.py         パブリック API まとめ
│   ├── state.py            ポーリング間状態管理（データクラス）
│   ├── io.py               signal.json アトミック書き込み / ea_state.json 読み込み
│   ├── utils.py            ステートレスユーティリティ（ロット計算・レジーム判定など）
│   ├── notify.py           Discord Webhook 通知・一時停止フラグ管理
│   ├── time_bias.py        時間帯バイアス分析・ロード
│   ├── sma20.py            SMA20 タッチマージン分析・キャッシュ
│   ├── signal_normal.py    compute_signal（H1 クロス戦略 + パターンエントリー）
│   ├── signal_scalp.py     compute_scalp_signal（M5 スキャルプ + パターンエントリー）
│   ├── param_override.py   ランタイムパラメータ上書き（JSON ベース）
│   ├── discord_cmd.py      Discord コマンドボット（!set / !get / !reset）
│   └── runner.py           run_bridge / main（ポーリングループ）
│
├── core/
│   ├── data.py             MT5 データ取得・合成データ生成
│   ├── indicators.py       テクニカル指標（RSI・ATR・ADX・RVOL・BB）
│   ├── patterns.py         パターン検知（Wボトム / ダブルトップ / 三尊 / 逆三尊）
│   ├── plot.py             可視化
│   └── strategy.py         シグナル検出・SL戦略・パターンシグナル・バックテスト
│
├── mt5/
│   └── XAUUSD_SL_Strategy.mq5   MT5 EA（MQL5）
│
├── mt5_backtest.py         MT5 実データ バックテスト（通常モード）
├── scalp_backtest.py       スキャルプ + パターンエントリー バックテスト
├── analyze_patterns.py     H1 パターン検知 & チャート出力（BTC/USD）
├── btc_predict.py          BTC 価格予測（4 統計モデル）
├── analyze_time_bias.py    時間帯バイアス分析
├── analyze_sma20_touch.py  SMA20 タッチマージン分析
├── analyze_risk.py         リスク分析・評価
├── local_analysis.py       ローカル分析（MT5不要）
├── monitor_rvol.py         RVOL リアルタイム監視
└── plot_rvol_analysis.py   RVOL 分析プロット
```

---

## 必要なパッケージ

```bash
pip install pandas numpy matplotlib scipy
pip install MetaTrader5       # Windows のみ（MT5 接続が必要な場合）
pip install requests          # Discord Webhook 通知用
pip install discord.py        # Discord コマンドボット用（任意）
pip install yfinance          # analyze_patterns.py / btc_predict.py 用（任意）
pip install prophet           # btc_predict.py Prophet モデル用（任意）
```

---

## secret.py の設定

```python
# secret.py（Git 管理外推奨）

# ウォッチドッグの監視通知用（Webhook）
DISCORD_WEBHOOK_URL    = "https://discord.com/api/webhooks/xxxx/yyyy"

# パラメータ制御コマンドボット用（Bot Token）
DISCORD_BOT_TOKEN      = "MTxxxxxxxxxxxxxxx.xxxxxx.xxxxxxxxxx"
DISCORD_CMD_CHANNEL_ID = 123456789012345678  # int
```

`DISCORD_BOT_TOKEN` と `DISCORD_CMD_CHANNEL_ID` がなければコマンドボットは無効化されます。`DISCORD_WEBHOOK_URL` がなければウォッチドッグ通知が無効になります。いずれかだけの設定でも動作します。

---

## 実行方法

### 1. 通常起動（ブリッジ単体）

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

### 2. ウォッチドッグモード（推奨）

異常終了時に MT5 端末を再起動してブリッジを自動再起動します。

```bash
# 基本
python mt5_monitor.py --watch

# オプション指定
python mt5_monitor.py --watch --mode scalp --symbol BTCUSD
python mt5_monitor.py --watch --target 1500 --lot 0.05

# ヘルスチェックのみ（タスクスケジューラ等で定期実行）
python mt5_monitor.py
```

**ウォッチドッグの動作フロー**

```
mt5_monitor.py --watch 起動
  │
  ├─ Webhook 通知「監視開始」→ Discord
  ├─ mt5_ea_bridge.py をサブプロセスとして起動
  │     └─ bridge/runner.py → start_discord_bot() → コマンドボット起動
  │
  ├─ 終了コード 0（正常・Ctrl+C）→ 再起動しない
  └─ 終了コード ≠ 0（連続失敗など）→ MT5 再起動 → ブリッジ再起動
```

ウォッチドッグ稼働中でも Discord コマンドボットは同時に使えます（ブリッジのサブプロセス内で起動するため）。

### 3. バックテスト

#### スキャルプ + パターンエントリー

```bash
# 合成データ（MT5不要）
python scalp_backtest.py --synthetic

# MT5 実データ
python scalp_backtest.py --symbol BTCUSD

# 全履歴
python scalp_backtest.py --full-data

# 期間指定
python scalp_backtest.py --from 2024-01-01 --to 2024-06-30

# タッチマージン指定
python scalp_backtest.py --touch-margin 10.0
```

バックテストは以下を含みます：
- MTF フィルタ（H1 weak_trend + DI + M5/M15 SMA20 傾き）
- H1 パターン ネックライン突破エントリー（事前計算・ルックアヘッドなし）
- シグナル種別ごとの統計（`rsi_cross` / `pattern_nl`）

#### 通常モード（H1 クロス戦略 + パターン）

```bash
python mt5_backtest.py
python mt5_backtest.py --symbol XAUUSD --h1 3000 --m1 50000
```

### 4. パターン検知 & チャート出力

```bash
python analyze_patterns.py
python analyze_patterns.py --bars 400 --window 6 --top 5
python analyze_patterns.py --out ./output/my_patterns.png
```

BTC/USD H1 データ（yfinance → 組み込みアンカーにフォールバック）でパターンを検知し `./output/patterns.png` に出力します。

### 5. BTC 価格予測

```bash
python btc_predict.py
```

4 つの統計モデルで BTC の将来価格を予測します：
- **ARIMA(1,0,1)** — 自己回帰モデル
- **Prophet** — Facebook 製トレンド分解モデル
- **LogLinear** — 対数線形トレンド外挿
- **Monte Carlo GBM** — 幾何ブラウン運動シミュレーション（n=3000）

出力: `./output/btc_predict.png`

### 6. その他の分析ツール

```bash
# 時間帯バイアス分析（output/time_bias.json を更新）
python analyze_time_bias.py

# SMA20 タッチマージン分析（output/sma20_touch_margins.json を更新）
python analyze_sma20_touch.py

# RVOL リアルタイム監視
python monitor_rvol.py --symbol BTCUSD --minutes 60 --interval 10
```

---

## Discord コマンドボット

ブリッジ稼働中にリアルタイムでパラメータを変更できます。

### セットアップ

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリを作成
2. **Bot** → **Add Bot** → Token をコピー
3. **Privileged Gateway Intents** → **Message Content Intent** を ON
4. **OAuth2 → URL Generator** → Scopes: `bot`、Permissions: `Send Messages / Read Messages` → URL でサーバーに招待
5. コマンド用チャンネルを右クリック → **ID をコピー**（開発者モードが必要）
6. `secret.py` に `DISCORD_BOT_TOKEN` と `DISCORD_CMD_CHANNEL_ID` を記入

### コマンド一覧

| コマンド | 例 | 説明 |
|---|---|---|
| `!params` | `!params` | 全パラメータの現在値を表示 |
| `!get <param>` | `!get target` | 指定パラメータの値を確認 |
| `!set <param> <value>` | `!set target 1500` | パラメータを変更（次のポーリングから反映） |
| `!reset` | `!reset` | 全オーバーライドを削除して config に戻す |
| `!help` | `!help` | コマンド一覧と全パラメータの説明 |

### 変更可能なパラメータ

| パラメータ | 型 | 説明 | 例 |
|---|---|---|---|
| `target` | 整数 | スキャルプ目標利益（JPY） | `!set target 2000` |
| `sl_ratio` | 小数 | SL/TP 比率 | `!set sl_ratio 2.0` |
| `tp_frac` | 小数 | TP = M5 ATR × frac | `!set tp_frac 0.6` |
| `buy` | on/off | BUY エントリー有効化 | `!set buy off` |
| `sell` | on/off | SELL エントリー有効化 | `!set sell on` |
| `max_trades` | 整数 | 1日の最大取引数 | `!set max_trades 15` |
| `cooldown` | 整数 | クールダウン（分） | `!set cooldown 60` |
| `jpy_rate` | 小数 | JPY/USD レート | `!set jpy_rate 155.0` |
| `lot` | 小数 | ロットサイズ上限 | `!set lot 0.1` |
| `risk` | 小数 | リスク割合（0.01 = 1%） | `!set risk 0.02` |
| `sl_multi` | 小数 | SL 倍率（ATR×） | `!set sl_multi 1.5` |
| `tp_multi` | 小数 | TP 倍率（ATR×） | `!set tp_multi 3.0` |
| `rsi_buy` | 小数 | BUY RSI 閾値 | `!set rsi_buy 38.0` |

オーバーライドは `./output/runtime_params.json` に保存され、ブリッジ再起動後も維持されます。

---

## テクニカルパターン認識

### 検知対象パターン

| パターン | 方向 | 説明 |
|---|---|---|
| **Wボトム（ダブルボトム）** | 強気 | 2 つの安値がほぼ同水準 |
| **ダブルトップ** | 弱気 | 2 つの高値がほぼ同水準 |
| **三尊（ヘッドアンドショルダー）** | 弱気 | 中央の高値が左右より高い |
| **逆三尊（逆ヘッドアンドショルダー）** | 強気 | 中央の安値が左右より低い |

### 信頼度スコア（0〜1）

```
信頼度 = 対称性（40%） + 高さ比率（30%） + 新しさ（30%）
```

閾値 0.45 以上でエントリーシグナルとして使用します。

### ネックライン突破エントリー

```
H1 前足終値 ≤ ネックライン < H1 現足終値（Wボトム等 強気）
  → new_buy_type = 'pattern_double_bottom' など
  → シグナルウィンドウ開始（valid_min 分間有効）
  → TP を測定値ターゲット（ネックライン + パターン高さ）に上書き

H1 前足終値 ≥ ネックライン > H1 現足終値（ダブルトップ等 弱気）
  → new_sell_type = 'pattern_double_top' など
```

**スキャルプモード**: ネックライン突破で `confirmed_signal` を直接セット → SMA20 タッチ待ちをスキップしてエントリー。クールダウン・日次上限は引き続き適用。

**同一パターンの重複エントリー防止**: `(パターン名, round(neckline, 0))` をフィンガープリントとして `state.pattern_traded` に記録（最大 120 件、超えたら自動クリア）。

---

## エントリーフロー

### 通常モード（H1 クロス戦略）

```
H1 RSI クロス（DIP / モメンタム）
  または
H1 ネックライン突破（Wボトム / ダブルトップ等）
  ↓
シグナルウィンドウ開始（valid_min 分間）
  ↓
M5 RSI フィルタ / M5 SMA20 方向 / H1 SMA20 方向 / M1 RSI ピーク確認
  ↓
SL/TP 計算（ATR ベース、パターン TP があれば上書き）
  ↓
BB2σ 圧縮 / スプリットエントリー / 分散エントリーゲート
  ↓
エントリー
```

### スキャルプモード（M5 クロス戦略）

```
H1 ネックライン突破                     M5 RSI クロス（閾値超え）
  → confirmed_signal（直接）              ↓
                                        MTF 条件チェック:
                                          H1 (weak_trend/trend) + DI 方向
                                          M5 SMA20 傾き
                                        SMA20 タッチ待ち（30分タイムアウト）
                                          ↓  傾き確認（ATR×0.10 以上）
                                        M1 確認バー 2 本待ち（30分タイムアウト）
                                          ↓
                              クールダウン / 日次上限 / ポジション数チェック
                                          ↓
                                        エントリー
```

大変動検知時（`detect_big_move` が `up`/`down`）: スキャルプを中断して通常モードシグナルにフォールバック。クールダウン中も同様。

---

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
    lot_size         = 0.05,
    risk_pct         = 0.03,
    fallback_balance = 15_000,
    log_dir          = r"G:\マイドライブ\mt5_log",
)
```

`log_dir` を設定すると `bridge_SYMBOL.log`（5MB × 5世代ローテーション）が出力されます。

### SCALP（スキャルプ設定）

```python
SCALP = dict(
    jpy_per_usd        = 150.0,
    target_profit_jpy  = 1000,     # 目標利益（円）
    sl_ratio           = 3,        # SL幅 = TP幅 × sl_ratio
    tp_atr_fraction    = 0.5,      # TP幅 = M5 ATR × tp_atr_fraction
    signal_tf          = 'M5',
    rsi_buy_thrs       = [55.0, 60.0, 65.0],
    rsi_sell_thrs      = [45.0, 40.0, 35.0],
    max_trades_day     = 20,
    cooldown_min       = 15,
    big_move_lookback  = 12,       # 大変動判定: 過去 N 本
    big_move_atr_multi = 5.0,      # 大変動判定: ATR × N 以上
    sma20_slope_bars   = 5,        # MTF SMA20 傾き計算バー数
    sma20_slope_atr_thr = 0.10,    # MTF SMA20 傾き閾値（ATR比）
)
```

### REGIME（レジーム設定）

```python
REGIME = dict(
    trend_thr            = 25.0,   # ADX ≥ 25 → トレンド
    range_thr            = 20.0,   # ADX < 20 → レンジ
    lot_multi_trend      = 1.5,
    lot_multi_weak       = 1.0,
    lot_multi_range      = 0.6,
    max_entry_per_signal = 3,
    entry_spacing_atr    = 0.5,
    scalp_reserve_slots  = 1,
)
```

**MTF フィルタ（スキャルプ）の判定ロジック**

```python
# H1 ADX に関わらず DI 方向が合致していればエントリー許可（案A）
mtf_buy_ok  = (regime_h1 in ('trend_up', 'weak_trend') and DI+ > DI-)
mtf_sell_ok = (regime_h1 in ('trend_down', 'weak_trend') and DI- > DI+)
```

ADX < 25（weak_trend）でも DI 方向が一致していればエントリーを許可します。以前の `trend_up` 限定より約2倍のシグナル頻度になります。

### TIME_BIAS（時間帯バイアス設定）

```python
TIME_BIAS = dict(
    enabled              = True,
    danger_win_rate_thr  = 0.40,
    danger_avg_pnl       = 0.0,
    min_trades_per_hour  = 5,
    skip_before_min      = 30,
    skip_after_min       = 0,
    rebias_interval_hours = 24,
    bias_file            = "./output/time_bias.json",
)
```

---

## アーキテクチャ概要

```
mt5_monitor.py（ウォッチドッグ）
  │  Webhook 通知（監視開始・再起動・停止）
  └─ subprocess 起動
        │
        ▼
mt5_ea_bridge.py（後方互換シム）
        │
        ▼
bridge/runner.py（ポーリングループ）
  │
  ├─ start_discord_bot()        Discord コマンドボット（バックグラウンドスレッド）
  ├─ apply_overrides(cfg)       runtime_params.json からオーバーライドを適用
  │
  ├─ [normal mode]
  │    └─ signal_normal.py
  │         ├─ compute_signal()
  │         ├─ core/patterns.py → detect_all_patterns()   H1 パターン検知
  │         └─ ネックライン突破 → new_buy/sell_type        パターン TP 上書き
  │
  └─ [scalp mode]
       └─ signal_scalp.py
            ├─ compute_scalp_signal()
            ├─ core/patterns.py → detect_all_patterns()   H1 パターン検知
            ├─ ネックライン突破 → confirmed_signal（直接）  SMA20 待ち省略
            └─ big_move / cooldown → compute_signal() フォールバック
```

---

## 状態管理（bridge/state.py）

ポーリング間の状態はすべてデータクラスで管理されます。

| クラス | 主なフィールド |
|---|---|
| `SignalState` | RSI 前回値 / BUY・SELL シグナルウィンドウ / 分散エントリー追跡 / BB2σ タッチ状態 / **pattern_traded / pattern_tp_target** |
| `ScalpState` | RSI 前回値 / SMA20 タッチ待ち / M1 確認バー追跡 / **pattern_traded / pattern_tp_target** |
| `TimeBiasState` | 危険時間帯セット / 直前クローズフラグ |
| `JpyRateCache` | USDJPY レート（1時間キャッシュ） |
| `Sma20TouchCache` | シンボル別 SMA20 タッチマージン |

---

## バックテスト詳細

### scalp_backtest.py

```
[1] データ取得（MT5 または合成）
[2] 指標計算（M5 / M1 / M15 / H1）
[2.5] H1 パターン先行計算
    └─ _precompute_h1_crossings()
       ・150 本ローリングウィンドウ（12 本ステップ）
       ・ルックアヘッドなし（bar k のパターンは bar k-1 までのデータで検知）
       ・信頼度 ≥ 0.45 のみ採用
[3] バックテスト実行
    ├─ パターンエントリー（優先）: 直接 M5 バーでエントリー
    └─ RSI クロスエントリー: SMA20 タッチ → M1 確認 2 本
[4] 統計表示（WR / PF / Sharpe / 月次 / シグナル種別）
[5] JSON 保存（./output/scalp_bt.json）
```

出力される統計例:
```
  シグナル種別:
    pattern_nl        N件  WR=XX%  累計=+XXX,XXX JPY
    rsi_cross         N件  WR=XX%  累計=+XXX,XXX JPY
```

---

## MT5 EA 側設定

1. `mt5/XAUUSD_SL_Strategy.mq5` を `MQL5/Experts/` に配置してコンパイル
2. チャートにアタッチ
3. `InpSignalFile` を `config.py` の `BRIDGE['signal_file']` に合わせる

**通信プロトコル**

```
Python → MT5 EA : signal_SYMBOL.json  （毎ポーリング更新）
MT5 EA → Python : ea_state_SYMBOL.json（EA が書き込む）
```

---

## 注意事項

- `secret.py` は `.gitignore` に追加して Git 管理外にすることを推奨します
- `MetaTrader5` パッケージは Windows 専用。Linux/macOS では合成データのみ動作します
- `trading_rules.json` がある場合、`RulesEngine` によるフィルタが適用されます
- `output/` フォルダに分析結果・バックテスト結果・ランタイムオーバーライドが保存されます
- `output/runtime_params.json` を削除すると全オーバーライドがリセットされます（`!reset` と同等）
