"""
monitor_rvol.py — RVOLリアルタイム監視・グラフ表示
==================================================
M5 足でのRVOL（相対出来高）をリアルタイムで監視し、
急騰初期検知との関係性をグラフ表示する

実行:
    python monitor_rvol.py --symbol XAUUSD --minutes 120
    python monitor_rvol.py --symbol BTCUSD --interval 5
"""
import sys, json, time, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data import connect_mt5, fetch_ohlcv
from core.indicators import add_m5_indicators, calc_rvol, calc_price_acceleration, detect_volume_surge

matplotlib.use('TkAgg')  # リアルタイム表示用バックエンド

# ── 日本語フォント設定 ──────────────────────────────
matplotlib.rcParams['font.sans-serif'] = ['Yu Gothic', 'MS Gothic', 'HGMaruGothicMPRO', 'DejaVu Sans']
matplotlib.rcParams['font.family'] = 'sans-serif'
matplotlib.rcParams['axes.unicode_minus'] = False


def setup_realtime_plot():
    """リアルタイムプロット用のFigure/Axesを初期化"""
    fig, axes = plt.subplots(4, 1, figsize=(14, 10), sharex=True)
    fig.patch.set_facecolor('#0d1117')
    
    for ax in axes:
        ax.set_facecolor('#161b22')
        ax.tick_params(colors='#8b949e', labelsize=9)
        for sp in ax.spines.values():
            sp.set_color('#21262d')
    
    return fig, axes


