"""bridge/state.py — ポーリングをまたぐ状態をデータクラスで管理"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SignalState:
    """compute_signal() がポーリング間で保持する状態"""
    prev_rsi_h1: Optional[float] = None
    prev_rsi_m1: Optional[float] = None
    rapid_fall_at: Optional[datetime] = None

    # BUY シグナルウィンドウ
    signal_active_type: Optional[str] = None
    signal_active_until: Optional[datetime] = None

    # SELL シグナルウィンドウ
    signal_sell_active_type: Optional[str] = None
    signal_sell_active_until: Optional[datetime] = None

    # BUY 分散エントリー追跡
    entry_in_window: int = 0
    last_entry_price: float = 0.0
    signal_window_key: tuple = field(default_factory=lambda: (None, None))

    # SELL 分散エントリー追跡
    sell_entry_in_window: int = 0
    sell_last_entry_price: float = 0.0
    sell_window_key: tuple = field(default_factory=lambda: (None, None))

    # BB2σ タッチ状態（globals() アクセスから移行）
    bb2_touched_buy: bool = False
    bb2_touched_sell: bool = False
    bb2_touched_at_buy: Optional[datetime] = None
    bb2_touched_at_sell: Optional[datetime] = None


@dataclass
class ScalpState:
    """compute_scalp_signal() がポーリング間で保持する状態"""
    prev_rsi: Optional[float] = None
    last_bar_time: Optional[datetime] = None
    last_at: Optional[datetime] = None
    count: int = 0
    date: object = None
    last_action: str = 'none'
    m1_rsi_above_65: bool = False

    # SELL SMA20 タッチ待ち
    sell_sma_pending: bool = False
    sell_sma_at: Optional[datetime] = None
    sell_sma_level: float = 0.0

    # BUY SMA20 タッチ待ち
    buy_sma_pending: bool = False
    buy_sma_at: Optional[datetime] = None
    buy_sma_level: float = 0.0

    # BUY 確認（SMA20 タッチ後に M1 上昇バー 2 本）
    buy_confirm_pending: bool = False
    buy_confirm_at: Optional[datetime] = None
    buy_confirm_count: int = 0
    buy_confirm_bar_time: object = None
    buy_confirm_level: float = 0.0

    # SELL 確認（SMA20 タッチ後に M1 下落バー 2 本）
    sell_confirm_pending: bool = False
    sell_confirm_at: Optional[datetime] = None
    sell_confirm_count: int = 0
    sell_confirm_bar_time: object = None
    sell_confirm_level: float = 0.0


@dataclass
class TimeBiasState:
    """run_bridge() 内の時間帯バイアス回避で使う状態"""
    hours: set = field(default_factory=set)
    danger_close_done_hr: int = -1
    prev_in_danger: bool = False
    danger_exit_until: Optional[datetime] = None
    last_rebias_at: float = 0.0


@dataclass
class JpyRateCache:
    """USDJPY レートの 1 時間キャッシュ"""
    value: float = 150.0
    fetched_at: Optional[datetime] = None


@dataclass
class Sma20TouchCache:
    """シンボル別 SMA20 タッチマージンキャッシュ"""
    margins: dict = field(default_factory=dict)
