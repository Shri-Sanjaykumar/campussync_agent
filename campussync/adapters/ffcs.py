"""Turns FFCS slot codes (A1, TA1, L31+L32 ...) into weekday + clock times."""
from __future__ import annotations

from datetime import time
from pathlib import Path

import yaml

from ..utils import WEEKDAYS, parse_hhmm


class SlotGrid:
    def __init__(self, path: Path):
        with open(path, encoding="utf-8") as f:
            g = yaml.safe_load(f)
        self.name = g.get("name", "slot grid")
        self.periods = [(parse_hhmm(a), parse_hhmm(b)) for a, b in g["theory_periods"]]
        self.map: dict[str, list[tuple[int, time, time]]] = {}
        for day, codes in g["theory_grid"].items():
            wd = WEEKDAYS.index(day)
            for i, code in enumerate(codes):
                s, e = self.periods[i]
                self.map.setdefault(code, []).append((wd, s, e))
        lr = g.get("lab_rule") or {}
        per_half = lr.get("periods_per_half", 5)
        n_days = len(g["theory_grid"])
        for d in range(n_days):
            for i in range(per_half):
                m_code = f"L{lr.get('morning_base', 1) + d * per_half + i}"
                a_code = f"L{lr.get('afternoon_base', 31) + d * per_half + i}"
                self.map[m_code] = [(d, *self.periods[i])]
                if per_half + i < len(self.periods):
                    self.map[a_code] = [(d, *self.periods[per_half + i])]

    def resolve(self, slot: str) -> list[tuple[int, time, time, int]]:
        """'A1+TA1' -> merged sessions [(weekday, start, end, units)].
        Sessions on the same day separated by <=10 min are merged (lab pairs)."""
        raw = []
        for code in slot.replace(" ", "").split("+"):
            if code not in self.map:
                raise KeyError(f"Unknown slot code {code!r} in {slot!r}")
            raw.extend(self.map[code])
        raw.sort()
        merged: list[list] = []
        for wd, s, e in raw:
            if merged and merged[-1][0] == wd:
                last = merged[-1]
                gap = (s.hour * 60 + s.minute) - (last[2].hour * 60 + last[2].minute)
                if 0 <= gap <= 10:
                    last[2] = e
                    last[3] += 1
                    continue
            merged.append([wd, s, e, 1])
        return [tuple(x) for x in merged]
