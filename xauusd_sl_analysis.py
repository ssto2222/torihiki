"""
XAUUSD 急落対応 ストップロス設計・比較分析
==========================================
【問題の構造】
  急激な下落(フラッシュクラッシュ・指標ショック等)では:
  1. スリッページ: SL価格を大きくギャップして約定 → 予定外の損失
  2. 誤刈り:      一時的な急落が回復する前にSLを刈られる
  3. 連続損失:    タイトなSLは通常の値動きでも刈られやすい

【分析する5つのSL戦略】
  A. 固定SL         : 一定USD額（ベースライン）
  B. ATR倍率SL      : ATR×N（ボラ適応型）
  C. 構造的SL       : 直近スイング安値の下（意味のある水準）
  D. 二段階SL       : タイトなSL + 急落バッファゾーンで猶予を持たせる
  E. ボラティリティ  : ATRが急上昇したらSLを自動拡大、平時はタイト
     適応型SL

【比較指標】
  - 総損失(pips)、SL刈られ回数
  - スリッページ超過損失
  - 急落後の回復取り逃がし率
  - 最大連続損失
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.rcParams['font.family'] = 'Noto Sans CJK JP'
matplotlib.rcParams['axes.unicode_minus'] = False
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
from scipy.signal import argrelextrema
import warnings, json
warnings.filterwarnings('ignore')

# ============================================================
# 定数
# ============================================================
SPREAD_USD  = 0.30
UNIT_LOT    = 1.0
ATR_PERIOD  = 14
CRASH_THR   = 2.5   # ATR×N以上の1本下落を「急落」と定義

# ============================================================
# データ生成（急落イベントを明示的に含む）
# ============================================================

def generate_xauusd_with_crashes(n: int = 5000, seed: int = 42) -> pd.DataFrame:
    """
    XAUUSD を模した合成データ
    通常の値動きに加えて急落イベント（ギャップダウン）を含む
    """
    np.random.seed(seed)
    rng   = np.random.default_rng(seed)
    dates = pd.date_range('2021-01-01', periods=n, freq='h')

    price  = 1800.0
    prices = [price]
    vol_base = 0.003    # 通常ボラティリティ（約0.3%/H1）

    crash_schedule = set()
    # 急落イベントをランダムに配置（全体の約1.5%）
    n_crashes = max(5, int(n * 0.015))
    crash_bars = rng.choice(range(200, n-100), size=n_crashes, replace=False)
    for b in crash_bars:
        crash_schedule.add(int(b))

    crash_magnitudes = {}
    for b in crash_bars:
        # 急落幅: -2%〜-6%（ランダム）
        mag = rng.uniform(0.02, 0.06)
        crash_magnitudes[int(b)] = mag

    for i in range(1, n):
        vol = vol_base * (1 + 0.5 * np.sin(i / 300))
        drift = 0.00003

        if i in crash_schedule:
            # 急落: ギャップダウン
            shock = -crash_magnitudes[i] + rng.normal(0, vol * 0.3)
        else:
            shock = rng.normal(drift, vol)
            # 通常の軽微な急落（0.5%確率で1.5〜2.5%下落）
            if rng.random() < 0.005:
                shock -= rng.uniform(0.015, 0.025)

        price = max(1200.0, price * np.exp(shock))
        prices.append(price)

    prices = np.array(prices)
    hl_r   = 0.004
    df = pd.DataFrame({
        'Open':   prices * (1 + rng.normal(0, 0.0004, n)),
        'High':   prices * (1 + np.abs(rng.normal(hl_r, 0.003, n))),
        'Low':    prices * (1 - np.abs(rng.normal(hl_r, 0.003, n))),
        'Close':  prices,
        'Volume': rng.integers(500, 5000, n).astype(float),
    }, index=dates)
    df['High'] = df[['Open','High','Close']].max(axis=1)
    df['Low']  = df[['Open','Low','Close']].min(axis=1)

    # 急落フラグ
    df['is_crash'] = False
    for b in crash_schedule:
        if b < len(df):
            df.iloc[b, df.columns.get_loc('is_crash')] = True

    return df, list(crash_schedule), crash_magnitudes

# ============================================================
# テクニカル指標
# ============================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # ATR
    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - df['Close'].shift()).abs(),
        (df['Low']  - df['Close'].shift()).abs(),
    ], axis=1).max(axis=1)
    df['ATR']      = tr.ewm(alpha=1/ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False).mean()
    # ATRの移動平均（平時ATR基準）
    df['ATR_MA']   = df['ATR'].rolling(50).mean()
    # ATR比（急上昇を検出）
    df['ATR_ratio'] = df['ATR'] / df['ATR_MA'].replace(0, np.nan)
    # RSI
    d = df['Close'].diff()
    g = d.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    df['RSI'] = 100 - 100 / (1 + g / l.replace(0, np.nan))
    # スイング安値（直近20本の最安値）
    df['Swing_Low']  = df['Low'].rolling(20).min()
    df['Swing_High'] = df['High'].rolling(20).max()
    return df.dropna()

# ============================================================
# SL戦略 定義
# ============================================================

class SLStrategy:
    """SL戦略の基底クラス"""
    name:    str = ''
    name_ja: str = ''
    color:   str = '#58a6ff'

    def calc_sl(self, entry_price: float, direction: str,
                bar_idx: int, df: pd.DataFrame) -> float:
        raise NotImplementedError

    def update_sl(self, current_sl: float, direction: str,
                  bar_idx: int, df: pd.DataFrame,
                  entry_price: float) -> float:
        """保有中のSL更新（デフォルト: 更新なし）"""
        return current_sl

class FixedSL(SLStrategy):
    """A. 固定SL: 一定USD額"""
    name = 'fixed'; name_ja = 'A. 固定SL ($15)'; color = '#8b949e'
    def __init__(self, usd: float = 15.0): self.usd = usd
    def calc_sl(self, ep, d, b, df):
        return ep - self.usd if d == 'buy' else ep + self.usd

class AtrSL(SLStrategy):
    """B. ATR倍率SL: ATR × N"""
    name = 'atr'; name_ja = 'B. ATR倍率SL (×1.5)'; color = '#58a6ff'
    def __init__(self, multi: float = 1.5): self.multi = multi
    def calc_sl(self, ep, d, b, df):
        atr = df['ATR'].iloc[b]
        return ep - atr * self.multi if d == 'buy' else ep + atr * self.multi

class StructuralSL(SLStrategy):
    """C. 構造的SL: 直近スイング安値の下にバッファ"""
    name = 'structural'; name_ja = 'C. 構造的SL (スイング安値-ATR×0.3)'; color = '#e3b341'
    def __init__(self, buffer_multi: float = 0.3): self.buf = buffer_multi
    def calc_sl(self, ep, d, b, df):
        atr = df['ATR'].iloc[b]
        if d == 'buy':
            swing = df['Swing_Low'].iloc[b]
            return swing - atr * self.buf
        else:
            swing = df['Swing_High'].iloc[b]
            return swing + atr * self.buf

class TwoStageSL(SLStrategy):
    """
    D. 二段階SL（急落バッファ型）
    通常フェーズ: タイトなSL（ATR×1.0）
    急落検知後:   SLをATR×2.5に一時拡大（誤刈り防止）
                 価格が回復したらタイトSLに戻す
    """
    name = 'two_stage'; name_ja = 'D. 二段階SL (急落バッファ型)'; color = '#3fb950'

    def calc_sl(self, ep, d, b, df):
        atr = df['ATR'].iloc[b]
        # 初期はタイトSL
        return ep - atr * 1.0 if d == 'buy' else ep + atr * 1.0

    def update_sl(self, current_sl, d, b, df, ep):
        atr       = df['ATR'].iloc[b]
        atr_ratio = df['ATR_ratio'].iloc[b] if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        close     = df['Close'].iloc[b]

        if d == 'buy':
            tight_sl = ep - atr * 1.0
            wide_sl  = ep - atr * 2.5
            # ATRが急上昇（急落中）はSLを拡大
            if atr_ratio >= 1.8:
                new_sl = wide_sl
            else:
                # 通常時: タイトなSLを引き上げ（トレーリング）
                new_sl = max(current_sl, tight_sl) if close > ep else current_sl
            return new_sl
        else:
            tight_sl = ep + atr * 1.0
            wide_sl  = ep + atr * 2.5
            if atr_ratio >= 1.8:
                new_sl = wide_sl
            else:
                new_sl = min(current_sl, tight_sl) if close < ep else current_sl
            return new_sl

class VolAdaptiveSL(SLStrategy):
    """
    E. ボラティリティ適応型SL
    ATR_ratio（現在ATR/平均ATR）に応じてSL幅を動的調整:
      ATR_ratio < 0.8  (低ボラ時):  ATR × 1.0（タイト）
      ATR_ratio 0.8〜1.5 (通常):   ATR × 1.5
      ATR_ratio > 1.5  (高ボラ時): ATR × 2.5（ワイド）
      ATR_ratio > 2.5  (急落時):   ATR × 4.0（最大）
    """
    name = 'vol_adaptive'; name_ja = 'E. ボラ適応型SL (ATR比連動)'; color = '#f85149'

    def _multi(self, atr_ratio):
        if np.isnan(atr_ratio): return 1.5
        if atr_ratio > 2.5:    return 4.0
        elif atr_ratio > 1.5:  return 2.5
        elif atr_ratio > 0.8:  return 1.5
        else:                  return 1.0

    def calc_sl(self, ep, d, b, df):
        atr   = df['ATR'].iloc[b]
        ratio = df['ATR_ratio'].iloc[b] if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        m     = self._multi(ratio)
        return ep - atr * m if d == 'buy' else ep + atr * m

    def update_sl(self, current_sl, d, b, df, ep):
        atr   = df['ATR'].iloc[b]
        ratio = df['ATR_ratio'].iloc[b] if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        m     = self._multi(ratio)
        new_sl = ep - atr * m if d == 'buy' else ep + atr * m
        if d == 'buy':
            return max(current_sl, new_sl)
        else:
            return min(current_sl, new_sl)

# ============================================================
# バックテストエンジン（SL戦略比較用・買いのみ）
# ============================================================

def simulate_sl_strategy(df: pd.DataFrame,
                          strategy: SLStrategy,
                          direction: str = 'buy',
                          n_trades: int = 80,
                          hold_max: int = 48,
                          tp_atr_multi: float = 3.0,
                          rsi_trig_thr: float = 75.0) -> dict:
    """
    ランダムにエントリーを配置して各SL戦略の性能を比較
    （シグナル検出は共通のエントリーポイントを使用 → SLだけの比較）
    """
    np.random.seed(99)
    rng   = np.random.default_rng(99)
    close = df['Close'].values
    high  = df['High'].values
    low   = df['Low'].values
    atr   = df['ATR'].values
    rsi   = df['RSI'].values
    n     = len(df)

    # エントリーポイント: RSI過売圏（<40）かつ十分なデータがある箇所
    entry_candidates = [i for i in range(100, n-hold_max-10)
                        if rsi[i] < 40 and not np.isnan(atr[i])]
    # ランダムサンプリング（但し急落直後は除外）
    is_crash = df['is_crash'].values
    valid_entries = [i for i in entry_candidates
                     if not any(is_crash[max(0,i-3):i+1])]

    if len(valid_entries) > n_trades:
        entry_bars = sorted(rng.choice(valid_entries, size=n_trades, replace=False))
    else:
        entry_bars = sorted(valid_entries)

    trades = []
    used_until = -1

    for eb in entry_bars:
        if eb <= used_until: continue

        ep = close[eb] + SPREAD_USD
        sl = strategy.calc_sl(ep, direction, eb, df)

        # SLが不合理な場合スキップ
        sl_dist = abs(ep - sl)
        if sl_dist < 0.5 or sl_dist > atr[eb] * 6: continue

        tp_rsi   = ep + atr[eb] * tp_atr_multi  # 保険TP
        trail_sl = None

        xp, xt_bar, reason = None, None, 'timeout'
        sl_triggered = False
        rsi_triggered = False

        for b in range(eb+1, min(eb+hold_max, n)):
            h_b, l_b = high[b], low[b]
            r_b      = rsi[b]
            a_b      = atr[b] if not np.isnan(atr[b]) else atr[eb]

            # SL更新
            sl = strategy.update_sl(sl, direction, b, df, ep)

            # RSI≥75でトレーリング起動（RSI連動TP）
            if not rsi_triggered and r_b >= rsi_trig_thr:
                rsi_triggered = True
                trail_sl = h_b - a_b * 1.5
                trail_sl = max(trail_sl, sl)

            if trail_sl is not None:
                new_trail = high[b] - a_b * 1.5
                if new_trail > trail_sl:
                    trail_sl = new_trail

            # SL・TP判定
            if direction == 'buy':
                eff_sl = trail_sl if trail_sl is not None else sl
                if l_b <= eff_sl:
                    # スリッページ計算: Open < SL なら Open で約定
                    actual_exit = min(df['Open'].iloc[b], eff_sl)
                    slippage    = eff_sl - actual_exit  # スリッページ額（正=不利）
                    xp = actual_exit; xt_bar = b
                    reason = 'sl_slip' if slippage > 0.5 else 'sl'
                    break
                if rsi_triggered and trail_sl is not None and h_b >= ep + a_b * tp_atr_multi:
                    xp = ep + a_b * tp_atr_multi; xt_bar = b; reason = 'tp'; break
            else:
                eff_sl = trail_sl if trail_sl is not None else sl
                if h_b >= eff_sl:
                    actual_exit = max(df['Open'].iloc[b], eff_sl)
                    slippage    = actual_exit - eff_sl
                    xp = actual_exit; xt_bar = b
                    reason = 'sl_slip' if slippage > 0.5 else 'sl'
                    break

        if xp is None:
            xt_bar = min(eb + hold_max, n-1)
            xp     = close[xt_bar]
            reason = 'timeout'

        pnl      = (xp - ep) * UNIT_LOT * 100 if direction == 'buy' else (ep - xp) * UNIT_LOT * 100
        sl_dist  = abs(ep - sl)
        was_crash = any(is_crash[eb:min(xt_bar+1, n)])

        trades.append({
            'entry_bar':   eb,
            'exit_bar':    xt_bar,
            'entry_price': ep,
            'exit_price':  xp,
            'sl':          sl,
            'sl_dist':     sl_dist,
            'pnl':         pnl,
            'reason':      reason,
            'was_crash':   was_crash,
            'rsi_triggered': rsi_triggered,
        })
        used_until = xt_bar

    if not trades:
        return {'strategy': strategy.name_ja, 'trades': [],
                'n_trades': 0, 'total_pnl': 0, 'win_rate': 0,
                'sl_hit_rate': 0, 'slip_rate': 0, 'avg_sl_dist': 0,
                'crash_survival': 0, 'max_consec_loss': 0, 'equity': []}

    pnls   = np.array([t['pnl'] for t in trades])
    equity = np.cumsum(pnls)
    wins   = pnls[pnls > 0]; losses = pnls[pnls <= 0]
    gp     = wins.sum()    if len(wins)   > 0 else 0.0
    gl     = -losses.sum() if len(losses) > 0 else 1e-9

    sl_hits   = [t for t in trades if 'sl' in t['reason']]
    sl_slips  = [t for t in trades if t['reason'] == 'sl_slip']
    crash_tr  = [t for t in trades if t['was_crash']]
    crash_surv= sum(1 for t in crash_tr if t['pnl'] > -30) / max(len(crash_tr), 1)

    # 最大連続損失
    max_cl = cur_cl = 0
    for p in pnls:
        if p < 0: cur_cl += 1; max_cl = max(max_cl, cur_cl)
        else: cur_cl = 0

    return {
        'strategy':        strategy.name_ja,
        'color':           strategy.color,
        'trades':          trades,
        'n_trades':        len(trades),
        'total_pnl':       float(pnls.sum()),
        'win_rate':        float(len(wins) / max(len(pnls), 1)),
        'profit_factor':   float(gp / gl),
        'max_dd':          float((np.maximum.accumulate(equity) - equity).max()),
        'sl_hit_rate':     float(len(sl_hits) / max(len(trades), 1)),
        'slip_rate':       float(len(sl_slips) / max(len(trades), 1)),
        'avg_sl_dist':     float(np.mean([t['sl_dist'] for t in trades])),
        'crash_survival':  float(crash_surv),
        'max_consec_loss': int(max_cl),
        'equity':          equity.tolist(),
        'pnls':            pnls.tolist(),
        'reason_counts':   {r: sum(1 for t in trades if t['reason']==r)
                            for r in set(t['reason'] for t in trades)},
    }

# ============================================================
# 可視化
# ============================================================

DARK='#0d1117'; PANEL='#161b22'; BORDER='#21262d'
TEXT='#c9d1d9'; MUTED='#8b949e'
GREEN='#3fb950'; RED='#f85149'; BLUE='#58a6ff'
YELLOW='#e3b341'; ORANGE='#f0883e'

def sax(ax):
    ax.set_facecolor(DARK)
    ax.tick_params(colors=MUTED, labelsize=8)
    for sp in ax.spines.values(): sp.set_color(BORDER)

def plot_sl_concept(df: pd.DataFrame, crash_bars: list):
    """急落時の各SL戦略の挙動を概念図で示す"""
    # 典型的な急落イベントを1件選ぶ
    crash_bars_in_df = [b for b in crash_bars if b > 100 and b < len(df)-50]
    if not crash_bars_in_df: return
    c_bar = crash_bars_in_df[len(crash_bars_in_df)//2]

    s, e  = max(0, c_bar-30), min(len(df), c_bar+40)
    sub   = df.iloc[s:e]
    x     = np.arange(len(sub))
    ci    = c_bar - s  # 急落バーの相対位置

    fig, axes = plt.subplots(1, 2, figsize=(18, 7))
    fig.patch.set_facecolor(DARK)
    fig.suptitle('急落時の SL戦略別 挙動の違い（概念図）',
                 color=TEXT, fontsize=13, y=1.01)

    close_arr = sub['Close'].values
    atr_val   = sub['ATR'].dropna().mean()
    entry_price = close_arr[max(0, ci-10)]  # 急落10バー前にエントリーしていたと仮定

    strategies_show = [
        ('A. 固定SL ($15)',        entry_price - 15,      '#8b949e', '固定額SL\n→急落でスリッページ'),
        ('B. ATR×1.5 SL',          entry_price - atr_val*1.5, BLUE, 'ATR倍率SL\n→通常ボラに追従'),
        ('C. 構造的SL',             min(sub['Low'].values[:ci]) - atr_val*0.3, YELLOW, '構造的SL\n→スイング安値ベース'),
        ('D. 二段階SL\n(急落時拡大)', entry_price - atr_val*2.5, GREEN, '急落時に\nSL自動拡大'),
        ('E. ボラ適応型SL',          entry_price - atr_val*4.0, RED,  'ATR急上昇で\n最大拡大'),
    ]

    for ax, (title, ax_content) in zip(axes, [('全体像', None), ('急落ズームイン', None)]):
        sax(ax)
        # 価格チャート
        ax.plot(x, close_arr, color=YELLOW, lw=1.0, alpha=0.9, label='価格')
        # 急落バーを強調
        if 0 <= ci < len(x):
            ax.axvline(ci, color=RED, lw=2.0, alpha=0.6, ls='-')
            ax.text(ci+0.3, close_arr[ci]*1.002, '急落\n発生', color=RED,
                    fontsize=8, va='bottom')
        # エントリーポイント
        ep_bar = max(0, ci-10)
        ax.scatter(ep_bar, entry_price + SPREAD_USD, marker='^', s=120,
                   color=GREEN, zorder=5, label=f'エントリー ${entry_price:,.0f}')
        # 各SL水準を横線で表示
        for sl_name, sl_price, sl_color, note in strategies_show:
            ax.axhline(sl_price, color=sl_color, lw=1.2, ls='--', alpha=0.8,
                       label=f'{sl_name} ${sl_price:,.1f}')

    axes[0].set_title('急落前後の全体像', color=TEXT, fontsize=10)
    axes[0].set_ylabel('価格 (USD)', color=MUTED, fontsize=9)
    axes[0].yaxis.set_major_formatter(plt.FuncFormatter(lambda v,_: f'${v:,.0f}'))
    axes[0].legend(facecolor=PANEL, edgecolor=BORDER, labelcolor=TEXT,
                   fontsize=7.5, loc='upper left')

    # 右: SL水準の比較バーチャート
    ax = axes[1]
    sax(ax)
    sl_names   = [s[0].replace('\n','') for s in strategies_show]
    sl_dists   = [abs(entry_price - s[1]) for s in strategies_show]
    sl_colors  = [s[2] for s in strategies_show]
    bars = ax.barh(sl_names, sl_dists, color=sl_colors, alpha=0.75)
    ax.axvline(atr_val, color=MUTED, lw=1.0, ls='--', label=f'ATR (${ atr_val:.1f})')
    ax.axvline(atr_val*2.5, color=RED, lw=0.8, ls=':', alpha=0.7,
               label=f'典型急落幅 ATR×2.5 (${atr_val*2.5:.1f})')
    for bar, dist in zip(bars, sl_dists):
        ax.text(bar.get_width()+0.2, bar.get_y()+bar.get_height()/2,
                f'${dist:.1f}', va='center', color=TEXT, fontsize=9)
    ax.set_xlabel('SL距離 (USD)', color=MUTED, fontsize=9)
    ax.set_title('SL距離の比較（大きいほど急落に強い）', color=TEXT, fontsize=10)
    ax.legend(facecolor=PANEL, edgecolor=BORDER, labelcolor=TEXT, fontsize=8)

    plt.tight_layout()
    plt.savefig('/home/claude/sl_concept.png', dpi=150,
                bbox_inches='tight', facecolor=DARK)
    print("[出力] sl_concept.png")
    plt.close()

def plot_comparison(results: list[dict], df: pd.DataFrame, crash_bars: list):
    """SL戦略 比較ダッシュボード"""
    fig = plt.figure(figsize=(22, 16))
    fig.patch.set_facecolor(DARK)
    gs  = gridspec.GridSpec(3, 3, hspace=0.45, wspace=0.35)
    fig.suptitle('XAUUSD 急落対応 ストップロス戦略 比較ダッシュボード',
                 color=TEXT, fontsize=14, y=0.99)

    # ── 1. エクイティカーブ比較 ──────────────────
    ax = fig.add_subplot(gs[0, :2])
    sax(ax)
    for res in results:
        if res['equity']:
            eq = np.array(res['equity'])
            ax.plot(eq, color=res['color'], lw=1.5, label=res['strategy'], alpha=0.85)
    ax.axhline(0, color=MUTED, lw=0.5, ls='--')
    # 急落タイミングを縦線
    ax.set_title('エクイティカーブ比較（全戦略）', color=TEXT, fontsize=10)
    ax.set_xlabel('トレード番号', color=MUTED, fontsize=9)
    ax.set_ylabel('累積損益 (pips)', color=MUTED, fontsize=9)
    ax.legend(facecolor=PANEL, edgecolor=BORDER, labelcolor=TEXT, fontsize=8)

    # ── 2. 総合スコア表 ──────────────────────────
    ax = fig.add_subplot(gs[0, 2])
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values(): sp.set_color(BORDER)
    ax.axis('off')
    ax.set_title('総合スコア比較', color=TEXT, fontsize=10)
    metrics = ['total_pnl','win_rate','profit_factor','max_dd',
               'sl_hit_rate','slip_rate','crash_survival','max_consec_loss']
    labels  = ['総利益(pips)','勝率','PF','最大DD(pips)',
               'SL刈り率','スリップ率','急落生存率','最大連続損失']
    y = 0.95
    ax.text(0.02, y, f"{'戦略':<14}", transform=ax.transAxes,
            color=MUTED, fontsize=7.5, va='top', fontweight='bold')
    for i, lbl in enumerate(labels):
        ax.text(0.02, y - (i+1)*0.085, lbl[:10], transform=ax.transAxes,
                color=MUTED, fontsize=7, va='top')
    for ci, res in enumerate(results):
        col_x = 0.35 + ci * 0.13
        ax.text(col_x, y, res['strategy'][:8], transform=ax.transAxes,
                color=res['color'], fontsize=6.5, va='top', fontweight='bold')
        for i, metric in enumerate(metrics):
            v = res.get(metric, 0)
            if metric in ('win_rate','sl_hit_rate','slip_rate','crash_survival'):
                val_str = f"{v*100:.0f}%"
            elif metric == 'max_consec_loss':
                val_str = str(int(v))
            elif metric in ('total_pnl','max_dd'):
                val_str = f"{v:.0f}"
            else:
                val_str = f"{v:.2f}"
            # 良い値は緑、悪い値は赤でハイライト
            good = (metric in ('total_pnl','win_rate','profit_factor','crash_survival')
                    and v == max(r.get(metric,0) for r in results))
            bad  = (metric in ('max_dd','sl_hit_rate','slip_rate','max_consec_loss')
                    and v == max(r.get(metric,0) for r in results))
            c = GREEN if good else (RED if bad else TEXT)
            ax.text(col_x, y - (i+1)*0.085, val_str, transform=ax.transAxes,
                    color=c, fontsize=7, va='top')

    # ── 3. SL距離分布 ──────────────────────────
    ax = fig.add_subplot(gs[1, 0])
    sax(ax)
    for res in results:
        dists = [t['sl_dist'] for t in res['trades']]
        if dists:
            ax.hist(dists, bins=20, alpha=0.55, color=res['color'],
                    label=res['strategy'][:15])
    atr_avg = df['ATR'].mean()
    ax.axvline(atr_avg,     color=YELLOW, lw=1.2, ls='--', label=f'ATR avg ${atr_avg:.1f}')
    ax.axvline(atr_avg*2.5, color=RED,    lw=0.8, ls=':',  label=f'典型急落幅 ${atr_avg*2.5:.1f}')
    ax.set_title('SL距離分布', color=TEXT, fontsize=10)
    ax.set_xlabel('SL距離 (USD)', color=MUTED, fontsize=9)
    ax.legend(facecolor=PANEL, edgecolor=BORDER, labelcolor=TEXT, fontsize=7)

    # ── 4. 急落時の損益分布 ──────────────────────
    ax = fig.add_subplot(gs[1, 1])
    sax(ax)
    for res in results:
        crash_pnls = [t['pnl'] for t in res['trades'] if t['was_crash']]
        if crash_pnls:
            ax.hist(crash_pnls, bins=15, alpha=0.55, color=res['color'],
                    label=f"{res['strategy'][:12]} (n={len(crash_pnls)})")
    ax.axvline(0, color=MUTED, lw=0.8, ls='--')
    ax.set_title('急落トレードの損益分布', color=TEXT, fontsize=10)
    ax.set_xlabel('損益 (pips)', color=MUTED, fontsize=9)
    ax.legend(facecolor=PANEL, edgecolor=BORDER, labelcolor=TEXT, fontsize=7)

    # ── 5. イグジット理由の内訳 ──────────────────
    ax = fig.add_subplot(gs[1, 2])
    sax(ax)
    reasons_all = ['sl', 'sl_slip', 'tp', 'timeout']
    reason_ja   = {'sl':'SL(正常)','sl_slip':'SL(スリップ)','tp':'TP','timeout':'タイムアウト'}
    x_pos = np.arange(len(results))
    bottoms = np.zeros(len(results))
    reason_colors = {'sl': MUTED, 'sl_slip': RED, 'tp': GREEN, 'timeout': BLUE}
    for r in reasons_all:
        counts = [res.get('reason_counts', {}).get(r, 0) for res in results]
        total  = [res['n_trades'] for res in results]
        pcts   = [c/max(t,1)*100 for c,t in zip(counts, total)]
        ax.bar(x_pos, pcts, bottom=bottoms, color=reason_colors[r],
               alpha=0.8, label=reason_ja[r])
        bottoms += np.array(pcts)
    ax.set_xticks(x_pos)
    ax.set_xticklabels([r['strategy'][:10] for r in results],
                        rotation=15, color=MUTED, fontsize=7)
    ax.set_ylabel('割合 (%)', color=MUTED, fontsize=9)
    ax.set_title('イグジット理由の内訳', color=TEXT, fontsize=10)
    ax.legend(facecolor=PANEL, edgecolor=BORDER, labelcolor=TEXT, fontsize=8)

    # ── 6. ATR_ratio と急落の関係 ────────────────
    ax = fig.add_subplot(gs[2, :2])
    sax(ax)
    n_show = min(1200, len(df))
    sub    = df.tail(n_show)
    x      = np.arange(len(sub))
    off    = len(df) - n_show
    ax.plot(x, sub['Close'].values, color=YELLOW, lw=0.8, alpha=0.8, label='Close')
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v,_: f'${v:,.0f}'))
    ax2 = ax.twinx()
    ax2.plot(x, sub['ATR_ratio'].values, color=ORANGE, lw=1.0, alpha=0.7, label='ATR比率')
    ax2.axhline(1.8, color=RED, lw=0.8, ls='--', alpha=0.7, label='急落検知閾値 1.8')
    ax2.axhline(2.5, color=RED, lw=0.8, ls=':',  alpha=0.7, label='高ボラ閾値 2.5')
    ax2.set_ylabel('ATR比率 (現在/平均)', color=ORANGE, fontsize=8)
    ax2.tick_params(colors=ORANGE, labelsize=7)
    ax2.set_facecolor(DARK)
    for sp in ax2.spines.values(): sp.set_color(BORDER)
    # 急落バーを赤縦線
    for cb in crash_bars:
        ri = cb - off
        if 0 <= ri < len(sub):
            ax.axvline(ri, color=RED, alpha=0.3, lw=0.8)
    ax.set_title('価格チャート + ATR比率（赤縦線=急落イベント）', color=TEXT, fontsize=10)
    ax.set_xlabel('バー番号', color=MUTED, fontsize=9)
    ax.set_ylabel('価格 (USD)', color=MUTED, fontsize=9)
    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1+lines2, labels1+labels2, facecolor=PANEL,
              edgecolor=BORDER, labelcolor=TEXT, fontsize=8)

    # ── 7. SL設計ガイドライン ─────────────────────
    ax = fig.add_subplot(gs[2, 2])
    ax.set_facecolor(PANEL)
    for sp in ax.spines.values(): sp.set_color(BORDER)
    ax.axis('off')
    ax.set_title('SL設計ガイドライン', color=TEXT, fontsize=10)
    guide = [
        (GREEN,  '急落に強いSLの条件'),
        (MUTED,  '① ATR×1.5以上の距離確保'),
        (MUTED,  '   通常ボラで刈られない最低水準'),
        (MUTED,  '② ATR_ratio監視で動的拡大'),
        (MUTED,  '   ATR_ratio≥1.8で自動ワイド化'),
        (MUTED,  '③ 構造的水準（スイング安値）'),
        (MUTED,  '   意味のある価格に設置'),
        (YELLOW, '④ 最大損失の事前定義'),
        (MUTED,  '   SL最大幅=ATR×4で上限設定'),
        (MUTED,  '   1トレード最大損失を固定する'),
        ('', ''),
        (RED,    '急落時のスリッページ対策'),
        (MUTED,  '⑤ スリッページバッファ'),
        (MUTED,  '   SL設定値より実際SLを少し'),
        (MUTED,  '   内側に設定（余裕を持たせる）'),
        (MUTED,  '⑥ 保証ロスカット注文の活用'),
        (MUTED,  '   MT5: StopLoss + SLIPPAGE'),
        (MUTED,  '   パラメータで最大スリップ制限'),
    ]
    y = 0.96
    for color, text in guide:
        if not text: y -= 0.03; continue
        ax.text(0.03, y, text, transform=ax.transAxes,
                color=color if color else MUTED, fontsize=8, va='top')
        y -= 0.054

    plt.savefig('/home/claude/sl_comparison.png', dpi=150,
                bbox_inches='tight', facecolor=DARK)
    print("[出力] sl_comparison.png")
    plt.close()

# ============================================================
# メイン
# ============================================================

def main():
    print("=" * 60)
    print("  XAUUSD 急落対応 ストップロス設計 比較分析")
    print("=" * 60)

    # データ生成
    print("\n[1] データ生成（急落イベント含む）")
    df_raw, crash_bars, crash_mags = generate_xauusd_with_crashes(n=5000)
    df = add_indicators(df_raw)
    close = df['Close'].values
    atr   = df['ATR'].values
    print(f"  期間: {df.index[0].date()} 〜 {df.index[-1].date()} ({len(df)}本)")
    print(f"  急落イベント数: {len(crash_bars)}件")
    print(f"  急落平均幅: {np.mean(list(crash_mags.values()))*100:.1f}%")
    print(f"  H1 ATR: avg=${atr.mean():.2f}  p25=${np.percentile(atr,25):.2f}"
          f"  p75=${np.percentile(atr,75):.2f}  max=${atr.max():.2f}")

    # ギャップ分析
    m1o = df_raw['Open'].values
    m1c = df_raw['Close'].values
    gaps_usd = m1o[1:] - m1c[:-1]
    neg_gaps = gaps_usd[gaps_usd < -0.5]
    print(f"\n  M1下方ギャップ(>$0.5): {len(neg_gaps)}件"
          f"  avg=${neg_gaps.mean():.2f}  worst=${neg_gaps.min():.2f}")

    # SL戦略定義
    print("\n[2] SL戦略の評価")
    strategies = [
        FixedSL(usd=15.0),
        AtrSL(multi=1.5),
        StructuralSL(buffer_multi=0.3),
        TwoStageSL(),
        VolAdaptiveSL(),
    ]

    # バックテスト
    results = []
    for strat in strategies:
        res = simulate_sl_strategy(df, strat, direction='buy',
                                   n_trades=80, hold_max=48,
                                   tp_atr_multi=3.0, rsi_trig_thr=75.0)
        results.append(res)
        rc = res.get('reason_counts', {})
        print(f"\n  {res['strategy']}")
        print(f"    トレード数   : {res['n_trades']}")
        print(f"    総利益       : {res['total_pnl']:+.1f} pips")
        print(f"    勝率         : {res['win_rate']*100:.1f}%")
        print(f"    PF           : {res['profit_factor']:.2f}")
        print(f"    最大DD       : {res['max_dd']:.1f} pips")
        print(f"    SL刈り率     : {res['sl_hit_rate']*100:.1f}%")
        print(f"    スリップ率   : {res['slip_rate']*100:.1f}%")
        print(f"    急落生存率   : {res['crash_survival']*100:.1f}%")
        print(f"    最大連続損失 : {res['max_consec_loss']}回")
        print(f"    exit内訳     : {rc}")
        print(f"    SL平均距離   : ${res['avg_sl_dist']:.2f}")

    # 可視化
    print("\n[3] 可視化")
    plot_sl_concept(df, crash_bars)
    plot_comparison(results, df, crash_bars)

    # 推奨設計 JSON
    recommendation = {
        'symbol': 'XAUUSD',
        'atr_avg_usd': float(atr.mean()),
        'gap_worst_usd': float(neg_gaps.min()) if len(neg_gaps) > 0 else 0,
        'recommended_strategy': 'VolAdaptiveSL (E)',
        'sl_rules': {
            'base': 'ATR × 1.5（通常ボラ時）',
            'medium_vol': 'ATR × 2.5（ATR_ratio > 1.5）',
            'high_vol': 'ATR × 4.0（ATR_ratio > 2.5: 急落時）',
            'max_loss_cap': 'ATR × 4.0 を絶対上限とし、それ以上は注文スキップ',
            'structural': '設定値が直近スイング安値より上なら構造的SLに変更',
        },
        'rsi_exit': {
            'trigger': 'RSI ≥ 75 でトレーリングSL起動',
            'trail': 'ATR × 1.5 幅でフォロー',
        },
        'slippage_handling': {
            'mt5_param': 'OrderSend の slippage パラメータで最大許容スリップを制限',
            'buffer': 'SL注文は計算値より ATR×0.1 だけ内側に設定（スリップ吸収）',
        },
    }
    with open('/home/claude/sl_design.json', 'w', encoding='utf-8') as f:
        json.dump(recommendation, f, ensure_ascii=False, indent=2)
    print("[出力] sl_design.json")
    print("\n完了")
    return results

if __name__ == '__main__':
    main()
