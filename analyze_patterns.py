"""analyze_patterns.py — テクニカルパターン検知 & 可視化

BTC/USD の H1 データでWボトム・ダブルトップ・三尊・逆三尊を検知し
チャートに描画して ./output/patterns.png に保存する。

使い方:
    python analyze_patterns.py
    python analyze_patterns.py --window 7 --bars 300
"""
from __future__ import annotations
import argparse
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates  as mdates
from pathlib import Path

from core.patterns import (
    find_swing_points, detect_all_patterns,
    detect_double_bottom, detect_double_top,
    detect_head_shoulders, detect_inv_head_shoulders,
    PatternResult,
)

# btc_predict.py と同じアンカーデータ
_BTC_ANCHORS: list[tuple[str, float]] = [
    ('2022-01-01', 46_211), ('2022-02-01', 38_160), ('2022-03-01', 43_193),
    ('2022-05-01', 37_644), ('2022-06-18', 18_009), ('2022-08-01', 23_298),
    ('2022-09-01', 19_988), ('2022-10-01', 19_315), ('2022-11-09', 16_200),
    ('2022-11-21', 15_480), ('2023-01-01', 16_618), ('2023-02-01', 23_140),
    ('2023-04-14', 30_434), ('2023-06-15', 25_108), ('2023-07-13', 31_386),
    ('2023-10-25', 34_023), ('2024-01-01', 42_265), ('2024-02-29', 62_372),
    ('2024-03-14', 73_084), ('2024-04-13', 62_480), ('2024-06-01', 67_523),
    ('2024-08-05', 49_159), ('2024-09-01', 59_142), ('2024-10-01', 63_302),
    ('2024-11-05', 68_249), ('2024-11-13', 90_243), ('2024-12-17', 107_142),
    ('2025-01-20', 109_225), ('2025-02-01', 96_491), ('2025-03-01', 82_118),
    ('2025-04-01', 82_456), ('2025-05-01', 94_878), ('2025-08-01', 98_320),
]


def fetch_data(bars: int = 400) -> pd.DataFrame:
    """BTC データを取得（yfinance → 組み込みアンカーにフォールバック）"""
    try:
        import yfinance as yf
        from datetime import datetime, timedelta
        end   = datetime.today()
        start = end - timedelta(days=bars * 2)
        df = yf.download('BTC-USD', start=start, end=end, progress=False, auto_adjust=True)
        if not df.empty:
            df = df[['Open', 'High', 'Low', 'Close']].copy()
            df.index = pd.to_datetime(df.index)
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df = df.tail(bars)
            print(f"[データ] yfinance  {len(df)}本  最新: ${df['Close'].iloc[-1]:,.0f}")
            return df
    except Exception:
        pass

    # 組み込みアンカー → PCHIP 補間
    from scipy.interpolate import PchipInterpolator
    anchors = [(pd.Timestamp(d), p) for d, p in _BTC_ANCHORS]
    all_days = pd.date_range(anchors[0][0], anchors[-1][0], freq='D')
    t0  = anchors[0][0]
    t_a = np.array([(d - t0).days for d, _ in anchors], dtype=float)
    p_a = np.log([p for _, p in anchors])
    ip  = PchipInterpolator(t_a, p_a)
    t_d = np.array([(d - t0).days for d in all_days], dtype=float)
    rng = np.random.default_rng(42)
    noise = np.cumsum(rng.normal(0, 0.012, len(all_days)))
    noise -= noise  # ゼロ平均
    prices = np.exp(ip(t_d) + noise * 0.3)

    # OHLC を疑似生成
    atr = np.abs(np.diff(prices, prepend=prices[0])) * 0.5 + prices * 0.005
    df = pd.DataFrame({
        'Open':  prices - rng.uniform(0, 1, len(prices)) * atr,
        'High':  prices + rng.uniform(0, 1, len(prices)) * atr * 1.5,
        'Low':   prices - rng.uniform(0, 1, len(prices)) * atr * 1.5,
        'Close': prices,
    }, index=all_days)
    df = df.tail(bars)
    print(f"[データ] 組み込みアンカー  {len(df)}本  最新: ${df['Close'].iloc[-1]:,.0f}")
    return df


