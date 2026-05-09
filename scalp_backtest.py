"""
scalp_backtest.py — スキャルプモードのバックテスト
===================================================
MT5 接続あり / なし（合成データ）どちらでも動作。

実行:
    python scalp_backtest.py                              # 合成データ (MT5不要)
    python scalp_backtest.py --symbol BTCUSD              # MT5実データ（全取得可能期間）
    python scalp_backtest.py --full-data                  # MT5 最大履歴
    python scalp_backtest.py --from 2024-01-01 --to 2024-06-30   # 期間指定
    python scalp_backtest.py --touch-margin 10.0          # タッチマージン指定
"""
import sys, argparse, json
from pathlib import Path
from datetime import date, datetime, timezone
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import (connect_mt5, fetch_ohlcv, fetch_ohlcv_range,
                              generate_m5_from_h1, generate_m1_from_h1, generate_h1)
from core.indicators import add_m1_indicators, add_m5_indicators
from mt5_ea_bridge   import _detect_regime


# ──────────────────────────────────────────────────────────────
# データ取得
# ──────────────────────────────────────────────────────────────

def load_scalp_data(symbol: str, mt5_cfg: dict,
                    m5_bars: int, m1_bars: int,
                    date_from: datetime | None = None,
                    date_to:   datetime | None = None,
                    force_synthetic: bool = False,
                    ) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    """
    MT5 から M5 + M1 を取得して返す。
    connect → fetch M5 → fetch M1 → shutdown の順で実行するため
    load_data (内部で shutdown) を経由しない。
    失敗時は合成データにフォールバック。
    """
    if not force_synthetic:
        try:
            import MetaTrader5 as mt5
            if not connect_mt5(symbol, mt5_cfg):
                print("[警告] MT5 接続失敗 → 合成データにフォールバック")
            else:
                df_m5_raw = df_m1_raw = None
                try:
                    if date_from is not None:
                        df_m5_raw = fetch_ohlcv_range(symbol, 'M5', date_from, date_to)
                        df_m1_raw = fetch_ohlcv_range(symbol, 'M1', date_from, date_to)
                    else:
                        df_m5_raw = fetch_ohlcv(symbol, 'M5', m5_bars)
                        df_m1_raw = fetch_ohlcv(symbol, 'M1', m1_bars)
                except Exception as fe:
                    print(f"[警告] MT5 fetch 例外: {fe}")
                finally:
                    mt5.shutdown()

                if df_m5_raw is None:
                    print("[警告] M5 データ取得失敗")
                elif df_m1_raw is None:
                    print("[警告] M1 データ取得失敗")
                else:
                    # M5 と M1 の共通期間に揃える
                    common_start = max(df_m5_raw.index[0], df_m1_raw.index[0])
                    common_end   = min(df_m5_raw.index[-1], df_m1_raw.index[-1])
                    df_m5_raw = df_m5_raw[
                        (df_m5_raw.index >= common_start) & (df_m5_raw.index <= common_end)]
                    df_m1_raw = df_m1_raw[
                        (df_m1_raw.index >= common_start) & (df_m1_raw.index <= common_end)]
                    m5_days = (df_m5_raw.index[-1] - df_m5_raw.index[0]).days
                    print(f"  共通期間: {common_start.date()} 〜 {common_end.date()} "
                          f"({m5_days}日)")
                    if df_m5_raw.empty or df_m1_raw.empty:
                        print("[警告] 共通期間が空です")
                    else:
                        return df_m5_raw, df_m1_raw, True
        except ImportError:
            print("[警告] MetaTrader5 未インストール → 合成データを使用")
        except Exception as e:
            print(f"[警告] MT5 予期しないエラー: {e} → 合成データにフォールバック")

    print("[フォールバック] 合成データを使用")
    loc = C.LOCAL
    h1_synth = generate_h1(n=loc.get('h1_bars_synth', 3600))
    df_m5_raw = generate_m5_from_h1(h1_synth)
    df_m1_raw = generate_m1_from_h1(h1_synth)
    return df_m5_raw, df_m1_raw, False


# ──────────────────────────────────────────────────────────────
# シミュレーション
# ──────────────────────────────────────────────────────────────

