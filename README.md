# torihiki 自動売買システム

## 概要

MT5 と Python を組み合わせた自動売買システムです。スキャルプモードを中心に、複数のシグナルパスとマルチタイムフレーム（MTF）フィルタを組み合わせてエントリーを判断します。

### 主な機能

| カテゴリ | 内容 |
|---|---|
| **スキャルプモード** | M5 RSI クロス → SMA20 タッチ → M1 確認1本でエントリー |
| **H1 パターン認識** | Wボトム / ダブルトップ / 三尊 / 逆三尊 をH1で検知しネックライン突破で直接エントリー |
| **エリオット波動 Wave2** | M5足でW1+W2パターンを検知しWave3への参入（Fib 38.2〜78.6% 押し目/戻り） |
| **ボリュームブレイクアウト** | RVOL急増 + ローソク実体比 + RSI方向確認で SMA20 待ちをスキップ |
| **MTF フィルタ** | M1/M5/M15 SMA20 傾き + D1 SMA20 方向 + H1 レジームで多段階フィルタリング |
| **リスク管理** | ATRベース TP/SL / 証拠金維持率チェック / ロット自動調整 / マクロバイアス補正 |
| **Discord 制御** | Webhook 通知 + Bot コマンドによるリアルタイムパラメータ変更 |
| **ウォッチドッグ** | 異常終了時の MT5 自動再起動・再起動回数管理 |

---

## ディレクトリ構成

```
torihiki/
├── mt5_ea_bridge.py        後方互換シム（bridge/ への委譲）
├── mt5_monitor.py          ウォッチドッグ / ヘルスチェック
├── config.py               全設定の一元管理
├── secret.py               認証情報（Git 管理外推奨）
│
├── bridge/
│   ├── runner.py           ポーリングループ・状態オーケストレーション
│   ├── state.py            ポーリング間状態（データクラス）
│   ├── signal_scalp.py     スキャルプシグナル計算（メインロジック）
│   ├── signal_normal.py    通常モードシグナル計算（H1 クロス戦略）
│   ├── io.py               signal.json アトミック書き込み / ea_state.json 読み込み
│   ├── notify.py           Discord Webhook 通知・一時停止フラグ管理
│   ├── dashboard.py        ターミナル UI 表示
│   ├── discord_cmd.py      Discord コマンドボット（!set / !get / !status 等）
│   ├── utils.py            ロット計算・レジーム判定・ポジション追跡
│   ├── time_bias.py        時間帯バイアス分析・ロード
│   ├── sma20.py            SMA20 タッチマージン分析・キャッシュ
│   └── param_override.py   ランタイムパラメータ上書き（JSON ベース）
│
├── core/
│   ├── data.py             MT5 データ取得・合成データ生成
│   ├── indicators.py       RSI / ATR / ADX / RVOL / SMA20 / Bollinger
│   ├── patterns.py         Wボトム / ダブルトップ / 三尊 / 逆三尊 検知
│   └── strategy.py         EW2検知 / ボリュームブレイクアウト / ウィップソー検出
│
├── mt5/
│   └── XAUUSD_SL_Strategy.mq5   MT5 EA（MQL5）
│
├── scalp_backtest.py       スキャルプバックテスト
├── mt5_backtest.py         通常モードバックテスト
├── analyze_patterns.py     H1 パターン検知 & チャート出力
├── analyze_time_bias.py    時間帯バイアス分析
└── analyze_sma20_touch.py  SMA20 タッチマージン分析
```

---

## 必要なパッケージ

```bash
pip install pandas numpy matplotlib scipy
pip install MetaTrader5       # Windows のみ（MT5 接続が必要な場合）
pip install requests          # Discord Webhook 通知用
pip install discord.py        # Discord コマンドボット用（任意）
```

---

## secret.py の設定

```python
# secret.py（Git 管理外推奨）
DISCORD_WEBHOOK_URL    = "https://discord.com/api/webhooks/xxxx/yyyy"
DISCORD_BOT_TOKEN      = "MTxxxxxxxxxxxxxxx.xxxxxx.xxxxxxxxxx"
DISCORD_CMD_CHANNEL_ID = 123456789012345678  # int
```

