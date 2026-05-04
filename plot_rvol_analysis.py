"""
plot_rvol_analysis.py — RVOL分析プロット（静的）
===================================================
過去のRVOLデータを分析・プロット表示

実行:
    python plot_rvol_analysis.py --symbol XAUUSD --hours 24
    python plot_rvol_analysis.py --symbol BTCUSD --hours 72
"""
import sys, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import warnings
warnings.filterwarnings('ignore')

# ── 日本語フォント設定 ──────────────────────────────
matplotlib.rcParams['font.sans-serif'] = ['Yu Gothic', 'MS Gothic', 'HGMaruGothicMPRO', 'DejaVu Sans']
matplotlib.rcParams['font.family'] = 'sans-serif'
matplotlib.rcParams['axes.unicode_minus'] = False

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data import connect_mt5, fetch_ohlcv
from core.indicators import add_m5_indicators, calc_rvol, calc_price_acceleration


def plot_rvol_analysis(symbol: str, cfg: dict, hours: int = 24, output_dir: str = './output'):
    """
    RVOL分析プロットを生成

    symbol: 通貨ペア
    hours: 分析対象期間（時間）
    output_dir: 出力ディレクトリ
    """
    print(f"\n[RVOL分析] {symbol} {hours}時間分を取得中...")

    if not connect_mt5(symbol, cfg['MT5']):
        return None

    try:
        # M5データ取得（hours時間分 = hours×12本）
        bars_needed = hours * 12 + 20  # 余裕を持って取得
        df_m5 = fetch_ohlcv(symbol, 'M5', bars_needed)
        if df_m5 is None or len(df_m5) < 20:
            print("[エラー] M5データ取得失敗")
            return None

        # インジケータ計算
        df_m5 = add_m5_indicators(df_m5, cfg)

        # 表示対象期間を制限
        display_bars = min(len(df_m5), max(1, hours * 12))
        df_disp = df_m5.tail(display_bars).copy()

        # 配色テーマ
        DARK = '#0d1117'
        TEXT = '#c9d1d9'
        MUTED = '#8b949e'
        GREEN = '#3fb950'
        RED = '#f85149'
        YELLOW = '#e3b341'
        BLUE = '#58a6ff'
        ORANGE = '#f0883e'
        PANEL = '#161b22'
        BORD = '#21262d'

        # Figure生成
        fig = plt.figure(figsize=(18, 12))
        fig.patch.set_facecolor(DARK)
        gs = gridspec.GridSpec(3, 2, hspace=0.35, wspace=0.3)
        fig.suptitle(f'RVOL分析 - {symbol} ({hours}時間分)',
                     color=TEXT, fontsize=14, weight='bold', y=0.995)

        x = np.arange(len(df_disp))
        close = df_disp['Close'].values
        rvol = df_disp['RVOL'].values
        accel = df_disp['Price_Accel'].values
        volume = df_disp['Volume'].values

        # 1. 価格 + RVOL（上部）
        ax1 = fig.add_subplot(gs[0, :])
        ax1.set_facecolor(PANEL)
        for sp in ax1.spines.values():
            sp.set_color(BORD)
        ax1.tick_params(colors=MUTED, labelsize=9)

        # 価格（左軸）
        ax1_price = ax1
        line1 = ax1_price.plot(x, close, color=YELLOW, lw=2.5, label='Close', marker='o', markersize=2)
        ax1_price.set_ylabel('Price (USD)', color=YELLOW, fontsize=11, weight='bold')
        ax1_price.tick_params(axis='y', labelcolor=YELLOW)
        ax1_price.grid(True, alpha=0.2, color=BORD, linestyle=':')
        ax1_price.set_axisbelow(True)

        # RVOL（右軸）
        ax1_rvol = ax1_price.twinx()
        colors_rvol = [GREEN if r <= 1.0 else ORANGE if r <= 1.5 else RED for r in rvol]
        bars = ax1_rvol.bar(x, rvol, color=colors_rvol, alpha=0.4, label='RVOL', width=0.6)
        ax1_rvol.axhline(1.0, color=MUTED, lw=1, ls='--', alpha=0.5)
        ax1_rvol.axhline(1.3, color=ORANGE, lw=1.5, ls='--', alpha=0.7, label='Early Surge (1.3x)')
        ax1_rvol.axhline(1.5, color=RED, lw=1.5, ls='--', alpha=0.7, label='High (1.5x)')
        ax1_rvol.set_ylabel('RVOL (倍率)', color=RED, fontsize=11, weight='bold')
        ax1_rvol.tick_params(axis='y', labelcolor=RED)
        ax1_rvol.set_ylim(0, max(rvol) * 1.1)

        lines = line1 + [bars] + ax1_rvol.get_lines()
        labels = ['Close', 'RVOL', 'Early Surge (1.3x)', 'High (1.5x)']
        ax1_price.legend(lines, labels, loc='upper left', facecolor=PANEL,
                         edgecolor=BORD, labelcolor=TEXT, fontsize=9)
        ax1_price.set_title('M5足価格 + RVOL（出来高倍率）', color=TEXT, fontsize=11, weight='bold')

        # 2. RVOL分布
        ax2 = fig.add_subplot(gs[1, 0])
        ax2.set_facecolor(PANEL)
        for sp in ax2.spines.values():
            sp.set_color(BORD)
        ax2.tick_params(colors=MUTED, labelsize=9)

        ax2.hist(rvol, bins=30, color=ORANGE, alpha=0.7, edgecolor=RED)
        ax2.axvline(1.0, color=GREEN, lw=2, ls='-', label=f'Normal (1.0x)')
        ax2.axvline(1.3, color=ORANGE, lw=2, ls='--', label=f'Early Surge (1.3x)')
        ax2.axvline(1.5, color=RED, lw=2, ls='--', label=f'High (1.5x)')
        ax2.axvline(np.mean(rvol), color=YELLOW, lw=2, ls=':', label=f'Mean ({np.mean(rvol):.2f}x)')
        ax2.set_xlabel('RVOL (倍率)', color=MUTED, fontsize=10)
        ax2.set_ylabel('度数', color=MUTED, fontsize=10)
        ax2.legend(facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=8)
        ax2.grid(True, alpha=0.2, color=BORD, linestyle=':')
        ax2.set_axisbelow(True)
        ax2.set_title('RVOL分布', color=TEXT, fontsize=11, weight='bold')

        # 3. 価格加速度
        ax3 = fig.add_subplot(gs[1, 1])
        ax3.set_facecolor(PANEL)
        for sp in ax3.spines.values():
            sp.set_color(BORD)
        ax3.tick_params(colors=MUTED, labelsize=9)

        colors_accel = [GREEN if a >= 0 else RED for a in accel]
        ax3.bar(x, accel, color=colors_accel, alpha=0.7, width=0.8)
        ax3.axhline(0, color=MUTED, lw=1, alpha=0.5)
        ax3.axhline(0.5, color=ORANGE, lw=1.5, ls='--', alpha=0.7, label='Threshold (0.5%)')
        ax3.axhline(-0.5, color=ORANGE, lw=1.5, ls='--', alpha=0.7)
        ax3.set_ylabel('Price Accel (%)', color=MUTED, fontsize=10)
        ax3.legend(facecolor=PANEL, edgecolor=BORD, labelcolor=TEXT, fontsize=8)
        ax3.grid(True, alpha=0.2, color=BORD, linestyle=':')
        ax3.set_axisbelow(True)
        ax3.set_title('価格加速度（短期SMA変化率）', color=TEXT, fontsize=11, weight='bold')

        # 4. 統計情報
        ax4 = fig.add_subplot(gs[2, :])
        ax4.set_facecolor(PANEL)
        for sp in ax4.spines.values():
            sp.set_color(BORD)
        ax4.axis('off')
        ax4.set_title('RVOL統計・急騰初期検知条件', color=TEXT, fontsize=11, weight='bold')

        # 急騰初期検知の条件
        surge_rvol_thr = cfg.get('INDICATOR', {}).get('early_surge_rvol_threshold', 1.3)
        surge_accel_thr = cfg.get('INDICATOR', {}).get('early_surge_accel_threshold', 0.5)

        # カウント
        early_surge_count = np.sum((rvol >= surge_rvol_thr) & (accel >= surge_accel_thr))
        high_rvol_count = np.sum(rvol >= 1.5)
        early_surge_pct = (early_surge_count / len(rvol) * 100) if len(rvol) > 0 else 0

        stats_text = [
            f"【RVOL統計】",
            f"  平均: {np.mean(rvol):.2f}x",
            f"  中央値: {np.median(rvol):.2f}x",
            f"  最大: {np.max(rvol):.2f}x",
            f"  最小: {np.min(rvol):.2f}x",
            f"  標準偏差: {np.std(rvol):.2f}x",
            f"",
            f"【急騰初期検知】（RVOL≥{surge_rvol_thr:.1f}x AND Accel≥{surge_accel_thr:.1f}%）",
            f"  該当本数: {early_surge_count}本 / {len(rvol)}本 ({early_surge_pct:.1f}%)",
            f"  ⇒ 推定発生頻度: {early_surge_pct:.1f}%",
            f"",
            f"【高出来高】（RVOL≥1.5x）",
            f"  該当本数: {high_rvol_count}本 / {len(rvol)}本 ({high_rvol_count/len(rvol)*100:.1f}%)",
            f"",
            f"【加速度統計】",
            f"  平均: {np.mean(accel):+.2f}%",
            f"  上昇本数: {np.sum(accel > 0)}本  下降本数: {np.sum(accel <= 0)}本",
        ]

        y = 0.95
        for text in stats_text:
            if text.startswith('【'):
                color = YELLOW
                weight = 'bold'
                size = 10
            else:
                color = TEXT if not text else MUTED
                weight = 'normal'
                size = 9
            ax4.text(0.02, y, text, transform=ax4.transAxes, color=color,
                     fontsize=size, weight=weight, family='monospace', va='top')
            y -= 0.05

        # 時刻ラベル（X軸）
        time_labels = []
        for i in range(0, len(df_disp), max(1, len(df_disp) // 20)):
            if i < len(df_disp):
                time_labels.append(df_disp.index[i].strftime('%H:%M'))
        ax1_price.set_xticks(np.arange(0, len(df_disp), max(1, len(df_disp) // 20)))
        ax1_price.set_xticklabels(time_labels, rotation=45, ha='right')

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        output_path = f"{output_dir}/rvol_analysis_{symbol}_{hours}h.png"
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor=DARK)
        plt.close()
        print(f"[出力] {output_path}")
        return output_path

    except Exception as e:
        print(f"[エラー] {e}")
        return None
    finally:
        try:
            import MetaTrader5 as mt5
            mt5.shutdown()
        except:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='RVOL分析プロットを生成'
    )
    parser.add_argument('--symbol', type=str, default='XAUUSD',
                        help='通貨ペア (XAUUSD, BTCUSD など)')
    parser.add_argument('--hours', type=int, default=24,
                        help='分析対象期間（時間） デフォルト: 24')
    parser.add_argument('--output', type=str, default='./output',
                        help='出力ディレクトリ デフォルト: ./output')
    args = parser.parse_args()

    CFG = {k: getattr(C, k) for k in
           ['MT5','INDICATOR','SIGNAL','EXECUTION','SL','RULES','LOCAL','PLOT',
            'BRIDGE','SCALP','REGIME','TIME_BIAS']}

    plot_rvol_analysis(args.symbol, CFG, args.hours, args.output)
