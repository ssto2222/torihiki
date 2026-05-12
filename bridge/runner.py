"""bridge/runner.py — MT5 EA ブリッジ ポーリングループ"""
from __future__ import annotations
import argparse
import io
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path


class _TeeWriter(io.TextIOBase):
    """sys.stdout をラップして、コンソールとバッファの両方に書き込む。
    log_dir が設定されている場合に使用し、ポーリング反復ごとに
    バッファをリセット → 反復末にファイルへ上書き保存する。
    """

    def __init__(self, original: io.TextIOBase) -> None:
        self._orig = original
        self._buf: list[str] = []

    def write(self, text: str) -> int:
        self._orig.write(text)
        self._buf.append(text)
        return len(text)

    def flush(self) -> None:
        self._orig.flush()

    def fileno(self):
        return self._orig.fileno()

    @property
    def encoding(self):
        return getattr(self._orig, 'encoding', 'utf-8')

    def reset(self) -> None:
        """反復開始時にバッファをクリアする"""
        self._buf.clear()

    def dump(self, path: Path) -> None:
        """バッファ内容をファイルに上書き保存する"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(self._buf), encoding='utf-8')
        except OSError:
            pass


class _ErrTeeWriter(io.TextIOBase):
    """sys.stderr をラップしてエラーログに追記する（上書きなし）。
    Python のトレースバックや予期せぬ例外を永続的に残すために使用する。
    """

    def __init__(self, original: io.TextIOBase, log_path: Path) -> None:
        self._orig = original
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(log_path, 'a', encoding='utf-8', errors='replace')
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            self._fh.write(f'\n=== ブリッジ起動 {ts} ===\n')
            self._fh.flush()
        except OSError:
            pass

    def write(self, text: str) -> int:
        self._orig.write(text)
        try:
            self._fh.write(text)
            self._fh.flush()
        except OSError:
            pass
        return len(text)

    def flush(self) -> None:
        self._orig.flush()

    def fileno(self):
        return self._orig.fileno()

    @property
    def encoding(self):
        return getattr(self._orig, 'encoding', 'utf-8')

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

import MetaTrader5 as mt5

import config as C
from core.data import connect_mt5

from bridge.state        import SignalState, ScalpState, TimeBiasState, JpyRateCache, Sma20TouchCache
from bridge.io           import write_signal, read_ea_state
from bridge.notify       import send_discord, check_pause_signal, _build_discord_signal_msg
from bridge.utils        import (_setup_file_logging, _is_in_danger_skip_window,
                                 _close_profitable_positions, _reset_entry_windows)
from bridge.time_bias    import _build_time_bias, _load_time_bias
from bridge.sma20        import _load_sma20_touch_margins
from bridge.signal_normal import compute_signal
from bridge.signal_scalp  import compute_scalp_signal

_logger = logging.getLogger('torihiki')
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _logger.addHandler(logging.NullHandler())

CFG = {k: getattr(C, k) for k in
       ['MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES', 'LOCAL', 'PLOT',
        'BRIDGE', 'SCALP', 'REGIME', 'TIME_BIAS']}


def run_bridge(cfg: dict, once: bool = False, mode: str = 'normal') -> None:
    symbol = cfg['MT5']['symbol']

    def _sym_path(base: str) -> str:
        p = Path(base)
        return str(p.with_name(p.stem + f'_{symbol}' + p.suffix))

    sig_path   = _sym_path(cfg['BRIDGE']['signal_file'])
    state_path = _sym_path(cfg['BRIDGE']['status_file'])
    log_dir    = cfg['BRIDGE'].get('log_dir', '')
    log_sig    = str(Path(log_dir) / Path(sig_path).name) if log_dir else ''
    flag_file  = str(Path(log_dir) / 'paused.flag') if log_dir else 'paused.flag'

    _setup_file_logging(log_dir, symbol)

    # コンソール出力をファイルにも上書き保存する（log_dir が設定されている場合）
    console_log_path = Path(log_dir) / f'console_{symbol}.log' if log_dir else None
    error_log_path   = Path(log_dir) / f'error_{symbol}.log'   if log_dir else None
    _tee = _TeeWriter(sys.stdout)
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    _err_tee: _ErrTeeWriter | None = None
    if console_log_path:
        sys.stdout = _tee
    if error_log_path:
        _err_tee = _ErrTeeWriter(sys.stderr, error_log_path)
        sys.stderr = _err_tee

    poll_sec   = cfg['BRIDGE']['poll_sec']
    lot_size   = cfg['BRIDGE']['lot_size']
    max_consec = cfg.get('RULES', {}).get('max_consecutive_losses', 3)
    min_score  = cfg.get('RULES', {}).get('min_score', 30)
    scalp_cfg  = cfg.get('SCALP', {})
    tb_cfg     = cfg.get('TIME_BIAS', {})
    magic      = cfg['MT5'].get('magic', 20240101)
    deviation  = cfg['MT5'].get('deviation', 10)

    # 状態インスタンスを生成（各ポーリング間で共有される）
    sig_state  = SignalState()
    sc_state   = ScalpState()
    tb_state   = TimeBiasState()
    jpy_cache  = JpyRateCache()
    sma20_cache = Sma20TouchCache()
    last_discord_action = ['none']

    rebias_interval = tb_cfg.get('rebias_interval_hours', 24)
    bias_file       = tb_cfg.get('bias_file', './output/time_bias.json')

    if tb_cfg.get('enabled', False):
        bias_path  = Path(bias_file)
        file_age_h = ((time.time() - bias_path.stat().st_mtime) / 3600
                      if bias_path.exists() else float('inf'))
        if file_age_h >= max(rebias_interval, 1):
            tb_state.hours          = _build_time_bias(cfg)
            tb_state.last_rebias_at = time.time()
        else:
            tb_state.hours          = _load_time_bias(bias_file)
            tb_state.last_rebias_at = bias_path.stat().st_mtime if bias_path.exists() else 0.0

    Path(sig_path).parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  MT5 EA ブリッジ  [{symbol}]  モード: {mode.upper()}")
    print(f"  signal.json  → {sig_path}")
    if log_sig:
        print(f"  signal copy  → {log_sig}")
    print(f"  ea_state.json← {state_path}")
    print(f"  ポーリング   : {poll_sec}秒  （Ctrl+C で終了）")
    if mode == 'scalp':
        print(f"  目標利益     : {scalp_cfg.get('target_profit_jpy', 300)}円"
              f"  クールダウン : {scalp_cfg.get('cooldown_min', 30)}分"
              f"  日次上限     : {scalp_cfg.get('max_trades_day', 20)}回")
    else:
        print(f"  ロット数     : {lot_size}  最小スコア: {min_score}")
        print(f"  連続損失上限 : {max_consec}回")
    if tb_cfg.get('enabled', False):
        if tb_state.hours:
            print(f"  時間帯バイアス: {len(tb_state.hours)}個の危険時間帯 "
                  f"{sorted(tb_state.hours)}  "
                  f"（{tb_cfg.get('close_before_min', 15)}分前に含み益決済）")
        else:
            print("  時間帯バイアス: bias_file 未生成 → analyze_time_bias.py を先に実行してください")
    print("=" * 60)

    if not connect_mt5(symbol, cfg['MT5']):
        print("\n[エラー] MT5 接続失敗。ターミナルを起動して再実行してください。")
        return

    if mode == 'scalp':
        _load_sma20_touch_margins([symbol], sma20_cache, cfg)
        m = sma20_cache.margins.get(symbol)
        if m is not None:
            print(f"  SMA20タッチマージン: {symbol} = {m:.2f} USD")

    _fail_count = 0
    _restart    = False
    try:
        itr = 0
        while True:
            if check_pause_signal(symbol, flag_file, mt5=mt5):
                time.sleep(0.1)
                continue

            itr += 1
            t_s  = time.time()
            if console_log_path:
                _tee.reset()

            if mode == 'scalp':
                data = compute_scalp_signal(symbol, cfg, sc_state, sig_state,
                                            jpy_cache, sma20_cache, mt5=mt5)
            else:
                data = compute_signal(symbol, cfg, sig_state, jpy_cache, mt5=mt5)

            if data:
                _fail_count = 0
                ea            = read_ea_state(state_path)
                consec_losses = ea.get('consecutive_losses', 0)
                pos           = ea.get('positions', 0)
                bal           = ea.get('balance', 'N/A')

                if consec_losses >= max_consec and data['action'] in ('buy', 'sell'):
                    data['action']      = 'none'
                    data['skip_reason'] = f'consecutive_losses={consec_losses}>={max_consec}'

                # 時間帯バイアス回避
                if tb_cfg.get('enabled', False) and tb_state.hours:
                    now_utc     = datetime.now(timezone.utc)
                    skip_before = tb_cfg.get('skip_before_min', 30)
                    skip_after  = tb_cfg.get('skip_after_min',  15)
                    in_skip_window = _is_in_danger_skip_window(
                        now_utc, tb_state.hours, skip_before, skip_after)

                    for danger_hour in tb_state.hours:
                        danger_start  = datetime(now_utc.year, now_utc.month, now_utc.day,
                                                 danger_hour, 0, tzinfo=timezone.utc)
                        pre_warn_start = danger_start - timedelta(minutes=skip_before)
                        pre_warn_end   = pre_warn_start + timedelta(minutes=5)
                        if (pre_warn_start <= now_utc < pre_warn_end and
                                tb_state.danger_close_done_hr != danger_hour):
                            n_closed = _close_profitable_positions(
                                symbol, magic, deviation, mt5=mt5)
                            tb_state.danger_close_done_hr = danger_hour
                            if n_closed:
                                print(f"  [時間帯バイアス] {now_utc.strftime('%H:%M')}UTC"
                                      f" → {danger_hour:02d}:00が危険時間帯"
                                      f" → 30分前からスキップ開始 → 含み益{n_closed}本を決済")

                    if in_skip_window and data.get('action') in ('buy', 'sell'):
                        data['action']      = 'none'
                        data['skip_reason'] = f'禁止時間帯({now_utc.strftime("%H:%M")}UTC)'

                    if tb_state.prev_in_danger and not in_skip_window:
                        _reset_entry_windows(sig_state)
                        tb_state.danger_exit_until = now_utc + timedelta(minutes=skip_after)
                        print(f"  [時間帯バイアス] {now_utc.strftime('%H:%M')}UTC"
                              f" スキップウィンドウ終了 → {skip_after}分後に再エントリー可")
                    tb_state.prev_in_danger = in_skip_window

                    if (tb_state.danger_exit_until is not None
                            and now_utc < tb_state.danger_exit_until
                            and data.get('action') in ('buy', 'sell')):
                        rem = int((tb_state.danger_exit_until - now_utc).total_seconds() / 60) + 1
                        data['action']      = 'none'
                        data['skip_reason'] = f'危険時間帯後クールダウン(残{rem}分)'
                    elif tb_state.danger_exit_until and now_utc >= tb_state.danger_exit_until:
                        tb_state.danger_exit_until = None

                write_signal(data, sig_path)
                if log_sig:
                    try:
                        Path(log_sig).parent.mkdir(parents=True, exist_ok=True)
                        write_signal(data, log_sig)
                    except OSError as _e:
                        _logger.warning(f"log_sig 書き込み失敗 ({_e}) → スキップ")

                # 定期再分析
                if (tb_cfg.get('enabled', False) and rebias_interval > 0
                        and time.time() - tb_state.last_rebias_at >= rebias_interval * 3600):
                    tb_state.hours          = _build_time_bias(cfg)
                    tb_state.last_rebias_at = time.time()

                ts = datetime.now().strftime('%H:%M:%S')

                if mode == 'scalp' and data.get('scalp_mode', True):
                    early_tag = ' [M1早期]' if data.get('execution_tf') == 'm1_early' else ''
                    print(f"\n[{ts}] #{itr} [SCALP]{early_tag}  "
                          f"close=${data['close']:,.2f}  "
                          f"RSI_M5={data['rsi_m5']:.1f}  "
                          f"RSI_M1={data['rsi_m1']:.1f}  "
                          f"ATR=${data['atr']:.2f}  "
                          f"残高=¥{bal}  "
                          f"lot={data['lot_size']}(TP={scalp_cfg.get('tp_atr_fraction',0.5)}×ATR)  "
                          f"今日={data['trades_today']}/{scalp_cfg.get('max_trades_day',20)}回")
                    if data.get('scalp_buy_sma_pending'):
                        status_tag = '  [BUY] SMA20タッチ待ち'
                    elif data.get('scalp_buy_confirm_pending'):
                        status_tag = f"  [BUY] 確認 {data.get('scalp_buy_confirm_count',0)}/2本"
                    elif data.get('scalp_sell_sma_pending'):
                        status_tag = '  [SELL] SMA20タッチ待ち'
                    elif data.get('scalp_sell_confirm_pending'):
                        status_tag = f"  [SELL] 確認 {data.get('scalp_sell_confirm_count',0)}/2本"
                    elif data.get('skip_reason'):
                        status_tag = f"  skip={data['skip_reason']}"
                    else:
                        b_ok = data.get('mtf_buy_ok',  False)
                        s_ok = data.get('mtf_sell_ok', False)
                        status_tag = (f"  [待機中] H1={data.get('regime_h1','?')}"
                                      f"  M5={data.get('regime_m5','?')}"
                                      f"  MTF:BUY={'OK' if b_ok else 'NG'}"
                                      f"  SELL={'OK' if s_ok else 'NG'}")
                    print(f"  action={data['action'].upper():4s}  "
                          f"signal={data['signal_type']}  "
                          f"expected_profit=+${data.get('expected_profit_usd',0):.2f}"
                          f"(¥{int(data.get('expected_profit_jpy',0))}) "
                          f"target=¥{data.get('target_profit_jpy',0)}  "
                          f"SL=${data['sl_price']:,.2f}  TP=${data['tp_price']:,.2f}"
                          f"{status_tag}")
                else:
                    surge_tag = f"[{data['m5_surge']}]" if data['m5_surge'] != 'none' else ''
                    scalp_tag = f"[SCALP:{data['scalp_type']}]" if data['scalp_type'] != 'none' else ''
                    trend_tag = '[SELL↓]' if data['downtrend_ok'] else ''
                    print(f"\n[{ts}] #{itr}  "
                          f"close=${data['close']:,.2f}  "
                          f"RSI_H1={data['rsi_h1']:.1f}  RSI_D1={data['rsi_d1']:.1f}{trend_tag}  "
                          f"RSI_M5={data['rsi_m5']:.1f}({'↑' if data['m5_filter_ok'] else '↓/NG'})  "
                          f"RSI_M1={data['rsi_m1']:.1f}{surge_tag}  "
                          f"ATR=${data['atr']:.2f}")
                    rg_tag = (f"[ADX_H1={data.get('adx_h1',0):.0f}/{data.get('regime_h1','?')}"
                              f" M5={data.get('adx_m5',0):.0f}/{data.get('regime_m5','?')}"
                              f" ×{data.get('regime_lot_multi',1.0)}]")
                    ep_tag = (f" entry#{data.get('entry_in_window',0)}/{cfg.get('REGIME',{}).get('max_entry_per_signal',3)}"
                              if data.get('entry_in_window', 0) > 0 else '')
                    print(f"  action={data['action'].upper():4s}  "
                          f"signal={data['signal_type']}{scalp_tag}  "
                          f"SL=${data['sl_price']:,.2f}  TP=${data['tp_price']:,.2f}  "
                          f"score={data['score']}({data['strength']})  "
                          f"lot={data['lot_size']}{ep_tag}")
                    if data.get('limit_prices'):
                        prices_str = ', '.join(f'${p:,.2f}' for p in data['limit_prices'])
                        print(f"  リミット注文: {prices_str}")
                    print(f"  {rg_tag}")
                    if data.get('careful_entry', False):
                        print("  [慎重分散エントリー: 2本連続陽線後3本目BB2σタッチ]")
                    if data['signal_valid_until']:
                        print(f"  buy_window_until={data['signal_valid_until']}")
                    if data['sell_signal_type'] != 'none':
                        print(f"  sell_signal={data['sell_signal_type']}  "
                              f"sell_window_until={data['sell_valid_until']}")
                    if data.get('scalp_cooldown_rem', 0) > 0:
                        print(f"  [SCALP cooldown残{data['scalp_cooldown_rem']}分 → 通常モード中]")

                if data['skip_reason'] and not (mode == 'scalp' and data.get('scalp_mode', True)):
                    print(f"  skip: {data['skip_reason']}")
                if data.get('sell_skip_reason'):
                    print(f"  sell_skip: {data['sell_skip_reason']}")
                max_p   = data.get('max_positions',   20)
                total_p = data.get('total_positions', pos)
                avail   = data.get('available_slots',  max_p - pos)
                print(f"  残高=¥{bal}  ポジション={total_p}/{max_p}件(空き{avail})  連続損失={consec_losses}回")

                # Discord 通知: アクション変化時のみ
                curr_action = data.get('action', 'none')
                if curr_action != last_discord_action[0]:
                    try:
                        send_discord(_build_discord_signal_msg(data, mode))
                    except Exception as _de:
                        print(f"  [Discord] 通知失敗: {_de}")
                    last_discord_action[0] = curr_action
            else:
                _fail_count += 1
                msg = (f"[{datetime.now().strftime('%H:%M:%S')}] #{itr}"
                       f"  データ取得失敗 ({_fail_count}/10回目)")
                print(msg)
                _logger.error(msg)
                if _fail_count >= 10:
                    crit = "[ブリッジ] データ取得失敗 10回連続 → プロセス再起動"
                    print(crit)
                    _logger.critical(crit)
                    if console_log_path:
                        _tee.dump(console_log_path)
                    _restart = True
                    break

            if console_log_path:
                _tee.dump(console_log_path)

            if once:
                break
            time.sleep(max(0, poll_sec - (time.time() - t_s)))

    except KeyboardInterrupt:
        print("\n[ブリッジ] 終了")
    except Exception:
        _logger.exception("[ブリッジ] run_bridge メインループ 予期せぬ例外")
        raise
    finally:
        sys.stdout = _orig_stdout
        sys.stderr = _orig_stderr
        if _err_tee is not None:
            _err_tee.close()
        if console_log_path:
            _tee.dump(console_log_path)
        try:
            mt5.shutdown()
        except Exception:
            pass
        if _restart:
            sys.exit(1)  # mt5_monitor.py が検知して再起動する


def main() -> None:
    ap = argparse.ArgumentParser(description='MT5 EA リアルタイムブリッジ')
    ap.add_argument('--once',   action='store_true', help='1回だけ計算して終了')
    ap.add_argument('--symbol', default=C.MT5['symbol'])
    ap.add_argument('--output', default='./output')
    ap.add_argument('--lot',    type=float, default=None,
                    help=f'1回の取引ロット数（省略時: {C.BRIDGE["lot_size"]}）')
    ap.add_argument('--reset-losses', action='store_true',
                    help='ea_state.json の consecutive_losses を 0 にリセットして終了')
    ap.add_argument('--mode',   choices=['normal', 'scalp'], default='scalp',
                    help='normal: H1クロス戦略 / scalp: M5 RSI50クロス, 円建てTP')
    ap.add_argument('--target', type=int, default=None,
                    help='スキャルプモード目標利益（円）（省略時: config.py の値）')
    ap.add_argument('--jpy',    type=float, default=None,
                    help='スキャルプモード JPY/USD レート（省略時: config.py の値）')
    args = ap.parse_args()

    CFG['MT5']['symbol'] = args.symbol
    if args.lot    is not None:
        CFG['BRIDGE']['lot_size']         = args.lot
    if args.target is not None:
        CFG['SCALP']['target_profit_jpy'] = args.target
    if args.jpy    is not None:
        CFG['SCALP']['jpy_per_usd']       = args.jpy

    if args.reset_losses:
        import json
        from datetime import timezone as _tz
        sym        = args.symbol
        state_path = Path(CFG['BRIDGE']['status_file'])
        state_path = state_path.with_name(state_path.stem + f'_{sym}' + state_path.suffix)
        reset_path = state_path.with_name(f'ea_reset_{sym}' + state_path.suffix)
        try:
            ea   = read_ea_state(str(state_path))
            prev = ea.get('consecutive_losses', 0)
            ea['consecutive_losses'] = 0
            state_path.write_text(
                json.dumps(ea, indent=2, ensure_ascii=False), encoding='ascii')
            reset_path.write_text(
                json.dumps({'reset_since': int(datetime.now(_tz.utc).timestamp()),
                            'symbol': sym},
                           indent=2, ensure_ascii=False), encoding='ascii')
            print(f"[リセット] {state_path.name}  consecutive_losses: {prev} → 0")
            print(f"[リセット] EAリセットファイル作成: {reset_path.name}")
        except Exception as e:
            print(f"[リセット] 書き込み失敗: {e}")
        sys.exit(0)

    send_discord(f"【システム】自動売買を開始しました。 symbol={args.symbol} mode={args.mode}")
    run_bridge(CFG, once=args.once, mode=args.mode)
