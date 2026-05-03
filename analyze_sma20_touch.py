"""
analyze_sma20_touch.py — シンボルごとの SMA20 タッチマージンを手動分析・更新する
=================================================================================
ブリッジ起動時にも自動実行されるが、このスクリプトで即時更新も可能。

実行:
    python analyze_sma20_touch.py                  # config.py の symbol
    python analyze_sma20_touch.py BTCUSD XAUUSD    # 複数シンボル指定
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as C
from mt5_ea_bridge import connect_mt5, _analyze_sma20_touch_margin, _load_sma20_touch_margins

CFG = {k: getattr(C, k) for k in
       ['MT5', 'INDICATOR', 'SIGNAL', 'EXECUTION', 'SL', 'RULES',
        'OPTIMIZE', 'LOCAL', 'PLOT', 'BRIDGE', 'SCALP', 'REGIME', 'TIME_BIAS']}

symbols = sys.argv[1:] if len(sys.argv) > 1 else [CFG['MT5']['symbol']]

if not connect_mt5(symbols[0], CFG['MT5']):
    print("[エラー] MT5 接続失敗。ターミナルを起動して再実行してください。")
    sys.exit(1)

# キャッシュを無視して強制再分析するため、出力ファイルを一時退避
import json, os
cache_path = CFG['EXECUTION'].get('sma20_touch_margin_file', './output/sma20_touch_margins.json')
if Path(cache_path).exists():
    os.remove(cache_path)

_load_sma20_touch_margins(symbols, CFG)

print(f"\n設定ファイル: {cache_path}")
print("ブリッジ起動時に自動適用されます。")
