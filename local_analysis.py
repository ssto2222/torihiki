"""
local_analysis.py — ローカル分析エントリーポイント
====================================================
MT5 不要。合成データで全分析を実行する。

実行:
    cd xauusd_system
    python local_analysis.py                  # 分析のみ
    python local_analysis.py --optimize       # 分析 + グリッド最適化
    python local_analysis.py --output ./out   # 出力先変更
"""
import sys, json, time, argparse
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import load_data
from core.indicators import add_h1_indicators, add_m1_indicators, detect_crash_events
from core.strategy   import (detect_h1_signals, get_all_strategies,
                              run_backtest, VolAdaptiveSL)
from core.plot       import plot_crash_analysis, plot_sl_comparison

CFG = {k: getattr(C, k) for k in
       ['MT5','INDICATOR','SIGNAL','EXECUTION','SL','CRASH','OPTIMIZE','LOCAL','PLOT','BRIDGE']}


# ── グリッド最適化 ─────────────────────────────────────────

def _score(res, min_t=6):
    if res['n_trades'] < min_t: return -9999.0
    dd  = res['max_dd'] + 1e-9
    pf  = min(res['profit_factor'], 8.0)
    nw  = np.sqrt(res['n_trades']) / np.sqrt(max(min_t, 8))
    sh  = float(np.clip(res.get('sharpe', 0), -3, 8))
    return float((res['total_pnl'] / dd) * pf * (1 + sh * 0.05) * nw)


def optimize(df_h1, df_m1, direction, n_samples=None):
    rng = np.random.default_rng(CFG['OPTIMIZE']['seed'])
    ns  = n_samples or CFG['OPTIMIZE']['n_samples']

    grid = dict(
        rsi_thr    = [35, 38, 40, 42],
        bb_touch   = [0.75, 0.80, 0.85],
        lookback   = [20, 25, 35],
        depth_tol  = [4.0, 5.0, 7.0],
        neck_th    = [1.5, 2.0, 3.0],
        touch_m    = [0.10, 0.20, 0.40],
        rsi_offset = [15.0, 20.0, 25.0],
        rsi_exit   = [70.0, 73.0, 75.0, 78.0],
        trail_m    = [1.0, 1.5, 2.0, 2.5],
    )
    keys = list(grid.keys())
    vals = list(grid.values())

    best_sc, best_p, best_r = -9999, None, None
    upd = cnt = 0

    for _ in range(ns):
        raw = dict(zip(keys, [rng.choice(v) for v in vals]))

        if direction == 'buy':
            sig_p = dict(buy_rsi_thr=raw['rsi_thr'], buy_bb_touch=raw['bb_touch'],
                         db_lookback=raw['lookback'], db_min_int=2, db_max_int=16,
                         db_depth_tol=raw['depth_tol'], db_neck_rise=raw['neck_th'],
                         local_order=2)
        else:
            sig_p = dict(sell_rsi_thr=raw['rsi_thr'], sell_bb_touch=raw['bb_touch'],
                         dt_lookback=raw['lookback'], dt_min_int=2, dt_max_int=16,
                         dt_depth_tol=raw['depth_tol'], dt_neck_drop=raw['neck_th'],
                         local_order=2)

        cfg_t = {**CFG,
                 'EXECUTION': {**CFG['EXECUTION'],
                               'touch_margin': raw['touch_m'],
                               'm1_rsi_offset': raw['rsi_offset']},
                 'SL':        {**CFG['SL'],
                               'rsi_exit_thr': raw['rsi_exit'],
                               'trail_multi':  raw['trail_m']}}

        strat = VolAdaptiveSL(cfg=cfg_t)
        res   = run_backtest(df_h1, df_m1, strat, sig_p, cfg_t, direction)
        sc    = _score(res, CFG['OPTIMIZE']['min_trades'])
        cnt  += 1

        if sc > best_sc:
            best_sc = sc; best_p = raw; best_r = res; upd += 1
            print(f"  更新#{upd:>2} [{cnt:>4}] score={sc:8.2f}  "
                  f"pnl={res['total_pnl']:+7.1f}pips  "
                  f"n={res['n_trades']:2d}  wr={res['win_rate']*100:.0f}%  "
                  f"PF={min(res['profit_factor'],99):.2f}  "
                  f"DD={res['max_dd']:.1f}pips")

    print(f"  探索完了: {cnt}/{ns}")
    return {'params': best_p, 'result': best_r, 'score': best_sc}


# ── メイン ─────────────────────────────────────────────────

