"""bridge/runner.py — MT5 EA ブリッジ ポーリングループ"""
from __future__ import annotations
import argparse
import io
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ANSI エスケープコード除去用（console_symbol.log に読める形で保存するため）
_ANSI_ESCAPE = re.compile(r'\033\[[0-9;]*[mA-Za-z]')


class _TeeWriter(io.TextIOBase):
    """sys.stdout をラップして、コンソールとバッファの両方に書き込む。
    log_dir が設定されている場合に使用し、ポーリング反復ごとに
    バッファをリセット → 反復末にファイルへ上書き保存する。
    """

    def __init__(self, original: io.TextIOBase) -> None:
        self._orig = original
        self._buf: list[str] = []

    def write(self, text: str) -> int:
        try:
            self._orig.write(text)
        except UnicodeEncodeError:
            # Windows cp932 など狭いエンコーディングで ¥ × 等が書けない場合
            enc = getattr(self._orig, 'encoding', 'utf-8') or 'utf-8'
            self._orig.write(text.encode(enc, errors='replace').decode(enc, errors='replace'))
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
        """バッファ内容から ANSI コードを除去してファイルに上書き保存する"""
        try:
            plain = _ANSI_ESCAPE.sub('', ''.join(self._buf))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(plain, encoding='utf-8-sig')
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
from core.data        import connect_mt5, fetch_ohlcv
from core.indicators  import add_m5_indicators
from core.strategy    import detect_big_move

from bridge.state        import SignalState, ScalpState, TimeBiasState, JpyRateCache, Sma20TouchCache, MacroBiasState
from bridge.io           import write_signal, read_ea_state
from bridge.notify       import (send_discord, check_pause_signal,
                                 _build_discord_signal_msg, _build_discord_hourly_msg)
from bridge.discord_cmd  import start_discord_bot
from bridge.utils        import (_setup_file_logging, _is_in_danger_skip_window,
                                 _close_profitable_positions, _reset_entry_windows)
from bridge.time_bias    import _build_time_bias, _load_time_bias
from bridge.sma20        import _load_sma20_touch_margins
from bridge.signal_normal  import compute_signal
from bridge.signal_scalp   import compute_scalp_signal
from bridge.param_override import apply_overrides
from bridge.dashboard      import print_poll_status, activate_dashboard_mode, format_poll_status_plain
from core.macro_analysis   import analyze_macro_bias

_logger = logging.getLogger('torihiki')
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _logger.addHandler(logging.NullHandler())