def update_rvol_plot(symbol: str, cfg: dict, duration_minutes: int = 120, interval_sec: int = 5):
    """
    RVOLをリアルタイムで監視・グラフ表示

    symbol: 通貨ペア (XAUUSD, BTCUSD など)
    duration_minutes: 表示対象期間（分）
    interval_sec: 更新間隔（秒）
    """
    print(f"\n[RVOL監視] {symbol} を開始します")
    print(f"  表示期間: {duration_minutes}分")
    print(f"  更新間隔: {interval_sec}秒")
    print(f"  Ctrl+C で終了\n")

    if not connect_mt5(symbol, cfg['MT5']):
        return

    fig, axes = setup_realtime_plot()
    fig.suptitle(f'RVOL リアルタイム監視 - {symbol}', color='#c9d1d9', fontsize=13)

    # レイアウト設定
    ax_price, ax_rvol, ax_accel, ax_surge = axes
    ax_price.set_ylabel('Price (USD)', color='#8b949e', fontsize=10)
    ax_rvol.set_ylabel('RVOL', color='#8b949e', fontsize=10)
    ax_accel.set_ylabel('Price Accel (%)', color='#8b949e', fontsize=10)
    ax_surge.set_ylabel('Volume Surge', color='#8b949e', fontsize=10)
    ax_surge.set_xlabel('時刻 (UTC)', color='#8b949e', fontsize=10)

    # グリッド設定
    for ax in axes:
        ax.grid(True, alpha=0.2, color='#30363d', linestyle=':')
        ax.set_axisbelow(True)

    data_history = {'timestamp': [], 'close': [], 'rvol': [], 'accel': [], 'surge': []}
    last_update = time.time()

    try:
        while True:
            now = time.time()
            if now - last_update < interval_sec:
                time.sleep(0.5)
                continue

            last_update = now

            # M5データ取得（最新120本 = 約10時間分）
            df_m5 = fetch_ohlcv(symbol, 'M5', 120)
            if df_m5 is None or len(df_m5) < 20:
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] データ取得待機中...")
                time.sleep(interval_sec)
                continue

            # インジケータ計算
            df_m5 = add_m5_indicators(df_m5, cfg)

            # 表示対象期間を限定
            display_bars = min(len(df_m5), max(1, duration_minutes // 5))
            df_disp = df_m5.tail(display_bars).copy()

            # データ履歴に追加
            for idx, row in df_disp.iterrows():
                ts = idx.strftime('%H:%M')
                close = float(row['Close'])
                rvol = float(row['RVOL']) if 'RVOL' in row else np.nan
                accel = float(row['Price_Accel']) if 'Price_Accel' in row else np.nan
                surge = float(row['Volume_Surge']) if 'Volume_Surge' in row else 0.0

                if ts not in data_history['timestamp']:
                    data_history['timestamp'].append(ts)
                    data_history['close'].append(close)
                    data_history['rvol'].append(rvol)
                    data_history['accel'].append(accel)
                    data_history['surge'].append(surge)

            # グラフをクリアして再描画
            for ax in axes:
                ax.clear()

            # 表示インデックス
            x = np.arange(len(data_history['timestamp']))

            # 1. 価格チャート
            ax_price.plot(x, data_history['close'], color='#e3b341', lw=2, label='Close', marker='o', markersize=3)
            ax_price.set_ylabel('Price (USD)', color='#8b949e', fontsize=10)
            ax_price.legend(loc='upper left', facecolor='#161b22', edgecolor='#21262d', labelcolor='#c9d1d9', fontsize=9)
            ax_price.grid(True, alpha=0.2, color='#30363d', linestyle=':')
            ax_price.set_axisbelow(True)

            # 2. RVOL
            colors_rvol = ['#3fb950' if r <= 1.0 else '#f0883e' if r <= 1.5 else '#f85149'
                           for r in data_history['rvol']]
            ax_rvol.bar(x, data_history['rvol'], color=colors_rvol, alpha=0.7, label='RVOL')
            ax_rvol.axhline(1.0, color='#8b949e', lw=1, ls='--', alpha=0.5, label='Normal (1.0x)')
            ax_rvol.axhline(1.3, color='#f0883e', lw=1, ls='--', alpha=0.7, label='Early Surge (1.3x)')
            ax_rvol.axhline(1.5, color='#f85149', lw=1, ls='--', alpha=0.7, label='High (1.5x)')
            ax_rvol.set_ylabel('RVOL', color='#8b949e', fontsize=10)
            ax_rvol.legend(loc='upper left', facecolor='#161b22', edgecolor='#21262d', labelcolor='#c9d1d9', fontsize=9)
            ax_rvol.grid(True, alpha=0.2, color='#30363d', linestyle=':')
            ax_rvol.set_axisbelow(True)

            # 3. 価格加速度
            colors_accel = ['#3fb950' if a >= 0 else '#f85149' for a in data_history['accel']]
            ax_accel.bar(x, data_history['accel'], color=colors_accel, alpha=0.7, label='Price Accel')
            ax_accel.axhline(0, color='#8b949e', lw=1, alpha=0.5)
            ax_accel.axhline(0.5, color='#f0883e', lw=1, ls='--', alpha=0.7, label='Early Surge (0.5%)')
            ax_accel.set_ylabel('Price Accel (%)', color='#8b949e', fontsize=10)
            ax_accel.legend(loc='upper left', facecolor='#161b22', edgecolor='#21262d', labelcolor='#c9d1d9', fontsize=9)
            ax_accel.grid(True, alpha=0.2, color='#30363d', linestyle=':')
            ax_accel.set_axisbelow(True)

            # 4. 出来高急増（Volume Surge）
            colors_surge = ['#3fb950' if s else '#161b22' for s in data_history['surge']]
            ax_surge.bar(x, data_history['surge'], color=colors_surge, alpha=0.7, label='Volume Surge')
            ax_surge.set_ylabel('Volume Surge', color='#8b949e', fontsize=10)
            ax_surge.legend(loc='upper left', facecolor='#161b22', edgecolor='#21262d', labelcolor='#c9d1d9', fontsize=9)
            ax_surge.grid(True, alpha=0.2, color='#30363d', linestyle=':')
            ax_surge.set_axisbelow(True)
            ax_surge.set_xticks(x[::max(1, len(x)//10)])
            ax_surge.set_xticklabels(
                [data_history['timestamp'][i] for i in ax_surge.get_xticks() if int(i) < len(data_history['timestamp'])],
                rotation=45, ha='right'
            )

            # 統計情報表示
            latest_rvol = data_history['rvol'][-1]
            latest_accel = data_history['accel'][-1]
            latest_surge = data_history['surge'][-1]
            surge_threshold_rvol = cfg.get('INDICATOR', {}).get('early_surge_rvol_threshold', 1.3)
            surge_threshold_accel = cfg.get('INDICATOR', {}).get('early_surge_accel_threshold', 0.5)

            info_text = (
                f"最新値 | RVOL: {latest_rvol:.2f}x "
                f"(閾値 {surge_threshold_rvol:.1f}x) | "
                f"Accel: {latest_accel:.2f}% "
                f"(閾値 {surge_threshold_accel:.1f}%) | "
                f"Surge: {'✓' if latest_surge else '✗'}"
            )
            fig.text(0.05, 0.02, info_text, ha='left', fontsize=10,
                     color='#3fb950' if latest_surge else '#f85149',
                     family='monospace', weight='bold')

            fig.tight_layout(rect=[0, 0.03, 1, 0.97])
            plt.pause(0.1)

            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                  f"RVOL={latest_rvol:.2f}x  Accel={latest_accel:.2f}%  Surge={latest_surge}  "
                  f"Bars={len(data_history['timestamp'])}")

    except KeyboardInterrupt:
        print("\n[終了] RVOLリアルタイム監視を停止しました")
    except Exception as e:
        print(f"[エラー] {e}")
    finally:
        try:
            import MetaTrader5 as mt5
            mt5.shutdown()
        except:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='RVOLをリアルタイムで監視・グラフ表示'
    )
    parser.add_argument('--symbol', type=str, default='XAUUSD',
                        help='通貨ペア (XAUUSD, BTCUSD など)')
    parser.add_argument('--minutes', type=int, default=120,
                        help='表示対象期間（分） デフォルト: 120')
    parser.add_argument('--interval', type=int, default=5,
                        help='更新間隔（秒） デフォルト: 5')
    args = parser.parse_args()

    CFG = {k: getattr(C, k) for k in
           ['MT5','INDICATOR','SIGNAL','EXECUTION','SL','RULES','LOCAL','PLOT',
            'BRIDGE','SCALP','REGIME','TIME_BIAS']}

    update_rvol_plot(args.symbol, CFG, args.minutes, args.interval)
