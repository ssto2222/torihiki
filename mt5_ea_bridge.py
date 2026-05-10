"""
mt5_ea_bridge.py — MT5 EA リアルタイム連携ブリッジ（後方互換シム）
====================================================================
全ロジックは bridge/ パッケージに移行済み。
このファイルは外部スクリプト（analyze_sma20_touch.py 等）向けの
後方互換インポートを維持するためのシムです。

実行:
    python mt5_ea_bridge.py           # ポーリングループ（Ctrl+C で終了）
    python mt5_ea_bridge.py --once    # 1回だけ計算して終了（動作確認用）
    python mt5_ea_bridge.py --symbol BTCUSD --lot 0.05
    python mt5_ea_bridge.py --mode scalp

通信プロトコル:
    Python → MT5 EA : signal_SYMBOL.json   （毎ポーリング更新）
    MT5 EA → Python : ea_state_SYMBOL.json （EA が書き込む状態）
"""

# ── bridge/ パッケージから全シンボルを再エクスポート ──────────────
from bridge import (
    SignalState, ScalpState, TimeBiasState, JpyRateCache, Sma20TouchCache,
    write_signal, read_ea_state,
    send_discord, _build_discord_signal_msg,
    _calc_lot, _detect_regime, _regime_lot_multi, _position_status,
    _has_positions_in_direction, _close_profitable_positions,
    _is_in_danger_skip_window, _reset_entry_windows,
    _setup_file_logging, _get_jpy_per_usd,
    _build_time_bias, _load_time_bias,
    _analyze_sma20_touch_margin, _load_sma20_touch_margins,
    compute_signal, compute_scalp_signal,
)

# connect_mt5 は core.data に実装されているが analyze_sma20_touch.py がここから import する
from core.data import connect_mt5


def __getattr__(name: str):
    """bridge.runner のシンボル（MetaTrader5 必須）を遅延インポートする"""
    if name in ('run_bridge', 'main', 'CFG', 'check_pause_signal'):
        from bridge import runner as _runner
        return getattr(_runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == '__main__':
    from bridge.runner import main as _main
    _main()
