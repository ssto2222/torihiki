"""
mt5_ea_bridge.py — MT5 EA リアルタイム連携ブリッジ
====================================================
Python でシグナル・SL水準を計算 → signal.json に書き込む
MT5 EA が OnTimer() で読み込み、注文を執行する

実行:
    python mt5_ea_bridge.py           # ポーリングループ（Ctrl+C で終了）
    python mt5_ea_bridge.py --once    # 1回だけ計算して終了（動作確認用）
    python mt5_ea_bridge.py --symbol GOLD --output ./out

通信プロトコル:
    Python → MT5 EA : output/signal.json   （毎ポーリング更新）
    MT5 EA → Python : output/ea_state.json （EA が書き込む状態）
"""
import sys, json, time, argparse
from pathlib import Path
from datetime import datetime, timezone
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import connect_mt5, fetch_ohlcv
from core.indicators import add_h1_indicators, add_m1_indicators
from core.strategy   import detect_h1_signals, VolAdaptiveSL

CFG = {k: getattr(C, k) for k in
       ['MT5','INDICATOR','SIGNAL','EXECUTION','SL','CRASH','LOCAL','PLOT','BRIDGE']}


# ── リアルタイム指標・シグナル計算 ────────────────────────────

def compute_signal(symbol: str, cfg: dict) -> dict | None:
    """
    最新 H1 + M1 から現在の売買シグナルと SL 水準を計算

    戻り値フォーマット（signal.json と同一）:
    {
      "timestamp":      "2024-01-15T03:00:00+00:00",
      "symbol":         "XAUUSD",
      "close":          1950.20,
      "atr":            12.34,
      "atr_ratio":      1.05,
      "rsi":            42.1,
      "sl_multi":       1.5,          // ボラ適応型で決定したATR倍率
      "action":         "buy",        // "buy" / "sell" / "none"
      "sl_price":       1931.70,      // 推奨SL価格
      "tp_price":       1987.22,      // 保険TP価格
      "rsi_exit_thr":   75.0,
      "trail_multi":    1.5,
      "max_slip_pt":    617,          // MT5 deviation パラメータ相当
      "signal_active":  true,
      "n_signals_buy":  1,
      "n_signals_sell": 0
    }
    """
    try:
        import MetaTrader5 as mt5

        df_h1_raw = fetch_ohlcv(symbol, 'H1', 200)
        df_m1_raw = fetch_ohlcv(symbol, 'M1', 500)
        if df_h1_raw is None or df_m1_raw is None:
            return None

        df_h1 = add_h1_indicators(df_h1_raw, cfg)
        df_m1 = add_m1_indicators(df_m1_raw, cfg)
        if df_h1.empty: return None

        last    = df_h1.iloc[-1]
        atr_v   = float(last['ATR'])
        ratio   = float(last['ATR_ratio']) if not np.isnan(last['ATR_ratio']) else 1.0
        rsi_v   = float(last['RSI'])
        bb_pct  = float(last['BB_pct'])
        close_v = float(last['Close'])

        # ボラ適応型 SL でマルチプライヤーを決定
        strat    = VolAdaptiveSL(cfg=cfg)
        sl_multi = strat._m(ratio)

        # シグナル検出（直近200本）
        sigs_buy  = detect_h1_signals(df_h1, cfg['SIGNAL'], 'buy')
        sigs_sell = detect_h1_signals(df_h1, cfg['SIGNAL'], 'sell')

        # 直近4H以内のシグナルを有効とみなす
        now         = df_h1.index[-1]
        active_buy  = any((now - s['signal_time']).total_seconds() <= 4 * 3600
                          for s in sigs_buy)
        active_sell = any((now - s['signal_time']).total_seconds() <= 4 * 3600
                          for s in sigs_sell)

        # アクション決定（買いと売りが同時発光なら none）
        if active_buy and not active_sell:
            action   = 'buy'
            sl_price = close_v - atr_v * sl_multi
            tp_price = close_v + atr_v * cfg['SL']['tp_atr_multi']
        elif active_sell and not active_buy:
            action   = 'sell'
            sl_price = close_v + atr_v * sl_multi
            tp_price = close_v - atr_v * cfg['SL']['tp_atr_multi']
        else:
            action   = 'none'
            sl_price = close_v - atr_v * sl_multi   # 参考値
            tp_price = close_v + atr_v * cfg['SL']['tp_atr_multi']

        # MT5 deviation ポイント換算
        tick   = mt5.symbol_info(symbol)
        point  = tick.point if tick else 0.01
        max_pt = max(1, int(atr_v * 0.5 / point))

        return {
            'timestamp':      datetime.now(timezone.utc).isoformat(),
            'symbol':         symbol,
            'close':          round(close_v, 2),
            'atr':            round(atr_v,   2),
            'atr_ratio':      round(ratio,   3),
            'rsi':            round(rsi_v,   1),
            'bb_pct':         round(bb_pct,  3),
            'sl_multi':       round(sl_multi, 2),
            'action':         action,
            'sl_price':       round(sl_price, 2),
            'tp_price':       round(tp_price, 2),
            'rsi_exit_thr':   cfg['SL']['rsi_exit_thr'],
            'trail_multi':    cfg['SL']['trail_multi'],
            'max_slip_pt':    max_pt,
            'signal_active':  active_buy or active_sell,
            'n_signals_buy':  len(sigs_buy),
            'n_signals_sell': len(sigs_sell),
        }
    except Exception as e:
        print(f"[ブリッジ] 計算エラー: {e}")
        return None


