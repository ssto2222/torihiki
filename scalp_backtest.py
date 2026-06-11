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
from datetime import datetime, timezone
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import (connect_mt5, fetch_ohlcv, fetch_ohlcv_range,
                              generate_m5_from_h1, generate_m1_from_h1, generate_h1)
from core.indicators import add_m1_indicators, add_m5_indicators, add_h1_indicators
from core.patterns   import detect_all_patterns
from core.strategy   import (detect_whipsaw,
                              detect_elliott_w2_buy, detect_elliott_w2_sell,
                              detect_volume_breakout)
from bridge.utils    import _detect_regime


# ──────────────────────────────────────────────────────────────
# データ取得
# ──────────────────────────────────────────────────────────────

def load_scalp_data(symbol: str, mt5_cfg: dict,
                    m5_bars: int, m1_bars: int,
                    date_from: datetime | None = None,
                    date_to:   datetime | None = None,
                    force_synthetic: bool = False,
                    ) -> tuple[pd.DataFrame, pd.DataFrame,
                               pd.DataFrame | None, pd.DataFrame | None, bool]:
    """
    MT5 から M5 + M1 + M15 + H1 を取得して返す。
    connect → fetch all TF → shutdown の順で実行するため shutdown は 1 回のみ。
    M15/H1 取得失敗時は None を返す（MTF フィルタが無効化される）。
    失敗時は合成データにフォールバック（M15/H1 は None）。
    """
    if not force_synthetic:
        try:
            import MetaTrader5 as mt5
            if not connect_mt5(symbol, mt5_cfg):
                print("[警告] MT5 接続失敗 → 合成データにフォールバック")
            else:
                df_m5_raw = df_m1_raw = df_m15_raw = df_h1_raw = None
                try:
                    if date_from is not None:
                        df_m5_raw  = fetch_ohlcv_range(symbol, 'M5',  date_from, date_to)
                        df_m1_raw  = fetch_ohlcv_range(symbol, 'M1',  date_from, date_to)
                        df_m15_raw = fetch_ohlcv_range(symbol, 'M15', date_from, date_to)
                        df_h1_raw  = fetch_ohlcv_range(symbol, 'H1',  date_from, date_to)
                    else:
                        df_m5_raw  = fetch_ohlcv(symbol, 'M5',  m5_bars)
                        df_m1_raw  = fetch_ohlcv(symbol, 'M1',  m1_bars)
                        df_m15_raw = fetch_ohlcv(symbol, 'M15', min(m5_bars // 3 + 50, 5000))
                        df_h1_raw  = fetch_ohlcv(symbol, 'H1',  min(m5_bars // 12 + 50, 2000))
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
                    # M15/H1 も共通期間に絞る（なければ None のまま）
                    if df_m15_raw is not None and not df_m15_raw.empty:
                        df_m15_raw = df_m15_raw[
                            (df_m15_raw.index >= common_start) &
                            (df_m15_raw.index <= common_end)]
                    if df_h1_raw is not None and not df_h1_raw.empty:
                        df_h1_raw = df_h1_raw[
                            (df_h1_raw.index >= common_start) &
                            (df_h1_raw.index <= common_end)]
                    m5_days = (df_m5_raw.index[-1] - df_m5_raw.index[0]).days
                    print(f"  共通期間: {common_start.date()} 〜 {common_end.date()} "
                          f"({m5_days}日)")
                    if df_m5_raw.empty or df_m1_raw.empty:
                        print("[警告] 共通期間が空です")
                    else:
                        return df_m5_raw, df_m1_raw, df_m15_raw, df_h1_raw, True
        except ImportError:
            print("[警告] MetaTrader5 未インストール → 合成データを使用")
        except Exception as e:
            print(f"[警告] MT5 予期しないエラー: {e} → 合成データにフォールバック")

    print("[フォールバック] 合成データを使用")
    loc = C.LOCAL
    h1_synth  = generate_h1(n=loc.get('h1_bars_synth', 3600))
    df_m5_raw = generate_m5_from_h1(h1_synth)
    df_m1_raw = generate_m1_from_h1(h1_synth)
    # H1 も渡して MTF レジームフィルタを有効化（None だと全シグナルがブロックされる）
    return df_m5_raw, df_m1_raw, None, h1_synth, False


# ──────────────────────────────────────────────────────────────
# シミュレーション
# ──────────────────────────────────────────────────────────────

def _regime(df_m5: pd.DataFrame, i: int, regime_cfg: dict) -> str:
    adx = float(df_m5['ADX'].iloc[i])    if 'ADX'      in df_m5.columns else float('nan')
    dip = float(df_m5['DI_plus'].iloc[i]) if 'DI_plus'  in df_m5.columns else float('nan')
    dim = float(df_m5['DI_minus'].iloc[i])if 'DI_minus' in df_m5.columns else float('nan')
    return _detect_regime(adx, dip, dim, regime_cfg)


def _precompute_h1_crossings(df_h1_raw: 'pd.DataFrame | None',
                              min_conf: float = 0.45,
                              lookback: int = 150,
                              step: int = 12) -> list[tuple]:
    """H1 ネックライン突破イベント先行計算（ルックアヘッドなし）。
    各エントリ: (crossing_ts, direction, neckline, target)"""
    if df_h1_raw is None or len(df_h1_raw) < lookback:
        return []
    closes     = df_h1_raw['Close'].to_numpy(dtype=float)
    times      = df_h1_raw.index.to_numpy()
    n          = len(df_h1_raw)
    events:    list[tuple] = []
    traded_fp: set         = set()
    active_pats: list      = []
    for k in range(lookback, n):
        if (k - lookback) % step == 0:
            past = df_h1_raw.iloc[k - lookback: k]
            try:
                active_pats = [p for p in detect_all_patterns(past, window=5, top_n=3)
                               if p.confidence >= min_conf]
            except Exception:
                active_pats = []
        prev_c = closes[k - 1]
        cur_c  = closes[k]
        for pat in active_pats:
            fp = (pat.name, round(pat.neckline, 0))
            if fp in traded_fp:
                continue
            if pat.direction == 'bullish' and prev_c <= pat.neckline < cur_c:
                events.append((times[k], 'buy',  pat.neckline, pat.target))
                traded_fp.add(fp)
            elif pat.direction == 'bearish' and prev_c >= pat.neckline > cur_c:
                events.append((times[k], 'sell', pat.neckline, pat.target))
                traded_fp.add(fp)
    return sorted(events, key=lambda e: e[0])


def run_scalp_bt(df_m5: pd.DataFrame, df_m1: pd.DataFrame,
                 cfg: dict, touch_margin: float,
                 df_m15: pd.DataFrame | None = None,
                 df_h1:  pd.DataFrame | None = None,
                 h1_crossings: list | None = None) -> list[dict]:
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
    target_jpy  = scalp.get('target_profit_jpy', 1000)
    jpy_rate    = scalp.get('jpy_per_usd', 150.0)
    timeout_m1  = 30   # バー数 = 分
    hold_max_m1 = 60 * 8

    # ── SMA20 バイパス設定 ──────────────────────────────────────
    buy_bypass  = scalp.get('buy_sma_bypass_atr',  2.0)
    sell_bypass = scalp.get('sell_sma_bypass_atr', 2.0)

    # ── Elliott Wave 2 設定 ─────────────────────────────────────
    ew_cfg          = cfg.get('ELLIOTT', {})
    _ew_enabled     = ew_cfg.get('enabled', True)
    _ew_lb          = ew_cfg.get('lookback_bars', 40)
    _ew_sw          = ew_cfg.get('sw_window', 3)
    _ew_fm          = ew_cfg.get('fib_min', 0.382)
    _ew_fmx         = ew_cfg.get('fib_max', 0.786)
    _ew_w1a         = ew_cfg.get('min_wave1_atr', 1.5)
    _ew_div         = ew_cfg.get('rsi_div_min', 3.0)
    _ew_bago        = ew_cfg.get('w2_bars_ago_max', 5)
    _ew_ext         = ew_cfg.get('fib_tp_ext', 1.618)
    _ew_slb         = ew_cfg.get('sl_buffer_atr', 0.3)
    _ew2_buy_rsi_max  = ew_cfg.get('w2_buy_rsi_max', 45.0)
    _ew2_sell_rsi_min = ew_cfg.get('w2_sell_rsi_min', 55.0)
    _ew_slice_len   = _ew_lb + _ew_sw + 10   # EW2 検出に必要なスライス長

    # ── ウィップソー設定 ────────────────────────────────────────
    ws_cfg  = cfg.get('WHIPSAW', {})
    _ws_n   = ws_cfg.get('ratio_n',  20)
    _ws_thr = ws_cfg.get('ratio_thr', 2.0)

    # ── ボリュームブレイクアウト設定 ────────────────────────────
    _vb_enabled      = scalp.get('vol_bo_enabled', True)
    _vb_tp_m         = scalp.get('vol_bo_tp_multi', 1.8)
    _vb_sl_m         = scalp.get('vol_bo_sl_multi', 0.8)
    _vb_rsi_buy_min  = scalp.get('vol_bo_rsi_buy_min', 52.0)
    _vb_rsi_sell_max = scalp.get('vol_bo_rsi_sell_max', 48.0)
    _has_rvol        = 'RVOL' in df_m5.columns

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

    # MTF フィルタ用配列
    m5_s20 = pd.Series(df_m5['Close'].values).rolling(20).mean().to_numpy(dtype=float)

    m15_t = m15_s20 = m15_atr_arr = None
    if df_m15 is not None and not df_m15.empty and 'SMA20' in df_m15.columns:
        m15_t       = df_m15.index.to_numpy()
        m15_s20     = df_m15['SMA20'].to_numpy(dtype=float)
        m15_atr_arr = df_m15['ATR'].to_numpy(dtype=float) if 'ATR' in df_m15.columns else None

    h1_t = h1_adx = h1_dip = h1_dim = None
    if df_h1 is not None and not df_h1.empty:
        h1_t   = df_h1.index.to_numpy()
        h1_adx = df_h1['ADX'].to_numpy(dtype=float)      if 'ADX'      in df_h1.columns else None
        h1_dip = df_h1['DI_plus'].to_numpy(dtype=float)  if 'DI_plus'  in df_h1.columns else None
        h1_dim = df_h1['DI_minus'].to_numpy(dtype=float) if 'DI_minus' in df_h1.columns else None

    def _slope_ok(s20_arr, atr_arr, idx: int, direction: str) -> bool:
        if s20_arr is None or idx < slope_bars:
            return True
        sma_now  = s20_arr[idx]
        sma_prev = s20_arr[idx - slope_bars]
        if np.isnan(sma_now) or np.isnan(sma_prev):
            return True
        slope = sma_now - sma_prev
        thr_v = (float(atr_arr[idx]) * slope_thr
                 if atr_arr is not None and not np.isnan(atr_arr[idx]) else 0.0)
        return slope > thr_v if direction == 'buy' else slope < -thr_v

    def _h1_regime_at(ts) -> str:
        if h1_t is None or h1_adx is None:
            return 'weak_trend'
        idx = int(np.searchsorted(h1_t, ts, side='right')) - 1
        if idx < 0:
            return 'weak_trend'
        adx = float(h1_adx[idx])
        dip = float(h1_dip[idx]) if h1_dip is not None else float('nan')
        dim = float(h1_dim[idx]) if h1_dim is not None else float('nan')
        return _detect_regime(adx, dip, dim, regime_cfg)

    def _mtf_ok(ts, m5_i: int, direction: str) -> bool:
        # 案A: H1 weak_trend も許可し DI 方向で判断（production と同じ）
        h1_reg = _h1_regime_at(ts)
        if h1_t is not None and h1_dip is not None and h1_dim is not None:
            h1_i = int(np.searchsorted(h1_t, ts, side='right')) - 1
            if h1_i >= 0:
                _dip = float(h1_dip[h1_i])
                _dim = float(h1_dim[h1_i])
                _di_valid = not np.isnan(_dip) and not np.isnan(_dim)
                if direction == 'buy':
                    if h1_reg not in ('trend_up', 'weak_trend'):
                        return False
                    if not _di_valid or _dip <= _dim:
                        return False
                else:
                    if h1_reg not in ('trend_down', 'weak_trend'):
                        return False
                    if not _di_valid or _dim <= _dip:
                        return False
        else:
            if direction == 'buy'  and h1_reg not in ('trend_up',   'weak_trend'):
                return False
            if direction == 'sell' and h1_reg not in ('trend_down', 'weak_trend'):
                return False
        # M5 SMA20 傾き
        if not _slope_ok(m5_s20, m5_atr, m5_i, direction):
            return False
        # M15 SMA20 傾き
        if m15_t is not None and m15_s20 is not None:
            m15_i = int(np.searchsorted(m15_t, ts, side='right')) - 1
            if m15_i >= 0 and not _slope_ok(m15_s20, m15_atr_arr, m15_i, direction):
                return False
        # M1 SMA20 傾き（M5 クロス時点）
        m1_i = int(np.searchsorted(m1_t, ts, side='right')) - 1
        if m1_i >= 0 and not _slope_ok(m1_s20, m1_atr, m1_i, direction):
            return False
        return True

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
            # SMA20 バイパス: 価格が大きく乖離している場合はタッチ不要
            _bypass = (
                (direction == 'buy'  and buy_bypass  > 0 and cl > s20 + atr_v * buy_bypass) or
                (direction == 'sell' and sell_bypass > 0 and cl < s20 - atr_v * sell_bypass)
            )
            if not _bypass and abs(cl - s20) > touch_margin:
                continue
            # SMA20 傾き確認（バイパス時はスキップ）
            if not _bypass and j >= slope_bars:
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

    def find_pattern_entry(direction: str, m5_i: int, pat_target: float) -> dict | None:
        """パターン ネックライン突破: SMA20 待機なしで直接エントリー。"""
        atr_v = m5_atr[m5_i]
        if np.isnan(atr_v) or atr_v <= 0:
            return None
        tp_move = atr_v * tp_frac
        sl_move = tp_move * sl_ratio
        j0 = m1_idx_at(m5_t[m5_i])
        if j0 >= len(m1_cl):
            return None
        ep = float(m1_cl[j0])
        # パターン TP: 測定値が 1〜8×tp_move の範囲なら上書き
        pt_dist = abs(pat_target - ep)
        if tp_move < pt_dist < tp_move * 8.0:
            tp_move = pt_dist
        return {
            'direction':     direction,
            'entry_time':    m5_t[m5_i],
            'entry_price':   ep,
            'confirm_bar':   j0,
            'tp_move':       tp_move,
            'sl_move':       sl_move,
            'signal_source': 'pattern_nl',
        }

    def find_direct_entry(direction: str, m5_i: int,
                          custom_tp: float | None, custom_sl: float | None,
                          src: str) -> dict | None:
        """SMA20 タッチ待ち不要のダイレクトエントリー（EW2 / vol_bo / extreme OS/OB 共用）。"""
        atr_v = m5_atr[m5_i]
        if np.isnan(atr_v) or atr_v <= 0:
            return None
        base_tp = atr_v * tp_frac
        tp_m = custom_tp if (custom_tp is not None and custom_tp > 0) else base_tp
        sl_m = custom_sl if (custom_sl is not None and custom_sl > 0) else tp_m * sl_ratio
        j0 = m1_idx_at(m5_t[m5_i])
        if j0 >= len(m1_cl):
            return None
        ep = float(m1_cl[j0])
        return {
            'direction':     direction,
            'entry_time':    m5_t[m5_i],
            'entry_price':   ep,
            'confirm_bar':   j0,
            'tp_move':       tp_m,
            'sl_move':       sl_m,
            'signal_source': src,
        }

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
    m5_rsi_prev: float = float('nan')
    crossing_ptr: int  = 0
    _crossings = list(h1_crossings or [])

    # 追加シグナル用ステート
    ew2_traded: set = set()         # (direction, round(w2_price, 0), bars_ago) 重複防止

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

        # レジーム
        regime = _regime(df_m5, i, regime_cfg)

        # ── ウィップソー検出 ─────────────────────────────────────
        _ws_block = False
        if _ws_n > 0 and i >= _ws_n:
            _ws_block, _ = detect_whipsaw(
                df_m5.iloc[max(0, i - _ws_n): i + 1], _ws_n, _ws_thr)

        # ── H1 パターン ネックライン突破 (優先) ──────────────────────
        info = None; signal = None; crossed = 0.0
        if crossing_ptr < len(_crossings):
            e_ts, e_dir, e_nl, e_tgt = _crossings[crossing_ptr]
            e_ts_pd = pd.Timestamp(e_ts)
            if e_ts_pd <= ts:
                crossing_ptr += 1
                stale = (ts - e_ts_pd).total_seconds() > 3600  # 1H 以上前は無視
                if not stale:
                    info    = find_pattern_entry(e_dir, i, e_tgt)
                    signal  = e_dir
                    crossed = e_nl

        # ── Elliott Wave 2 エントリー ─────────────────────────────
        if info is None and _ew_enabled and not _ws_block:
            atr_i    = m5_atr[i]
            ew_start = max(0, i - _ew_slice_len)
            df_ew    = df_m5.iloc[ew_start: i + 1]
            _lb      = min(_ew_lb, len(df_ew) - 1)

            if buy_en and regime != 'trend_down' and _mtf_ok(m5_t[i], i, 'buy'):
                _ew2b = detect_elliott_w2_buy(
                    df_ew, lookback=_lb, sw_window=_ew_sw,
                    fib_min=_ew_fm, fib_max=_ew_fmx,
                    min_wave1_atr=_ew_w1a, rsi_div_min=_ew_div,
                    w2_rsi_max=_ew2_buy_rsi_max, w2_bars_ago_max=_ew_bago,
                )
                if _ew2b is not None:
                    _fp = ('ew2_buy', round(_ew2b['w2_low'], 0), _ew2b['w2_bars_ago'])
                    if _fp not in ew2_traded:
                        if len(ew2_traded) > 120:
                            ew2_traded.clear()
                        ew2_traded.add(_fp)
                        _ew2_tp = _ew2b['w2_low'] + _ew2b['wave1_size'] * _ew_ext
                        _ew2_sl = _ew2b['w2_low'] - atr_i * _ew_slb
                        j0 = m1_idx_at(m5_t[i])
                        if j0 < len(m1_cl):
                            ep = float(m1_cl[j0])
                            _tp_move = max(atr_i * 0.1, _ew2_tp - ep) if _ew2_tp > ep else atr_i * tp_frac
                            _sl_move = max(atr_i * 0.1, ep - _ew2_sl) if _ew2_sl < ep else atr_i * tp_frac * sl_ratio
                            info   = find_direct_entry('buy', i, _tp_move, _sl_move,
                                                       f'ew2_buy_fib{_ew2b["fib_level"]:.2f}')
                            signal = 'buy'
                            crossed = _ew2b['w2_low']

            if info is None and sell_en and regime != 'trend_up' and _mtf_ok(m5_t[i], i, 'sell'):
                _ew2s = detect_elliott_w2_sell(
                    df_ew, lookback=_lb, sw_window=_ew_sw,
                    fib_min=_ew_fm, fib_max=_ew_fmx,
                    min_wave1_atr=_ew_w1a, rsi_div_min=_ew_div,
                    w2_rsi_min=_ew2_sell_rsi_min, w2_bars_ago_max=_ew_bago,
                )
                if _ew2s is not None:
                    _fp = ('ew2_sell', round(_ew2s['w2_high'], 0), _ew2s['w2_bars_ago'])
                    if _fp not in ew2_traded:
                        if len(ew2_traded) > 120:
                            ew2_traded.clear()
                        ew2_traded.add(_fp)
                        _ew2_tp = _ew2s['w2_high'] - _ew2s['wave1_size'] * _ew_ext
                        _ew2_sl = _ew2s['w2_high'] + atr_i * _ew_slb
                        j0 = m1_idx_at(m5_t[i])
                        if j0 < len(m1_cl):
                            ep = float(m1_cl[j0])
                            _tp_move = max(atr_i * 0.1, ep - _ew2_tp) if _ew2_tp < ep else atr_i * tp_frac
                            _sl_move = max(atr_i * 0.1, _ew2_sl - ep) if _ew2_sl > ep else atr_i * tp_frac * sl_ratio
                            info   = find_direct_entry('sell', i, _tp_move, _sl_move,
                                                       f'ew2_sell_fib{_ew2s["fib_level"]:.2f}')
                            signal = 'sell'
                            crossed = _ew2s['w2_high']

        # ── ボリュームブレイクアウト ──────────────────────────────
        if info is None and _vb_enabled and _has_rvol and not _ws_block:
            _vol_bo = detect_volume_breakout(df_m5.iloc[max(0, i - 4): i + 1], cfg)
            if _vol_bo['direction'] != 'none':
                atr_i    = m5_atr[i]
                _tp_base = atr_i * tp_frac
                _sl_base = _tp_base * sl_ratio
                if (_vol_bo['direction'] == 'up' and buy_en
                        and rsi_cur >= _vb_rsi_buy_min and regime != 'trend_down'
                        and _mtf_ok(m5_t[i], i, 'buy')):
                    info   = find_direct_entry('buy', i,
                                               _tp_base * _vb_tp_m, _sl_base * _vb_sl_m,
                                               f'vol_bo_up_rvol{_vol_bo["rvol"]:.1f}')
                    signal = 'buy'
                    crossed = rsi_cur
                elif (_vol_bo['direction'] == 'down' and sell_en
                        and rsi_cur <= _vb_rsi_sell_max and regime != 'trend_up'
                        and _mtf_ok(m5_t[i], i, 'sell')):
                    info   = find_direct_entry('sell', i,
                                               _tp_base * _vb_tp_m, _sl_base * _vb_sl_m,
                                               f'vol_bo_down_rvol{_vol_bo["rvol"]:.1f}')
                    signal = 'sell'
                    crossed = rsi_cur

        # ── RSI クロス検出 ─────────────────────────────────────────
        if info is None:
            if buy_en and regime != 'trend_down' and not _ws_block:
                for thr in buy_thrs:
                    if rsi_cur > thr and rsi_prev <= thr:
                        signal = 'buy'; crossed = thr; break
            if signal is None and sell_en and regime != 'trend_up' and not _ws_block:
                for thr in sell_thrs:
                    if rsi_cur < thr and rsi_prev >= thr:
                        signal = 'sell'; crossed = thr; break
            if signal is None:
                continue

            # MTF フィルタ: M1/M5/M15 SMA20 傾き + H1 レジーム (DI 方向込み)
            if not _mtf_ok(m5_t[i], i, signal):
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
            'signal_source': info.get('signal_source', 'rsi_cross'),
        })

        last_entry_ts = entry_ts

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

    # シグナル種別内訳
    by_src: dict[str, list] = {}
    for t in trades:
        src = t.get('signal_source', 'rsi_cross')
        by_src.setdefault(src, []).append(t)
    if len(by_src) > 1:
        print(f"\n  シグナル種別:")
        for src, ts_list in sorted(by_src.items()):
            sp  = np.array([t['pnl_jpy'] for t in ts_list])
            sw  = (sp > 0).sum()
            print(f"    {src:<15}  {len(ts_list):3d}件  "
                  f"WR={sw/len(ts_list)*100:.0f}%  "
                  f"累計={sp.sum():+,.0f} JPY")

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
            'OPTIMIZE', 'LOCAL', 'PLOT', 'BRIDGE', 'SCALP', 'REGIME',
            'TIME_BIAS', 'ELLIOTT', 'WHIPSAW']}
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

    # データ取得（MT5 接続 → M5+M1+M15+H1 取得 → shutdown を1回で完結）
    print("\n[1] データ取得")
    df_m5_raw, df_m1_raw, df_m15_raw, df_h1_raw, is_real = load_scalp_data(
        args.symbol, cfg['MT5'], m5_bars, m1_bars,
        date_from=date_from, date_to=date_to,
        force_synthetic=args.synthetic,
    )
    print(f"  ソース: {'MT5実データ' if is_real else '合成データ'}")

    # 指標
    print("\n[2] 指標計算")
    df_m5  = add_m5_indicators(df_m5_raw, cfg)
    df_m1  = add_m1_indicators(df_m1_raw, cfg)
    df_m15 = add_m1_indicators(df_m15_raw, cfg) if df_m15_raw is not None else None
    df_h1  = add_h1_indicators(df_h1_raw,  cfg) if df_h1_raw  is not None else None

    data_days = (df_m5.index[-1] - df_m5.index[0]).total_seconds() / 86400
    mtf_status = (f"M15={len(df_m15)}本  H1={len(df_h1)}本"
                  if df_m15 is not None and df_h1 is not None else "M15/H1=なし(MTFフィルタ無効)")
    print(f"  M5: {len(df_m5)}本  M1: {len(df_m1)}本  {mtf_status}")
    print(f"  期間: {df_m5.index[0].date()} 〜 {df_m5.index[-1].date()} ({data_days:.0f}日)")

    # H1 パターン先行計算
    print("\n[2.5] H1 パターン ネックライン突破 先行計算")
    h1_crossings = _precompute_h1_crossings(df_h1_raw)
    print(f"  ネックライン突破イベント: {len(h1_crossings)} 件")
    for e_ts, e_dir, e_nl, _ in h1_crossings[:5]:
        print(f"    {pd.Timestamp(e_ts).date()}  {e_dir:<4}  NL={e_nl:,.2f}")
    if len(h1_crossings) > 5:
        print(f"    ... 他 {len(h1_crossings) - 5} 件")

    # バックテスト実行
    print("\n[3] バックテスト実行中...")
    trades = run_scalp_bt(df_m5, df_m1, cfg, touch_margin, df_m15=df_m15, df_h1=df_h1,
                          h1_crossings=h1_crossings)
    print(f"  完了: {len(trades)} トレード")

    # 結果表示
    print_stats(trades, target_jpy, sl_ratio, data_days)

    # 保存
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(trades, ensure_ascii=False, indent=2))
    print(f"  トレード詳細: {args.output}")


if __name__ == '__main__':
    main()
