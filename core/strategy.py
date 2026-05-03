"""core/strategy.py — シグナル検出・SL戦略・バックテストエンジン"""
from __future__ import annotations
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')


# ── H1 シグナル検出（SMA20 + RSI）────────────────────────────

def detect_sma_rsi_signals(df: pd.DataFrame, p: dict, direction: str) -> list[dict]:
    """
    H1 RSI シグナル検出

    買い（2種類）:
      DIP:      RSI が buy_rsi_thr を下抜け（売られすぎ）
      MOMENTUM: RSI が momentum_thrs の各値を上抜け（モメンタム）
                XAU好適ゾーン 55-80・BTC好適ゾーン 60-85 へのエントリー

    売り: RSI が sell_rsi_thr を上抜け（禁止）

    返り値: [{'signal_bar', 'signal_time', 'signal_price', 'atr', 'signal_type'}, ...]
    """
    rsi   = df['RSI'].values
    sma   = df['SMA20'].values
    close = df['Close'].values
    atr   = df['ATR'].values
    n     = len(df)

    buy_th       = p.get('buy_rsi_thr',   45.0)
    sell_th      = p.get('sell_rsi_thr',  62.0)
    mom_thrs     = sorted(p.get('momentum_thrs', [55.0, 60.0, 65.0, 70.0, 75.0]))

    results = []
    for i in range(1, n - 2):
        if np.isnan(sma[i]) or np.isnan(rsi[i]) or np.isnan(rsi[i-1]):
            continue
        if direction == 'buy':
            if rsi[i] < buy_th and rsi[i-1] >= buy_th:
                results.append({
                    'signal_bar':   i,
                    'signal_time':  df.index[i],
                    'signal_price': float(close[i]),
                    'atr':          float(atr[i]) if not np.isnan(atr[i]) else 1.0,
                    'signal_type':  'dip',
                })
            else:
                for m_thr in mom_thrs:
                    if rsi[i] > m_thr and rsi[i-1] <= m_thr:
                        results.append({
                            'signal_bar':   i,
                            'signal_time':  df.index[i],
                            'signal_price': float(close[i]),
                            'atr':          float(atr[i]) if not np.isnan(atr[i]) else 1.0,
                            'signal_type':  f'momentum_{int(m_thr)}',
                        })
                        break  # 1バーで複数閾値を同時超えしない
        else:
            if rsi[i] > sell_th and rsi[i-1] <= sell_th:
                results.append({
                    'signal_bar':   i,
                    'signal_time':  df.index[i],
                    'signal_price': float(close[i]),
                    'atr':          float(atr[i]) if not np.isnan(atr[i]) else 1.0,
                    'signal_type':  'sell',
                })

    # 重複除去（5本以内は最初だけ）
    out, last = [], -99
    for r in sorted(results, key=lambda x: x['signal_bar']):
        if r['signal_bar'] - last > 5:
            out.append(r); last = r['signal_bar']
    return out


# ── M5 エントリーフィルタ ──────────────────────────────────

def check_m5_entry_filter(rsi_m5: float, rsi_m5_prev: float,
                           rsi_d1: float, symbol: str) -> bool:
    """
    M5 RSI エントリータイミングフィルタ。
    RSI が上昇中（rising）かつ指定ゾーンにある場合のみ True を返す。

    BTCUSD:
      - 押し目ゾーン  : 40〜55
      - モメンタムゾーン: 70〜80
      - D1 強い時のみ : >80（rsi_d1 > 70 が必要）
      - 禁止ゾーン    : 60〜70（シグナル反転多発帯）

    XAUUSD:
      - 有効ゾーン    : 50〜70
      - >80 は絶対禁止（急反転リスク）
    """
    if np.isnan(rsi_m5) or np.isnan(rsi_m5_prev):
        return False
    rising = rsi_m5 > rsi_m5_prev
    if not rising:
        return False

    if symbol == 'BTCUSD':
        zone_ok = (
            (40 <= rsi_m5 <= 55) or
            (70 <= rsi_m5 <= 80) or
            (rsi_m5 > 80 and rsi_d1 > 70)
        )
        forbidden = (60 <= rsi_m5 <= 70)
        return zone_ok and not forbidden
    else:  # XAUUSD / default
        return (50 <= rsi_m5 <= 70) and rsi_m5 < 80


