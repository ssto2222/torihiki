"""bridge/utils.py — ステートレスなユーティリティ関数群"""
from __future__ import annotations
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
import numpy as np

if TYPE_CHECKING:
    from bridge.state import SignalState, JpyRateCache

_logger = logging.getLogger('torihiki')


def _setup_file_logging(log_dir: str, symbol: str) -> None:
    """run_bridge 起動時に log_dir にログファイルを作成する"""
    if not log_dir:
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(log_dir) / f'bridge_{symbol}.log'
    handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    ))
    if not any(isinstance(h, RotatingFileHandler) and
               getattr(h, 'baseFilename', '') == str(log_path.resolve())
               for h in _logger.handlers):
        _logger.addHandler(handler)
    _logger.warning(f'ログ開始: {log_path}')


def _get_jpy_per_usd(cache: 'JpyRateCache', fallback: float = 150.0, *, mt5) -> float:
    """USDJPY レートを MT5 から取得し 1 時間キャッシュする"""
    now = datetime.now(timezone.utc)
    if cache.fetched_at is not None and (now - cache.fetched_at).total_seconds() < 3600:
        return cache.value
    try:
        tick = mt5.symbol_info_tick("USDJPY")
        if tick and tick.bid > 0:
            cache.value = float(tick.bid)
            cache.fetched_at = now
            print(f"[JPY/USD] 更新: {cache.value:.3f}")
            return cache.value
    except Exception:
        pass
    if cache.fetched_at is None:
        print(f"[JPY/USD] USDJPY 取得失敗 → フォールバック {fallback}")
    return fallback


def _calc_lot(balance: float, risk_pct: float, sl_dist: float,
              contract_size: float,
              lot_min: float, lot_max: float, lot_step: float,
              fallback: float) -> float:
    """残高ベースのロットサイズ計算"""
    if sl_dist <= 0 or contract_size <= 0:
        return fallback
    risk_usd = balance * risk_pct
    lot = risk_usd / (sl_dist * contract_size)
    lot = round(lot / lot_step) * lot_step
    return max(lot_min, min(lot_max, lot))


def _position_status(risk_pct: float, total_risk_pct: float, *, mt5) -> dict:
    """全ポジション数と空きスロット数を返す"""
    max_positions = max(1, int(total_risk_pct / risk_pct))
    try:
        positions = mt5.positions_get()
        total = len(positions) if positions is not None else 0
    except Exception:
        total = 0
    return {
        'max_positions':   max_positions,
        'total_positions': total,
        'available_slots': max(0, max_positions - total),
    }


def _detect_regime(adx: float, di_plus: float, di_minus: float,
                   regime_cfg: dict) -> str:
    """ADX に基づくレジーム判定。returns: 'trend_up' | 'trend_down' | 'weak_trend' | 'range'"""
    if np.isnan(adx):
        return 'range'
    trend_thr = regime_cfg.get('trend_thr', 25.0)
    range_thr = regime_cfg.get('range_thr', 20.0)
    if adx >= trend_thr:
        return 'trend_up' if di_plus > di_minus else 'trend_down'
    if adx >= range_thr:
        return 'weak_trend'
    return 'range'


def _regime_lot_multi(regime_h1: str, regime_m5: str, regime_cfg: dict) -> float:
    """H1 / M5 のレジームからロット倍率を返す"""
    t_h1 = regime_h1.startswith('trend')
    t_m5 = regime_m5.startswith('trend')
    if t_h1 and t_m5:
        return float(regime_cfg.get('lot_multi_trend', 1.5))
    if t_h1 or t_m5:
        return float(regime_cfg.get('lot_multi_weak',  1.0))
    return float(regime_cfg.get('lot_multi_range', 0.6))


def _has_positions_in_direction(symbol: str, magic: int, direction: str, *, mt5) -> bool:
    """指定シンボル・magic のポジションが direction 方向に存在するか調べる"""
    try:
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return False
        order_type = mt5.ORDER_TYPE_BUY if direction == 'buy' else mt5.ORDER_TYPE_SELL
        return any(p.magic == magic and p.type == order_type for p in positions)
    except Exception:
        return False


def _close_profitable_positions(symbol: str, magic: int, deviation: int, *, mt5) -> int:
    """MT5 の含み益ポジション（magic 一致）を全決済する。決済した件数を返す。"""
    closed = 0
    try:
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return 0
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return 0
        for pos in positions:
            if pos.magic != magic or pos.profit <= 0:
                continue
            is_buy = (pos.type == mt5.ORDER_TYPE_BUY)
            req = {
                'action':       mt5.TRADE_ACTION_DEAL,
                'symbol':       symbol,
                'volume':       pos.volume,
                'type':         mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                'position':     pos.ticket,
                'price':        tick.bid if is_buy else tick.ask,
                'deviation':    deviation,
                'magic':        magic,
                'comment':      'time_bias_close',
                'type_time':    mt5.ORDER_TIME_GTC,
                'type_filling': mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(req)
            if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
                closed += 1
                print(f"    ticket={pos.ticket}  profit={pos.profit:+.2f}  → 決済完了")
    except Exception as e:
        print(f"  [時間帯バイアス] 決済エラー: {e}")
    return closed


def _is_in_danger_skip_window(now_utc: datetime, danger_hours: set,
                              skip_before_min: int = 30,
                              skip_after_min: int = 15) -> bool:
    """危険時間帯の前後スキップ対象かチェックする"""
    if not danger_hours:
        return False
    for danger_hour in danger_hours:
        danger_start = datetime(now_utc.year, now_utc.month, now_utc.day,
                                danger_hour, 0, tzinfo=timezone.utc)
        danger_end   = danger_start + timedelta(hours=1)
        skip_start   = danger_start - timedelta(minutes=skip_before_min)
        skip_end     = danger_end   + timedelta(minutes=skip_after_min)
        if skip_start <= now_utc < skip_end:
            return True
    return False


def _reset_entry_windows(state: 'SignalState') -> None:
    """危険時間帯終了後に分散エントリーカウンタをリセットする"""
    state.entry_in_window       = 0
    state.last_entry_price      = 0.0
    state.signal_window_key     = (None, None)
    state.sell_entry_in_window  = 0
    state.sell_last_entry_price = 0.0
    state.sell_window_key       = (None, None)
