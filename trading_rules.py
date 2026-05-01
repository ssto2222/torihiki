"""
trading_rules.py
================
バックテスト由来のトレードフィルタールールエンジン。
外部ライブラリ不要（標準ライブラリのみ）。

使い方:
    from trading_rules import RulesEngine, SignalResult

    engine = RulesEngine()                        # デフォルトルール読み込み
    # engine = RulesEngine("custom_rules.json")   # カスタムJSONも可

    result: SignalResult = engine.evaluate(
        symbol    = "BTCUSD",
        rsi_h1    = 72.5,
        rsi_d1    = 75.0,
        direction = "buy",
        hour_utc  = 19,
        dow       = 0,          # 0=Mon ... 6=Sun
    )

    if result.signal in ("BUY", "SELL"):
        tp_minutes = result.tp_hold_minutes   # TP目安時間
        print(f"ENTRY {result.signal}  score={result.score}  TP in ~{tp_minutes}min")
    else:
        print(f"WAIT  reasons={result.reasons}")

互換性: Python 3.8+
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─── デフォルトJSONパス ───────────────────────────────────────
_HERE = Path(__file__).parent
_DEFAULT_JSON = _HERE / "trading_rules.json"


# ─── 結果型 ───────────────────────────────────────────────────
@dataclass
class ZoneInfo:
    zone:       str
    verdict:    str           # "forbidden" | "caution" | "ok" | "good" | "best"
    wr:         Optional[int] = None
    avg_profit: Optional[int] = None
    buy_wr:     Optional[int] = None
    sell_wr:    Optional[int] = None
    cnt:        Optional[int] = None
    tp_effect:  Optional[int] = None


@dataclass
class CrossInfo:
    verdict:    str
    avg_profit: int
    wr:         int
    d1_bucket:  str
    h1_bucket:  str
    key:        str


@dataclass
class SignalResult:
    # ─ 最終シグナル ─
    signal:          str            # "BUY" | "SELL" | "WAIT"
    strength:        Optional[str]  # "strong" | "normal" | "weak" | None
    verdict:         str            # "ok" | "caution" | "forbidden"
    score:           int            # 0〜100

    # ─ 判定根拠 ─
    reasons:         list[str]
    penalties:       int
    bonuses:         int

    # ─ ゾーン詳細 ─
    h1_zone:         ZoneInfo
    d1_zone:         ZoneInfo
    cross:           CrossInfo

    # ─ TP推奨 ─
    tp_hold_minutes: Optional[int] = None   # TP目安保有時間（分）
    tp_priority:     Optional[str] = None   # "critical"|"high"|"medium"|"low"|"none"

    # ─ 禁止フラグ（ビット単位でフィルタ確認用）─
    blocked_by_h1:    bool = False
    blocked_by_d1:    bool = False
    blocked_by_cross: bool = False
    blocked_by_hour:  bool = False
    blocked_by_dow:   bool = False
    blocked_by_dir:   bool = False


# ─── メインエンジン ──────────────────────────────────────────
class RulesEngine:
    """
    trading_rules.json を読み込み、evaluate() でシグナルを返す。

    Parameters
    ----------
    rules_path : str | Path | None
        JSON ファイルパス。None の場合は同ディレクトリの trading_rules.json を使用。
    """

    def __init__(self, rules_path: "str | Path | None" = None):
        path = Path(rules_path) if rules_path else _DEFAULT_JSON
        with open(path, encoding="utf-8") as f:
            self._rules: dict = json.load(f)

        self._global   = self._rules["global_rules"]
        self._session  = self._rules["session_filter"]
        self._dow      = self._rules["dow_filter"]
        self._h1_zones = self._rules["rsi_h1_zones"]
        self._d1_zones = self._rules["rsi_d1_zones"]
        self._cross    = self._rules["cross_h1_d1"]
        self._tp       = self._rules["tp_timing"]
        self._w        = self._rules["scoring_weights"]

    # ── ゾーンラベル変換 ────────────────────────────────────

    _BINS   = [0, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 100]
    _LABELS = [
        "<20","20-25","25-30","30-35","35-40",
        "40-45","45-50","50-55","55-60",
        "60-65","65-70","70-75","75-80","80-85",">85",
    ]

    def _zone_label(self, rsi: float) -> str:
        for lo, hi, lbl in zip(self._BINS[:-1], self._BINS[1:], self._LABELS):
            if lo <= rsi < hi:
                return lbl
        return ">85"

    def _cross_bucket(self, rsi: float) -> str:
        if rsi < 40:  return "<40"
        if rsi < 50:  return "40-50"
        if rsi < 60:  return "50-60"
        if rsi < 70:  return "60-70"
        return ">70"

    # ── ゾーン取得 ──────────────────────────────────────────

    def _get_h1_zone(self, symbol: str, rsi: float) -> ZoneInfo:
        sym   = symbol if symbol in self._h1_zones else "BTCUSD"
        label = self._zone_label(rsi)
        raw   = self._h1_zones[sym].get(label, {"verdict": "ok"})
        return ZoneInfo(zone=label, **{k: v for k, v in raw.items()
                                       if k in ZoneInfo.__dataclass_fields__})

    def _get_d1_zone(self, symbol: str, rsi: float) -> ZoneInfo:
        sym   = symbol if symbol in self._d1_zones else "BTCUSD"
        label = self._zone_label(rsi)
        raw   = self._d1_zones[sym].get(label, {"verdict": "ok"})
        return ZoneInfo(zone=label, **{k: v for k, v in raw.items()
                                       if k in ZoneInfo.__dataclass_fields__})

    def _get_cross(self, symbol: str, rsi_h1: float, rsi_d1: float) -> CrossInfo:
        sym  = symbol if symbol in self._cross else "BTCUSD"
        d1b  = self._cross_bucket(rsi_d1)
        h1b  = self._cross_bucket(rsi_h1)
        key  = f"{d1b}_x_{h1b}"
        raw  = self._cross[sym].get(key, {"verdict": "ok", "avg_profit": 0, "wr": 50})
        return CrossInfo(
            verdict    = raw["verdict"],
            avg_profit = raw["avg_profit"],
            wr         = raw["wr"],
            d1_bucket  = d1b,
            h1_bucket  = h1b,
            key        = key,
        )

    def _get_tp(self, symbol: str, rsi_h1: float) -> tuple[Optional[int], Optional[str]]:
        sym   = symbol if symbol in self._tp else "BTCUSD"
        label = self._zone_label(rsi_h1)
        # 完全一致がなければ範囲で近似探索
        tp_data = self._tp[sym]
        if label in tp_data:
            d = tp_data[label]
            return d.get("hold_minutes_median"), d.get("priority")
        # 近傍探索（5ポイント以内）
        rsi_mid = (self._BINS[self._LABELS.index(label)] +
                   self._BINS[self._LABELS.index(label) + 1]) / 2
        best_key, best_dist = None, 999
        for k in tp_data:
            try:
                idx = self._LABELS.index(k)
                mid = (self._BINS[idx] + self._BINS[idx + 1]) / 2
                d   = abs(rsi_mid - mid)
                if d < best_dist:
                    best_dist, best_key = d, k
            except ValueError:
                continue
        if best_key and best_dist <= 10:
            d = tp_data[best_key]
            return d.get("hold_minutes_median"), d.get("priority")
        return None, None

    # ── メイン評価 ──────────────────────────────────────────

    def evaluate(
        self,
        symbol:    str,
        rsi_h1:    float,
        rsi_d1:    float,
        direction: str,
        hour_utc:  int,
        dow:       int,
    ) -> SignalResult:
        """
        Parameters
        ----------
        symbol    : "BTCUSD" or "XAUUSD"
        rsi_h1    : RSI(14) value on 1H chart
        rsi_d1    : RSI(14) value on D1 chart
        direction : "buy" or "sell"
        hour_utc  : current hour in UTC (0-23)
        dow       : day of week  0=Mon, 6=Sun
        """
        w = self._w
        h1z   = self._get_h1_zone(symbol, rsi_h1)
        d1z   = self._get_d1_zone(symbol, rsi_d1)
        cross = self._get_cross(symbol, rsi_h1, rsi_d1)

        reasons:  list[str] = []
        penalties = 0
        bonuses   = 0
        blocked   = dict(h1=False, d1=False, cross=False, hour=False, dow=False, dir=False)

        # ── H1 RSI ──
        if h1z.verdict == "forbidden":
            ap = h1z.avg_profit or 0
            reasons.append(f"[H1 BLOCKED] RSI {h1z.zone} forbidden zone (avg ${ap:,})")
            penalties += w["h1_zone_forbidden_penalty"]
            blocked["h1"] = True
        elif h1z.verdict == "caution":
            reasons.append(f"[H1 CAUTION] RSI {h1z.zone} caution zone (WR {h1z.wr}%)")
            penalties += w["h1_zone_caution_penalty"]
        else:
            bonuses += 1

        # ── D1 RSI ──
        if d1z.verdict == "forbidden":
            ap = d1z.avg_profit or 0
            reasons.append(f"[D1 BLOCKED] RSI {d1z.zone} forbidden zone (avg ${ap:,})")
            penalties += w["d1_zone_forbidden_penalty"]
            blocked["d1"] = True
        elif d1z.verdict == "caution":
            reasons.append(f"[D1 CAUTION] RSI {d1z.zone} caution zone")
            penalties += w["d1_zone_caution_penalty"]
        else:
            bonuses += 1

        # ── H1×D1 クロス ──
        cv = cross.verdict
        if cv == "best":
            reasons.append(f"[CROSS BEST] D1:{cross.d1_bucket} x H1:{cross.h1_bucket} avg ${cross.avg_profit:,}")
            bonuses += w["cross_best_bonus"]
        elif cv == "good":
            reasons.append(f"[CROSS GOOD] D1:{cross.d1_bucket} x H1:{cross.h1_bucket} avg ${cross.avg_profit:,}")
            bonuses += w["cross_good_bonus"]
        elif cv == "forbidden":
            reasons.append(f"[CROSS BLOCKED] D1:{cross.d1_bucket} x H1:{cross.h1_bucket} avg ${cross.avg_profit:,}")
            penalties += w["cross_forbidden_penalty"]
            blocked["cross"] = True
        elif cv == "caution":
            reasons.append(f"[CROSS CAUTION] D1:{cross.d1_bucket} x H1:{cross.h1_bucket}")
            penalties += w["cross_caution_penalty"]

        # ── 方向フィルター ──
        if direction == "buy":
            bwr = h1z.buy_wr
            if bwr is not None and bwr < 45:
                reasons.append(f"[DIR CAUTION] Buy WR {bwr}% in H1 {h1z.zone}")
                penalties += w["direction_unfavorable_penalty"]
                blocked["dir"] = True
        else:
            swr = h1z.sell_wr
            if swr is not None and swr < 45:
                reasons.append(f"[DIR CAUTION] Sell WR {swr}% in H1 {h1z.zone}")
                penalties += w["direction_unfavorable_penalty"]
                blocked["dir"] = True
            # Sell は構造的に禁止
            reasons.append("[DIR BLOCKED] Sell is structurally losing (total PnL: -$7.87M)")
            penalties += w["h1_zone_forbidden_penalty"]
            blocked["dir"] = True

        # ── 時間帯 ──
        jst = (hour_utc + 9) % 24
        if hour_utc in self._session["forbidden_hours_utc"]:
            reasons.append(f"[HOUR BLOCKED] UTC{hour_utc} (JST{jst}) worst session")
            penalties += w["hour_forbidden_penalty"]
            blocked["hour"] = True
        elif hour_utc in self._session["caution_hours_utc"]:
            reasons.append(f"[HOUR CAUTION] UTC{hour_utc} (JST{jst}) caution session")
            penalties += w["hour_caution_penalty"]

        # ── 曜日 ──
        dow_names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        if dow in self._dow["forbidden_dow"]:
            reasons.append(f"[DOW BLOCKED] {dow_names[dow]} is forbidden")
            penalties += w["dow_forbidden_penalty"]
            blocked["dow"] = True
        elif dow in self._dow["caution_dow"]:
            reasons.append(f"[DOW CAUTION] {dow_names[dow]} is caution day")
            penalties += w["dow_caution_penalty"]

        # ── スコア計算 ──
        score = min(100, max(0,
            w["score_base"] + bonuses * w["score_bonus_per_point"]
            - penalties * w["score_penalty_per_point"]
        ))

        # ── 総合判定 ──
        thr_forb = w["signal_threshold_forbidden"]
        thr_caut = w["signal_threshold_caution"]

        if penalties >= thr_forb:
            verdict  = "forbidden"
            signal   = "WAIT"
            strength = None
            score    = max(0, score - 20)
        elif penalties >= thr_caut:
            verdict  = "caution"
            signal   = "WAIT"
            strength = None
            score    = min(score, 50)
        else:
            verdict = "ok"
            if cv == "best" or (h1z.verdict == "good" and d1z.verdict == "good"):
                strength = "strong"
                score    = min(100, score + 20)
            elif h1z.verdict in ("ok", "good"):
                strength = "normal"
            else:
                strength = "weak"
            signal = direction.upper()

        if not reasons:
            reasons.append(f"[OK] H1 {h1z.zone} / D1 {d1z.zone} — favorable zones")

        # ── TP推奨 ──
        tp_min, tp_pri = self._get_tp(symbol, rsi_h1)

        return SignalResult(
            signal           = signal,
            strength         = strength,
            verdict          = verdict,
            score            = score,
            reasons          = reasons,
            penalties        = penalties,
            bonuses          = bonuses,
            h1_zone          = h1z,
            d1_zone          = d1z,
            cross            = cross,
            tp_hold_minutes  = tp_min,
            tp_priority      = tp_pri,
            blocked_by_h1    = blocked["h1"],
            blocked_by_d1    = blocked["d1"],
            blocked_by_cross = blocked["cross"],
            blocked_by_hour  = blocked["hour"],
            blocked_by_dow   = blocked["dow"],
            blocked_by_dir   = blocked["dir"],
        )

    # ── 便利メソッド ────────────────────────────────────────

    def is_allowed_hour(self, hour_utc: int) -> bool:
        return hour_utc not in self._session["forbidden_hours_utc"]

    def is_allowed_dow(self, dow: int) -> bool:
        return dow not in self._dow["forbidden_dow"]

    def get_h1_verdict(self, symbol: str, rsi: float) -> str:
        return self._get_h1_zone(symbol, rsi).verdict

    def get_d1_verdict(self, symbol: str, rsi: float) -> str:
        return self._get_d1_zone(symbol, rsi).verdict

    def get_cross_verdict(self, symbol: str, rsi_h1: float, rsi_d1: float) -> str:
        return self._get_cross(symbol, rsi_h1, rsi_d1).verdict

    def get_tp_minutes(self, symbol: str, rsi_h1: float) -> Optional[int]:
        minutes, _ = self._get_tp(symbol, rsi_h1)
        return minutes

    def summary(self, result: SignalResult) -> str:
        """1行サマリー文字列を返す"""
        lines = [
            f"signal={result.signal:4s}  strength={str(result.strength):6s}  "
            f"score={result.score:3d}  penalties={result.penalties}  bonuses={result.bonuses}",
            f"  H1:{result.h1_zone.zone}({result.h1_zone.verdict})  "
            f"D1:{result.d1_zone.zone}({result.d1_zone.verdict})  "
            f"cross:{result.cross.verdict}({result.cross.key})",
        ]
        if result.tp_hold_minutes:
            lines.append(f"  TP target: ~{result.tp_hold_minutes}min  priority={result.tp_priority}")
        lines.append("  " + " | ".join(result.reasons))
        return "\n".join(lines)