# ── 描画ヘルパー ───────────────────────────────────────────────────────────────

COLORS = {
    'double_bottom':    '#3fb950',   # 緑
    'double_top':       '#f85149',   # 赤
    'head_shoulders':   '#f85149',   # 赤
    'inv_head_shoulders': '#3fb950', # 緑
}

MARKERS = {
    'double_bottom':    '^',
    'double_top':       'v',
    'head_shoulders':   'v',
    'inv_head_shoulders': '^',
}


def _draw_pattern(ax, df: pd.DataFrame, p: PatternResult, alpha: float = 0.85) -> None:
    color = COLORS[p.name]
    idx   = df.index

    # キーポイントをマーク
    for bar_i, price in p.key_points:
        if 0 <= bar_i < len(idx):
            mk = MARKERS[p.name]
            offset = price * (-0.012 if mk == 'v' else 0.012)
            ax.plot(idx[bar_i], price + offset, mk, color=color,
                    markersize=10, alpha=alpha, zorder=5)

    # ネックライン
    if p.key_points:
        left_bar = p.key_points[0][0]
        right_bar = p.key_points[-1][0]
        ext_bar   = min(right_bar + 30, len(idx) - 1)
        x_nl = [idx[left_bar], idx[ext_bar]]
        ax.plot(x_nl, [p.neckline, p.neckline], '--', color=color,
                lw=1.4, alpha=0.7, zorder=4)

        # ラベル
        conf_str = f'{p.label} {p.confidence:.0%}'
        ok_str   = ' ✓' if p.confirmed else ''
        ax.annotate(conf_str + ok_str,
                    xy=(idx[right_bar], p.neckline),
                    xytext=(8, 6 if p.direction == 'bullish' else -14),
                    textcoords='offset points',
                    fontsize=7.5, color=color, alpha=alpha,
                    bbox=dict(boxstyle='round,pad=0.2', fc='#0d1117', ec=color, alpha=0.7))

        # 目標価格の矢印
        if 0 <= right_bar + 5 < len(idx):
            ax.annotate('', xy=(idx[min(right_bar + 20, len(idx) - 1)], p.target),
                        xytext=(idx[right_bar], p.neckline),
                        arrowprops=dict(arrowstyle='->', color=color, lw=1.2, alpha=0.5))


