"""
mt5_ea_bridge.py — MT5 EA リアルタイム連携ブリッジ
====================================================
Python でシグナル・SL水準を計算 → signal.json に書き込む
MT5 EA が OnTimer() で読み込み、注文を執行する

実行:
    python mt5_ea_bridge.py           # ポーリングループ（Ctrl+C で終了）
    python mt5_ea_bridge.py --once    # 1回だけ計算して終了（動作確認用）
    python mt5_ea_bridge.py --symbol BTCUSD --lot 0.05

通信プロトコル:
    Python → MT5 EA : output/signal.json   （毎ポーリング更新）
    MT5 EA → Python : output/ea_state.json （EA が書き込む状態）

ルール適用（trading_rules.json）:
    - 買いのみ（売りは構造的損失）
    - 禁止時間帯（UTC 9/16/21）はスキップ
    - 金曜日はスキップ
    - H1/D1 RSI ゾーン + クロスフィルターで品質スコア算出
    - スコア < min_score のシグナルはスキップ
    - EA 連続損失 >= 3 回でその日停止
"""
import sys, json, time, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
import numpy as np


sys.path.insert(0, str(Path(__file__).parent))
import config as C
from core.data       import connect_mt5, fetch_ohlcv
from core.indicators import add_h1_indicators, add_d1_indicators, add_m5_indicators
from core.strategy   import check_m5_entry_filter, check_m5_surge

CFG = {k: getattr(C, k) for k in
       ['MT5','INDICATOR','SIGNAL','EXECUTION','SL','RULES','LOCAL','PLOT','BRIDGE']}

# ── RulesEngine ロード（なければフィルタなし）────────────────
try:
    from trading_rules import RulesEngine
    _engine = RulesEngine()
    print("[ルール] trading_rules.json 読み込み完了")
except Exception as _e:
    _engine = None
    print(f"[ルール] trading_rules 読み込み失敗: {_e} → フィルタなし")

# ── シグナル状態（クロス検出用）─────────────────────────────
_prev_rsi_h1: float | None = None          # 前回ポーリング時の H1 RSI
_signal_active: dict       = {             # 現在有効なシグナルウィンドウ
    'type':  None,                         # 'dip' / 'momentum_55' / ...
    'until': None,                         # 有効期限 (datetime UTC)
}


# ── リアルタイム指標・シグナル計算 ────────────────────────────

