"""
mt5_backtest.py — MT5 実データ バックテスト
=============================================
Windows + MetaTrader5 ターミナル起動状態で実行。

実行:
    python mt5_backtest.py
    python mt5_backtest.py --symbol XAUUSD --h1 3000 --m1 50000
    python mt5_backtest.py --symbol GOLD   --output ./result
"""
import sys, json, time, argparse
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import load_data
from core.indicators import add_h1_indicators, add_m1_indicators, detect_crash_events
from core.strategy   import detect_h1_signals, get_all_strategies, run_backtest
from core.plot       import plot_crash_analysis, plot_sl_comparison


def main(args):
    cfg = {k: getattr(C, k) for k in
           ['MT5','INDICATOR','SIGNAL','EXECUTION','SL','CRASH','OPTIMIZE','LOCAL','PLOT','BRIDGE']}
    cfg['MT5'] = {**cfg['MT5'], 'symbol': args.symbol,
                  'h1_bars': args.h1, 'm1_bars': args.m1}
    out = args.output
    Path(out).mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  XAUUSD MT5実データ バックテスト  [{cfg['MT5']['symbol']}]")
    print("=" * 60)
    t0 = time.time()

    # 1. データ取得
    print("\n[1] MT5 データ取得")
    df_h1_raw, df_m1_raw, is_real = load_data(cfg, force_synthetic=False)
    src = "MT5実データ" if is_real else "合成データ（MT5未接続）"
    print(f"  ソース: {src}")

    # 2. 指標
    print("\n[2] 指標計算")
    df_h1 = add_h1_indicators(df_h1_raw, cfg)
    df_m1 = add_m1_indicators(df_m1_raw, cfg)
    atr_a = df_h1['ATR'].mean()
    print(f"  H1: {len(df_h1)}本  ATR avg=${atr_a:.2f}  "
          f"${df_h1['Close'].min():,.0f}〜${df_h1['Close'].max():,.0f}")
    print(f"  M1: {len(df_m1)}本")

    # 3. 急落検出
    print("\n[3] 急落イベント検出")
    df_crashes = detect_crash_events(df_h1, df_m1, cfg)
    crash_set  = set(df_crashes['bar'].tolist()) if not df_crashes.empty else set()

    # 4. 買い/売り × 5戦略
    print("\n[4] SL戦略バックテスト（買い/売り × 5戦略）")
    sig_p   = cfg['SIGNAL']
    strats  = get_all_strategies(cfg)
    results = {'buy': [], 'sell': []}

    for direction in ['buy', 'sell']:
        lbl = '買い' if direction == 'buy' else '売り'
        print(f"\n  ── {lbl} ──")
        for strat in strats:
            res = run_backtest(df_h1, df_m1, strat, sig_p, cfg,
                               direction=direction, crash_bar_set=crash_set)
            results[direction].append(res)
            rc = res.get('reason_counts', {})
            print(f"  {res['strategy'][:30]:<30} "
                  f"n={res['n_trades']:3d}  {res['total_pnl']:+7.1f}pips  "
                  f"wr={res['win_rate']*100:5.1f}%  PF={res['profit_factor']:.2f}  "
                  f"slip=${res['avg_slip_usd']:.2f}  "
                  f"crash_surv={res['crash_survival']*100:.0f}%")

    # 5. グラフ（買いのみ）
    print("\n[5] グラフ出力")
    plot_crash_analysis(df_h1, df_crashes, cfg, out)
    plot_sl_comparison(results['buy'], df_h1, df_crashes, cfg, out)

    # 6. JSON
    def san(v):
        if isinstance(v, (np.integer,)):  return int(v)
        if isinstance(v, (np.floating,)): return float(v)
        return v

    out_json = {
        'symbol':    cfg['MT5']['symbol'],
        'is_real':   is_real,
        'period':    f"{df_h1.index[0]} 〜 {df_h1.index[-1]}",
        'atr_avg':   round(float(atr_a), 3),
        'n_crashes': len(df_crashes),
        'results': {
            d: [{k: san(v) for k, v in r.items()
                 if k not in ('trades','equity','pnls')}
                for r in rs]
            for d, rs in results.items()
        },
        # MT5 EA に直接コピペできるパラメータ
        'mt5_ea_params': {
            'SL_ATR_multi_low':    cfg['SL']['sl_multi_low'],
            'SL_ATR_multi_normal': cfg['SL']['sl_multi_normal'],
            'SL_ATR_multi_medium': cfg['SL']['sl_multi_medium'],
            'SL_ATR_multi_high':   cfg['SL']['sl_multi_high'],
            'ATR_ratio_thr_medium':cfg['SL']['atr_ratio_medium'],
            'ATR_ratio_thr_high':  cfg['SL']['atr_ratio_high'],
            'RSI_exit_thr':        cfg['SL']['rsi_exit_thr'],
            'Trail_ATR_multi':     cfg['SL']['trail_multi'],
            'Max_deviation_pt':    cfg['MT5']['deviation'],
            'MagicNumber':         cfg['MT5']['magic'],
        },
    }
    jp = f"{out}/mt5_backtest.json"
    with open(jp, 'w', encoding='utf-8') as f:
        json.dump(out_json, f, ensure_ascii=False, indent=2, default=str)
    print(f"[出力] {jp}")

    print(f"\n{'='*60}")
    print(f"  完了  ({time.time()-t0:.1f}秒)  ソース: {src}")
    print(f"  出力先: {out}/")
    print(f"    sl_crash_analysis.png  急落イベント分析")
    print(f"    sl_comparison.png      SL戦略比較（買い）")
    print(f"    mt5_backtest.json      数値結果 + EA パラメータ")
    print(f"{'='*60}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='XAUUSD MT5実データバックテスト')
    ap.add_argument('--symbol', default=C.MT5['symbol'], help='シンボル名')
    ap.add_argument('--h1',     type=int, default=C.MT5['h1_bars'], help='H1本数')
    ap.add_argument('--m1',     type=int, default=C.MT5['m1_bars'], help='M1本数')
    ap.add_argument('--output', default='./output', help='出力先ディレクトリ')
    main(ap.parse_args())