def _regime(df_m5: pd.DataFrame, i: int, regime_cfg: dict) -> str:
    adx = float(df_m5['ADX'].iloc[i])    if 'ADX'      in df_m5.columns else float('nan')
    dip = float(df_m5['DI_plus'].iloc[i]) if 'DI_plus'  in df_m5.columns else float('nan')
    dim = float(df_m5['DI_minus'].iloc[i])if 'DI_minus' in df_m5.columns else float('nan')
    return _detect_regime(adx, dip, dim, regime_cfg)


def run_scalp_bt(df_m5: pd.DataFrame, df_m1: pd.DataFrame,
                 cfg: dict, touch_margin: float) -> list[dict]:
    scalp       = cfg['SCALP']
    regime_cfg  = cfg.get('REGIME', {})
    buy_thrs    = scalp.get('rsi_buy_thrs',  [55.0, 60.0, 65.0])
    sell_thrs   = scalp.get('rsi_sell_thrs', [45.0, 40.0, 35.0])
    buy_en      = scalp.get('buy_enabled',  True)
    sell_en     = scalp.get('sell_enabled', False)
    tp_frac     = scalp.get('tp_atr_fraction', 0.5)
    sl_ratio    = scalp.get('sl_ratio', 3)
    slope_bars  = scalp.get('sma20_slope_bars', 5)
    slope_thr   = scalp.get('sma20_slope_atr_thr', 0.10)
    cooldown_m  = scalp.get('cooldown_min', 15)
    max_day     = scalp.get('max_trades_day', 20)
    target_jpy  = scalp.get('target_profit_jpy', 1000)
    jpy_rate    = scalp.get('jpy_per_usd', 150.0)
    timeout_m1  = 30   # バー数 = 分
    hold_max_m1 = 60 * 8

    # numpy 配列（高速アクセス用）
    m1_t   = df_m1.index.to_numpy()
    m1_cl  = df_m1['Close'].to_numpy(dtype=float)
    m1_hi  = df_m1['High'].to_numpy(dtype=float)
    m1_lo  = df_m1['Low'].to_numpy(dtype=float)
    m1_s20 = df_m1['SMA20'].to_numpy(dtype=float)
    m1_atr = df_m1['ATR'].to_numpy(dtype=float)

    m5_t   = df_m5.index.to_numpy()
    m5_rsi = df_m5['RSI'].to_numpy(dtype=float)
    m5_atr = df_m5['ATR'].to_numpy(dtype=float)

    def m1_idx_at(ts):
        return int(np.searchsorted(m1_t, ts, side='left'))

    def find_entry(direction: str, m5_i: int) -> dict | None:
        atr_v = m5_atr[m5_i]
        if np.isnan(atr_v) or atr_v <= 0:
            return None
        tp_move = atr_v * tp_frac
        sl_move = tp_move * sl_ratio

        j0 = m1_idx_at(m5_t[m5_i])
        sma_found = None
        for j in range(j0, min(len(m1_t), j0 + timeout_m1)):
            s20 = m1_s20[j]; cl = m1_cl[j]
            if np.isnan(s20) or np.isnan(cl):
                continue
            if abs(cl - s20) > touch_margin:
                continue
            # SMA20 傾き確認
            if j >= slope_bars:
                atr_j  = m1_atr[j]
                s20_prv = m1_s20[j - slope_bars]
                if not (np.isnan(atr_j) or np.isnan(s20_prv)):
                    slope = s20 - s20_prv
                    if direction == 'buy'  and slope <= atr_j * slope_thr:
                        continue
                    if direction == 'sell' and slope >= -(atr_j * slope_thr):
                        continue
            sma_found = j
            break

        if sma_found is None:
            return None

        # 2本連続確認待ち
        count = 0
        for j in range(sma_found + 1, min(len(m1_t), sma_found + 1 + timeout_m1)):
            cur  = m1_cl[j]
            prev = m1_cl[j - 1]
            if np.isnan(cur) or np.isnan(prev):
                continue
            ok = (cur > prev) if direction == 'buy' else (cur < prev)
            if ok:
                count += 1
                if count >= 2:
                    return {
                        'direction':   direction,
                        'entry_time':  m1_t[j],
                        'entry_price': float(cur),
                        'confirm_bar': j,
                        'tp_move':     tp_move,
                        'sl_move':     sl_move,
                    }
            else:
                count = 0
        return None

    def simulate_exit(info: dict) -> tuple[float, str, object]:
        d  = info['direction']
        ep = info['entry_price']
        tp = ep + info['tp_move'] if d == 'buy' else ep - info['tp_move']
        sl = ep - info['sl_move'] if d == 'buy' else ep + info['sl_move']

        start = info['confirm_bar'] + 1
        end   = min(len(m1_t), start + hold_max_m1)
        for j in range(start, end):
            hi = m1_hi[j]; lo = m1_lo[j]
            if np.isnan(hi) or np.isnan(lo):
                continue
            if d == 'buy':
                if lo <= sl: return sl, 'sl', m1_t[j]  # 悲観: SL 優先
                if hi >= tp: return tp, 'tp', m1_t[j]
            else:
                if hi >= sl: return sl, 'sl', m1_t[j]
                if lo <= tp: return tp, 'tp', m1_t[j]

        j_last = min(end - 1, len(m1_t) - 1)
        return float(m1_cl[j_last]), 'timeout', m1_t[j_last]

    # ─ メインループ ─
    trades: list[dict] = []
    last_entry_ts: pd.Timestamp | None = None
    day_counts: dict[date, int] = {}
    m5_rsi_prev: float = float('nan')

    for i in range(15, len(m5_t)):
        rsi_cur  = m5_rsi[i]
        rsi_prev = m5_rsi_prev
        m5_rsi_prev = rsi_cur
        if np.isnan(rsi_cur) or np.isnan(rsi_prev):
            continue

        ts = pd.Timestamp(m5_t[i])

        # クールダウン
        if (last_entry_ts is not None and
                (ts - last_entry_ts).total_seconds() < cooldown_m * 60):
            continue

        # 日次上限
        dk = ts.date()
        if day_counts.get(dk, 0) >= max_day:
            continue

        # レジーム
        regime = _regime(df_m5, i, regime_cfg)

        # RSI クロス検出
        signal = None; crossed = 0.0
        if buy_en and regime != 'trend_down':
            for thr in buy_thrs:
                if rsi_cur > thr and rsi_prev <= thr:
                    signal = 'buy'; crossed = thr; break
        if signal is None and sell_en and regime != 'trend_up':
            for thr in sell_thrs:
                if rsi_cur < thr and rsi_prev >= thr:
                    signal = 'sell'; crossed = thr; break
        if signal is None:
            continue

        info = find_entry(signal, i)
        if info is None:
            continue

        exit_price, reason, exit_ts = simulate_exit(info)

        d  = info['direction']
        ep = info['entry_price']
        raw_pnl = (exit_price - ep) if d == 'buy' else (ep - exit_price)

        # 正規化 R: tp_move を 1 とした倍率
        R = raw_pnl / info['tp_move'] if info['tp_move'] > 0 else 0.0
        pnl_jpy = R * target_jpy

        entry_ts = pd.Timestamp(info['entry_time'])
        dur_min  = (pd.Timestamp(exit_ts) - entry_ts).total_seconds() / 60

        trades.append({
            'entry_time':    str(entry_ts),
            'exit_time':     str(pd.Timestamp(exit_ts)),
            'direction':     d,
            'crossed_level': crossed,
            'regime':        regime,
            'entry_price':   round(ep, 4),
            'exit_price':    round(float(exit_price), 4),
            'tp_move':       round(info['tp_move'], 4),
            'sl_move':       round(info['sl_move'], 4),
            'R':             round(R, 3),
            'pnl_jpy':       round(pnl_jpy, 0),
            'exit_reason':   reason,
            'duration_min':  round(dur_min, 1),
        })

        last_entry_ts = entry_ts
        day_counts[dk] = day_counts.get(dk, 0) + 1

    return trades