def plot_patterns(df: pd.DataFrame, patterns: list[PatternResult],
                  swing_highs, swing_lows, out: str) -> None:
    fig, (ax_price, ax_conf) = plt.subplots(
        2, 1, figsize=(16, 9),
        gridspec_kw={'height_ratios': [4, 1]},
        facecolor='#0d1117',
    )
    for ax in (ax_price, ax_conf):
        ax.set_facecolor('#161b22')
        ax.tick_params(colors='#c9d1d9', labelsize=8)
        for sp in ax.spines.values():
            sp.set_edgecolor('#21262d')

    # 価格ライン
    ax_price.plot(df.index, df['Close'], color='#58a6ff', lw=1.2, zorder=3)
    if 'High' in df.columns:
        ax_price.fill_between(df.index, df['Low'], df['High'],
                              color='#58a6ff', alpha=0.08, zorder=2)

    # スイングポイント
    for i, p in swing_highs:
        if 0 <= i < len(df):
            ax_price.plot(df.index[i], p, 'v', color='#f0883e',
                          markersize=5, alpha=0.5, zorder=4)
    for i, p in swing_lows:
        if 0 <= i < len(df):
            ax_price.plot(df.index[i], p, '^', color='#d2a8ff',
                          markersize=5, alpha=0.5, zorder=4)

    # パターン描画
    for pat in patterns:
        _draw_pattern(ax_price, df, pat)

    ax_price.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: f'${v:,.0f}'))
    ax_price.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax_price.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    plt.setp(ax_price.xaxis.get_majorticklabels(), rotation=30, ha='right')
    ax_price.set_ylabel('BTC/USD', color='#c9d1d9')
    ax_price.grid(alpha=0.15, color='#21262d')

    # 凡例
    legend_patches = [
        mpatches.Patch(color='#3fb950', label='Wボトム / 逆三尊 (強気)'),
        mpatches.Patch(color='#f85149', label='ダブルトップ / 三尊 (弱気)'),
        mpatches.Patch(color='#f0883e', label='スイングハイ', alpha=0.6),
        mpatches.Patch(color='#d2a8ff', label='スイングロー', alpha=0.6),
    ]
    ax_price.legend(handles=legend_patches, fontsize=8,
                    facecolor='#0d1117', edgecolor='#21262d', labelcolor='#c9d1d9',
                    loc='upper left')

    n_pats = len(patterns)
    ax_price.set_title(
        f'BTC/USD パターン検知  — {n_pats}件検出  ({df.index[0].date()} 〜 {df.index[-1].date()})',
        fontsize=12, color='#c9d1d9', pad=8)

    # 下段: 信頼度バー
    if patterns:
        labels = [f'{p.label}\n{p.confidence:.0%}' for p in patterns[:10]]
        confs  = [p.confidence for p in patterns[:10]]
        colors = [COLORS[p.name] for p in patterns[:10]]
        xs     = range(len(labels))
        ax_conf.bar(xs, confs, color=colors, alpha=0.8, width=0.6)
        ax_conf.set_xticks(list(xs))
        ax_conf.set_xticklabels(labels, fontsize=7.5, color='#c9d1d9')
        ax_conf.set_ylim(0, 1.05)
        ax_conf.set_ylabel('信頼度', color='#c9d1d9', fontsize=8)
        ax_conf.axhline(0.6, color='#e3b341', lw=0.8, ls='--', alpha=0.6)
        ax_conf.grid(axis='y', alpha=0.15, color='#21262d')
        ax_conf.set_title('検出パターン 信頼度ランキング', fontsize=9,
                          color='#8b949e', pad=4)
    else:
        ax_conf.text(0.5, 0.5, 'パターン未検出', ha='center', va='center',
                     color='#8b949e', transform=ax_conf.transAxes, fontsize=11)

    plt.tight_layout(h_pad=0.5)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out, dpi=150, facecolor='#0d1117')
    print(f"[出力] {out}")


# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description='BTC パターン検知')
    ap.add_argument('--bars',   type=int, default=400,  help='分析バー数 (デフォルト: 400)')
    ap.add_argument('--window', type=int, default=6,    help='スイングウィンドウ (デフォルト: 6)')
    ap.add_argument('--top',    type=int, default=5,    help='表示上位件数')
    ap.add_argument('--out',    default='./output/patterns.png')
    args = ap.parse_args()

    df = fetch_data(bars=args.bars)
    sh, sl = find_swing_points(df, window=args.window)
    print(f"スイングハイ: {len(sh)}本  スイングロー: {len(sl)}本")

    patterns = detect_all_patterns(df, window=args.window, top_n=args.top)

    print(f"\n{'='*60}")
    print(f"  検出パターン  ({len(patterns)}件)")
    print(f"{'='*60}")
    if patterns:
        for i, p in enumerate(patterns, 1):
            print(f"  [{i}] {p.signal}")
    else:
        print("  パターンは検出されませんでした。")
        print("  --bars を増やすか --window を変えてみてください。")
    print(f"{'='*60}")

    plot_patterns(df, patterns[:args.top], sh, sl, args.out)


if __name__ == '__main__':
    main()
