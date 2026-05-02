"""
analyze_risk.py — SL幅別のバックテストからケリー基準で適正リスク率を算出する
"""
import sys, json
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import load_data, generate_m5_from_h1
from core.indicators import add_h1_indicators, add_d1_rsi_to_h1, add_m1_indicators, add_m5_indicators
from core.strategy   import run_backtest, AtrSL

CFG = {k: getattr(C, k) for k in
       ['MT5','INDICATOR','SIGNAL','EXECUTION','SL','RULES','OPTIMIZE','LOCAL','PLOT','BRIDGE','SCALP']}

# ── データ生成 ────────────────────────────────────────────────
print("データ生成中...")
df_h1_raw, df_m1_raw, _ = load_data(CFG, force_synthetic=True)
df_m5_raw  = generate_m5_from_h1(df_h1_raw)
df_h1      = add_h1_indicators(df_h1_raw, CFG)
df_h1      = add_d1_rsi_to_h1(df_h1, CFG)
df_m1      = add_m1_indicators(df_m1_raw, CFG)
df_m5      = add_m5_indicators(df_m5_raw, CFG)

ATR_AVG = float(df_h1['ATR'].mean())
print(f"H1: {len(df_h1)}本  ATR平均 ${ATR_AVG:.2f}\n")

# ── SL幅 × TP幅 グリッドサーチ ─────────────────────────────────
SL_MULTIS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
TP_MULTIS = [2.0, 2.5, 3.0, 3.5, 4.0]

print(f"{'SL_m':>5} {'TP_m':>5} {'n':>5} {'WR%':>7} {'PF':>6} {'Sharpe':>8} "
      f"{'SL_hit%':>8} {'SL_dist$':>9} {'Kelly%':>7} {'HalfK%':>7} {'QK%':>5}")
print("-" * 90)

best_sharpe = -999
best_row    = None
all_rows    = []

for sl_m in SL_MULTIS:
    for tp_m in TP_MULTIS:
        cfg_t = {**CFG,
                 'SL': {**CFG['SL'], 'sl_multi': sl_m, 'tp_atr_multi': tp_m}}
        strat = AtrSL(multi=sl_m)
        res   = run_backtest(df_h1, df_m1, strat, CFG['SIGNAL'], cfg_t,
                             direction='buy', df_m5=df_m5)

        n = res['n_trades']
        if n < 10:
            continue

        p  = res['win_rate']
        pf = res['profit_factor']
        sh = res.get('sharpe', 0.0)

        # R = avg_win / avg_loss = PF × (1-p) / p
        R      = (pf * (1 - p) / p) if p > 0 else 0
        kelly  = float(np.clip(p - (1 - p) / R, 0, 1)) if R > 0 else 0
        half_k = kelly / 2
        qk     = kelly / 4

        row = dict(
            sl_multi   = sl_m,
            tp_multi   = tp_m,
            n_trades   = n,
            win_rate   = p,
            profit_factor = pf,
            sharpe     = sh,
            sl_hit_rate   = res['sl_hit_rate'],
            avg_sl_dist   = res['avg_sl_dist'],
            kelly      = kelly,
            half_kelly = half_k,
            quarter_kelly = qk,
            max_dd     = res['max_dd'],
            total_pnl  = res['total_pnl'],
        )
        all_rows.append(row)

        marker = ' ◀ BEST' if sh > best_sharpe else ''
        if sh > best_sharpe:
            best_sharpe = sh
            best_row    = row

        print(f"{sl_m:>5.2f} {tp_m:>5.1f} {n:>5d} "
              f"{p*100:>7.1f} {pf:>6.2f} {sh:>8.2f} "
              f"{res['sl_hit_rate']*100:>8.1f} {res['avg_sl_dist']:>9.1f} "
              f"{kelly*100:>7.1f} {half_k*100:>7.1f} {qk*100:>5.1f}{marker}")

# ── 推奨値の算出 ─────────────────────────────────────────────
print("\n" + "=" * 90)
print("▶ 最高シャープレシオ  "
      f"SL={best_row['sl_multi']}×ATR  TP={best_row['tp_multi']}×ATR  "
      f"WR={best_row['win_rate']*100:.1f}%  PF={best_row['profit_factor']:.2f}  "
      f"Sharpe={best_row['sharpe']:.2f}")
print(f"  Kelly={best_row['kelly']*100:.1f}%  "
      f"Half-Kelly={best_row['half_kelly']*100:.1f}%  "
      f"Quarter-Kelly={best_row['quarter_kelly']*100:.1f}%")

# 保守的な推奨: Quarter-Kelly、最大2%に丸める
recommended_risk   = min(0.02, round(best_row['quarter_kelly'], 3))
recommended_total  = round(recommended_risk * 10, 2)   # 最大10ポジション相当

# 現行より良い sl_multi を選ぶ
best_sl   = best_row['sl_multi']
best_tp   = best_row['tp_multi']

print(f"\n【推奨設定】")
print(f"  sl_multi       : {best_sl}  (現在: {CFG['SL']['sl_multi']})")
print(f"  tp_atr_multi   : {best_tp}  (現在: {CFG['SL']['tp_atr_multi']})")
print(f"  risk_pct       : {recommended_risk:.3f} = {recommended_risk*100:.1f}%/トレード  "
      f"(Quarter-Kelly={best_row['quarter_kelly']*100:.1f}%  現在: {CFG['BRIDGE']['risk_pct']*100:.1f}%)")
print(f"  total_risk_pct : {recommended_total:.2f} = {recommended_total*100:.0f}%  "
      f"(max_positions={int(recommended_total/recommended_risk)})")
print(f"  SL距離目安     : ${best_row['avg_sl_dist']:.1f}  (ATR={ATR_AVG:.1f} × {best_sl})")
print(f"  SL刈り率       : {best_row['sl_hit_rate']*100:.1f}%  "
      f"(現在比: 改善={best_row['sl_hit_rate'] < CFG.get('_sl_hit_ref', 0.61)})")

# ── JSON 保存 ─────────────────────────────────────────────────
Path("output").mkdir(exist_ok=True)
result = {
    "best": {k: float(v) for k, v in best_row.items()},
    "recommended": {
        "sl_multi":       best_sl,
        "tp_atr_multi":   best_tp,
        "risk_pct":       recommended_risk,
        "total_risk_pct": recommended_total,
        "max_positions":  int(recommended_total / recommended_risk),
    },
    "all_rows": [{k: float(v) for k, v in r.items()} for r in all_rows],
}
with open("output/risk_analysis.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("\n出力: output/risk_analysis.json")