# ──────────────────────────────────────────────────────────────
# 統計表示
# ──────────────────────────────────────────────────────────────

def print_stats(trades: list[dict], target_jpy: int, sl_ratio: int,
                data_days: float) -> None:
    n = len(trades)
    if n == 0:
        print("  トレードなし")
        return

    pnl  = np.array([t['pnl_jpy'] for t in trades])
    wins = pnl[pnl > 0]
    loss = pnl[pnl <= 0]
    wr   = len(wins) / n

    gross_w = wins.sum()  if len(wins) else 0
    gross_l = abs(loss.sum()) if len(loss) else 1e-9
    pf      = gross_w / gross_l

    cum     = np.cumsum(pnl)
    peak    = np.maximum.accumulate(cum)
    dd      = (cum - peak)
    max_dd  = dd.min()

    avg_dur = np.mean([t['duration_min'] for t in trades])

    # 月次換算
    months      = max(data_days / 30, 0.1)
    trades_pm   = n / months
    expected_pm = pnl.sum() / months

    # Sharpe (simple, annualized)
    if pnl.std() > 0:
        sharpe = (pnl.mean() / pnl.std()) * np.sqrt(n * 30 / data_days * 12)
    else:
        sharpe = 0.0

    # exit reason breakdown
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t['exit_reason']] = reasons.get(t['exit_reason'], 0) + 1

    print(f"\n{'='*52}")
    print(f"  スキャルプ バックテスト 結果")
    print(f"{'='*52}")
    print(f"  期間:           {data_days:.0f} 日 ({months:.1f} ヶ月)")
    print(f"  対象 target:    {target_jpy:,} JPY / trade")
    print(f"  TP:SL 比率:     1:{sl_ratio}")
    print(f"  損益分岐 WR:    {sl_ratio/(sl_ratio+1)*100:.0f}%")
    print(f"\n  トレード数:     {n} ({trades_pm:.1f} 件/月)")
    print(f"  勝率:           {wr*100:.1f}%")
    print(f"  Profit Factor:  {pf:.2f}")
    print(f"  Sharpe (年率):  {sharpe:.2f}")
    print(f"\n  平均勝ち:       +{wins.mean():.0f} JPY" if len(wins) else "  平均勝ち:      -")
    print(f"  平均負け:       {loss.mean():.0f} JPY"   if len(loss) else "  平均負け:      -")
    print(f"  期待値/trade:   {pnl.mean():+.0f} JPY")
    print(f"\n  累積 PnL:       {pnl.sum():+,.0f} JPY")
    print(f"  最大 DD:        {max_dd:,.0f} JPY")
    print(f"  月次期待収益:   {expected_pm:+,.0f} JPY/月")
    print(f"\n  平均保有時間:   {avg_dur:.0f} 分")
    reason_str = "  ", "  ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
    print(f"  exit内訳:       {'  '.join(f'{k}={v}' for k, v in sorted(reasons.items()))}")

    # 方向別
    buys  = [t for t in trades if t['direction'] == 'buy']
    sells = [t for t in trades if t['direction'] == 'sell']
    if buys:
        b_pnl = np.array([t['pnl_jpy'] for t in buys])
        print(f"\n  BUY  {len(buys):3d}件  WR={len(b_pnl[b_pnl>0])/len(buys)*100:.1f}%  "
              f"累計={b_pnl.sum():+,.0f} JPY")
    if sells:
        s_pnl = np.array([t['pnl_jpy'] for t in sells])
        print(f"  SELL {len(sells):3d}件  WR={len(s_pnl[s_pnl>0])/len(sells)*100:.1f}%  "
              f"累計={s_pnl.sum():+,.0f} JPY")

    # 月次内訳
    if data_days > 30:
        print(f"\n  月次 PnL 内訳:")
        df_t = pd.DataFrame(trades)
        df_t['month'] = pd.to_datetime(df_t['entry_time']).dt.to_period('M')
        for m, grp in df_t.groupby('month'):
            mp = grp['pnl_jpy'].sum()
            mw = (grp['pnl_jpy'] > 0).sum()
            mn = len(grp)
            print(f"    {m}  {mn:3d}件  WR={mw/mn*100:.0f}%  {mp:+,.0f} JPY")

    print(f"{'='*52}\n")


