"""bridge/io.py — signal.json ファイル I/O"""
from __future__ import annotations
import json
import time
from pathlib import Path


def write_signal(data: dict, path: str) -> None:
    """signal.json をアトミックに書き込む (Windows ファイルロック対応)"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='ascii') as f:
        json.dump(data, f, ensure_ascii=True, indent=2)

    retries = 5
    for attempt in range(retries):
        try:
            Path(tmp).replace(Path(path))
            return
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(0.1)
            else:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except OSError:
                    pass
                raise


def read_ea_state(path: str) -> dict:
    try:
        with open(path, encoding='ascii') as f:
            return json.load(f)
    except Exception:
        return {}
