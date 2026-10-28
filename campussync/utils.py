from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Iterable

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_hhmm(s: str | time) -> time:
    if isinstance(s, time):
        return s
    h, m = str(s).strip().split(":")[:2]
    return time(int(h), int(m))


def combine(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def daterange(a: date, b: date) -> Iterable[date]:
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def round_up(dt: datetime, step_min: int) -> datetime:
    base = dt.replace(second=0, microsecond=0)
    if base < dt:
        base += timedelta(minutes=1)
    rem = (-base.minute) % step_min
    return base + timedelta(minutes=rem)


def overlaps(a_s: datetime, a_e: datetime, b_s: datetime, b_e: datetime) -> bool:
    return a_s < b_e and b_s < a_e


def merge_intervals(iv: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[list[datetime]] = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def subtract_intervals(base: tuple[datetime, datetime],
                       blocks: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    free = []
    cur_s, end = base
    for s, e in merge_intervals(blocks):
        if e <= cur_s or s >= end:
            continue
        if s > cur_s:
            free.append((cur_s, min(s, end)))
        cur_s = max(cur_s, e)
        if cur_s >= end:
            break
    if cur_s < end:
        free.append((cur_s, end))
    return free


def fmt_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {d.strftime('%b')}"


def fmt_time(t: time | datetime) -> str:
    return t.strftime("%H:%M")


def fmt_dt(dt: datetime) -> str:
    return f"{fmt_date(dt.date())}, {fmt_time(dt)}"


def fmt_range(s: datetime, e: datetime) -> str:
    if s.date() == e.date():
        return f"{fmt_date(s.date())}, {fmt_time(s)}-{fmt_time(e)}"
    return f"{fmt_dt(s)} - {fmt_dt(e)}"


_TITLES = re.compile(r"^(dr|prof|mr|ms|mrs)\.?\s+", re.I)


def first_name(name: str) -> str:
    n = _TITLES.sub("", name.strip())
    parts = [p for p in n.split() if len(p.rstrip(".")) > 1]
    return parts[0] if parts else n


def salutation(person) -> str:
    """How an email should address this person."""
    if person.role == "faculty":
        return person.name
    return first_name(person.name)
