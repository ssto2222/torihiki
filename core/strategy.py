"""core/strategy.py — シグナル検出・SL戦略・バックテストエンジン"""
from __future__ import annotations
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')


# ── H1 シグナル検出（SMA20 + RSI）────────────────────────────

def detect_sma_rsi_signals(df: pd.DataFrame, p: dict, direction: str) -> list[dict]:
    """
    H1 SMA20 + RSI シグナル検出（平均回帰）

    買い: RSI が buy_rsi_thr を下抜けて「売られすぎ」に突入
          かつ Close が SMA20 を下抜けた後に SMA20 を回復（反発確認）
          ※ SMA20 クロスのない場合は RSI 単独でも発火

    売り: RSI が sell_rsi_thr を上抜けて「買われすぎ」に突入
          かつ Close が SMA20 を上抜けた後に SMA20 を下抜け（反落確認）
          ※ SMA20 クロスのない場合は RSI 単独でも発火

    返り値: [{'signal_bar', 'signal_time', 'signal_price', 'atr'}, ...]
    """
    rsi   = df['RSI'].values
    sma   = df['SMA20'].values
    close = df['Close'].values
    atr   = df['ATR'].values
    n     = len(df)

    buy_th  = p.get('buy_rsi_thr',  38.0)
    sell_th = p.get('sell_rsi_thr', 62.0)

    results = []
    for i in range(1, n - 2):
        if np.isnan(sma[i]) or np.isnan(rsi[i]) or np.isnan(rsi[i-1]):
            continue
        if direction == 'buy':
            # RSI が buy_rsi_thr を下抜け → 売られすぎ突入
            if rsi[i] < buy_th and rsi[i-1] >= buy_th:
                results.append({
                    'signal_bar':   i,
                    'signal_time':  df.index[i],
                    'signal_price': float(close[i]),
                    'atr':          float(atr[i]) if not np.isnan(atr[i]) else 1.0,
                })
        else:
            # RSI が sell_rsi_thr を上抜け → 買われすぎ突入
            if rsi[i] > sell_th and rsi[i-1] <= sell_th:
                results.append({
                    'signal_bar':   i,
                    'signal_time':  df.index[i],
                    'signal_price': float(close[i]),
                    'atr':          float(atr[i]) if not np.isnan(atr[i]) else 1.0,
                })

    # 重複除去（10本以内は最初だけ）
    out, last = [], -99
    for r in sorted(results, key=lambda x: x['signal_bar']):
        if r['signal_bar'] - last > 10:
            out.append(r); last = r['signal_bar']
    return out


# ── M1 執行ロジック ────────────────────────────────────────

def find_m1_entry(df_m1: pd.DataFrame, signal_time: pd.Timestamp,
                  direction: str, cfg: dict,
                  rsi_thr: float) -> dict | None:
    """
    SMAプルバック + RSIゲートで M1 エントリーを探す

    Step1: Close が SMA20 を クロス
    Step2: Low/High が SMA±margin にタッチ
    Step3: Close > SMA(買い) / Close < SMA(売り)  かつ  RSI < thr(買い) / RSI > thr(売り)
    """
    exe    = cfg.get('EXECUTION', {})
    sl_cfg = cfg.get('SL', {})
    margin = exe.get('touch_margin',    0.20)
    valid  = exe.get('signal_valid_m1', 240)
    spread = sl_cfg.get('spread_usd',   0.30)

    slc = df_m1[df_m1.index >= signal_time].head(valid)
    if len(slc) < 25: return None

    sma   = slc['SMA20'].values
    rsi   = slc['RSI'].values   if 'RSI'  in slc.columns else np.full(len(slc), 50.0)
    close = slc['Close'].values
    high  = slc['High'].values
    low   = slc['Low'].values
    idx   = slc.index

    # Step1: SMA クロス
    cross = None
    for i in range(1, len(close)):
        if np.isnan(sma[i]): continue
        if direction == 'buy'  and close[i] > sma[i] and close[i-1] <= sma[i-1]:
            cross = i; break
        if direction == 'sell' and close[i] < sma[i] and close[i-1] >= sma[i-1]:
            cross = i; break
    if cross is None: return None

    # Step2: タッチ
    touch = None
    for i in range(cross + 1, len(close)):
        if np.isnan(sma[i]): continue
        if direction == 'buy'  and low[i]  <= sma[i] + margin: touch = i; break
        if direction == 'sell' and high[i] >= sma[i] - margin: touch = i; break
    if touch is None: return None

    # Step3: RSI ゲート（タッチ後20本以内）
    for i in range(touch, min(touch + 20, len(close))):
        if np.isnan(sma[i]) or np.isnan(rsi[i]): continue
        if direction == 'buy'  and close[i] > sma[i] and rsi[i] < rsi_thr:
            return {'entry_time':   idx[i],
                    'entry_price':  float(close[i]) + spread,
                    'sma_at_entry': float(sma[i]),
                    'rsi_at_entry': float(rsi[i])}
        if direction == 'sell' and close[i] < sma[i] and rsi[i] > rsi_thr:
            return {'entry_time':   idx[i],
                    'entry_price':  float(close[i]) - spread,
                    'sma_at_entry': float(sma[i]),
                    'rsi_at_entry': float(rsi[i])}
    return None


