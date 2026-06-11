"""bridge/param_override.py — ランタイムパラメータ オーバーライド管理 v2

全 config.py セクションに対応。ドット記法でキー指定:
  SCALP.target_profit_jpy         フラット値
  SL.tp_atr_multi.BTCUSD          dict サブキー
  SCALP.rsi_buy_thrs              リスト (JSON配列)

プロセス再起動で base config.py の値に戻る（override ファイルを削除しない限り）。
"""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from typing import Any, NamedTuple

_logger = logging.getLogger('torihiki')

OVERRIDE_FILE = './output/runtime_params.json'
_CONFIG_PATH  = Path(__file__).parent.parent / 'config.py'

# CFG に含まれるセクション名（runner.py と同期）
CFG_SECTIONS = [
    'MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES',
    'LOCAL', 'PLOT', 'BRIDGE', 'SCALP', 'REGIME', 'TIME_BIAS',
    'WHIPSAW', 'ELLIOTT',
]


# ── config.py ドキュメント解析 ─────────────────────────────────────────────

def _parse_config_docs() -> dict[str, dict[str, str]]:
    """config.py のインラインコメントをパラメータ説明として抽出する。
    {SECTION: {key: 'description'}}
    """
    if not _CONFIG_PATH.exists():
        return {}
    src     = _CONFIG_PATH.read_text(encoding='utf-8')
    result: dict[str, dict[str, str]] = {}
    current: str | None = None
    depth   = 0
    sect_re = re.compile(r'^(\w+)\s*=\s*dict\s*\(')
    key_re  = re.compile(r'^\s+(\w+)\s*=.+?#\s*(.+)$')
    bare_re = re.compile(r'^\s+(\w+)\s*=')
    for line in src.splitlines():
        m = sect_re.match(line)
        if m and depth == 0:
            current = m.group(1)
            result[current] = {}
            depth = line.count('(') - line.count(')')
            continue
        if current:
            depth += line.count('(') - line.count(')')
            if depth <= 0:
                depth = 0
                current = None
                continue
            m = key_re.match(line)
            if m:
                result[current][m.group(1)] = m.group(2).strip()
                continue
            m = bare_re.match(line)
            if m:
                result[current].setdefault(m.group(1), '')
    return result


_DOCS: dict[str, dict[str, str]] | None = None


def _get_docs() -> dict[str, dict[str, str]]:
    global _DOCS
    if _DOCS is None:
        _DOCS = _parse_config_docs()
    return _DOCS


def get_param_desc(section: str, key: str) -> str:
    docs = _get_docs()
    desc = docs.get(section, {}).get(key, '')
    # RULES は RULES_GENERAL / _ENTRY / _RISK / _EXIT からも探す
    if not desc and section == 'RULES':
        for sub in ('RULES_GENERAL', 'RULES_ENTRY', 'RULES_RISK', 'RULES_EXIT'):
            desc = docs.get(sub, {}).get(key, '')
            if desc:
                break
    return desc


# ── パス解決 ─────────────────────────────────────────────────────────────

def resolve_path(cfg: dict, dot_path: str) -> tuple[str, str, str | None, Any] | None:
    """
    'SL.sl_multi'            → (section, key, None, cur_val)
    'SL.tp_atr_multi.BTCUSD' → (section, key, subkey, cur_subval)
    不正パスは None を返す。
    """
    parts = dot_path.split('.')
    if len(parts) == 2:
        section, key = parts[0].upper(), parts[1]
        sec = cfg.get(section)
        if not isinstance(sec, dict) or key not in sec:
            return None
        return section, key, None, sec[key]
    if len(parts) == 3:
        section, key, subkey = parts[0].upper(), parts[1], parts[2]
        sec = cfg.get(section)
        if not isinstance(sec, dict):
            return None
        val = sec.get(key)
        if not isinstance(val, dict) or subkey not in val:
            return None
        return section, key, subkey, val[subkey]
    return None


# ── 型変換 ────────────────────────────────────────────────────────────────

