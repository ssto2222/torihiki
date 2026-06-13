"""bridge/perf_report.py — エントリーログ × MT5約定履歴によるシグナル別パフォーマンス集計"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _read_entries(path: str, since: datetime) -> list[dict]:
    """entries_{symbol}.jsonl から since 以降のレコードを読み込む"""
    entries = []
    try:
        with open(path, encoding='ascii') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = datetime.strptime(rec['timestamp'], '%Y.%m.%d %H:%M:%S').replace(tzinfo=timezone.utc)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if ts >= since:
                    rec['_ts'] = ts
                    entries.append(rec)
    except FileNotFoundError:
        pass
    return entries


def _closed_profit(entry: dict, deals: list, magic: int, *, mt5) -> 'float | None':
    """エントリーログ1件に対応するポジションが決済済みなら損益合計を返す（未決済/不明なら None）"""
    direction = mt5.DEAL_TYPE_BUY if entry['action'] == 'buy' else mt5.DEAL_TYPE_SELL
    opens = [
        d for d in deals
        if d.symbol == entry['symbol'] and d.magic == magic
        and d.entry == mt5.DEAL_ENTRY_IN and d.type == direction
        and abs(datetime.fromtimestamp(d.time, tz=timezone.utc) - entry['_ts']) <= timedelta(minutes=2)
    ]
    if not opens:
        return None
    open_deal = min(opens, key=lambda d: abs(datetime.fromtimestamp(d.time, tz=timezone.utc) - entry['_ts']))
    closes = [d for d in deals
              if d.position_id == open_deal.position_id and d.entry == mt5.DEAL_ENTRY_OUT]
    if not closes:
        return None
    return sum(d.profit + d.commission + d.swap for d in closes)


def build_performance_report(symbol: str, cfg: dict, *, mt5) -> 'str | None':
    """直近 perf_report_lookback_h 時間のエントリーをシグナル別に集計し、Discord通知文を返す。

    対象エントリーが無ければ None を返す（通知しない）。
    """
    scalp       = cfg.get('SCALP', {})
    lookback_h  = scalp.get('perf_report_lookback_h', 24)
    min_winrate = scalp.get('perf_report_min_winrate', 0.40)

    log_dir  = cfg['BRIDGE'].get('log_dir', '') or 'logs'
    log_path = str(Path(log_dir) / f'entries_{symbol}.jsonl')

    since   = datetime.now(timezone.utc) - timedelta(hours=lookback_h)
    entries = _read_entries(log_path, since)
    if not entries:
        return None

    magic = cfg['MT5'].get('magic', 20240101)
    try:
        deals = mt5.history_deals_get(since - timedelta(hours=2), datetime.now(timezone.utc)) or []
    except Exception:
        deals = []

    stats: dict[str, dict] = {}
    for e in entries:
        st = stats.setdefault(e.get('signal_type', 'unknown'),
                               {'count': 0, 'closed': 0, 'wins': 0, 'pnl': 0.0})
        st['count'] += 1
        profit = _closed_profit(e, deals, magic, mt5=mt5)
        if profit is not None:
            st['closed'] += 1
            st['pnl']    += profit
            if profit > 0:
                st['wins'] += 1

    lines = [f'**{symbol} シグナル別パフォーマンス（直近{lookback_h}時間）**']
    suggestions = []
    for sig_type, st in sorted(stats.items(), key=lambda kv: -kv[1]['count']):
        if st['closed'] > 0:
            winrate = st['wins'] / st['closed']
            winrate_txt = f'{winrate * 100:.0f}%'
            if winrate < min_winrate and st['pnl'] < 0:
                suggestions.append(
                    f'- `{sig_type}`: 勝率{winrate_txt}・損益{st["pnl"]:+.2f} → 戦略見直しを推奨'
                )
        else:
            winrate_txt = '-'
        lines.append(
            f'- `{sig_type}`: 発火{st["count"]}件 / 決済済{st["closed"]}件 '
            f'/ 勝率{winrate_txt} / 損益{st["pnl"]:+.2f}'
        )

    if suggestions:
        lines.append('')
        lines.append('**戦略見直し候補**')
        lines.extend(suggestions)

    return '\n'.join(lines)