def compute_signal(symbol: str, cfg: dict) -> dict | None:
    """
    バックテストと同じシグナルロジック（クロス検出）でリアルタイムシグナルを生成。

    DIP:      H1 RSI が buy_rsi_thr (40) を下抜けた瞬間
    MOMENTUM: H1 RSI が momentum_thrs (55/60/65/70/75) を上抜けた瞬間
    いずれも signal_valid_m1 分間（デフォルト 240 分）シグナルウィンドウが開く。
    ウィンドウ内かつ RulesEngine=BUY のポーリング時に action='buy' を出力。

    signal.json フォーマット:
      action          : "buy" / "none"
      signal_type     : "dip" / "momentum_55" / ... / "none"
      signal_valid_until: シグナルウィンドウ終了時刻
      score           : RulesEngine スコア (0〜100)
      strength        : "strong" / "normal" / "weak" / "none"
      tp_hold_minutes : TP目安保有時間（分）
      lot_size        : 発注ロット数
      timestamp       : "YYYY.MM.DD HH:MM:SS"（MQL5 StringToTime 互換）
    """
    global _prev_rsi_h1, _signal_active

    try:
        import MetaTrader5 as mt5

        df_h1_raw = fetch_ohlcv(symbol, 'H1', 200)
        df_d1_raw = fetch_ohlcv(symbol, 'D1', 50)
        df_m5_raw = fetch_ohlcv(symbol, 'M5', 60)
        if df_h1_raw is None or df_d1_raw is None:
            return None

        df_h1 = add_h1_indicators(df_h1_raw, cfg)
        df_d1 = add_d1_indicators(df_d1_raw, cfg)
        if df_h1.empty or df_d1.empty or 'SMA20' not in df_h1.columns:
            return None

        last     = df_h1.iloc[-1]
        close_v  = float(last['Close'])
        atr_v    = float(last['ATR'])
        rsi_h1_v = float(last['RSI'])
        sma20    = float(last['SMA20'])
        rsi_d1_v = float(df_d1['RSI'].iloc[-1])

        # M5 RSI（直近2本で rising 判定 + 急騰急落検出）
        rsi_m5_cur  = float('nan')
        rsi_m5_prev = float('nan')
        m5_ok       = False
        surge       = 'none'
        if df_m5_raw is not None and len(df_m5_raw) >= 3:
            df_m5 = add_m5_indicators(df_m5_raw, cfg)
            if not df_m5.empty and len(df_m5) >= 2:
                rsi_m5_cur  = float(df_m5['RSI'].iloc[-1])
                rsi_m5_prev = float(df_m5['RSI'].iloc[-2])
                m5_ok = check_m5_entry_filter(rsi_m5_cur, rsi_m5_prev, rsi_d1_v, symbol)
                surge = check_m5_surge(df_m5)

        sl_multi    = cfg['SL']['sl_multi']
        sig_p       = cfg['SIGNAL']
        buy_thr     = sig_p.get('buy_rsi_thr',  40.0)
        mom_thrs    = sorted(sig_p.get('momentum_thrs', [55.0, 60.0, 65.0, 70.0, 75.0]))
        valid_min   = cfg['EXECUTION'].get('signal_valid_m1', 240)  # M1本数 = 分数

        now      = datetime.now(timezone.utc)
        hour_utc = now.hour
        dow      = now.weekday()

        # ── クロス検出（バックテストと同ロジック）──────────────
        new_type = None
        if _prev_rsi_h1 is not None:
            if rsi_h1_v < buy_thr and _prev_rsi_h1 >= buy_thr:
                new_type = 'dip'
            else:
                for thr in mom_thrs:
                    if rsi_h1_v > thr and _prev_rsi_h1 <= thr:
                        new_type = f'momentum_{int(thr)}'
                        break
        _prev_rsi_h1 = rsi_h1_v

        # 新クロス検出 → シグナルウィンドウを開く（上書き）
        if new_type:
            _signal_active['type']  = new_type
            _signal_active['until'] = now + timedelta(minutes=valid_min)

        # ウィンドウ内かどうか
        in_window = (
            _signal_active['until'] is not None and
            now <= _signal_active['until']
        )
        sig_type = _signal_active['type'] if in_window else 'none'

        # ── RulesEngine フィルタ ────────────────────────────
        active_buy      = in_window
        score           = 0
        strength        = 'none'
        tp_hold_minutes = 0
        skip_reason     = ''

        if _engine is not None:
            result = _engine.evaluate(
                symbol     = symbol,
                rsi_h1     = rsi_h1_v,
                rsi_d1     = rsi_d1_v,
                direction  = 'buy',
                hour_utc   = hour_utc,
                dow        = dow,
                minute_utc = now.minute,
            )
            score           = result.score
            strength        = result.strength or 'none'
            tp_hold_minutes = result.tp_hold_minutes or 0

            if m5_ok:
                score = min(100, score + 10)

            if result.signal != 'BUY':
                active_buy  = False
                skip_reason = ' | '.join(result.reasons[:2])

        # 急騰急落チェック（M5 RSI が 25分で 20pt 以上変化）
        if surge != 'none' and active_buy:
            active_buy  = False
            skip_reason = f'm5_surge={surge}'

        action = 'buy' if active_buy else 'none'
        valid_until_str = (_signal_active['until'].strftime('%Y.%m.%d %H:%M:%S')
                           if _signal_active['until'] else '')

        sl_price = close_v - atr_v * sl_multi
        tp_price = close_v + atr_v * cfg['SL']['tp_atr_multi']

        tick  = mt5.symbol_info(symbol)
        point = tick.point if tick else 0.01
        max_pt = max(1, int(atr_v * 0.5 / point))

        return {
            'timestamp':          datetime.now(timezone.utc).strftime('%Y.%m.%d %H:%M:%S'),
            'symbol':             symbol,
            'close':              round(close_v, 2),
            'atr':                round(atr_v,    2),
            'rsi_h1':             round(rsi_h1_v, 1),
            'rsi_d1':             round(rsi_d1_v, 1),
            'rsi_m5':             round(rsi_m5_cur, 1) if not np.isnan(rsi_m5_cur) else 0.0,
            'rsi_m5_prev':        round(rsi_m5_prev, 1) if not np.isnan(rsi_m5_prev) else 0.0,
            'm5_filter_ok':       m5_ok,
            'm5_surge':           surge,
            'sma20':              round(sma20,    2),
            'sl_multi':           round(sl_multi,  2),
            'action':             action,
            'signal_type':        sig_type,
            'signal_valid_until': valid_until_str,
            'sl_price':           round(sl_price,  2),
            'tp_price':           round(tp_price,  2),
            'score':              score,
            'strength':           strength,
            'tp_hold_minutes':    tp_hold_minutes,
            'skip_reason':        skip_reason,
            'rsi_exit_thr':       cfg['SL']['rsi_exit_thr'],
            'trail_multi':        cfg['SL']['trail_multi'],
            'max_slip_pt':     max_pt,
            'lot_size':        cfg['BRIDGE']['lot_size'],
        }
    except Exception as e:
        print(f"[ブリッジ] 計算エラー: {e}")
        return None