`DISCORD_BOT_TOKEN` / `DISCORD_CMD_CHANNEL_ID` がなければコマンドボットは無効。`DISCORD_WEBHOOK_URL` がなければ Webhook 通知が無効。いずれかだけでも動作します。

---

## 実行方法

```bash
# スキャルプモード（推奨）
python mt5_ea_bridge.py --mode scalp

# オプション指定
python mt5_ea_bridge.py --mode scalp --symbol BTCUSD --target 1000 --jpy 150

# 通常モード（H1 クロス戦略）
python mt5_ea_bridge.py --mode normal

# ウォッチドッグモード（異常終了時に自動再起動）
python mt5_monitor.py --watch --mode scalp --symbol BTCUSD

# バックテスト
python scalp_backtest.py --symbol BTCUSD
python scalp_backtest.py --synthetic     # MT5 不要（合成データ）
```

---

## スキャルプモード シグナルパス

シグナルは以下の 5 つのパスで生成されます。優先度は上から順。

### パス 1：H1 パターン ネックライン突破（直接エントリー）

H1 足でテクニカルパターンを検知し、ネックライン突破で即エントリー。SMA20 タッチ待ちをスキップ。

```
検知対象:  Wボトム（強気）/ ダブルトップ（弱気）/ 逆三尊（強気）/ 三尊（弱気）
信頼度:    ≥ 0.45（対称性40% + 高さ比率30% + 新しさ30%）
トリガー:  H1 前足終値 ≤ ネックライン < H1 現足終値（BUY）
           H1 前足終値 ≥ ネックライン > H1 現足終値（SELL）
TP:        パターン測定値（ネックライン + パターン高さ）に自動上書き
重複防止:  (パターン名, round(neckline, 0)) をフィンガープリントとして記録（最大120件）
フラグ:    _direct_confirmed = True
```

### パス 2：Elliott Wave 2 エントリー（直接エントリー）

M5 足専用データ（130本以上）でエリオット波動 Wave1 + Wave2 を検知し Wave3 を狙うエントリー。

```
検知条件（BUY）:
  - Wave1 = W1_high - W1_low ≥ ATR × 1.5
  - Wave2 が Wave1 の 38.2%〜78.6% を押し目として形成
  - Wave2 RSI ≤ 50.0（中立圏以下）
  - Wave2 が直近 8 本以内
  - RSI ダイバージェンス: RSI_W1 - RSI_W2 ≥ 3.0（強気ダイバージェンス）

TP:  Wave2底 + Wave1_size × 1.618（フィボナッチ Wave3 黄金比目標）
SL:  Wave2底 - ATR × 0.3（波動失効ライン）
重複防止: ('ew2_buy', round(w2_low, 0)) をフィンガープリントとして記録
フラグ:   _direct_confirmed = True、_is_ew2_signal = True
```