def _infer_parse(current_val: Any, raw: str) -> tuple[Any, str]:
    """current_val の型に合わせて raw を変換する。(value, '') / (None, error)"""
    if isinstance(current_val, bool):
        if raw.lower() in ('on', 'true', '1', 'yes', 'enable', 'enabled'):
            return True, ''
        if raw.lower() in ('off', 'false', '0', 'no', 'disable', 'disabled'):
            return False, ''
        return None, f'`on` / `off` で指定してください（入力: {raw}）'
    if isinstance(current_val, int):
        try:
            return int(raw), ''
        except ValueError:
            return None, f'整数を指定してください（入力: {raw}）'
    if isinstance(current_val, float):
        try:
            return float(raw), ''
        except ValueError:
            return None, f'数値を指定してください（入力: {raw}）'
    if isinstance(current_val, str):
        return raw, ''
    if isinstance(current_val, (list, dict)):
        try:
            parsed = json.loads(raw)
            expect = '配列' if isinstance(current_val, list) else 'オブジェクト'
            if not isinstance(parsed, type(current_val)):
                return None, f'{expect} を JSON で指定してください（例: {json.dumps(current_val, ensure_ascii=False)}）'
            return parsed, ''
        except json.JSONDecodeError:
            return None, f'JSON フォーマットで入力してください（例: {json.dumps(current_val, ensure_ascii=False)}）'
    return raw, ''


# ── ファイル読み書き ────────────────────────────────────────────────────────

def load_overrides(path: str = OVERRIDE_FILE) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_overrides(overrides: dict, path: str = OVERRIDE_FILE) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding='utf-8')


# ── メイン操作 ────────────────────────────────────────────────────────────

def set_override_path(dot_path: str, raw_value: str, cfg: dict,
                      path: str = OVERRIDE_FILE) -> tuple[Any, str]:
    """
    ドット記法で設定値を上書きする。
    Returns (new_value, '') / (None, error_message)
    """
    resolved = resolve_path(cfg, dot_path)
    if resolved is None:
        # 候補を探して示す
        parts = dot_path.split('.')
        if len(parts) >= 1:
            section = parts[0].upper()
            if section in cfg:
                keys = list(cfg[section].keys())[:8]
                hint = ', '.join(f'`{section}.{k}`' for k in keys)
                return None, f'`{dot_path}` が見つかりません。{section} の例: {hint}'
        return None, f'`{dot_path}` が見つかりません。形式: `SECTION.KEY` または `SECTION.KEY.SUBKEY`'

    section, key, subkey, cur = resolved

    if subkey is not None:
        val, err = _infer_parse(cur, raw_value)
        if err:
            return None, err
        ov = load_overrides(path)
        ov.setdefault(section, {})
        # 現在の dict 全体に subkey 変更を重ねる
        base_dict = dict(cfg[section][key])
        if section in ov and key in ov[section] and isinstance(ov[section][key], dict):
            base_dict.update(ov[section][key])
        base_dict[subkey] = val
        ov[section][key] = base_dict
        save_overrides(ov, path)
        return val, ''
    else:
        val, err = _infer_parse(cur, raw_value)
        if err:
            return None, err
        ov = load_overrides(path)
        ov.setdefault(section, {})[key] = val
        save_overrides(ov, path)
        return val, ''


def reset_override_path(dot_path: str | None, path: str = OVERRIDE_FILE) -> str:
    """dot_path=None で全削除、指定があれば該当のみ削除。"""
    ov = load_overrides(path)
    if not dot_path:
        save_overrides({}, path)
        return '全パラメータのオーバーライドを削除しました'
    parts = dot_path.split('.')
    if len(parts) >= 2:
        section = parts[0].upper()
        key     = parts[1]
        if section in ov and key in ov.get(section, {}):
            del ov[section][key]
            if not ov[section]:
                del ov[section]
            save_overrides(ov, path)
            return f'`{dot_path}` のオーバーライドを削除しました'
    return f'`{dot_path}` にオーバーライドはありません'


def apply_overrides(cfg: dict, path: str = OVERRIDE_FILE) -> dict:
    """オーバーライドファイルを cfg にマージして返す（cfg 自体は変更しない）。"""
    ov = load_overrides(path)
    if not ov:
        return cfg
    merged = {k: dict(v) if isinstance(v, dict) else v for k, v in cfg.items()}
    for section, params in ov.items():
        if section not in merged or not isinstance(merged[section], dict):
            merged[section] = params
            continue
        for k, v in params.items():
            if isinstance(v, dict) and isinstance(merged[section].get(k), dict):
                merged[section][k] = {**merged[section][k], **v}
            else:
                merged[section][k] = v
    return merged