# ── ファイル I/O ───────────────────────────────────────────

def write_signal(data: dict, path: str):
    """signal.json をアトミックに書き込む (Windows ファイルロック対応)"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='ascii') as f:
        json.dump(data, f, ensure_ascii=True, indent=2)

    retries = 5
    for attempt in range(retries):
        try:
            Path(tmp).replace(Path(path))
            return
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(0.1)
            else:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except OSError:
                    pass
                raise


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
    lot_size   = cfg['BRIDGE']['lot_size']
    max_consec = cfg.get('RULES', {}).get('max_consecutive_losses', 3)
    min_score  = cfg.get('RULES', {}).get('min_score', 30)

    Path(sig_path).parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  MT5 EA ブリッジ  [{symbol}]")
    print(f"  signal.json  → {sig_path}")
    print(f"  ea_state.json← {state_path}")
    print(f"  ポーリング   : {poll_sec}秒  （Ctrl+C で終了）")
    print(f"  ロット数     : {lot_size}  最小スコア: {min_score}")
    print(f"  連続損失上限 : {max_consec}回")
    print("=" * 60)

    if not connect_mt5(symbol, cfg['MT5']):
        print("\n[エラー] MT5 接続失敗。ターミナルを起動して再実行してください。")
        return

    try:
        itr = 0
        while True:
            itr += 1
            t_s  = time.time()
            data = compute_signal(symbol, cfg)

            if data:
                # 連続損失チェック（EA state から読む）
                ea            = read_ea_state(state_path)
                consec_losses = ea.get('consecutive_losses', 0)
                pos           = ea.get('positions', 0)
                bal           = ea.get('balance', 'N/A')

                if consec_losses >= max_consec and data['action'] == 'buy':
                    data['action']      = 'none'
                    data['skip_reason'] = f'consecutive_losses={consec_losses}>={max_consec}'

                write_signal(data, sig_path)
                ts = datetime.now().strftime('%H:%M:%S')
                surge_tag = f"[{data['m5_surge']}]" if data['m5_surge'] != 'none' else ''
                print(f"\n[{ts}] #{itr}  "
                      f"close=${data['close']:,.2f}  "
                      f"RSI_H1={data['rsi_h1']:.1f}  RSI_D1={data['rsi_d1']:.1f}  "
                      f"RSI_M5={data['rsi_m5']:.1f}({'↑' if data['m5_filter_ok'] else '↓/NG'}){surge_tag}  "
                      f"ATR=${data['atr']:.2f}")
                print(f"  action={data['action'].upper():4s}  "
                      f"signal={data['signal_type']}  "
                      f"SL=${data['sl_price']:,.2f}  TP=${data['tp_price']:,.2f}  "
                      f"score={data['score']}({data['strength']})  "
                      f"lot={data['lot_size']}")
                if data['signal_valid_until']:
                    print(f"  window_until={data['signal_valid_until']}")
                if data['skip_reason']:
                    print(f"  skip: {data['skip_reason']}")
                print(f"  残高={bal}  ポジション={pos}件  連続損失={consec_losses}回")
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
    ap.add_argument('--lot',    type=float, default=None,
                    help=f'1回の取引ロット数（省略時: {C.BRIDGE["lot_size"]}）')
    args = ap.parse_args()

    CFG['MT5']['symbol']          = args.symbol
    CFG['BRIDGE']['signal_file']  = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/signal.json"
    CFG['BRIDGE']['status_file']  = "C:/Users/YK/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ea_state.json"
    if args.lot is not None:
        CFG['BRIDGE']['lot_size'] = args.lot

    run_bridge(CFG, once=args.once)
