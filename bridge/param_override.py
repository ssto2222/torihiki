"""bridge/param_override.py — ランタイムパラメータ オーバーライド管理

Discord bot から書き込まれた JSON をポーリングごとに cfg に上書きマージする。
設定ファイル (config.py) は変更しない。プロセス再起動で元の値に戻る。
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any, NamedTuple

_logger = logging.getLogger('torihiki')

OVERRIDE_FILE = './output/runtime_params.json'

# ── パラメータ仕様 ─────────────────────────────────────────────────────────
class ParamSpec(NamedTuple):
    section: str        # cfg キー（'SCALP', 'BRIDGE', 'SL', 'SIGNAL' …）
    key:     str        # section 内のキー
    type_:   type       # int / float / bool
    min_v:   Any        # 最小値（bool は None）
    max_v:   Any        # 最大値（bool は None）
    desc:    str        # 説明文

PARAMS: dict[str, ParamSpec] = {
    # スキャルプ設定
    'target':      ParamSpec('SCALP',  'target_profit_jpy',  int,   100,   50_000, '目標利益(円)'),
    'sl_ratio':    ParamSpec('SCALP',  'sl_ratio',           float, 1.0,   10.0,   'SL比率(TP幅の何倍か)'),
    'tp_frac':     ParamSpec('SCALP',  'tp_atr_fraction',    float, 0.1,   2.0,    'TP幅 = M5 ATR × この値'),
    'buy':         ParamSpec('SCALP',  'buy_enabled',        bool,  None,  None,   'BUY有効/無効'),
    'sell':        ParamSpec('SCALP',  'sell_enabled',       bool,  None,  None,   'SELL有効/無効'),
    'max_trades':  ParamSpec('SCALP',  'max_trades_day',     int,   1,     200,    '1日の最大エントリー回数'),
    'cooldown':    ParamSpec('SCALP',  'cooldown_min',       int,   1,     240,    'エントリー間クールダウン(分)'),
    'jpy_rate':    ParamSpec('SCALP',  'jpy_per_usd',        float, 100.0, 200.0,  'JPY/USDレート'),
    # ブリッジ設定
    'lot':         ParamSpec('BRIDGE', 'lot_size',           float, 0.01,  10.0,   'フォールバックロットサイズ'),
    'risk':        ParamSpec('BRIDGE', 'risk_pct',           float, 0.001, 0.10,   'リスク割合(0.03=3%)'),
    # SL/TP設定
    'sl_multi':    ParamSpec('SL',     'sl_multi',           float, 0.5,   5.0,    'SL幅 = ATR × この値'),
    'tp_multi':    ParamSpec('SL',     'tp_atr_multi',       float, 0.5,   10.0,   'TP幅 = ATR × この値'),
    # シグナル設定
    'rsi_buy':     ParamSpec('SIGNAL', 'buy_rsi_thr',        float, 20.0,  60.0,   'BUY RSI閾値(下抜けでDIPシグナル)'),
}

# ── バリデーション ──────────────────────────────────────────────────────────

def parse_value(name: str, raw: str) -> tuple[Any, str]:
    """
    Discord コマンドの生文字列を適切な型に変換・検証する。
    Returns (value, '') on success, (None, error_msg) on failure.
    """
    spec = PARAMS.get(name)
    if spec is None:
        known = ', '.join(sorted(PARAMS))
        return None, f'不明なパラメータ: `{name}`\n使用可能: {known}'

    # bool
    if spec.type_ is bool:
        if raw.lower() in ('on', 'true', '1', 'yes', 'enable', 'enabled'):
            return True, ''
        if raw.lower() in ('off', 'false', '0', 'no', 'disable', 'disabled'):
            return False, ''
        return None, f'`{name}` は on/off で指定してください'

    # int / float
    try:
        val = spec.type_(raw)
    except ValueError:
        return None, f'`{name}` には {"整数" if spec.type_ is int else "数値"} を指定してください'

    if spec.min_v is not None and val < spec.min_v:
        return None, f'`{name}` の最小値は {spec.min_v} です（入力: {val}）'
    if spec.max_v is not None and val > spec.max_v:
        return None, f'`{name}` の最大値は {spec.max_v} です（入力: {val}）'

    return val, ''


# ── ファイル読み書き ────────────────────────────────────────────────────────

def load_overrides(path: str = OVERRIDE_FILE) -> dict:
    """オーバーライドファイルを読み込む。存在しない/壊れていれば空 dict を返す。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_overrides(overrides: dict, path: str = OVERRIDE_FILE) -> None:
    """オーバーライドをファイルに保存する。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding='utf-8')


def set_override(name: str, value: Any, path: str = OVERRIDE_FILE) -> None:
    """単一パラメータをオーバーライドファイルに書き込む。"""
    spec = PARAMS[name]
    ov   = load_overrides(path)
    ov.setdefault(spec.section, {})[spec.key] = value
    save_overrides(ov, path)


def clear_overrides(path: str = OVERRIDE_FILE) -> None:
    """全オーバーライドを削除する。"""
    p = Path(path)
    if p.exists():
        p.unlink()


# ── cfg へのマージ ──────────────────────────────────────────────────────────

def apply_overrides(cfg: dict, path: str = OVERRIDE_FILE) -> dict:
    """
    オーバーライドファイルの値を cfg に上書きマージして返す。
    cfg 自体は変更しない（浅いコピーを返す）。
    """
    ov = load_overrides(path)
    if not ov:
        return cfg

    merged = {k: dict(v) if isinstance(v, dict) else v for k, v in cfg.items()}
    for section, params in ov.items():
        if section in merged and isinstance(merged[section], dict):
            merged[section] = {**merged[section], **params}
        else:
            merged[section] = params
    return merged


# ── 現在値サマリ ────────────────────────────────────────────────────────────

def current_values_text(cfg: dict, path: str = OVERRIDE_FILE) -> str:
    """全パラメータの現在値（+ オーバーライド有無）を文字列で返す。"""
    ov    = load_overrides(path)
    lines = ['```']
    for name, spec in PARAMS.items():
        base_val = cfg.get(spec.section, {}).get(spec.key, '(未設定)')
        ov_val   = ov.get(spec.section, {}).get(spec.key)
        cur_val  = ov_val if ov_val is not None else base_val
        tag      = ' ★override' if ov_val is not None else ''
        lines.append(f'{name:<12} = {cur_val}{tag}')
    lines.append('```')
    return '\n'.join(lines)
