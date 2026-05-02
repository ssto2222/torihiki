"""
analyze_time_bias.py — 時間帯別の勝率・損益を集計し危険時間帯を自動検出する
========================================================================
実行:
    python analyze_time_bias.py

出力:
    output/time_bias.json  ← mt5_ea_bridge.py が読み込む

注意: 時間帯はブローカーサーバー時間（MT5 が返す raw タイムスタンプ）を使用。
      バックテストと bridge で同じ時間軸になるため UTC オフセット調整は不要。
"""
import sys, json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import load_data, generate_m5_from_h1
from core.indicators import add_h1_indicators, add_d1_rsi_to_h1, add_m1_indicators, add_m5_indicators
from core.strategy   import run_backtest, AtrSL

CFG = {k: getattr(C, k) for k in
       ['MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES',
        'OPTIMIZE', 'LOCAL', 'PLOT', 'BRIDGE', 'SCALP']}

TB           = getattr(C, 'TIME_BIAS', {})
MIN_N        = TB.get('min_trades_per_hour',  5)
WR_THR       = TB.get('danger_win_rate_thr', 0.40)
APNL_THR     = TB.get('danger_avg_pnl',      0.0)

# ── データ取得 ────────────────────────────────────────────────────
print(f"データ取得中 (H1 {CFG['MT5']['h1_bars']}本 ≈ 7ヶ月)...")
df_h1_raw, df_m1_raw, is_real = load_data(CFG, force_synthetic=False)
src = "MT5実データ" if is_real else "合成データ（MT5未接続）"
print(f"  ソース: {src}")

df_m5_raw = generate_m5_from_h1(df_h1_raw)
df_h1 = add_h1_indicators(df_h1_raw, CFG)
df_h1 = add_d1_rsi_to_h1(df_h1, CFG)
df_m1 = add_m1_indicators(df_m1_raw, CFG)
df_m5 = add_m5_indicators(df_m5_raw, CFG)
print(f"  H1: {len(df_h1)}本  期間: {df_h1.index[0]} 〜 {df_h1.index[-1]}\n")

# ── バックテスト（BUY + SELL 両方向）────────────────────────────
sl_m  = CFG['SL']['sl_multi']
strat = AtrSL(multi=sl_m)
all_trades = []
for direction in ('buy', 'sell'):
    res = run_backtest(df_h1, df_m1, strat, CFG['SIGNAL'], CFG,
                       direction=direction, df_m5=df_m5)
    all_trades.extend(res.get('trades', []))
print(f"総トレード数: {len(all_trades)}\n")

# ── 時間帯別集計 ─────────────────────────────────────────────────
hour_pnls = defaultdict(list)
for t in all_trades:
    h = int(t['entry_time'].hour)
    hour_pnls[h].append(t['pnl'])

# ── 統計 + 危険時間帯判定 ──────────────────────────────────────
print(f"{'時':>4} {'N':>5} {'勝率%':>7} {'平均損益':>10} {'判定'}")
print("-" * 50)

hour_stats   = {}
danger_hours = []

for h in range(24):
    pnls = hour_pnls.get(h, [])
    n    = len(pnls)
    if n == 0:
        hour_stats[h] = {'n': 0, 'win_rate': None, 'avg_pnl': None, 'is_danger': False}
        print(f"{h:>2}:00  {'--':>5}  {'--':>7}  {'--':>10}")
        continue

    wins  = sum(1 for p in pnls if p > 0)
    wr    = wins / n
    apnl  = float(np.mean(pnls))

    # 危険判定: 十分なサンプルかつ（勝率低い OR 期待値マイナス）
    is_danger = n >= MIN_N and (wr < WR_THR or apnl < APNL_THR)
    if is_danger:
        danger_hours.append(h)
        flag = '  <-- DANGER'
    else:
        flag = ''

    hour_stats[h] = {
        'n':         n,
        'win_rate':  round(wr,   3),
        'avg_pnl':   round(apnl, 2),
        'is_danger': is_danger,
    }
    print(f"{h:>2}:00  {n:>5d}  {wr*100:>6.1f}%  {apnl:>+10.2f}{flag}")

print("-" * 50)
print(f"\n危険時間帯 ({len(danger_hours)}個): {danger_hours}")
print(f"  判定基準: N≥{MIN_N} かつ (勝率<{WR_THR*100:.0f}% または 平均損益<{APNL_THR})")

# ── 保存 ─────────────────────────────────────────────────────────
Path("output").mkdir(exist_ok=True)
result = {
    "generated":        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    "symbol":           CFG['MT5']['symbol'],
    "data_source":      src,
    "n_trades_total":   len(all_trades),
    "danger_hours":     danger_hours,
    "params": {
        "danger_win_rate_thr":  WR_THR,
        "danger_avg_pnl":       APNL_THR,
        "min_trades_per_hour":  MIN_N,
    },
    "hour_stats": {str(h): v for h, v in hour_stats.items()},
}
out_path = "output/time_bias.json"
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"\n出力: {out_path}")
print("次のステップ: python mt5_ea_bridge.py  （TIME_BIAS.enabled=True で自動適用）")