# ── SL 戦略クラス ──────────────────────────────────────────

class SLStrategy:
    name    = ''
    name_ja = ''
    color   = '#58a6ff'

    def calc_sl(self, ep: float, direction: str,
                bar: int, df: pd.DataFrame) -> float:
        raise NotImplementedError

    def update_sl(self, sl: float, direction: str,
                  bar: int, df: pd.DataFrame, ep: float) -> float:
        return sl   # デフォルト: 固定


class FixedSL(SLStrategy):
    """A. 固定SL（ベースライン）"""
    name = 'fixed'; name_ja = 'A. 固定SL ($15)'; color = '#8b949e'
    def __init__(self, usd=15.0): self.usd = usd
    def calc_sl(self, ep, d, b, df):
        return ep - self.usd if d == 'buy' else ep + self.usd


class AtrSL(SLStrategy):
    """B. ATR×1.5 SL"""
    name = 'atr'; name_ja = 'B. ATR×1.5 SL'; color = '#58a6ff'
    def __init__(self, multi=1.5): self.m = multi
    def calc_sl(self, ep, d, b, df):
        atr = float(df['ATR'].iloc[b])
        return ep - atr * self.m if d == 'buy' else ep + atr * self.m


class StructuralSL(SLStrategy):
    """C. 構造的SL（スイング安値/高値ベース）"""
    name = 'struct'; name_ja = 'C. 構造的SL'; color = '#e3b341'
    def __init__(self, buf=0.3): self.buf = buf
    def calc_sl(self, ep, d, b, df):
        atr = float(df['ATR'].iloc[b])
        if d == 'buy':
            return float(df['Swing_Low'].iloc[b])  - atr * self.buf
        else:
            return float(df['Swing_High'].iloc[b]) + atr * self.buf


class TwoStageSL(SLStrategy):
    """
    D. 二段階SL（急落バッファ型）
    通常時 ATR×1.0、ATR_ratio ≥ 1.8 で ATR×2.5 に自動拡大
    """
    name = 'two_stage'; name_ja = 'D. 二段階SL'; color = '#3fb950'
    def calc_sl(self, ep, d, b, df):
        atr = float(df['ATR'].iloc[b])
        return ep - atr * 1.0 if d == 'buy' else ep + atr * 1.0
    def update_sl(self, sl, d, b, df, ep):
        atr   = float(df['ATR'].iloc[b])
        ratio = float(df['ATR_ratio'].iloc[b]) if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        m     = 2.5 if ratio >= 1.8 else 1.0
        return ep - atr * m if d == 'buy' else ep + atr * m


class VolAdaptiveSL(SLStrategy):
    """
    E. ボラ適応型SL ★推奨★
    ATR_ratio に応じて SL 幅を動的調整
      < 0.8  → ×1.0（低ボラ）
      〜1.5  → ×1.5（通常）
      〜2.5  → ×2.5（高ボラ）
      > 2.5  → ×4.0（急落）
    """
    name = 'vol_adapt'; name_ja = 'E. ボラ適応型SL★'; color = '#f85149'

    def __init__(self, cfg: dict | None = None):
        sl = (cfg or {}).get('SL', {})
        self.low    = sl.get('sl_multi_low',    1.0)
        self.normal = sl.get('sl_multi_normal', 1.5)
        self.medium = sl.get('sl_multi_medium', 2.5)
        self.high   = sl.get('sl_multi_high',   4.0)
        self.thr_m  = sl.get('atr_ratio_medium', 1.5)
        self.thr_h  = sl.get('atr_ratio_high',   2.5)

    def _m(self, ratio: float) -> float:
        if np.isnan(ratio): return self.normal
        if ratio > self.thr_h:  return self.high
        elif ratio > self.thr_m: return self.medium
        elif ratio > 0.8:       return self.normal
        else:                   return self.low

    def calc_sl(self, ep, d, b, df):
        atr = float(df['ATR'].iloc[b])
        r   = float(df['ATR_ratio'].iloc[b]) if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        return ep - atr * self._m(r) if d == 'buy' else ep + atr * self._m(r)

    def update_sl(self, sl, d, b, df, ep):
        atr = float(df['ATR'].iloc[b])
        r   = float(df['ATR_ratio'].iloc[b]) if not np.isnan(df['ATR_ratio'].iloc[b]) else 1.0
        new = ep - atr * self._m(r) if d == 'buy' else ep + atr * self._m(r)
        return max(sl, new) if d == 'buy' else min(sl, new)