# ── 表示ヘルパー ────────────────────────────────────────────────────────────

def get_value_text(cfg: dict, dot_path: str, path: str = OVERRIDE_FILE) -> str:
    resolved = resolve_path(cfg, dot_path)
    if resolved is None:
        return f'パスが見つかりません: `{dot_path}`'
    section, key, subkey, cur = resolved
    ov   = load_overrides(path)
    desc = get_param_desc(section, key)
    tag  = ' ★override' if (section in ov and key in ov.get(section, {})) else ''
    if subkey:
        return f'`{dot_path}` = **{cur}**{tag}  {desc}'
    val_str = json.dumps(cur, ensure_ascii=False) if isinstance(cur, (dict, list)) else str(cur)
    return f'`{dot_path}` = **{val_str}**{tag}  {desc}'


def section_lines(cfg: dict, section: str, path: str = OVERRIDE_FILE) -> list[str]:
    """セクションの全パラメータ行リストを返す（Discord 用）。"""
    section = section.upper()
    sec_cfg = cfg.get(section)
    if not isinstance(sec_cfg, dict):
        return [f'セクション `{section}` が見つかりません。利用可能: {", ".join(CFG_SECTIONS)}']
    ov   = load_overrides(path)
    ov_s = ov.get(section, {})
    lines: list[str] = []
    for key, base_val in sec_cfg.items():
        ov_val  = ov_s.get(key)
        cur_val = ov_val if ov_val is not None else base_val
        tag     = ' ★' if ov_val is not None else ''
        desc    = get_param_desc(section, key)
        if isinstance(cur_val, dict):
            val_str = json.dumps(cur_val, ensure_ascii=False)
        elif isinstance(cur_val, list):
            val_str = json.dumps(cur_val, ensure_ascii=False)
        else:
            val_str = str(cur_val)
        lines.append(f'`{section}.{key}` = {val_str}{tag}  {desc}')
    return lines


def all_overrides_text(path: str = OVERRIDE_FILE) -> str:
    ov = load_overrides(path)
    if not ov:
        return 'オーバーライドはありません'
    lines = ['**現在のオーバーライド一覧**', '```json']
    lines.append(json.dumps(ov, ensure_ascii=False, indent=2))
    lines.append('```')
    return '\n'.join(lines)


# ── 後方互換 (旧 PARAMS / parse_value / set_override / current_values_text) ──

class ParamSpec(NamedTuple):
    section: str
    key:     str
    type_:   type
    min_v:   Any
    max_v:   Any
    desc:    str


