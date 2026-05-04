# RVOLリアルタイム監視機能 - 実装ガイド

## 概要

RVOL（相対出来高）をリアルタイムで監視し、急騰初期検知を可視化するシステムです。

## 新しいスクリプト

### 1. `monitor_rvol.py` - リアルタイム監視

**目的**: M5足のRVOLを常時表示し、急騰初期検知の信号を視覚的に把握

**実行方法**:
```bash
# XAUUSD を監視（120分間、5秒更新）
python monitor_rvol.py --symbol XAUUSD

# BTCUSD を監視（60分間、10秒更新）
python monitor_rvol.py --symbol BTCUSD --minutes 60 --interval 10
```

**グラフ構成（4パネル）**:

1. **価格チャート**
   - M5足の終値プロット
   - リアルタイム更新される価格動向を確認

2. **RVOL（相対出来高）**
   - 色分け表示:
     - 🟢 緑: RVOL ≤ 1.0x（正常）
     - 🟠 オレンジ: 1.0x < RVOL ≤ 1.5x（やや高い）
     - 🔴 赤: RVOL > 1.5x（非常に高い）
   - 閾値ライン:
     - 1.0x: 正常レベル（移動平均比）
     - 1.3x: **急騰初期検知閾値**
     - 1.5x: 高出来高警告

3. **Price Accel（価格加速度）**
   - 短期SMAの%変化率
   - 上昇時: 緑、下降時: 赤
   - 0.5%閾値: 急騰初期の判定基準

4. **Volume Surge（出来高急増フラグ）**
   - 二値表示（Yes/No）
   - 出来高 ≥ 過去平均×2.0 かつ RVOL ≥ 1.5x で点灯

**統計情報表示**:
```
最新値 | RVOL: 1.45x (閾値 1.3x) | Accel: 0.82% (閾値 0.5%) | Surge: ✓
```

### 2. `plot_rvol_analysis.py` - 過去データ分析プロット

**目的**: 過去のRVOLデータを統計的に分析し、急騰初期検知の有効性を検証

**実行方法**:
```bash
# XAUUSD の過去24時間を分析
python plot_rvol_analysis.py --symbol XAUUSD --hours 24

# BTCUSD の過去72時間を分析、カスタム出力ディレクトリ
python plot_rvol_analysis.py --symbol BTCUSD --hours 72 --output ./analysis
```

**出力内容**:

1. **M5足価格 + RVOL（時系列）**
   - 黄色ライン: 終値
   - カラー棒グラフ: RVOL
   - 検知閾値ライン表示

2. **RVOL分布（ヒストグラム）**
   - 過去期間のRVOL範囲を可視化
   - 平均値・閾値ラインマーク

3. **価格加速度分析**
   - 短期SMA変化率の分布
   - 上昇/下降バー数

4. **統計情報パネル**
   - RVOL: 平均、中央値、最大/最小、標準偏差
   - 急騰初期検知: 該当本数・発生頻度
   - 高出来高本数・発生頻度
   - 価格加速度: 平均、上昇/下降本数

**出力ファイル例**:
```
./output/rvol_analysis_XAUUSD_24h.png
./output/rvol_analysis_BTCUSD_72h.png
```

## 関連パラメータ（config.py）

```python
INDICATOR = dict(
    # RVOL・急騰検知関連
    rvol_period               = 20,     # RVOL計算期間（本数）
    accel_period              = 5,      # 価格加速計算期間（本数）
    volume_surge_threshold    = 2.0,    # 出来高急増閾値（倍率）
    rvol_surge_threshold      = 1.5,    # RVOL急増閾値
    early_surge_rvol_threshold = 1.3,   # ★急騰初期RVOL閾値
    early_surge_accel_threshold = 0.5,  # ★急騰初期価格加速閾値
    surge_overbought_threshold = 70.0,  # 急騰中段階RSI閾値
    surge_avoid_accel_threshold = 1.5,  # 急騰回避価格加速閾値
)
```

## 急騰初期検知の論理

```
is_early_surge = (
    RVOL >= 1.3 AND                    # 出来高が1.3倍以上
    Price_Accel >= 0.5% AND            # 価格加速が0.5%以上
    Volume_Surge == True               # 出来高が2倍以上かつRVOL1.5倍以上
)
```

## エントリール変更（mt5_ea_bridge.py）

急騰初期検知時のエントリー判定:

- **BUY**: 急騰兆候時も信頼度0.3以上で強制許可
- **SELL**: 急騰初期（信頼度60%以上）でのみ許可
- **ロット**: 調整なし（フル標準ロット）

## 実装例

### リアルタイム監視中にエントリー判定を確認

```bash
# ターミナル1: リアルタイム監視開始
python monitor_rvol.py --symbol XAUUSD --minutes 120

# ターミナル2: 通常モードのブリッジ起動
python mt5_ea_bridge.py --mode normal --symbol XAUUSD
```

グラフを見ながらブリッジのログを確認することで、RVOLと実際のエントリー判定の相関性を検証できます。

### 過去データの統計分析

```bash
# 直近24時間のRVOLデータで急騰初期検知の有効性を検証
python plot_rvol_analysis.py --symbol XAUUSD --hours 24

# 出力ファイル: ./output/rvol_analysis_XAUUSD_24h.png
# → 急騰初期検知の発生頻度や精度を確認できます
```

## トラブルシューティング

### グラフが表示されない場合
- バックエンド: `TkAgg` を使用（Windows推奨）
- 必要パッケージ: `matplotlib`, `numpy`, `pandas`
- インストール: `pip install matplotlib numpy pandas`

### MT5接続エラー
- MT5ターミナルを起動・ログイン
- または `config.py` の `MT5['login']` / `password` / `server` を設定

### データが古い
- M5データの遅延は通常5秒程度
- リアルタイム性が必要な場合、M1足を使用検討

## パフォーマンス最適化

**リアルタイム監視**:
- 更新間隔を10秒以上推奨（CPU負荷軽減）
- `--minutes 120` 程度が最適（メモリ効率）

**分析プロット**:
- `--hours 24` 推奨（統計的信頼性）
- 複数シンボル分析時は実行を分ける

---

**最終更新**: 2026-05-04  
**対応シンボル**: BTCUSD, XAUUSD, その他MT5対応通貨ペア
