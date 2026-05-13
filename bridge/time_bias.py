"""bridge/time_bias.py — 時間帯バイアス分析・ロード"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np


def _build_time_bias(cfg: dict) -> set:
    """
    バックテストを実行して時間帯別統計を計算し time_bias.json を更新する。
    危険時間帯のセットを返す。
    """
    from core.data       import load_data, generate_m5_from_h1
    from core.indicators import add_d1_rsi_to_h1, add_m1_indicators, add_h1_indicators, add_m5_indicators
    from core.strategy   import run_backtest, AtrSL

    tb_cfg   = cfg.get('TIME_BIAS', {})
    MIN_N    = tb_cfg.get('min_trades_per_hour',  5)
    WR_THR   = tb_cfg.get('danger_win_rate_thr', 0.40)
    APNL_THR = tb_cfg.get('danger_avg_pnl',      0.0)
    out_path = tb_cfg.get('bias_file', './output/time_bias.json')

    print("[時間帯バイアス] 再分析を開始...")
    try:
        df_h1_raw, df_m1_raw, is_real = load_data(cfg, force_synthetic=False)
        df_m5_raw = generate_m5_from_h1(df_h1_raw)
        df_h1 = add_h1_indicators(df_h1_raw, cfg)
        df_h1 = add_d1_rsi_to_h1(df_h1, cfg)
        df_m1 = add_m1_indicators(df_m1_raw, cfg)
        df_m5 = add_m5_indicators(df_m5_raw, cfg)

        strat = AtrSL(multi=cfg['SL']['sl_multi'])
        all_trades: list = []
        for direction in ('buy', 'sell'):
            res = run_backtest(df_h1, df_m1, strat, cfg['SIGNAL'], cfg,
                               direction=direction, df_m5=df_m5)
            all_trades.extend(res.get('trades', []))

        time_ref  = tb_cfg.get('time_ref', 'exit')
        hour_pnls: dict = defaultdict(list)
        for t in all_trades:
            key_time = t.get('exit_time', t['entry_time']) if time_ref == 'exit' else t['entry_time']
            hour_pnls[int(key_time.hour)].append(t['pnl'])

        hour_stats   = {}
        danger_hours = []
        for h in range(24):
            pnls = hour_pnls.get(h, [])
            n    = len(pnls)
            if n == 0:
                hour_stats[h] = {'n': 0, 'win_rate': None, 'avg_pnl': None, 'is_danger': False}
                continue
            wins      = sum(1 for p in pnls if p > 0)
            wr        = wins / n
            apnl      = float(np.mean(pnls))
            is_danger = n >= MIN_N and (wr < WR_THR or apnl < APNL_THR)
            if is_danger:
                danger_hours.append(h)
            hour_stats[h] = {'n': n, 'win_rate': round(wr, 3),
                             'avg_pnl': round(apnl, 2), 'is_danger': is_danger}

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        result = {
            'generated':      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol':         cfg['MT5']['symbol'],
            'data_source':    'MT5実データ' if is_real else '合成データ',
            'n_trades_total': len(all_trades),
            'danger_hours':   danger_hours,
            'params':         {'danger_win_rate_thr': WR_THR, 'danger_avg_pnl': APNL_THR,
                               'min_trades_per_hour': MIN_N},
            'hour_stats':     {str(h): v for h, v in hour_stats.items()},
        }
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        src = 'MT5実データ' if is_real else '合成データ'
        print(f"[時間帯バイアス] 完了 ({src}, {len(all_trades)}トレード)"
              f" → 危険時間帯: {danger_hours}")
        return set(danger_hours)

    except Exception as e:
        print(f"[時間帯バイアス] 分析エラー: {e}")
        return set()


def _load_time_bias(path: str) -> set:
    """time_bias.json から危険時間帯セットを返す。ファイルがなければ空セット。"""
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return set(int(h) for h in d.get('danger_hours', []))
    except FileNotFoundError:
        return set()
    except Exception as e:
        print(f"[時間帯バイアス] {path} 読み込みエラー: {e}")
        return set()