def main(args):
    out = args.output
    Path(out).mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  XAUUSD ローカル分析（合成データ）")
    print("=" * 60)
    t0 = time.time()

    # 1. データ
    print("\n[1] データ生成")
    df_h1_raw, df_m1_raw, is_real = load_data(CFG, force_synthetic=True)

    # 2. 指標
    print("\n[2] 指標計算")
    df_h1 = add_h1_indicators(df_h1_raw, CFG)
    df_m1 = add_m1_indicators(df_m1_raw, CFG)
    atr_a = df_h1['ATR'].mean()
    print(f"  H1: {len(df_h1)}本  ATR avg=${atr_a:.2f}  "
          f"${df_h1['Close'].min():,.0f}〜${df_h1['Close'].max():,.0f}")
    print(f"  M1: {len(df_m1)}本")

    # 3. 急落検出
    print("\n[3] 急落イベント検出")
    df_crashes = detect_crash_events(df_h1, df_m1, CFG)
    crash_set  = set(df_crashes['bar'].tolist()) if not df_crashes.empty else set()

    # 4. SL戦略バックテスト比較
    print("\n[4] SL戦略バックテスト（5戦略比較）")
    sig_p   = CFG['SIGNAL']
    strats  = get_all_strategies(CFG)
    results = []
    for strat in strats:
        res = run_backtest(df_h1, df_m1, strat, sig_p, CFG,
                           direction='buy', crash_bar_set=crash_set)
        results.append(res)
        rc = res.get('reason_counts', {})
        print(f"\n  {res['strategy']}")
        print(f"    n={res['n_trades']:3d}  pnl={res['total_pnl']:+8.1f}pips  "
              f"wr={res['win_rate']*100:5.1f}%  PF={res['profit_factor']:.2f}  "
              f"Sharpe={res.get('sharpe',0):.2f}")
        print(f"    SL刈り率={res['sl_hit_rate']*100:.1f}%  "
              f"スリップ率={res['slip_rate']*100:.1f}%  "
              f"avg_slip=${res['avg_slip_usd']:.2f}  "
              f"急落生存={res['crash_survival']*100:.1f}%  "
              f"最大連続損失={res['max_consec_loss']}回")
        print(f"    exit: {rc}  SL距離avg=${res['avg_sl_dist']:.1f}")

    # 5. 最適化（オプション）
    opt_results = {}
    if args.optimize:
        print(f"\n[5] パラメータ最適化（{CFG['OPTIMIZE']['n_samples']}サンプル）")
        for d in ['buy', 'sell']:
            lbl = '買い' if d == 'buy' else '売り'
            print(f"\n  [{lbl}]")
            t1 = time.time()
            opt_results[d] = optimize(df_h1, df_m1, d)
            print(f"  完了 {time.time()-t1:.1f}秒")
            p = opt_results[d]['params'] or {}
            r = opt_results[d]['result'] or {}
            if r:
                print(f"  → pnl={r.get('total_pnl',0):+.1f}pips  "
                      f"wr={r.get('win_rate',0)*100:.0f}%  PF={r.get('profit_factor',0):.2f}")
    else:
        print("\n[5] 最適化スキップ（--optimize で実行）")

    # 6. グラフ
    print("\n[6] グラフ出力")
    plot_crash_analysis(df_h1, df_crashes, CFG, out)
    plot_sl_comparison(results, df_h1, df_crashes, CFG, out)

    # 7. JSON
    def san(v):
        if isinstance(v, (np.integer,)):  return int(v)
        if isinstance(v, (np.floating,)): return float(v)
        return v

    output = {
        'source':   '合成データ',
        'period':   f"{df_h1.index[0]} 〜 {df_h1.index[-1]}",
        'atr_avg':  round(float(atr_a), 3),
        'n_crashes': len(df_crashes),
        'sl_results': [
            {k: san(v) for k, v in r.items()
             if k not in ('trades','equity','pnls')}
            for r in results
        ],
        'recommended_sl': {
            'strategy': 'E. ボラ適応型SL',
            'rules': {
                'ATR_ratio<0.8':    f"×{CFG['SL']['sl_multi_low']}",
                'ATR_ratio 0.8〜1.5': f"×{CFG['SL']['sl_multi_normal']}",
                'ATR_ratio 1.5〜2.5': f"×{CFG['SL']['sl_multi_medium']}",
                'ATR_ratio>2.5':    f"×{CFG['SL']['sl_multi_high']} (急落)",
            },
            'rsi_exit_thr':  CFG['SL']['rsi_exit_thr'],
            'trail_multi':   CFG['SL']['trail_multi'],
        },
    }
    if opt_results:
        output['optimization'] = {
            d: {
                'params':  {k: san(v) for k, v in (o['params'] or {}).items()},
                'metrics': {k: san(v) for k, v in (o['result'] or {}).items()
                            if k not in ('trades','equity','pnls')},
            }
            for d, o in opt_results.items()
        }

    jp = f"{out}/local_analysis.json"
    with open(jp, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"[出力] {jp}")

    print(f"\n{'='*60}")
    print(f"  完了  ({time.time()-t0:.1f}秒)")
    print(f"  出力先: {out}/")
    print(f"    sl_crash_analysis.png  急落イベント分析")
    print(f"    sl_comparison.png      SL戦略比較")
    print(f"    local_analysis.json    数値結果")
    print(f"{'='*60}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='XAUUSD ローカル分析')
    ap.add_argument('--optimize', action='store_true', help='グリッド最適化を実行')
    ap.add_argument('--output',   default='./output',  help='出力先ディレクトリ')
    main(ap.parse_args())