def check_m5_surge(df_m5: pd.DataFrame,
                   lookback: int = 5,
                   threshold: float = 20.0) -> str:
    """
    M5 RSI の急変（急騰・急落）を検出する。

    lookback 本前の RSI と現在の RSI の差が threshold 以上なら急変と判定。
    デフォルト: 5本（25分）で 20 ポイント超の変化。

    Returns: 'rapid_rise' / 'rapid_fall' / 'none'
    """
    if df_m5 is None or 'RSI' not in df_m5.columns:
        return 'none'
    rsi = df_m5['RSI'].dropna()
    if len(rsi) < lookback + 1:
        return 'none'
    delta = float(rsi.iloc[-1]) - float(rsi.iloc[-1 - lookback])
    if delta >= threshold:
        return 'rapid_rise'
    if delta <= -threshold:
        return 'rapid_fall'
    return 'none'


def detect_early_surge(df_m5: pd.DataFrame, cfg: dict) -> dict:
    """
    急騰初期検知: RVOLと価格加速を使って急騰の始まりを検知

    Returns: {
        'is_early_surge': bool,
        'surge_strength': float (0-1),
        'confidence': float (0-1)
    }
    """
    if df_m5 is None or len(df_m5) < 20:
        return {'is_early_surge': False, 'surge_strength': 0.0, 'confidence': 0.0}

    # RVOLと価格加速の確認
    has_volume_data = 'RVOL' in df_m5.columns and 'Price_Accel' in df_m5.columns

    if not has_volume_data:
        # 出来高データがない場合は従来のRSI急変検知を使用
        surge_type = check_m5_surge(df_m5)
        is_early = surge_type == 'rapid_rise'
        return {
            'is_early_surge': is_early,
            'surge_strength': 1.0 if is_early else 0.0,
            'confidence': 0.5 if is_early else 0.0
        }

    # RVOLベースの検知
    rvol = df_m5['RVOL'].iloc[-1]
    price_accel = df_m5['Price_Accel'].iloc[-1]
    volume_surge = df_m5['Volume_Surge'].iloc[-1] if 'Volume_Surge' in df_m5.columns else False

    # 急騰初期の条件
    rvol_threshold = cfg.get('INDICATOR', {}).get('early_surge_rvol_threshold', 1.3)
    accel_threshold = cfg.get('INDICATOR', {}).get('early_surge_accel_threshold', 0.5)

    is_early_surge = (
        rvol > rvol_threshold and
        price_accel > accel_threshold and
        volume_surge
    )

    # 強度と信頼度の計算
    surge_strength = min(1.0, (rvol - 1.0) * 0.5 + price_accel * 0.3)
    confidence = min(1.0, rvol * 0.4 + (1.0 if volume_surge else 0.0) * 0.6)

    return {
        'is_early_surge': is_early_surge,
        'surge_strength': surge_strength,
        'confidence': confidence
    }


def should_avoid_entry_during_surge(df_m5: pd.DataFrame, cfg: dict) -> bool:
    """
    急騰中段階でのエントリーを避けるべきかを判定

    急騰がすでに進んでいて、反転リスクが高い場合にTrueを返す
    """
    if df_m5 is None or len(df_m5) < 10:
        return False

    # RSIがすでに高すぎる場合（70以上）は避ける
    rsi_current = df_m5['RSI'].iloc[-1]
    rsi_overbought = cfg.get('INDICATOR', {}).get('surge_overbought_threshold', 70.0)

    if rsi_current > rsi_overbought:
        return True

    # 価格加速が極端に高い場合（すでに急騰が長時間続いている）
    if 'Price_Accel' in df_m5.columns:
        accel_recent = df_m5['Price_Accel'].tail(5).mean()
        accel_threshold = cfg.get('INDICATOR', {}).get('surge_avoid_accel_threshold', 1.5)
        if accel_recent > accel_threshold:
            return True

    return False


def detect_big_move(df_m5: pd.DataFrame,
                    lookback: int = 12,
                    atr_multi: float = 2.0) -> str:
    """
    大変動検知: スキャルプ→通常モード自動切換えのトリガー。

    2つの条件どちらかで判定:
      1. 60分（M5×12本）の価格変動が ATR×atr_multi を超える（方向性大変動）
      2. 直近 ATR が 20 本移動平均の 1.8 倍超（ボラスパイク）

    Returns: 'up' / 'down' / 'none'
    """
    if df_m5 is None or 'ATR' not in df_m5.columns or len(df_m5) < lookback + 1:
        return 'none'
    atr = float(df_m5['ATR'].iloc[-1])
    if atr <= 0:
        return 'none'
    close_now  = float(df_m5['Close'].iloc[-1])
    close_prev = float(df_m5['Close'].iloc[-1 - lookback])
    change     = close_now - close_prev

    # 条件①: 方向性変動 > ATR × atr_multi
    if abs(change) > atr * atr_multi:
        return 'up' if change > 0 else 'down'

    # 条件②: ATR スパイク (現在 ATR / 20 本 MA > 1.8)
    atr_ma = df_m5['ATR'].rolling(20, min_periods=10).mean().iloc[-1]
    if not np.isnan(atr_ma) and atr_ma > 0 and atr / atr_ma > 1.8:
        return 'up' if change > 0 else 'down'

    return 'none'