# ── ファイル I/O ───────────────────────────────────────────

def write_signal(data: dict, path: str):
    """signal.json をアトミックに書き込む"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='ascii') as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
    Path(tmp).replace(Path(path))


def read_ea_state(path: str) -> dict:
    try:
        with open(path, encoding='ascii') as f:
            return json.load(f)
    except Exception:
        return {}


# ── ポーリングループ ─────────────────────────────────────────

def run_bridge(cfg: dict, once: bool = False):
    symbol     = cfg['MT5']['symbol']
    sig_path   = cfg['BRIDGE']['signal_file']
    state_path = cfg['BRIDGE']['status_file']
    poll_sec   = cfg['BRIDGE']['poll_sec']

    Path(sig_path).parent.mkdir(parents=True, exist_ok=True)

    print("=" * 58)
    print(f"  MT5 EA ブリッジ  [{symbol}]")
    print(f"  signal.json  → {sig_path}")
    print(f"  ea_state.json← {state_path}")
    print(f"  ポーリング   : {poll_sec}秒  （Ctrl+C で終了）")
    print("=" * 58)

    if not connect_mt5(symbol):
        print("\n[エラー] MT5 接続失敗。ターミナルを起動して再実行してください。")
        return

    try:
        import MetaTrader5 as mt5
        itr = 0
        while True:
            itr += 1
            t_s = time.time()
            data = compute_signal(symbol, cfg)

            if data:
                write_signal(data, sig_path)
                ea  = read_ea_state(state_path)
                pos = ea.get('positions', 0)
                bal = ea.get('balance',   'N/A')
                ts  = datetime.now().strftime('%H:%M:%S')
                print(f"\n[{ts}] #{itr}  close=${data['close']:,.2f}  "
                      f"ATR=${data['atr']:.2f}  ratio={data['atr_ratio']:.2f}  "
                      f"RSI={data['rsi']:.1f}")
                print(f"  SL=ATR×{data['sl_multi']}  "
                      f"action={data['action'].upper():4s}  "
                      f"SL=${data['sl_price']:,.2f}  TP=${data['tp_price']:,.2f}  "
                      f"max_slip={data['max_slip_pt']}pt")
                print(f"  signal={data['n_signals_buy']}買/{data['n_signals_sell']}売  "
                      f"残高={bal}  ポジション={pos}件")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] #{itr}  データ取得失敗")

            if once: break
            time.sleep(max(0, poll_sec - (time.time() - t_s)))

    except KeyboardInterrupt:
        print("\n[ブリッジ] 終了")
    finally:
        try:
            import MetaTrader5 as mt5; mt5.shutdown()
        except Exception:
            pass


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='MT5 EA リアルタイムブリッジ')
    ap.add_argument('--once',   action='store_true', help='1回だけ計算して終了')
    ap.add_argument('--symbol', default=C.MT5['symbol'])
    ap.add_argument('--output', default='./output')
    args = ap.parse_args()

    CFG['MT5']['symbol']          = args.symbol
    CFG['BRIDGE']['signal_file']  = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/signal.json"
    CFG['BRIDGE']['status_file']  = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ea_state.json"

    run_bridge(CFG, once=args.once)
