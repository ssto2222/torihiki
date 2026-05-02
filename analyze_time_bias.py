"""
analyze_time_bias.py — 時間帯別の危険時間帯を手動で分析・更新する
==================================================================
ブリッジ実行中は rebias_interval_hours ごとに自動再分析される。
このスクリプトは手動で即時更新したい場合に使用する。

実行:
    python analyze_time_bias.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from mt5_ea_bridge import _build_time_bias

CFG = {k: getattr(C, k) for k in
       ['MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES',
        'OPTIMIZE', 'LOCAL', 'PLOT', 'BRIDGE', 'SCALP', 'REGIME', 'TIME_BIAS']}

hours = _build_time_bias(CFG)
print(f"\n危険時間帯 ({len(hours)}個): {sorted(hours)}")
print(f"設定ファイル: {CFG['TIME_BIAS']['bias_file']}")
print("ブリッジ起動時 / 定期再分析時に自動適用されます。")