EW2 は多くのフィルターを免除されます（[エントリーゲート](#エントリーゲート) を参照）。

### パス 3：ボリュームブレイクアウト（直接エントリー）

出来高急増と方向性の組み合わせでブレイクアウトを検知。SMA20 タッチ待ちをスキップ。

```
条件:
  - RVOL ≥ 2.0（相対出来高）
  - ローソク実体/レンジ比率 ≥ 0.45（騙しフィルター）
  - 同バー内価格変動 ≥ ATR × 0.3（動き確認）
  - BUY: RSI ≥ 52.0 / SELL: RSI ≤ 48.0
  - 同バーで既に発火済みでないこと

TP: 通常 TP × 1.8（ブレイクアウトは勢いが続くため）
SL: 通常 SL × 0.8（根拠明確なのでタイト）
```

### パス 4：M1 早期実行（SMA20 タッチ待ち開始）

M5 RSI が閾値の 2.0 以内に近づいた状態で M1 足が先にクロスした場合、SMA20 タッチ待ちを先行開始します。

```
条件: M5 RSI が閾値（50/55/60/65）まで 2.0 以内かつ M1 RSI が先にクロス
→ buy/sell_sma_pending = True を直接セット（M5 クロス待ちをスキップ）
```

### パス 5：M5 RSI クロス → SMA20 タッチ → M1 確認

通常のスキャルプエントリーフロー。最も保守的で最も多くのフィルターを経由します。

```
[Step 1] M5 RSI クロス検出
  BUY:  RSI が 50/55/60/65 のいずれかを下から上に抜ける
  SELL: RSI が 50/45/40/35 のいずれかを上から下に抜ける

[Step 2] MTF チェック → SMA20 タッチ待ち開始
  H1 レジームが trend_down でない（BUY）/ trend_up でない（SELL）
  M5 SMA20 傾きが逆方向に急傾斜でない
  → buy/sell_sma_pending = True（30分タイムアウト）

[Step 3] SMA20 タッチ待ち（M1 足で確認）
  タッチ条件: |close_M1 - SMA20_M1| ≤ touch_margin
  バイパス条件:
    BUY:  close > SMA20 + ATR × 1.2（急騰でSMA20から大きく上離れ）
    SELL: close < SMA20 - ATR × 1.2（急落でSMA20から大きく下離れ）
  → sell/buy_confirm_pending = True、confirm_count = 0（30分タイムアウト）

[Step 4] M1 確認バー（1本）
  BUY:  close_M1 > prev_close かつ close_M1 ≥ SMA20_M1
  SELL: close_M1 < prev_close かつ close_M1 ≤ SMA20_M1
  カウントが同一バーで重複しない
  → confirmed_signal = 'buy'/'sell'
```

---

## エントリーゲート

`confirmed_signal` がセットされた後、以下の条件を上から順に評価します。いずれかが失敗すると `action = 'none'`（スキップ）。

| # | ゲート | ブロック条件 | EW2 | _direct_confirmed |
|---|--------|------------|:---:|:-----------------:|
| 1 | ウィップソーブロック | 直近2時間に双方向損失 | 適用 | 適用 |
| 2 | M1 RSI 過熱抑制 | RSI>65 で追加 BUY（既存ポジあり） | 適用 | 適用 |
| 3 | M1 RSI 売られすぎ抑制 | RSI<35 で追加 SELL（既存ポジあり） | 適用 | 適用 |
| 4 | **M1 SMA20 絶対ゲート** | M1 SMA20 下落中 → BUY禁止（逆も対称） | **免除** | 適用 |
| 5 | **M5 SMA20 価格位置** | close < SMA20_M5 → BUY禁止（逆も対称） | **免除** | **免除** |
| 6 | **D1 SMA20 方向** | D1 SMA20 下落中 → BUY禁止（逆も対称） | **免除** | 適用 |
| 7 | M5/M15 コンセンサス | M5 と M15 が両方とも逆方向傾斜 | **免除** | 適用 |
| 8 | RSI ハードゲート | RSI < 40.0 → BUY禁止 / RSI > 60.0 → SELL禁止 | **免除** | 適用 |
| 9 | M5 レジーム逆張り禁止 | M5 trend_up で SELL / trend_down で BUY | **免除** | **免除** |
| 10 | 禁止時間帯 | UTC 21 時台 | 適用 | 適用 |
| 11 | クールダウン | N 回ごとに cooldown_min 分待機 | 適用 | 適用 |
| 12 | ポジション上限 | available_slots ≤ 0（反対方向があればヘッジ許可） | 適用 | 適用 |

**EW2 免除の理由**: Wave2 形成中は M1/M5 SMA20 が逆方向・RSI が低水準であることが構造的に正常なため、これらのゲートを通過できなければ EW2 シグナルが永続的に死路になります。

**`_direct_confirmed` 免除の理由**: H1 パターンのネックライン突破はSMA20を跨いで発動するため、M5 価格位置ゲートとM5レジームゲートを免除します。

---

## SMA20 コンセンサスロジック

M1 / M5 / M15 / D1 の各 SMA20 傾きを独立して評価します。

```
_sma20_ok(df, direction):
  slope = SMA20[now] - SMA20[5本前]
  threshold = ATR × 0.10
  BUY:  slope > -threshold（明確な下落でなければ OK、フラットは許容）
  SELL: slope < +threshold（明確な上昇でなければ OK、フラットは許容）
  ※ データ不足 / NaN → True（許容）
```

| ゲート | ブロック条件 |
|---|---|
| M1 SMA20 絶対ゲート | M1 が明確に下落中は BUY 禁止（例外なし、EW2除く） |
| M5 SMA20 価格位置 | close < SMA20_M5 は BUY 禁止（EW2・直接確認シグナル除く） |
| D1 SMA20 方向ゲート | D1 が明確に下落中は BUY 禁止（EW2 除く） |
| M5/M15 コンセンサス | M5 と M15 が**両方とも**逆傾斜の場合のみブロック（片方 OK なら通過） |

---

## リスク管理

### ロットサイズ計算

```
target_usd = target_profit_jpy / jpy_per_usd
tp_move    = ATR_M5 × tp_atr_fraction
sl_move    = tp_move × sl_ratio
lot_raw    = target_usd / (tp_move × contract_size)
lot        = clamp(lot_raw × regime_multiplier, lot_min, lot_max)
```

レジーム別ロット倍率: `trend=1.5 / weak_trend=1.0 / range=0.6`

### 証拠金維持率チェック（毎ポーリング）

```
追加後維持率 = equity / (現在証拠金 + 追加証拠金) × 100
維持率 < min_margin_level(200%)の場合:
  → ロットを維持率が200%を維持できる最大値に削減
  → 削減後ロット < lot_min の場合: action = 'none'（スキップ）
```

### SL/TP 上書き優先度

```
[1] 通常計算: TP = close ± (ATR × tp_atr_fraction)、SL = close ∓ (ATR × tp_atr_fraction × sl_ratio)
[2] EW2 上書き: TP = Fib 1.618 延長目標、SL = Wave2 失効ライン
[3] パターン上書き: TP = パターン測定値ターゲット（H1 パターン）
[4] ボリュームBO 上書き: TP × 1.8、SL × 0.8
[5] マクロバイアス補正: D1/W1/MN1 パターンに基づく TP/SL 倍率調整
SL 下限: max(sl_price, close ∓ ATR × 0.5)（EA 執行ギャップ対策）
```

### クールダウン（カウントベース）

毎 N 回（`cooldown_trades=3`）エントリーするごとに `cooldown_min=15` 分の待機を挟みます。時間ベースではなくトレード回数ベース。

---

## エリオット波動 Wave2 検知詳細

M5 専用データ（lookback 100本 + バッファ 30本 = 合計 130本取得）でスイングポイントを探索します。

```
スイング確定ウィンドウ: 両側 3 本（sw_window=3）
Wave1 条件: W1_high - W1_low ≥ ATR × 1.5（min_wave1_atr）
Wave2 押し目: (W1_high - W2_low) / Wave1_size ∈ [38.2%, 78.6%]
Wave2 RSI:   ≤ 50.0（BUY）/ ≥ 50.0（SELL）
直近性:       Wave2 が直近 8 本以内（w2_bars_ago_max）
ダイバージェンス: RSI_W1 - RSI_W2 ≥ 3.0（rsi_div_min）

TP: Wave2底 + Wave1_size × 1.618
SL: Wave2底 - ATR × 0.3
```

EW2 シグナルは `_is_ew2_signal = True` フラグを持ち、M1 SMA20・M5 価格位置・D1 SMA20・コンセンサス・RSI ゲートをすべて免除されます（Wave2 形成中はこれらのゲートが逆方向を示すことが正常なため）。

---

## 状態管理（bridge/state.py）

| クラス | 主なフィールド |
|---|---|
| `ScalpState` | SMA20 タッチ待ち(BUY/SELL) / M1 確認バー追跡 / EW2 執行済みセット / ボリュームBO 直前バー |
| `SignalState` | H1 RSI 前回値 / BUY・SELL シグナルウィンドウ / 分散エントリー追跡 |
| `MacroBiasState` | D1/W1/MN1 マクロバイアス / TP・SL 倍率 / 最終更新時刻 |
| `JpyRateCache` | USDJPY レート（1時間キャッシュ） |
| `Sma20TouchCache` | シンボル別 SMA20 タッチマージン |

`ScalpState` の主な状態遷移ルール:
- H1 が `trend_up` に転換 → SELL 系 pending を全クリア
- H1 が `trend_down` に転換 → BUY 系 pending を全クリア
- `buy_enabled/sell_enabled = False` の場合は即座に対応 pending をクリア

---

## Discord 通知・コマンド

### Webhook 通知（notify.py）

| タイミング | 内容 |
|---|---|
| シグナル変化時 | 🟢 BUY / 🔴 SELL / ⬜ 消灯 + RSI・SMA20・証拠金維持率 |
| 1時間おき | 全指標サマリー（RSI/ATR/RVOL/ADX/DI/SMA20 M5-D1/EW2スキャン結果/ポジション/証拠金） |
| スマホ一時停止 | Magic=0 の Buy Stop 注文を検知して自動停止 / 削除で再開 |

### コマンドボット（discord_cmd.py）

| コマンド | 説明 |
|---|---|
| `!status` | 現在の全指標・シグナル状態を即時表示 |
| `!params` | 全パラメータの現在値を表示 |
| `!get <param>` | 指定パラメータの値を確認 |
| `!set <param> <value>` | パラメータをリアルタイム変更（次ポーリングから反映） |
| `!reset` | 全オーバーライドを削除して config.py の値に戻す |
| `!help` | コマンド一覧と全パラメータの説明 |

変更可能なパラメータ: `target` / `sl_ratio` / `tp_frac` / `buy` / `sell` / `max_trades` / `cooldown` / `jpy_rate` / `lot` / `risk` / `sl_multi` / `tp_multi` / `rsi_buy`

オーバーライドは `./output/runtime_params.json` に保存され、ブリッジ再起動後も維持されます。

---

## config.py 主要パラメータ

### SCALP（スキャルプ設定）

```python
SCALP = dict(
    target_profit_jpy  = 1000,        # 1トレードあたりの目標利益（円）
    sl_ratio           = 3,           # SL幅 = TP幅 × sl_ratio（リスクリワード 1:0.33）
    tp_atr_fraction    = {'BTCUSD': 0.5},  # TP幅 = M5 ATR × fraction
    rsi_buy_thrs       = [50.0, 55.0, 60.0, 65.0],   # M5 RSI クロス閾値（BUY）
    rsi_sell_thrs      = [50.0, 45.0, 40.0, 35.0],   # M5 RSI クロス閾値（SELL）
    rsi_buy_gate_min   = 40.0,        # RSI ハードゲート BUY 最低値
    rsi_sell_gate_max  = 60.0,        # RSI ハードゲート SELL 最高値
    cooldown_trades    = 3,           # N 回ごとにクールダウン発動
    cooldown_min       = 15,          # クールダウン時間（分）
    min_margin_level   = 200.0,       # 証拠金維持率の最低ライン（%）
    lot_max            = {'BTCUSD': 0.10},  # シンボル別最大ロット
    sma20_slope_bars   = 5,           # SMA20 傾き計算バー数
    sma20_slope_atr_thr = 0.10,       # 傾き閾値 = ATR × 0.10
    buy_sma_bypass_atr  = 1.2,        # BUY SMA20タッチバイパス（close > SMA20 + ATR×1.2）
    sell_sma_bypass_atr = 1.2,        # SELL SMA20タッチバイパス
    h1_di_filter        = False,      # False=H1レジームのみ / True=DI方向も必須
    vol_bo_enabled      = True,       # ボリュームブレイクアウト有効化
    vol_bo_rvol_thr     = 2.0,        # ブレイクアウト RVOL 閾値
    vol_bo_rsi_buy_min  = 52.0,       # ブレイクアウト BUY の最低 RSI
    vol_bo_rsi_sell_max = 48.0,       # ブレイクアウト SELL の最高 RSI
    vol_bo_tp_multi     = 1.8,        # ブレイクアウト TP 倍率
    vol_bo_sl_multi     = 0.8,        # ブレイクアウト SL 倍率
)
```

### ELLIOTT（エリオット波動設定）

```python
ELLIOTT = dict(
    enabled          = True,
    lookback_bars    = 100,    # M5 スイング探索バー数（約8時間）
    sw_window        = 3,      # スイング確定ウィンドウ（両側 N 本）
    fib_min          = 0.382,  # 押し目 Fibonacci 下限
    fib_max          = 0.786,  # 押し目 Fibonacci 上限
    min_wave1_atr    = 1.5,    # Wave1 最小サイズ（ATR 倍）
    rsi_div_min      = 3.0,    # RSI ダイバージェンス最小値
    w2_buy_rsi_max   = 50.0,   # BUY Wave2 の RSI 上限
    w2_sell_rsi_min  = 50.0,   # SELL Wave2 の RSI 下限
    w2_bars_ago_max  = 8,      # Wave2 の直近性（M5 バー数）
    fib_tp_ext       = 1.618,  # Wave3 TP = W2 + Wave1 × 1.618
    sl_buffer_atr    = 0.3,    # SL = W2 - ATR × 0.3
)
```

### REGIME（レジーム設定）

```python
REGIME = dict(
    trend_thr            = 25.0,   # ADX ≥ 25 → トレンド
    range_thr            = 20.0,   # ADX < 20 → レンジ
    lot_multi_trend      = 1.5,    # トレンド時のロット倍率
    lot_multi_weak       = 1.0,    # weak_trend 時
    lot_multi_range      = 0.6,    # レンジ時
)
```

---

## アーキテクチャ概要

```
mt5_monitor.py（ウォッチドッグ）
  └─ subprocess 起動
        ▼
mt5_ea_bridge.py → bridge/runner.py（ポーリングループ）
  │
  ├─ start_discord_bot()          コマンドボット（バックグラウンドスレッド）
  ├─ apply_overrides(cfg)         runtime_params.json 適用
  ├─ update_macro_bias()          D1/W1/MN1 マクロバイアス（4時間ごと）
  │
  ├─ [scalp mode] compute_scalp_signal()
  │    ├─ fetch M1/M5/M15/H1/D1/EW2専用M5
  │    ├─ H1 パターン検知（信頼度 ≥ 0.45）
  │    ├─ EW2 検知（M5 専用データ 130本）
  │    ├─ ボリュームブレイクアウト検知
  │    ├─ SMA20 タッチ → M1 確認 フロー
  │    └─ エントリーゲート（13段階）
  │
  └─ [big_move / normal mode] compute_signal()
       └─ H1 RSI クロス + パターンエントリー

signal.json（Python → EA）
ea_state.json（EA → Python）
```

---

## MT5 EA 側設定

1. `mt5/XAUUSD_SL_Strategy.mq5` を `MQL5/Experts/` に配置してコンパイル
2. チャートにアタッチ
3. `InpSignalFile` を `config.py` の `BRIDGE['signal_file']` に合わせる

**通信プロトコル**

```
Python → MT5 EA : signal_SYMBOL.json（毎ポーリング更新、アトミック書き込み）
MT5 EA → Python : ea_state_SYMBOL.json（EA が書き込む）
```

---

## バックテスト（scalp_backtest.py）

```bash
python scalp_backtest.py --symbol BTCUSD
python scalp_backtest.py --synthetic          # MT5 不要
python scalp_backtest.py --from 2024-01-01 --to 2024-06-30
```

バックテストが再現する内容:
- H1 パターン先行計算（150本ローリングウィンドウ、ルックアヘッドなし）
- EW2 検知（M5 100本ルックバック）
- ボリュームブレイクアウト
- MTF SMA20 コンセンサスフィルタ（M1/M5/M15/D1）
- RSI ハードゲート（40/60）
- M5 SMA20 価格位置チェック
- M5 RSI クロス → SMA20 タッチ → M1 確認1本

出力される統計: シグナル種別ごとの勝率・PF・Sharpe・累積損益

---

## 注意事項

- `secret.py` は `.gitignore` に追加して Git 管理外にすること
- `MetaTrader5` パッケージは Windows 専用。Linux/macOS では合成データのみ動作
- `output/runtime_params.json` を削除すると全オーバーライドがリセット（`!reset` と同等）
- `output/time_bias.json` / `output/sma20_touch_margins.json` は分析スクリプトで事前生成推奨