PARAMS: dict[str, ParamSpec] = {
    'target':        ParamSpec('SCALP',  'target_profit_jpy',       int,   100,    50_000, '目標利益(円)'),
    'sl_ratio':      ParamSpec('SCALP',  'sl_ratio',                float, 1.0,    10.0,   'SL比率'),
    'tp_frac':       ParamSpec('SCALP',  'tp_atr_fraction',         float, 0.1,    2.0,    'TP幅 = ATR × この値'),
    'buy':           ParamSpec('SCALP',  'buy_enabled',             bool,  None,   None,   'BUY有効/無効'),
    'sell':          ParamSpec('SCALP',  'sell_enabled',            bool,  None,   None,   'SELL有効/無効'),
    'cooldown':      ParamSpec('SCALP',  'cooldown_min',            int,   1,      240,    'クールダウン(分)'),
    'jpy_rate':      ParamSpec('SCALP',  'jpy_per_usd',             float, 100.0,  200.0,  'JPY/USDレート'),
    'lot':           ParamSpec('BRIDGE', 'lot_size',                float, 0.01,   10.0,   'フォールバックロット'),
    'risk':          ParamSpec('BRIDGE', 'risk_pct',                float, 0.001,  0.10,   'リスク割合'),
    'sl_multi':      ParamSpec('SL',     'sl_multi',                float, 0.5,    5.0,    'SL幅 = ATR × 倍率'),
    'tp_multi':      ParamSpec('SL',     'tp_atr_multi',            float, 0.5,    10.0,   'TP幅 = ATR × 倍率'),
    'rsi_buy':       ParamSpec('SIGNAL', 'buy_rsi_thr',             float, 20.0,   60.0,   'BUY RSI閾値'),
    # ── SCALP 追加エイリアス ──────────────────────────────────────────────────
    'max_pos':       ParamSpec('SCALP',  'max_positions',           int,   0,      20,     '同時保有ポジション上限(0=自動)'),
    'min_bal':       ParamSpec('SCALP',  'min_balance_jpy',         int,   0,      100_000,'残高下限（円）'),
    'vol_bo':        ParamSpec('SCALP',  'vol_bo_enabled',          bool,  None,   None,   'ボリュームBO有効/無効'),
    'retest_bars':   ParamSpec('SCALP',  'nl_retest_min_bars',      int,   1,      10,     'NLリテスト確認H1バー数'),
    'retest_margin': ParamSpec('SCALP',  'nl_retest_margin_atr',    float, 0.1,    3.0,    'NLリテストタッチ幅(ATR倍)'),
    'retest_sl':     ParamSpec('SCALP',  'nl_retest_sl_atr',        float, 0.1,    5.0,    'NLリテストSLバッファ(ATR倍)'),
    'retest_expire': ParamSpec('SCALP',  'nl_retest_expire_h',      int,   1,      168,    'NLリテスト有効期限(時間)'),
    'm1_ob':         ParamSpec('SCALP',  'm1_rsi_ob_gate',          float, 60.0,   95.0,   'M1 RSI過熱ブロックゲート'),
    'm1_os':         ParamSpec('SCALP',  'm1_rsi_os_gate',          float, 5.0,    40.0,   'M1 RSI売られ過ぎブロックゲート'),
    'pend_timeout':  ParamSpec('SCALP',  'sma_pending_timeout_min', int,   5,      120,    'SMAペンディングタイムアウト(分)'),
    # ── TTM スクイーズ エイリアス ────────────────────────────────────────────
    'ttm':           ParamSpec('SCALP',  'ttm_squeeze_enabled',     bool,  None,   None,   'TTMスクイーズ有効/無効'),
    'ttm_bars':      ParamSpec('SCALP',  'ttm_squeeze_min_bars',    int,   1,      20,     'TTM発火に必要な最小スクイーズ継続バー数'),
    'ttm_tp':        ParamSpec('SCALP',  'ttm_tp_multi',            float, 0.5,    5.0,    'TTM発火 TP倍率'),
    'ttm_sl':        ParamSpec('SCALP',  'ttm_sl_multi',            float, 0.1,    3.0,    'TTM発火 SL倍率'),
}


def parse_value(name: str, raw: str) -> tuple[Any, str]:
    spec = PARAMS.get(name)
    if spec is None:
        return None, f'不明な短縮名: `{name}`'
    if spec.type_ is bool:
        if raw.lower() in ('on', 'true', '1', 'yes'):
            return True, ''
        if raw.lower() in ('off', 'false', '0', 'no'):
            return False, ''
        return None, f'`{name}` は on/off で指定してください'
    try:
        val = spec.type_(raw)
    except ValueError:
        return None, f'{"整数" if spec.type_ is int else "数値"} を指定してください'
    if spec.min_v is not None and val < spec.min_v:
        return None, f'最小値は {spec.min_v}（入力: {val}）'
    if spec.max_v is not None and val > spec.max_v:
        return None, f'最大値は {spec.max_v}（入力: {val}）'
    return val, ''


def set_override(name: str, value: Any, path: str = OVERRIDE_FILE) -> None:
    spec = PARAMS[name]
    ov   = load_overrides(path)
    ov.setdefault(spec.section, {})[spec.key] = value
    save_overrides(ov, path)


def clear_overrides(path: str = OVERRIDE_FILE) -> None:
    p = Path(path)
    if p.exists():
        p.unlink()


def current_values_text(cfg: dict, path: str = OVERRIDE_FILE) -> str:
    ov    = load_overrides(path)
    lines = ['```']
    for name, spec in PARAMS.items():
        base_val = cfg.get(spec.section, {}).get(spec.key, '(未設定)')
        ov_val   = ov.get(spec.section, {}).get(spec.key)
        cur_val  = ov_val if ov_val is not None else base_val
        tag      = ' ★' if ov_val is not None else ''
        lines.append(f'{name:<12} = {cur_val}{tag}')
    lines.append('```')
    return '\n'.join(lines)
