"""bridge/io.py — signal.json ファイル I/O"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path


def write_signal(data: dict, path: str) -> None:
    """signal.json をアトミックに書き込む (Windows ファイルロック対応)

    Google Drive など .tmp 作成が PermissionError になるパスでは
    直接書き込みにフォールバックする。
    """
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='ascii') as f:
            json.dump(data, f, ensure_ascii=True, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # ネットワークドライブなど fsync 非対応の場合はスキップ

        for attempt in range(5):
            try:
                Path(tmp).replace(Path(path))
                return
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.1)
                else:
                    try:
                        Path(tmp).unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise

    except PermissionError:
        # .tmp 作成 / rename が失敗した場合（Google Drive など）は直接書き込みで代替
        with open(path, 'w', encoding='ascii') as f:
            json.dump(data, f, ensure_ascii=True, indent=2)


def read_ea_state(path: str) -> dict:
    try:
        with open(path, encoding='ascii') as f:
            return json.load(f)
    except Exception:
        return {}