CFG = {k: getattr(C, k) for k in
       ['MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES', 'LOCAL', 'PLOT',
        'BRIDGE', 'SCALP', 'REGIME', 'TIME_BIAS', 'WHIPSAW', 'ELLIOTT', 'MACRO']}


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

    # Windows cp932 コンソールでも ¥ × などの文字を出力できるよう UTF-8 に切り替え
    # （-X utf8 モードで起動された場合や既に UTF-8 の場合は実質ノーオペレーション）
    for _stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(_stream, 'reconfigure') and getattr(_stream, 'encoding', '') != 'utf-8':
                _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    # コンソール出力をファイルにも上書き保存する（log_dir が設定されている場合）
    console_log_path = Path(log_dir) / f'console_{symbol}.log' if log_dir else None
    error_log_path   = Path(log_dir) / f'error_{symbol}.log'   if log_dir else None
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    _tee = _TeeWriter(_orig_stdout)
    _err_tee: _ErrTeeWriter | None = None

    # ダッシュボードモード: config が True ならターミナル種別に依存せず有効化
    # isatty() は使わない — ランチャー経由や nohup でも動作させるため
    _dash_cfg = cfg['BRIDGE'].get('dashboard_mode', True)
    _is_dashboard = bool(_dash_cfg)
    if _is_dashboard:
        activate_dashboard_mode()  # _USE_COLOR=True + Windows ANSI 強制有効

    # _tee を常に stdout に差し込む（ダッシュボード用ログバッファ確保のため）
    # log_dir 未設定でも _tee._buf でポーリング毎のログを蓄積できる
    sys.stdout = _tee
    if error_log_path:
        _err_tee = _ErrTeeWriter(_orig_stderr, error_log_path)
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
    sig_state   = SignalState()
    sc_state    = ScalpState()
    tb_state    = TimeBiasState()
    jpy_cache   = JpyRateCache()
    sma20_cache = Sma20TouchCache()
    macro_state = MacroBiasState()
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

    # MT5 接続リトライ（ターミナル起動直後などに間に合わないケースに対応）
    _MT5_CONNECT_RETRIES = 3
    _MT5_CONNECT_WAIT    = 10  # 秒
    _connected = False
    for _attempt in range(1, _MT5_CONNECT_RETRIES + 1):
        if connect_mt5(symbol, cfg['MT5']):
            _connected = True
            break
        if _attempt < _MT5_CONNECT_RETRIES:
            print(f"[MT5] 接続失敗 ({_attempt}/{_MT5_CONNECT_RETRIES}) → {_MT5_CONNECT_WAIT}秒後に再試行")
            time.sleep(_MT5_CONNECT_WAIT)
    if not _connected:
        print(f"\n[エラー] MT5 接続失敗 ({_MT5_CONNECT_RETRIES}回試行) → ウォッチドッグが再起動します")
        sys.exit(2)  # 2=接続失敗: watchdog は MT5 を kill せずブリッジのみ再起動

    if mode == 'scalp':
        _load_sma20_touch_margins([symbol], sma20_cache, cfg)
        m = sma20_cache.margins.get(symbol)
        if m is not None:
            print(f"  SMA20タッチマージン: {symbol} = {m:.2f} USD")

    _fail_count           = 0
    _restart              = False
    _last_hourly_discord  = 0.0   # 1時間ごと Discord タイマー（epoch 秒）
    _scalp_was_active     = (mode == 'scalp')  # scalp モードが有効だったことがあるか
    _calm_count           = 0                  # 大変動解消の連続ポール数

    # Discord ボット: !status コマンドが参照できるよう最新 data / macro_state を共有する
    # !mode / !symbol コマンドで書き込まれる切り替えリクエストも同じ仕組みで受け取る
    _shared_data   = [None]    # type: list[dict | None]
    _shared_macro  = [None]    # type: list[MacroBiasState | None]
    _mode_ref      = [mode]    # type: list[str]
    _symbol_ref    = [symbol]  # type: list[str]
    start_discord_bot(cfg, _shared_data, _shared_macro, _mode_ref, _symbol_ref)

    try:
        itr = 0
        while True:
            if check_pause_signal(symbol, flag_file, mt5=mt5):
                time.sleep(0.1)
                continue

            itr += 1
            t_s  = time.time()
            _tee.reset()  # 常にリセット（ダッシュボード用バッファを毎イテレーション更新）

            # ── Discord からのモード/シンボル切り替えリクエストを処理 ───────────────
            _req_mode = _mode_ref[0]
            if _req_mode != mode:
                _logger.info(f'[モード切替] {mode} → {_req_mode}')
                print(f"  [モード切替] {mode.upper()} → {_req_mode.upper()}")
                mode = _req_mode

            _req_symbol = _symbol_ref[0]
            if _req_symbol != symbol:
                _old_sym = symbol
                _logger.info(f'[シンボル切替] {_old_sym} → {_req_symbol}')
                print(f"  [シンボル切替] {_old_sym} → {_req_symbol}  MT5再接続・状態リセット中...")
                mt5.shutdown()
                symbol               = _req_symbol
                cfg['MT5']['symbol'] = symbol
                # パス再計算（_sym_path クロージャは symbol 変数を参照するため再計算可能）
                sig_path   = _sym_path(cfg['BRIDGE']['signal_file'])
                state_path = _sym_path(cfg['BRIDGE']['status_file'])
                log_sig    = str(Path(log_dir) / Path(sig_path).name) if log_dir else ''
                Path(sig_path).parent.mkdir(parents=True, exist_ok=True)
                # 状態リセット（新シンボル用に新インスタンスを生成）
                sig_state              = SignalState()
                sc_state               = ScalpState()
                jpy_cache              = JpyRateCache()
                sma20_cache            = Sma20TouchCache()
                macro_state            = MacroBiasState()
                last_discord_action[0] = 'none'
                # MT5 再接続
                _sym_ok = False
                for _att in range(1, _MT5_CONNECT_RETRIES + 1):
                    if connect_mt5(symbol, cfg['MT5']):
                        _sym_ok = True
                        break
                    if _att < _MT5_CONNECT_RETRIES:
                        print(f"  [MT5] 接続失敗 ({_att}/{_MT5_CONNECT_RETRIES})"
                              f" → {_MT5_CONNECT_WAIT}秒後に再試行")
                        time.sleep(_MT5_CONNECT_WAIT)
                if not _sym_ok:
                    print(f"  [シンボル切替] MT5 接続失敗: {symbol}"
                          f" → 元のシンボル {_old_sym} に戻します")
                    _logger.error(f'[シンボル切替] MT5 接続失敗: {symbol}')
                    symbol               = _old_sym
                    cfg['MT5']['symbol'] = symbol
                    _symbol_ref[0]       = symbol
                    sig_path   = _sym_path(cfg['BRIDGE']['signal_file'])
                    state_path = _sym_path(cfg['BRIDGE']['status_file'])
                    log_sig    = str(Path(log_dir) / Path(sig_path).name) if log_dir else ''
                    for _att in range(1, _MT5_CONNECT_RETRIES + 1):
                        if connect_mt5(symbol, cfg['MT5']):
                            break
                        time.sleep(_MT5_CONNECT_WAIT)
                else:
                    if mode == 'scalp':
                        _load_sma20_touch_margins([symbol], sma20_cache, cfg)
                    print(f"  [シンボル切替] {symbol} に切り替えました")

            effective_cfg = apply_overrides(cfg)
            _eff_scalp    = effective_cfg.get('SCALP', {})
            _eff_rules    = effective_cfg.get('RULES', {})
            _macro_cfg    = effective_cfg.get('MACRO', {})

            # ── マクロバイアス定期更新 ─────────────────────────────────
            if _macro_cfg.get('enabled', True):
                _macro_interval_h = _macro_cfg.get('update_interval_h', 4)
                _macro_elapsed_h  = (time.time() - macro_state.last_updated_at) / 3600
                if macro_state.last_updated_at == 0.0 or _macro_elapsed_h >= max(_macro_interval_h, 0.1):
                    try:
                        _close_now = 0.0
                        _atr_now   = 0.0
                        _mb = analyze_macro_bias(symbol, effective_cfg, _close_now, _atr_now, mt5=mt5)
                        macro_state.bias            = _mb['bias']
                        macro_state.bias_label      = _mb['bias_label']
                        macro_state.buy_tp_multi    = _mb['buy_tp_multi']
                        macro_state.sell_tp_multi   = _mb['sell_tp_multi']
                        macro_state.buy_risk_multi  = _mb['buy_risk_multi']
                        macro_state.sell_risk_multi = _mb['sell_risk_multi']
                        macro_state.score_adj_buy   = _mb['score_adj_buy']
                        macro_state.score_adj_sell  = _mb['score_adj_sell']
                        macro_state.nearest_nl      = _mb['nearest_nl']
                        macro_state.nl_dir          = _mb['nl_dir']
                        macro_state.target_up       = _mb['target_up']
                        macro_state.target_down     = _mb['target_down']
                        macro_state.d1_rsi          = _mb['d1_rsi']
                        macro_state.d1_above_sma200 = _mb['d1_above_sma200']
                        macro_state.summary         = _mb['summary']
                        macro_state.last_updated_at = time.time()
                        print(f"  [マクロ] {macro_state.summary}")
                    except Exception as _me:
                        print(f"  [マクロ] 分析失敗: {_me}")

            # ── 通常モード → スキャルプ自動復帰チェック ───────────────────
            if mode == 'scalp':
                _scalp_was_active = True
                _calm_count       = 0
            elif _scalp_was_active and _eff_scalp.get('scalp_auto_restore', True):
                _bm_lookback = _eff_scalp.get('big_move_lookback', 12)
                _bm_multi    = _eff_scalp.get('big_move_atr_multi', 2.0)
                _restore_thr = _eff_scalp.get('scalp_restore_calm_bars', 5)
                try:
                    _bm_raw = fetch_ohlcv(symbol, 'M5', _bm_lookback + 15)
                    if _bm_raw is not None:
                        _bm_df  = add_m5_indicators(_bm_raw, effective_cfg)
                        _bm_val = detect_big_move(_bm_df, _bm_lookback, _bm_multi)
                        if _bm_val == 'none':
                            _calm_count += 1
                            if _calm_count >= _restore_thr:
                                _calm_count  = 0
                                mode         = 'scalp'
                                _mode_ref[0] = 'scalp'
                                _logger.info(f'[自動復帰] {_restore_thr}ポール安定 → スキャルプ復帰')
                                print(f"  [通常→スキャルプ自動復帰] {_restore_thr}ポール連続安定 → スキャルプモードに戻ります")
                        else:
                            _calm_count = 0
                except Exception as _br_e:
                    _logger.debug(f'[スキャルプ復帰チェック] {_br_e}')

            if mode == 'scalp':
                data = compute_scalp_signal(symbol, effective_cfg, sc_state, sig_state,
                                            jpy_cache, sma20_cache, mt5=mt5,
                                            macro_state=macro_state)
            else:
                data = compute_signal(symbol, effective_cfg, sig_state, jpy_cache,
                                      mt5=mt5, macro_state=macro_state)

            # !status コマンド用に最新データを共有
            if data:
                _shared_data[0]  = data
                _shared_macro[0] = macro_state

            if data:
                _fail_count = 0
                ea            = read_ea_state(state_path)
                consec_losses = ea.get('consecutive_losses', 0)
                pos           = ea.get('positions', 0)
                bal           = ea.get('balance', 'N/A')

                def _runner_cancel(reason: str) -> None:
                    """runner.py 側でエントリーをキャンセルし、signal_scalp で加算済みの
                    state.count を戻す（実エントリーなしなので trades_today を増やさない）。"""
                    data['action']      = 'none'
                    data['skip_reason'] = reason
                    if sc_state.count > 0:
                        sc_state.count -= 1
                    data['trades_today'] = sc_state.count

                if consec_losses >= _eff_rules.get('max_consecutive_losses', max_consec) and data['action'] in ('buy', 'sell'):
                    _runner_cancel(f'consecutive_losses={consec_losses}>={max_consec}')

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
                            tb_state.danger_close_done_hr = danger_hour

                    if in_skip_window and data.get('action') in ('buy', 'sell'):
                        _runner_cancel(f'禁止時間帯({now_utc.strftime("%H:%M")}UTC)')

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
                        _runner_cancel(f'危険時間帯後クールダウン(残{rem}分)')
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

                # ダッシュボード用: 今のイテレーションで _tee に蓄積されたログ行を抽出
                _recent_logs = ''.join(_tee._buf).splitlines() if _is_dashboard else []

                print_poll_status(data, mode, itr, bal, consec_losses, effective_cfg,
                                  macro_state=macro_state,
                                  dashboard_mode=_is_dashboard,
                                  recent_logs=_recent_logs)

                # !status Discord コマンド用スナップショット（ポーリング毎に上書き）
                try:
                    _sym_sfx = Path(sig_path).stem[len('signal'):]  # '_BTCUSD' など
                    _sd_path = Path(sig_path).parent / f'status_display{_sym_sfx}.txt'
                    _sd_path.write_text(
                        format_poll_status_plain(data, mode, itr, bal, consec_losses,
                                                 effective_cfg, macro_state=macro_state),
                        encoding='utf-8',
                    )
                except Exception:
                    pass

                # Discord 通知: アクション変化時のみ
                curr_action = data.get('action', 'none')
                if curr_action != last_discord_action[0]:
                    try:
                        send_discord(_build_discord_signal_msg(data, mode))
                    except Exception as _de:
                        print(f"  [Discord] 通知失敗: {_de}")
                    last_discord_action[0] = curr_action

                # Discord 通知: 1時間ごとのステータスサマリー
                _now_ts = time.time()
                if _now_ts - _last_hourly_discord >= 3600:
                    try:
                        send_discord(_build_discord_hourly_msg(data, macro_state))
                    except Exception as _de:
                        print(f"  [Discord hourly] 通知失敗: {_de}")
                    _last_hourly_discord = _now_ts

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