def get_all_strategies(cfg: dict | None = None) -> list[SLStrategy]:
    return [FixedSL(), AtrSL(), StructuralSL(), TwoStageSL(), VolAdaptiveSL(cfg)]


# ── バックテストエンジン ────────────────────────────────────

def run_backtest(df_h1: pd.DataFrame, df_m1: pd.DataFrame,
                 strategy: SLStrategy, sig_p: dict, cfg: dict,
                 direction: str = 'buy',
                 crash_bar_set: set | None = None) -> dict:
    """
    H1 シグナル → M1 エントリー → H1 バーで SL/TP 判定
    M1 の Open でスリッページを精密再現
    RSI≥75（買い）/ RSI≤25（売り）でトレーリング起動
    """
    sl_c     = cfg.get('SL', {})
    exe      = cfg.get('EXECUTION', {})
    spread   = sl_c.get('spread_usd',    0.30)
    hold_max = sl_c.get('hold_max_h1',   48)
    rsi_exit = sl_c.get('rsi_exit_thr',  75.0)
    trail_m  = sl_c.get('trail_multi',   1.5)
    tp_m     = sl_c.get('tp_atr_multi',  3.0)
    rsi_off  = exe.get('m1_rsi_offset',  20.0)

    crash_bars = crash_bar_set or set()
    signals    = detect_sma_rsi_signals(df_h1, sig_p, direction)

    m1_rsi_thr = (sig_p.get('buy_rsi_thr',  38.0) + rsi_off if direction == 'buy'
                  else sig_p.get('sell_rsi_thr', 62.0) - rsi_off)

    close_h1 = df_h1['Close'].values
    high_h1  = df_h1['High'].values
    low_h1   = df_h1['Low'].values
    atr_h1   = df_h1['ATR'].values
    rsi_h1   = df_h1['RSI'].values
    idx_h1   = df_h1.index
    n_h1     = len(df_h1)

    trades     = []
    used_until = pd.Timestamp.min

    for sig in signals:
        if sig['signal_time'] < used_until:
            continue

        info = find_m1_entry(df_m1, sig['signal_time'], direction, cfg, m1_rsi_thr)
        if info is None:
            continue

        ep  = info['entry_price']
        sb  = sig['signal_bar']
        atr = float(atr_h1[sb]) if not np.isnan(atr_h1[sb]) else sig['atr']

        sl      = strategy.calc_sl(ep, direction, sb, df_h1)
        sl_dist = abs(ep - sl)

        # SL 距離フィルタ
        if sl_dist < atr * 0.3 or sl_dist > atr * 6:
            continue

        tp         = ep + atr * tp_m if direction == 'buy' else ep - atr * tp_m
        trail_sl   = None
        rsi_trig   = False
        best_price = ep
        was_crash  = False

        try:
            epos = df_h1.index.searchsorted(info['entry_time'])
        except Exception:
            continue

        xp, xt, reason = None, None, 'timeout'

        for b in range(epos + 1, min(epos + hold_max, n_h1)):
            h_b  = high_h1[b]
            l_b  = low_h1[b]
            a_b  = float(atr_h1[b]) if not np.isnan(atr_h1[b]) else atr
            r_b  = float(rsi_h1[b])

            if b in crash_bars: was_crash = True

            # SL 更新（戦略依存）
            sl = strategy.update_sl(sl, direction, b, df_h1, ep)

            # RSI 連動トレーリング起動
            if not rsi_trig:
                trig = ((direction == 'buy'  and r_b >= rsi_exit) or
                        (direction == 'sell' and r_b <= 100 - rsi_exit))
                if trig:
                    rsi_trig = True
                    trail_sl = (h_b - a_b * trail_m if direction == 'buy'
                                else l_b + a_b * trail_m)
                    trail_sl = (max(trail_sl, sl) if direction == 'buy'
                                else min(trail_sl, sl))

            if trail_sl is not None:
                if direction == 'buy':
                    if h_b > best_price: best_price = h_b
                    nt = best_price - a_b * trail_m
                    if nt > trail_sl: trail_sl = nt
                else:
                    if l_b < best_price: best_price = l_b
                    nt = best_price + a_b * trail_m
                    if nt < trail_sl: trail_sl = nt

            eff_sl = (max(sl, trail_sl) if trail_sl is not None and direction == 'buy'
                      else min(sl, trail_sl) if trail_sl is not None else sl)

            # SL 到達: M1 で精密なスリッページ計算
            sl_hit = ((direction == 'buy'  and l_b <= eff_sl) or
                      (direction == 'sell' and h_b >= eff_sl))
            if sl_hit:
                h1_time  = idx_h1[b]
                m1_slice = df_m1[(df_m1.index >= h1_time) &
                                  (df_m1.index <  h1_time + pd.Timedelta(hours=1))]
                actual   = eff_sl
                slip_usd = 0.0
                if len(m1_slice) > 0:
                    for _, mr in m1_slice.iterrows():
                        mo = float(mr['Open'])
                        if (direction == 'buy'  and mo < eff_sl) or \
                           (direction == 'sell' and mo > eff_sl):
                            actual   = mo
                            slip_usd = abs(eff_sl - mo)
                            break
                xp     = actual
                xt     = idx_h1[b]
                reason = 'sl_slip' if slip_usd > 0.5 else 'sl'
                break

            # TP 到達
            if ((direction == 'buy'  and h_b >= tp) or
                (direction == 'sell' and l_b <= tp)):
                xp = tp; xt = idx_h1[b]; reason = 'tp'; break

        if xp is None:
            eb = min(epos + hold_max, n_h1 - 1)
            xp = float(close_h1[eb]); xt = idx_h1[eb]; reason = 'timeout'
            slip_usd = 0.0

        pnl  = ((xp - ep) if direction == 'buy' else (ep - xp)) * 100
        trades.append({
            'direction':     direction,
            'entry_time':    info['entry_time'],
            'exit_time':     xt,
            'entry_price':   ep,
            'exit_price':    xp,
            'sl_dist':       sl_dist,
            'slippage_usd':  slip_usd,
            'pnl':           pnl,
            'reason':        reason,
            'was_crash':     was_crash,
            'rsi_triggered': rsi_trig,
        })
        used_until = xt

    return _metrics(trades, strategy)