def find_m5_entry(df_m5: pd.DataFrame, signal_time: pd.Timestamp,
                  direction: str, cfg: dict,
                  rsi_d1: float = 50.0,
                  symbol: str = 'BTCUSD') -> dict | None:
    """
    H1 シグナル発火後、最初に RSI が上昇している M5 バーをエントリーに使う。
    M5 RSI ゾーン条件は m5_bonus フラグとして記録（ゲートではなくボーナス）。
    """
    exe    = cfg.get('EXECUTION', {})
    sl_cfg = cfg.get('SL', {})
    valid  = max(exe.get('signal_valid_m1', 240) // 5, 12)
    spread = sl_cfg.get('spread_usd', 0.30)

    slc = df_m5[df_m5.index >= signal_time].head(valid)
    if len(slc) < 3:
        return None

    rsi   = slc['RSI'].values
    close = slc['Close'].values
    idx   = slc.index

    for i in range(1, len(close)):
        if np.isnan(rsi[i]) or np.isnan(rsi[i - 1]):
            continue
        if direction == 'buy' and rsi[i] > rsi[i - 1]:  # rising のみ（ゾーン制限なし）
            ep = float(close[i]) + spread
            return {
                'entry_time':      idx[i],
                'entry_price':     ep,
                'sma_at_entry':    float(close[i]),
                'rsi_at_entry':    float(rsi[i]),
                'm5_bonus':        check_m5_entry_filter(rsi[i], rsi[i-1], rsi_d1, symbol),
            }
    return None


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
                 crash_bar_set: set | None = None,
                 df_m5: pd.DataFrame | None = None) -> dict:
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

    # trading_rules フィルタ（buy のみ有効、import 失敗時はスキップ）
    try:
        from trading_rules import RulesEngine as _RE
        _rules_engine = _RE()
        symbol    = cfg.get('MT5', {}).get('symbol', 'BTCUSD')
        min_score = cfg.get('RULES', {}).get('min_score', 0)
        rsi_d1    = df_h1.get('RSI_D1') if hasattr(df_h1, 'get') else df_h1['RSI_D1'] if 'RSI_D1' in df_h1.columns else None

        filtered = []
        for sig in signals:
            st         = sig['signal_time']
            hour_utc   = st.hour
            minute_utc = st.minute
            dow        = st.dayofweek
            rsi_h1_v = float(df_h1['RSI'].iloc[sig['signal_bar']])
            rsi_d1_v = float(rsi_d1.iloc[sig['signal_bar']]) if rsi_d1 is not None and not np.isnan(rsi_d1.iloc[sig['signal_bar']]) else 50.0
            res = _rules_engine.evaluate(
                symbol=symbol, rsi_h1=rsi_h1_v, rsi_d1=rsi_d1_v,
                direction=direction, hour_utc=hour_utc, dow=dow,
                minute_utc=minute_utc,
            )
            if res.signal in ('BUY', 'SELL') and res.score >= min_score:
                filtered.append(sig)
        signals = filtered
    except Exception:
        pass  # trading_rules 未インストール / 使用不可の場合はフィルタなし

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

        # D1 RSI（M5フィルタ + rules フィルタ共用）
        sb       = sig['signal_bar']
        rsi_d1_v = (float(df_h1['RSI_D1'].iloc[sb])
                    if 'RSI_D1' in df_h1.columns and not np.isnan(df_h1['RSI_D1'].iloc[sb])
                    else 50.0)
        symbol   = cfg.get('MT5', {}).get('symbol', 'BTCUSD')

        # M5 優先、なければ M1 フォールバック
        if df_m5 is not None and not df_m5.empty:
            info = find_m5_entry(df_m5, sig['signal_time'], direction, cfg, rsi_d1_v, symbol)
        else:
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

        xp, xt, reason, slip_usd = None, None, 'timeout', 0.0

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
