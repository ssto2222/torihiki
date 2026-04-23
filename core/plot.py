"""core/plot.py — 可視化（急落分析・SL比較）"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
import warnings
warnings.filterwarnings('ignore')


def _setup(cfg):
    p = cfg.get('PLOT', {})
    matplotlib.rcParams['font.family']        = p.get('font_family', ['DejaVu Sans'])
    matplotlib.rcParams['axes.unicode_minus'] = False


def _sax(ax, cfg):
    p = cfg.get('PLOT', {})
    ax.set_facecolor(p.get('dark_bg', '#0d1117'))
    ax.tick_params(colors=p.get('muted', '#8b949e'), labelsize=8)
    for sp in ax.spines.values():
        sp.set_color(p.get('border', '#21262d'))


# ── 急落分析（4パネル）──────────────────────────────────────

def plot_crash_analysis(df_h1: pd.DataFrame, df_crashes: pd.DataFrame,
                         cfg: dict, out_dir: str = '.') -> str:
    _setup(cfg)
    p     = cfg.get('PLOT', {})
    DARK  = p.get('dark_bg', '#0d1117')
    TEXT  = p.get('text',    '#c9d1d9')
    MUTED = p.get('muted',   '#8b949e')
    GREEN = p.get('green',   '#3fb950')
    RED   = p.get('red',     '#f85149')
    YELLOW= p.get('yellow',  '#e3b341')
    BLUE  = p.get('blue',    '#58a6ff')
    ORANGE= p.get('orange',  '#f0883e')
    PURPLE= p.get('purple',  '#d2a8ff')
    PANEL = p.get('panel_bg','#161b22')
    BORD  = p.get('border',  '#21262d')

    fig = plt.figure(figsize=(20, 13))
    fig.patch.set_facecolor(DARK)
    gs  = gridspec.GridSpec(3, 2, hspace=0.45, wspace=0.32)
    s   = df_h1.index[0].strftime('%Y/%m/%d')
    e   = df_h1.index[-1].strftime('%Y/%m/%d')
    fig.suptitle(f"XAUUSD 急落イベント分析  {s} 〜 {e}",
                 color=TEXT, fontsize=13, y=0.99)

    # 価格チャート + 急落マーク
    ax = fig.add_subplot(gs[0, :])
    _sax(ax, cfg)
    n_show = min(1500, len(df_h1))
    sub    = df_h1.tail(n_show)
    x      = np.arange(len(sub))
    off    = len(df_h1) - n_show
    ax.plot(x, sub['Close'].values, color=YELLOW, lw=0.8, alpha=0.9, label='Close')
    ax.plot(x, sub['EMA21'].values, color=BLUE,   lw=0.9, alpha=0.6, label='EMA21')
    if 'BB_upper' in sub.columns:
        ax.plot(x, sub['BB_upper'].values, color=RED,   lw=0.7, ls='--', alpha=0.4)
        ax.plot(x, sub['BB_lower'].values, color=GREEN, lw=0.7, ls='--', alpha=0.4)
    if not df_crashes.empty:
        clr = {'drop': RED, 'gap': ORANGE, 'spike': PURPLE}
        for _, cr in df_crashes.iterrows():
            ri = int(cr['bar']) - off
            if 0 <= ri < len(sub):
                c = clr.get(cr['cause'], RED)
                ax.axvline(ri, color=c, alpha=0.25, lw=0.8)
                ax.scatter(ri, sub['Low'].iloc[ri], marker='v', s=30, color=c, zorder=5)
    elems = [
        Line2D([0],[0], color=YELLOW, lw=1.5, label='Close'),
        Line2D([0],[0], color=BLUE,   lw=1.0, label='EMA21'),
        mpatches.Patch(color=RED,    alpha=0.5, label='急落(下落幅)'),
        mpatches.Patch(color=ORANGE, alpha=0.5, label='急落(ギャップ)'),
        mpatches.Patch(color=PURPLE, alpha=0.5, label='急落(ボラ急騰)'),
    ]
    ax.legend(handles=elems, facecolor=PANEL, edgecolor=BORD,
              labelcolor=TEXT, fontsize=8, loc='upper left')
    ax.set_title('価格チャート + 急落イベント（▼マーク）', color=TEXT, fontsize=10)
    ax.set_ylabel('価格 (USD)', color=MUTED, fontsize=9)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v,_: f'${v:,.0f}'))

    # 急落幅/ATR分布
    ax = fig.add_subplot(gs[1, 0])
    _sax(ax, cfg)
    thr = cfg.get('CRASH', {}).get('atr_multi', 2.5)
    if not df_crashes.empty:
        vals = df_crashes['drop_atr'].clip(0, 8)
        ax.hist(vals, bins=25, color=RED, alpha=0.75, edgecolor=DARK)
        ax.axvline(thr, color=YELLOW, lw=1.5, ls='--', label=f'検出閾値 ATR×{thr}')
        for pc, lb in [(75,'p75'), (90,'p90'), (95,'p95')]:
            v = np.percentile(vals, pc)
            ax.axvline(v, color=MUTED, lw=0.8, ls=':', alpha=0.7, label=f'{lb}={v:.1f}')
    ax.set_title('急落幅 / ATR 分布', color=TEXT, fontsize=10)
    ax.set_xlabel('急落幅 (ATR倍率)', color=MUTED, fontsize=9)
    ax.set_ylabel('件数', color=MUTED, fontsize=9)
    ax.legend(facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=8)

    # 急落後の回復分布
    ax = fig.add_subplot(gs[1, 1])
    _sax(ax, cfg)
    if not df_crashes.empty:
        for col, lb, c in [('recovery_3h','3H後',BLUE),
                            ('recovery_6h','6H後',YELLOW),
                            ('recovery_12h','12H後',GREEN)]:
            v = df_crashes[col].clip(-15, 15)
            ax.hist(v, bins=20, alpha=0.55, color=c,
                    label=f'{lb} avg={v.mean():+.2f}%')
        ax.axvline(0, color=MUTED, lw=0.8, ls='--')
    ax.set_title('急落後の価格回復分布', color=TEXT, fontsize=10)
    ax.set_xlabel('回復率 (%)', color=MUTED, fontsize=9)
    ax.legend(facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=8)

    # ATR_ratio 推移 + SL幅連動
    ax = fig.add_subplot(gs[2, :])
    _sax(ax, cfg)
    ratio = sub['ATR_ratio'].values
    ax.plot(x, ratio, color=ORANGE, lw=0.8, alpha=0.8, label='ATR比率')
    spk = cfg.get('CRASH', {}).get('vol_spike', 2.0)
    ax.axhline(spk, color=RED,  lw=1.0, ls='--', label=f'急落閾値 {spk}')
    ax.axhline(1.0, color=MUTED, lw=0.5, alpha=0.3)
    ax.fill_between(x, ratio, spk, where=ratio > spk,
                    alpha=0.20, color=RED, label='急落ゾーン')
    sl_c = cfg.get('SL', {})
    sl_m = np.where(ratio > sl_c.get('atr_ratio_high', 2.5),  sl_c.get('sl_multi_high', 4.0),
           np.where(ratio > sl_c.get('atr_ratio_medium', 1.5), sl_c.get('sl_multi_medium', 2.5),
           np.where(ratio > 0.8, sl_c.get('sl_multi_normal', 1.5),
                    sl_c.get('sl_multi_low', 1.0))))
    ax2 = ax.twinx()
    ax2.plot(x, sl_m, color=GREEN, lw=1.2, alpha=0.7, label='SL幅(ATR×)')
    ax2.set_ylabel('SL幅(ATR倍率)', color=GREEN, fontsize=8)
    ax2.tick_params(colors=GREEN, labelsize=7)
    ax2.set_facecolor(DARK)
    for sp in ax2.spines.values(): sp.set_color(BORD)
    l1, b1 = ax.get_legend_handles_labels()
    l2, b2 = ax2.get_legend_handles_labels()
    ax.legend(l1+l2, b1+b2, facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=8)
    ax.set_title('ATR比率推移（急落ゾーンでSL自動拡大）', color=TEXT, fontsize=10)
    ax.set_xlabel('バー番号', color=MUTED, fontsize=9)
    ax.set_ylabel('ATR比率', color=MUTED, fontsize=9)

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = f"{out_dir}/sl_crash_analysis.png"
    plt.tight_layout()
    plt.savefig(path, dpi=p.get('dpi', 150), bbox_inches='tight', facecolor=DARK)
    plt.close()
    print(f"[出力] {path}")
    return path


# ── SL比較ダッシュボード（6パネル）───────────────────────────

def plot_sl_comparison(results: list[dict], df_h1: pd.DataFrame,
                        df_crashes,
                        cfg: dict, out_dir: str = '.') -> str:
    if df_crashes is None:
        df_crashes = pd.DataFrame()
    _setup(cfg)
    p     = cfg.get('PLOT', {})
    DARK  = p.get('dark_bg', '#0d1117')
    TEXT  = p.get('text',    '#c9d1d9')
    MUTED = p.get('muted',   '#8b949e')
    GREEN = p.get('green',   '#3fb950')
    RED   = p.get('red',     '#f85149')
    BLUE  = p.get('blue',    '#58a6ff')
    YELLOW= p.get('yellow',  '#e3b341')
    PANEL = p.get('panel_bg','#161b22')
    BORD  = p.get('border',  '#21262d')

    atr_a = float(df_h1['ATR'].mean())
    fig   = plt.figure(figsize=(22, 16))
    fig.patch.set_facecolor(DARK)
    gs    = gridspec.GridSpec(3, 3, hspace=0.45, wspace=0.35)
    s     = df_h1.index[0].strftime('%Y/%m/%d')
    e     = df_h1.index[-1].strftime('%Y/%m/%d')
    fig.suptitle(f"XAUUSD SL戦略比較  ATR平均=${atr_a:.2f}  "
                 f"急落{len(df_crashes)}件  {s}〜{e}",
                 color=TEXT, fontsize=13, y=0.99)

    # エクイティカーブ
    ax = fig.add_subplot(gs[0, :2])
    _sax(ax, cfg)
    for res in results:
        if res.get('equity'):
            eq = np.array(res['equity'])
            ax.plot(eq, color=res['color'], lw=1.6, alpha=0.85,
                    label=f"{res['strategy']}  {res['total_pnl']:+.0f}pips  "
                          f"wr={res['win_rate']*100:.0f}%")
    ax.axhline(0, color=MUTED, lw=0.5, ls='--')
    ax.set_title('エクイティカーブ比較', color=TEXT, fontsize=10)
    ax.set_xlabel('トレード番号', color=MUTED, fontsize=9)
    ax.set_ylabel('累積損益 (pips)', color=MUTED, fontsize=9)
    ax.legend(facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=8)

    # スコアカード
    ax = fig.add_subplot(gs[0, 2])
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values(): sp.set_color(BORD)
    ax.axis('off')
    ax.set_title('スコアカード', color=TEXT, fontsize=10)
    metrics = [
        ('total_pnl',      '総利益(pips)',  '{:+.0f}',  True),
        ('win_rate',       '勝率',          '{:.1%}',   True),
        ('profit_factor',  'PF',            '{:.2f}',   True),
        ('max_dd',         '最大DD(pips)',  '{:.0f}',   False),
        ('sl_hit_rate',    'SL刈り率',      '{:.1%}',   False),
        ('slip_rate',      'スリップ率',    '{:.1%}',   False),
        ('avg_slip_usd',   '平均スリップ$', '{:.2f}',   False),
        ('avg_sl_dist',    'SL距離avg$',   '{:.1f}',   True),
        ('crash_survival', '急落生存率',    '{:.1%}',   True),
        ('max_consec_loss','最大連続損失',  '{}回',     False),
    ]
    y = 0.95
    for k, lbl, fmt, hb in metrics:
        vals = [res.get(k, 0) for res in results]
        ax.text(0.02, y, lbl[:12], transform=ax.transAxes, color=MUTED, fontsize=7, va='top')
        for ci, (res, v) in enumerate(zip(results, vals)):
            best  = max(vals) if hb else min(vals)
            worst = min(vals) if hb else max(vals)
            c = GREEN if v == best else (RED if v == worst else TEXT)
            ax.text(0.38 + ci * 0.12, y, fmt.format(v),
                    transform=ax.transAxes, color=c, fontsize=6.5, va='top')
        y -= 0.088

    # SL距離分布
    ax = fig.add_subplot(gs[1, 0])
    _sax(ax, cfg)
    for res in results:
        dists = [t['sl_dist'] for t in res.get('trades', [])]
        if dists:
            ax.hist(dists, bins=20, alpha=0.55, color=res['color'],
                    label=res['strategy'][:12])
    ax.axvline(atr_a,     color=YELLOW, lw=1.2, ls='--', label=f'ATR avg ${atr_a:.1f}')
    ax.axvline(atr_a*2.5, color=RED,    lw=0.8, ls=':',  label=f'典型急落 ATR×2.5')
    ax.set_title('SL距離分布', color=TEXT, fontsize=10)
    ax.set_xlabel('SL距離 (USD)', color=MUTED, fontsize=9)
    ax.legend(facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=7)

    # スリッページ平均
    ax = fig.add_subplot(gs[1, 1])
    _sax(ax, cfg)
    names  = [r['strategy'][:12] for r in results]
    slips  = [r.get('avg_slip_usd', 0) for r in results]
    colors = [r['color'] for r in results]
    bars   = ax.bar(names, slips, color=colors, alpha=0.75)
    for bar, v in zip(bars, slips):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.02,
                f'${v:.2f}', ha='center', color=TEXT, fontsize=8)
    ax.set_title('平均スリッページ (USD)', color=TEXT, fontsize=10)
    ax.set_xticklabels(names, rotation=15, fontsize=7, color=MUTED)

    # 急落トレード損益
    ax = fig.add_subplot(gs[1, 2])
    _sax(ax, cfg)
    for res in results:
        cp = [t['pnl'] for t in res.get('trades', []) if t.get('was_crash')]
        if cp:
            ax.hist(cp, bins=15, alpha=0.55, color=res['color'],
                    label=f"{res['strategy'][:10]} (n={len(cp)})")
    ax.axvline(0, color=MUTED, lw=0.8, ls='--')
    ax.set_title('急落トレード損益', color=TEXT, fontsize=10)
    ax.set_xlabel('損益 (pips)', color=MUTED, fontsize=9)
    ax.legend(facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=7)

    # イグジット内訳
    ax = fig.add_subplot(gs[2, 0])
    _sax(ax, cfg)
    rmap  = {'sl':'SL(正常)', 'sl_slip':'SL(スリップ)', 'tp':'TP', 'timeout':'タイムアウト'}
    rclrs = {'sl': MUTED, 'sl_slip': RED, 'tp': GREEN, 'timeout': BLUE}
    xpos  = np.arange(len(results))
    bots  = np.zeros(len(results))
    for r in ['sl', 'sl_slip', 'tp', 'timeout']:
        pcts = [res.get('reason_counts', {}).get(r, 0) / max(res['n_trades'], 1) * 100
                for res in results]
        ax.bar(xpos, pcts, bottom=bots, color=rclrs[r], alpha=0.82, label=rmap[r])
        bots += np.array(pcts)
    ax.set_xticks(xpos)
    ax.set_xticklabels([r['strategy'][:10] for r in results], rotation=15, color=MUTED, fontsize=7)
    ax.set_ylabel('割合 (%)', color=MUTED, fontsize=9)
    ax.set_title('イグジット理由の内訳', color=TEXT, fontsize=10)
    ax.legend(facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=8)

    # 急落生存率
    ax = fig.add_subplot(gs[2, 1])
    _sax(ax, cfg)
    surv  = [r.get('crash_survival', 0) * 100 for r in results]
    bars  = ax.bar([r['strategy'][:12] for r in results],
                   surv, color=[r['color'] for r in results], alpha=0.75)
    for bar, v in zip(bars, surv):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                f'{v:.0f}%', ha='center', color=TEXT, fontsize=9)
    ax.axhline(50, color=MUTED, lw=0.7, ls='--', alpha=0.5)
    ax.set_title('急落トレード生存率（損失<$30）', color=TEXT, fontsize=10)
    ax.set_xticklabels([r['strategy'][:12] for r in results],
                        rotation=15, color=MUTED, fontsize=7)

    # 設計指針
    ax = fig.add_subplot(gs[2, 2])
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values(): sp.set_color(BORD)
    ax.axis('off')
    ax.set_title('急落対応SL 設計指針', color=TEXT, fontsize=10)
    sl_c  = cfg.get('SL', {})
    lines = [
        (GREEN,  f'ATR平均: ${atr_a:.2f}'),
        (MUTED,  ''),
        (YELLOW, '【ボラ適応型SL（推奨）】'),
        (MUTED,  f' ATR_ratio < 0.8  : ×{sl_c.get("sl_multi_low",1.0)}  (${atr_a*sl_c.get("sl_multi_low",1.0):.1f})'),
        (MUTED,  f' 0.8 〜 1.5       : ×{sl_c.get("sl_multi_normal",1.5)}  (${atr_a*sl_c.get("sl_multi_normal",1.5):.1f})'),
        (MUTED,  f' 1.5 〜 2.5       : ×{sl_c.get("sl_multi_medium",2.5)}  (${atr_a*sl_c.get("sl_multi_medium",2.5):.1f})'),
        (RED,    f' ATR_ratio > 2.5  : ×{sl_c.get("sl_multi_high",4.0)}  (${atr_a*sl_c.get("sl_multi_high",4.0):.1f})'),
        (MUTED,  ''),
        (YELLOW, '【RSI連動イグジット】'),
        (MUTED,  f' RSI≥{sl_c.get("rsi_exit_thr",75.0):.0f} でトレーリングSL起動'),
        (MUTED,  f' トレーリング幅: ATR×{sl_c.get("trail_multi",1.5)}'),
        (MUTED,  ''),
        (YELLOW, '【MT5 EA スリッページ対策】'),
        (MUTED,  f' 最大スリップ: ATR×0.5=${atr_a*0.5:.1f}'),
        (MUTED,  ' OrderSend deviation でポイント制限'),
        (MUTED,  ' SL値をATR×0.1内側に設定'),
    ]
    y = 0.97
    for color, text in lines:
        ax.text(0.03, y, text, transform=ax.transAxes, color=color, fontsize=7.5, va='top')
        y -= 0.062 if text else 0.03

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = f"{out_dir}/sl_comparison.png"
    plt.tight_layout()
    plt.savefig(path, dpi=p.get('dpi', 150), bbox_inches='tight', facecolor=DARK)
    plt.close()
    print(f"[出力] {path}")
    return path