def _metrics(trades: list, strategy: SLStrategy) -> dict:
    empty = dict(strategy=strategy.name_ja, color=strategy.color,
                 trades=[], n_trades=0, total_pnl=0.0, win_rate=0.0,
                 profit_factor=0.0, max_dd=0.0, sharpe=0.0,
                 sl_hit_rate=0.0, slip_rate=0.0,
                 avg_sl_dist=0.0, avg_slip_usd=0.0,
                 crash_survival=0.0, max_consec_loss=0,
                 equity=[], pnls=[], reason_counts={})
    if not trades: return empty

    pnls   = np.array([t['pnl'] for t in trades])
    equity = np.cumsum(pnls)
    peak   = np.maximum.accumulate(equity)
    wins   = pnls[pnls > 0]; losses = pnls[pnls <= 0]
    gp     = wins.sum()    if len(wins)   > 0 else 0.0
    gl     = -losses.sum() if len(losses) > 0 else 1e-9

    sl_h  = [t for t in trades if 'sl' in t.get('reason', '')]
    sl_s  = [t for t in trades if t.get('reason') == 'sl_slip']
    cr_t  = [t for t in trades if t.get('was_crash')]
    c_sur = sum(1 for t in cr_t if t['pnl'] > -30) / max(len(cr_t), 1)
    max_cl = cur_cl = 0
    for p in pnls:
        if p < 0: cur_cl += 1; max_cl = max(max_cl, cur_cl)
        else:     cur_cl = 0

    sharpe = (pnls.mean() / (pnls.std() + 1e-9)
              * np.sqrt(252 * 24)) if len(pnls) > 1 else 0.0

    return dict(
        strategy        = strategy.name_ja,
        color           = strategy.color,
        trades          = trades,
        n_trades        = len(trades),
        total_pnl       = float(pnls.sum()),
        win_rate        = float(len(wins) / max(len(pnls), 1)),
        profit_factor   = float(gp / gl),
        max_dd          = float((peak - equity).max()),
        sharpe          = float(sharpe),
        sl_hit_rate     = float(len(sl_h) / max(len(trades), 1)),
        slip_rate       = float(len(sl_s) / max(len(trades), 1)),
        avg_sl_dist     = float(np.mean([t['sl_dist'] for t in trades])),
        avg_slip_usd    = float(np.mean([t['slippage_usd'] for t in trades])),
        crash_survival  = float(c_sur),
        max_consec_loss = int(max_cl),
        equity          = equity.tolist(),
        pnls            = pnls.tolist(),
        reason_counts   = {r: sum(1 for t in trades if t.get('reason') == r)
                           for r in set(t.get('reason', '') for t in trades)},
    )