# ──────────────────────────────────────────────────────────────
# エントリーポイント
# ──────────────────────────────────────────────────────────────

_FULL_M5  = 999_999   # MT5 が保持する最大本数をリクエスト
_FULL_M1  = 999_999
_FULL_H1  = 999_999


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol',       default=C.MT5['symbol'])
    ap.add_argument('--m5-bars',      type=int, default=None,
                    help=f'M5取得本数 (デフォルト: config値={C.MT5["m5_bars"]})')
    ap.add_argument('--m1-bars',      type=int, default=None,
                    help=f'M1取得本数 (デフォルト: config値={C.MT5["m1_bars"]})')
    ap.add_argument('--h1-bars',      type=int, default=None)
    ap.add_argument('--full-data',    action='store_true',
                    help='MT5 から取得できる全期間データを使用')
    ap.add_argument('--touch-margin', type=float, default=None,
                    help='SMA20 タッチマージン (例: 10.0). 省略時はキャッシュ→config順に読む')
    ap.add_argument('--from', dest='date_from', default=None, metavar='YYYY-MM-DD',
                    help='バックテスト開始日 (例: 2024-01-01)')
    ap.add_argument('--to',   dest='date_to',   default=None, metavar='YYYY-MM-DD',
                    help='バックテスト終了日 (例: 2024-06-30)')
    ap.add_argument('--synthetic',    action='store_true', help='強制的に合成データを使用')
    ap.add_argument('--output',       default='./output/scalp_bt.json')
    args = ap.parse_args()

    # 期間指定の解析
    date_from = date_to = None
    if args.date_from:
        date_from = datetime.strptime(args.date_from, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        date_to   = (datetime.strptime(args.date_to, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                     if args.date_to else datetime.now(timezone.utc))

    if args.full_data or date_from is not None:
        m5_bars = _FULL_M5
        m1_bars = _FULL_M1
        h1_bars = _FULL_H1
    else:
        m5_bars = args.m5_bars or C.MT5['m5_bars']
        m1_bars = args.m1_bars or C.MT5['m1_bars']
        h1_bars = args.h1_bars or C.MT5['h1_bars']

    cfg = {k: getattr(C, k) for k in
           ['MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES',
            'OPTIMIZE', 'LOCAL', 'PLOT', 'BRIDGE', 'SCALP', 'REGIME', 'TIME_BIAS']}
    cfg['MT5'] = {**cfg['MT5'], 'symbol': args.symbol,
                  'h1_bars': h1_bars, 'm1_bars': m1_bars, 'm5_bars': m5_bars}

    scalp      = cfg['SCALP']
    target_jpy = scalp.get('target_profit_jpy', 1000)
    sl_ratio   = scalp.get('sl_ratio', 3)

    if date_from:
        period_tag = f"{args.date_from} 〜 {args.date_to or '現在'}"
    elif args.full_data:
        period_tag = '全取得可能期間'
    else:
        period_tag = f'M5:{m5_bars}本 / M1:{m1_bars}本'
    print("=" * 52)
    print(f"  スキャルプ バックテスト  [{args.symbol}]  ({period_tag})")
    print("=" * 52)

    # タッチマージン決定
    touch_margin = args.touch_margin
    if touch_margin is None:
        cache_path = cfg['EXECUTION'].get(
            'sma20_touch_margin_file', './output/sma20_touch_margins.json')
        if Path(cache_path).exists():
            try:
                cached = json.loads(Path(cache_path).read_text())
                touch_margin = cached.get(args.symbol)
                if touch_margin:
                    print(f"  touch_margin: {touch_margin:.4f} (キャッシュ)")
            except Exception:
                pass
    if touch_margin is None:
        touch_margin = cfg['EXECUTION'].get('touch_margin', 0.20)
        print(f"  touch_margin: {touch_margin:.4f} (config fallback)")
    elif args.touch_margin is not None:
        print(f"  touch_margin: {touch_margin:.4f} (CLI指定)")

    # データ取得（MT5 接続 → M5+M1 取得 → shutdown を1回で完結）
    print("\n[1] データ取得")
    df_m5_raw, df_m1_raw, is_real = load_scalp_data(
        args.symbol, cfg['MT5'], m5_bars, m1_bars,
        date_from=date_from, date_to=date_to,
        force_synthetic=args.synthetic,
    )
    print(f"  ソース: {'MT5実データ' if is_real else '合成データ'}")

    # 指標
    print("\n[2] 指標計算")
    df_m5 = add_m5_indicators(df_m5_raw, cfg)
    df_m1 = add_m1_indicators(df_m1_raw, cfg)

    data_days = (df_m5.index[-1] - df_m5.index[0]).total_seconds() / 86400
    print(f"  M5: {len(df_m5)}本  M1: {len(df_m1)}本  "
          f"期間: {df_m5.index[0].date()} 〜 {df_m5.index[-1].date()} "
          f"({data_days:.0f}日)")

    # バックテスト実行
    print("\n[3] バックテスト実行中...")
    trades = run_scalp_bt(df_m5, df_m1, cfg, touch_margin)
    print(f"  完了: {len(trades)} トレード")

    # 結果表示
    print_stats(trades, target_jpy, sl_ratio, data_days)

    # 保存
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(trades, ensure_ascii=False, indent=2))
    print(f"  トレード詳細: {args.output}")


if __name__ == '__main__':
    main()
