"""Numbers for the faculty dashboard: meeting load, cabin-hour use, how fast requests get answered,
and how the agent handled them."""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

from ..utils import WEEKDAYS, combine, daterange


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60


def faculty_stats(svc, fid: str, start: date, end: date) -> dict:
    eng, store = svc.engine, svc.store
    meets = [m for m in store.meetings(fid) if m.status == "confirmed" and start <= m.start.date() <= end]
    normal = [m for m in meets if m.kind != "office_hours"]
    tokens = [m for m in meets if m.kind == "office_hours"]

    # cabin-hour use
    oh_total = oh_used = 0.0
    for d in daterange(start, end):
        if not eng.is_meeting_day(d) or eng.day_kind(d) == "exam":
            continue
        for ws, we in eng.office_windows(fid, d):
            oh_total += _minutes(ws, we)
    oh_used = sum(_minutes(m.start, m.end) for m in tokens)

    # time in classes vs meetings vs free, over working hours
    busy = {"class": 0.0, "meeting": 0.0, "other": 0.0}
    work = 0.0
    for d in daterange(start, end):
        if not eng.is_meeting_day(d):
            continue
        work += _minutes(combine(d, eng.day_start), combine(d, eng.day_end))
        for e in eng.person_events(fid, d, d):
            if not e.hard or e.kind in ("pref", "holiday"):
                continue
            k = "class" if e.kind == "class" else "meeting" if e.kind == "meeting" else "other"
            busy[k] += _minutes(e.start, e.end)

    # request handling (mails to this person and the agent's replies)
    inbox = [m for m in svc.mail.inbox(fid) if m.kind == "human" and start - timedelta(days=30) <= m.sent_at.date() <= end]
    sent = {m.in_reply_to: m for m in svc.mail.sent(fid) if m.kind == "agent" and m.in_reply_to}
    resp, decisions = [], {}
    for m in inbox:
        r = sent.get(m.id)
        if r:
            resp.append(_minutes(m.sent_at, r.sent_at))
            dcs = r.meta.get("decision", "other")
            decisions[dcs] = decisions.get(dcs, 0) + 1
    by_day = {WEEKDAYS[i]: 0 for i in range(6)}
    for m in meets:
        if m.start.weekday() < 6:
            by_day[WEEKDAYS[m.start.weekday()]] += 1
    students = {p for m in meets for p in m.participant_ids if p != fid}
    return {
        "meetings": len(normal), "tokens": len(tokens),
        "meeting_hours": round(sum(_minutes(m.start, m.end) for m in normal) / 60, 1),
        "students_met": len(students),
        "cabin_use_pct": round(100 * oh_used / oh_total) if oh_total else None,
        "cabin_minutes": int(oh_total), "cabin_used_minutes": int(oh_used),
        "requests": len(inbox), "answered": len(resp),
        "median_reply_min": round(statistics.median(resp)) if resp else None,
        "decisions": decisions, "by_day": by_day,
        "busy_pct": {k: round(100 * v / work) if work else 0 for k, v in busy.items()},
        "free_pct": max(0, 100 - sum(round(100 * v / work) if work else 0 for v in busy.values())),
        "waitlist": len([w for w in store.waitlist() if w["faculty_id"] == fid and w["status"] == "waiting"]),
    }


def department_table(svc, start: date, end: date) -> list[dict]:
    rows = []
    for p in svc.people("faculty"):
        st = faculty_stats(svc, p.id, start, end)
        rows.append({"Faculty": p.name, "Meetings": st["meetings"], "Cabin tokens": st["tokens"],
                     "Cabin use %": st["cabin_use_pct"], "Hours in meetings": st["meeting_hours"],
                     "Median reply (min)": st["median_reply_min"], "Waitlist": st["waitlist"]})
    return rows
