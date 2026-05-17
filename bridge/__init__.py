"""bridge — MT5 EA ブリッジ パッケージ

注意: runner モジュールは MetaTrader5 を必要とするため、
      ここでは自動インポートしない。
      run_bridge / main は bridge.runner または mt5_ea_bridge から直接インポートすること。
"""
from bridge.state         import (SignalState, ScalpState, TimeBiasState,
                                   JpyRateCache, Sma20TouchCache)
from bridge.io            import write_signal, read_ea_state
from bridge.notify        import send_discord, _build_discord_signal_msg
from bridge.utils         import (_calc_lot, _detect_regime, _regime_lot_multi,
                                   _position_status, _has_positions_in_direction,
                                   _close_profitable_positions, _is_in_danger_skip_window,
                                   _reset_entry_windows, _setup_file_logging, _get_jpy_per_usd)
from bridge.time_bias     import _build_time_bias, _load_time_bias
from bridge.sma20         import _analyze_sma20_touch_margin, _load_sma20_touch_margins
from bridge.signal_normal  import compute_signal
from bridge.signal_scalp   import compute_scalp_signal
from bridge.param_override import (
    PARAMS, parse_value, load_overrides, save_overrides,
    set_override, clear_overrides, apply_overrides, current_values_text,
)
from bridge.discord_cmd    import start_discord_bot

__all__ = [
    'SignalState', 'ScalpState', 'TimeBiasState', 'JpyRateCache', 'Sma20TouchCache',
    'write_signal', 'read_ea_state',
    'send_discord', '_build_discord_signal_msg',
    '_calc_lot', '_detect_regime', '_regime_lot_multi', '_position_status',
    '_has_positions_in_direction', '_close_profitable_positions',
    '_is_in_danger_skip_window', '_reset_entry_windows',
    '_setup_file_logging', '_get_jpy_per_usd',
    '_build_time_bias', '_load_time_bias',
    '_analyze_sma20_touch_margin', '_load_sma20_touch_margins',
    'compute_signal', 'compute_scalp_signal',
    'PARAMS', 'parse_value', 'load_overrides', 'save_overrides',
    'set_override', 'clear_overrides', 'apply_overrides', 'current_values_text',
    'start_discord_bot',
]
