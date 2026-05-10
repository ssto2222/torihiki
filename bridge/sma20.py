"""bridge/sma20.py — SMA20 タッチマージン分析・キャッシュ"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from core.data import fetch_ohlcv

if TYPE_CHECKING:
    from bridge.state import Sma20TouchCache


def _analyze_sma20_touch_margin(symbol: str, cfg: dict) -> float:
    """
    M5 RSI sell クロス後に M1 価格が SMA20 にどこまで接近するか分析し、
    過去シグナルの pct% をキャッチできる touch_margin (USD) を返す。
    """
    try:
        from core.indicators import add_m1_indicators

        scalp    = cfg.get('SCALP', {})
        exec_cfg = cfg.get('EXECUTION', {})
        sell_thrs = sorted(scalp.get('rsi_sell_thrs', [45.0, 40.0, 35.0]), reverse=True)
        pct      = exec_cfg.get('sma20_touch_pct', 70)
        window   = 30

        df_raw = fetch_ohlcv(symbol, 'M1', 20_000)
        if df_raw is None or len(df_raw) < 300:
            return exec_cfg.get('touch_margin', 0.20)

        df = add_m1_indicators(df_raw, cfg)
        if df.empty or 'SMA20' not in df.columns:
            return exec_cfg.get('touch_margin', 0.20)

        rsi   = df['RSI'].values
        close = df['Close'].values
        sma20 = df['SMA20'].values
        n     = len(rsi)

        gaps = []
        i = 1
        while i < n - window:
            for thr in sell_thrs:
                if rsi[i] < thr <= rsi[i - 1]:
                    max_close = float(np.max(close[i:i + window]))
                    gaps.append(sma20[i] - max_close)
                    i += window
                    break
            else:
                i += 1

        if len(gaps) < 10:
            print(f"[SMA20分析:{symbol}] シグナル数不足 ({len(gaps)}件) → デフォルト使用")
            return exec_cfg.get('touch_margin', 0.20)

        margin = float(np.percentile(gaps, pct))
        margin = max(0.0, round(margin, 2))
        print(f"[SMA20分析:{symbol}] シグナル{len(gaps)}件 → touch_margin={margin:.2f} USD "
              f"(p{pct})")
        return margin

    except Exception as e:
        print(f"[SMA20分析:{symbol}] エラー: {e}")
        return cfg.get('EXECUTION', {}).get('touch_margin', 0.20)


def _load_sma20_touch_margins(symbols: list, cache: 'Sma20TouchCache', cfg: dict) -> None:
    """起動時にシンボルごとの SMA20 タッチマージンを分析・キャッシュする"""
    exec_cfg   = cfg.get('EXECUTION', {})
    cache_path = exec_cfg.get('sma20_touch_margin_file',
                              './output/sma20_touch_margins.json')
    max_age_d  = 7

    cached = {}
    try:
        p = Path(cache_path)
        if p.exists():
            data     = json.loads(p.read_text(encoding='utf-8'))
            saved_at = datetime.fromisoformat(data.get('saved_at', '2000-01-01T00:00:00+00:00'))
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - saved_at).total_seconds() / 86400
            if age_days < max_age_d:
                cached = data.get('margins', {})
    except Exception:
        pass

    updated = False
    for sym in symbols:
        if sym in cached:
            cache.margins[sym] = float(cached[sym])
            print(f"[SMA20マージン] {sym}: {cached[sym]:.2f} USD (キャッシュ)")
        else:
            margin = _analyze_sma20_touch_margin(sym, cfg)
            cache.margins[sym] = margin
            cached[sym] = margin
            updated = True

    if updated:
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            out = {
                'saved_at': datetime.now(timezone.utc).isoformat(),
                'margins':  cached,
            }
            Path(cache_path).write_text(
                json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            print(f"[SMA20マージン] キャッシュ保存失敗: {e}")
