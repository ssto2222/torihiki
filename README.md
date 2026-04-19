# XAUUSD 自動売買システム

## ディレクトリ構成

```
xauusd_system/
├── config.py              ★ 全設定の一元管理
├── local_analysis.py      ローカル分析（MT5不要）
├── mt5_backtest.py        MT5実データ バックテスト
├── mt5_ea_bridge.py       MT5 EA リアルタイム連携
├── core/
│   ├── data.py            データ取得（MT5 / 合成）
│   ├── indicators.py      テクニカル指標・急落検出
│   ├── strategy.py        シグナル検出・SL戦略・バックテスト
│   └── plot.py            可視化
├── mt5/
│   └── XAUUSD_SL_Strategy.mq5   MT5 EA（MQL5）
└── output/                出力先
    ├── signal.json         Python→EA シグナル
    ├── ea_state.json       EA→Python 状態
    ├── sl_crash_analysis.png
    ├── sl_comparison.png
    └── *.json
```

## 実行方法

### A. ローカル分析（MT5 不要）

```bash
pip install pandas numpy matplotlib scipy
cd xauusd_system

python local_analysis.py                    # 分析のみ
python local_analysis.py --optimize         # 分析 + グリッド最適化
python local_analysis.py --output ./result  # 出力先変更
```

### B. MT5 実データ バックテスト

```bash
pip install MetaTrader5 pandas numpy matplotlib scipy

# MT5 ターミナルを起動した状態で実行
python mt5_backtest.py
python mt5_backtest.py --symbol XAUUSD --h1 3000 --m1 50000
python mt5_backtest.py --symbol GOLD   --output ./gold_result
```

### C. MT5 EA リアルタイム連携

```bash
# 1. Python ブリッジを起動（MT5ターミナル起動状態で）
python mt5_ea_bridge.py
python mt5_ea_bridge.py --once    # 1回だけ確認

# 2. MT5 に EA を設置
#    mt5/XAUUSD_SL_Strategy.mq5 → MQL5/Experts/ にコピー
#    MT5 でコンパイル（F7）
#    チャートにアタッチ
#    InpSignalFile にフルパスを設定（例: C:\xauusd_system\output\signal.json）
```

## config.py の主要設定

```python
MT5 = dict(
    symbol = "XAUUSD",   # ← ブローカーにより GOLD / XAUUSDm 等に変更
)

SL = dict(
    rsi_exit_thr    = 75.0,   # RSI≥75 でトレーリングSL起動（買い）
    trail_multi     = 1.5,    # トレーリング幅 = ATR × 1.5
    sl_multi_low    = 1.0,    # ATR_ratio < 0.8（低ボラ）
    sl_multi_normal = 1.5,    # ATR_ratio 0.8〜1.5（通常）
    sl_multi_medium = 2.5,    # ATR_ratio 1.5〜2.5（高ボラ）
    sl_multi_high   = 4.0,    # ATR_ratio > 2.5（急落）
)

CRASH = dict(
    atr_multi = 2.5,   # 1本下落幅 > ATR×2.5 → 急落検出
    gap_usd   = 8.0,   # ギャップダウン > $8 → 急落検出
    vol_spike = 2.0,   # ATR_ratio > 2.0 → ボラスパイク
)
```

## SL 戦略一覧

| 戦略 | 説明 |
|---|---|
| A. 固定SL | USD絶対値（ベースライン） |
| B. ATR×1.5 SL | ATR倍率（ボラ追従） |
| C. 構造的SL | スイング安値/高値ベース |
| D. 二段階SL | 急落時 ATR×2.5 に自動拡大 |
| **E. ボラ適応型SL** ★推奨 | ATR_ratio で動的調整 |

## 通信プロトコル

```
[Python ブリッジ]          [MT5 EA]
      │                       │
      │ signal.json を書き込む │
      │ ──────────────────→  │ OnTimer() で読み込み
      │                       │ エントリー / SL更新
      │ ea_state.json を読む  │
      │ ←──────────────────  │ 残高・ポジション数
```

### signal.json フォーマット

```json
{
  "timestamp":      "2024-01-15T03:00:00+00:00",
  "symbol":         "XAUUSD",
  "close":          1950.20,
  "atr":            12.34,
  "atr_ratio":      1.05,
  "rsi":            42.1,
  "sl_multi":       1.5,
  "action":         "buy",
  "sl_price":       1931.70,
  "tp_price":       1987.22,
  "rsi_exit_thr":   75.0,
  "trail_multi":    1.5,
  "max_slip_pt":    617,
  "signal_active":  true,
  "n_signals_buy":  1,
  "n_signals_sell": 0
}
```
