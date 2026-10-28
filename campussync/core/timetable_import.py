"""Paste-your-timetable import. A student copies their course list (course code + slot, e.g. from
the VTOP timetable page) and the agent builds their weekly timetable from it.

The exact VTOP copy-paste layout isn't known here, so the reader is forgiving: in each line it looks
for a course code (SWE3001, ISWE411L...) and an FFCS slot (A1+TA1, L31+L32...). Lines with a weekday
and a clock range ("Mon 09:00-10:00 CS301 Operating Systems") also work, for colleges without slots."""
from __future__ import annotations

import re

from ..models import ClassSession
from ..utils import WEEKDAYS, parse_hhmm

SLOT_TOKEN = r"(?:T[A-G]{1,2}\d|[A-G]\d|L\d{1,2})"
SLOT_RE = re.compile(rf"(?<![A-Za-z0-9]){SLOT_TOKEN}(?:\s*\+\s*{SLOT_TOKEN})*(?![A-Za-z0-9])")
CODE_RE = re.compile(r"\b([A-Z]{2,5}\d{3,4}[A-Z]?)\b")
VENUE_RE = re.compile(r"\b((?:SJT|TT|SMV|MB|PRP|CDMM|GDN|MGB|CBMR|SJTG|AB\d?|LAB)\s*-?\s*G?\d{2,4}[A-Z]?)\b", re.I)
DAYTIME_RE = re.compile(r"\b(mon|tue|wed|thu|fri|sat)[a-z]*\.?\s+([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:-|–|to)\s*([01]?\d|2[0-3])[:.]([0-5]\d)", re.I)
NOISE_RE = re.compile(r"\b(embedded theory|embedded lab|theory only|lab only|eth|ela|elb|th|lo|ss|soft skill|"
                      r"regular|registered|course|slot|venue|faculty|credits?)\b", re.I)


def parse_timetable_text(text: str, grid=None) -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    for n, raw in enumerate(text.splitlines(), start=1):
        line = re.sub(r"^\s*\d{1,2}\s*[|.):\t]?\s+", "", raw.strip())  # drop serial numbers
        if not line:
            continue
        code_m = None
        for m in CODE_RE.finditer(line):
            if not VENUE_RE.fullmatch(m.group(1)):
                code_m = m
                break
        dt = DAYTIME_RE.search(line)
        venue_m = VENUE_RE.search(line)
        venue = re.sub(r"\s+", " ", venue_m.group(1)).upper() if venue_m else ""
        work = line
        if venue_m:
            work = work.replace(venue_m.group(0), " ")
        slot = None
        if not dt:
            for sm in SLOT_RE.finditer(work):
                cand = re.sub(r"\s+", "", sm.group(0))
                if code_m and sm.start() >= code_m.start() and sm.end() <= code_m.end():
                    continue
                slot = cand
                break
        if not code_m and not slot and not dt:
            continue  # header or junk line
        if not code_m:
            problems.append(f"Line {n}: no course code found - skipped ({line[:50]})")
            continue
        code = code_m.group(1)
        title = work
        for piece in (code_m.group(0), slot or "", dt.group(0) if dt else ""):
            if piece:
                title = title.replace(piece, " ")
        title = re.sub(rf"(?<![A-Za-z0-9]){SLOT_TOKEN}(\+{SLOT_TOKEN})*(?![A-Za-z0-9])", " ", title)
        title = NOISE_RE.sub(" ", title)
        title = re.sub(r"\b(dr|prof)\.?\s+[A-Za-z.\s]+$", " ", title, flags=re.I)
        title = re.sub(r"[|,;:\t\-]+", " ", title)
        title = re.sub(r"\s+", " ", title).strip() or code
        if dt and not venue:
            last = title.split()[-1] if title.split() else ""
            if re.fullmatch(r"[A-Z]{1,4}-?\d{2,4}[A-Z]?", last):
                venue, title = last, " ".join(title.split()[:-1]) or code
        if dt:
            rows.append({"course_code": code, "course_title": title, "weekday": WEEKDAYS.index(dt.group(1)[:3].title()),
                         "start": f"{int(dt.group(2)):02d}:{dt.group(3)}", "end": f"{int(dt.group(4)):02d}:{dt.group(5)}",
                         "venue": venue, "kind": "lab" if "lab" in line.lower() else "theory"})
            continue
        if not slot:
            problems.append(f"Line {n}: {code} has no slot - skipped")
            continue
        if grid is not None:
            try:
                grid.resolve(slot)
            except KeyError as e:
                problems.append(f"Line {n}: {e.args[0]} - skipped")
                continue
        rows.append({"course_code": code, "course_title": title, "slot": slot, "venue": venue,
                     "kind": "lab" if slot.startswith("L") else "theory"})
    if grid is not None:
        seen: dict = {}
        for r in rows:
            if "slot" not in r:
                continue
            for wd, s, e, _u in grid.resolve(r["slot"]):
                for (wd2, s2, e2), other in seen.items():
                    if wd == wd2 and s < e2 and s2 < e:
                        problems.append(f"{r['course_code']} ({r['slot']}) clashes with {other} - check the slots")
                seen[(wd, s, e)] = r["course_code"]
    return rows, problems


def rows_to_sessions(rows: list[dict], grid=None) -> list[ClassSession]:
    out = []
    for r in rows:
        if "slot" in r and grid is not None:
            for wd, s, e, units in grid.resolve(r["slot"]):
                out.append(ClassSession(r["course_code"], r["course_title"], r["slot"], wd, s, e, r.get("kind", "theory"),
                                        r.get("venue", ""), None, units))
        elif "weekday" in r:
            out.append(ClassSession(r["course_code"], r["course_title"], "", int(r["weekday"]), parse_hhmm(r["start"]),
                                    parse_hhmm(r["end"]), r.get("kind", "theory"), r.get("venue", "")))
    return sorted(out, key=lambda c: (c.weekday, c.start))


class TimetableOverlay:
    """Wraps any portal connector. If a person imported their own timetable, that one is used;
    everything else goes straight to the real connector."""

    def __init__(self, base, store, directory=None):
        self.base, self.store, self.directory = base, store, directory
        self.grid = getattr(base, "grid", None)

    def get_person(self, person_id: str):
        if self.directory is None:
            return self.base.get_person(person_id)
        if person_id and person_id.startswith("X"):
            return self.directory.external_person(person_id)
        return self.directory.decorate(self.base.get_person(person_id))

    def list_people(self):
        if self.directory is None:
            return self.base.list_people()
        return [self.directory.decorate(p) for p in self.base.list_people()]

    def weekly_classes(self, person_id: str):
        rows = self.store.imported_timetable(person_id)
        if rows:
            return rows_to_sessions(rows, self.grid)
        return self.base.weekly_classes(person_id)

    def describe(self) -> str:
        return self.base.describe()

    def __getattr__(self, name):
        return getattr(self.base, name)