def _is_bridge_duplicate(symbol: str) -> bool:
    """同一シンボルのブリッジが既に動いているか確認する"""
    try:
        import psutil
        my_pid  = os.getpid()
        my_ppid = os.getppid()
        script  = 'mt5_ea_bridge.py'
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            if p.pid in (my_pid, my_ppid):
                continue
            try:
                if 'python' not in (p.info.get('name') or '').lower():
                    continue
                cl = ' '.join(p.info.get('cmdline') or [])
                if script in cl and symbol in cl and p.is_running():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


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

    if _is_bridge_duplicate(args.symbol):
        print(f"[Bridge] シンボル {args.symbol} のブリッジは既に起動中です → 終了します。")
        sys.exit(1)

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
            reset_ts = int(datetime.now(_tz.utc).timestamp())
            ea['consecutive_losses'] = 0
            ea['reset_since']        = reset_ts  # EA 再起動後も復元できるよう永続化
            state_path.write_text(
                json.dumps(ea, indent=2, ensure_ascii=False), encoding='ascii')
            reset_path.write_text(
                json.dumps({'reset_since': reset_ts, 'symbol': sym},
                           indent=2, ensure_ascii=False), encoding='ascii')
            print(f"[リセット] {state_path.name}  consecutive_losses: {prev} → 0")
            print(f"[リセット] EAリセットファイル作成: {reset_path.name}")
        except Exception as e:
            print(f"[リセット] 書き込み失敗: {e}")
        sys.exit(0)

    send_discord(f"【システム】自動売買を開始しました。 symbol={args.symbol} mode={args.mode}")
    run_bridge(CFG, once=args.once, mode=args.mode)
