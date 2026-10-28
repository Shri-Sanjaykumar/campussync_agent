"""Proctor check-ins. Each faculty member proctors a group of students. The agent scores every
proctee's risk from their attendance (and how long since the last check-in), then books a short
check-in with each one, highest risk first, at times both are free."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..models import Mail, Meeting
from ..utils import fmt_range, salutation
from .attendance import attendance_summary


class ProctorDesk:
    def __init__(self, engine, store, portal, mail, reminders, notifier, settings, clock):
        self.engine, self.store, self.portal, self.mail = engine, store, portal, mail
        self.reminders, self.notifier, self.settings, self.clock = reminders, notifier, settings, clock
        self.stale_days = int(settings.get("proctor.checkin_every_days", 30))

    def proctees(self, fid: str):
        return sorted([p for p in self.portal.list_people() if p.role == "student" and p.proctor_id == fid],
                      key=lambda p: p.id)

    def last_checkin(self, fid: str, sid: str) -> Meeting | None:
        now = self.clock()
        past = [m for m in self.store.meetings(sid) if m.kind == "proctor" and fid in m.participant_ids and m.start <= now]
        return past[-1] if past else None

    def next_checkin(self, fid: str, sid: str) -> Meeting | None:
        now = self.clock()
        up = [m for m in self.store.meetings(sid) if m.kind == "proctor" and fid in m.participant_ids and m.start > now]
        return up[0] if up else None

    def risk(self, student, fid: str | None = None) -> dict:
        rows = attendance_summary(student, self.portal.attendance(student.id), self.settings)
        below = [r for r in rows if r["status"] == "below minimum"]
        edge = [r for r in rows if r["status"] == "can't miss any"]
        score, reasons = 0, []
        if below:
            score += 3 * min(len(below), 3)
            reasons.append("below minimum in " + ", ".join(f"{r['course_code']} ({r['percent']}%)" for r in below[:3])
                           + (f" and {len(below) - 3} more" if len(below) > 3 else ""))
        if edge:
            score += min(len(edge), 3)
            reasons.append(f"can't miss any more classes in {len(edge)} course(s)")
        if fid:
            last = self.last_checkin(fid, student.id)
            if not last or (self.clock() - last.start).days > self.stale_days:
                score += 1
                reasons.append("no check-in in the last 30 days" if not last else f"last check-in {last.start:%d %b}")
        level = "high" if score >= 3 else "medium" if score >= 1 else "low"
        lowest = min((r["percent"] for r in rows if r["percent"] is not None), default=None)
        return {"score": score, "level": level, "reasons": reasons, "lowest": lowest, "below": len(below)}

    def overview(self, fid: str) -> list[dict]:
        out = []
        for s in self.proctees(fid):
            r = self.risk(s, fid)
            last, nxt = self.last_checkin(fid, s.id), self.next_checkin(fid, s.id)
            out.append({"student": s, **r, "last": last, "next": nxt})
        return sorted(out, key=lambda x: (-x["score"], x["student"].id))

    def plan(self, fid: str, start: date, end: date, duration: int = 15, only_at_risk: bool = False,
             skip_booked: bool = True) -> list[dict]:
        reserved: dict = {}
        items = []
        for row in self.overview(fid):
            s = row["student"]
            if only_at_risk and row["level"] == "low":
                continue
            if skip_booked and row["next"]:
                continue
            slots = self.engine.find_common_slots([fid, s.id], start, end, duration, max_results=3, extra_busy=reserved)
            if row["level"] == "high":  # at-risk students: their earliest common free time
                slot = min(slots, key=lambda x: x.start) if slots else None
            else:
                slot = min(slots, key=lambda x: (x.score, x.start)) if slots else None
            if slot:
                for p in (fid, s.id):
                    reserved.setdefault(p, []).append((slot.start, slot.end, "planned check-in"))
            items.append({**row, "slot": slot})
        return items

    def apply(self, fid: str, items: list[dict]) -> list[Meeting]:
        fac = self.portal.get_person(fid)
        now = self.clock()
        booked = []
        for it in items:
            if not it.get("slot"):
                continue
            s = it["student"]
            m = Meeting(self.store.new_id("MT"), "Proctor check-in", fid, [fid, s.id], it["slot"].start, it["slot"].end,
                        "confirmed", fac.cabin or "Cabin", history=[{"at": now.isoformat(timespec="minutes"),
                                                                     "action": "check-in planned", "by": fid}],
                        kind="proctor")
            m.thread_id = m.id
            self.store.upsert_meeting(m)
            self.reminders.sync_calendar(m)
            self.reminders.schedule(m)
            extra = ("\n\nWe'll go through your attendance, so please bring any questions about it."
                     if it["level"] == "high" else "")
            self.mail.send(Mail("", fid, [s.id], "Proctor check-in",
                                f"Hi {salutation(s)},\n\nYour proctor check-in is booked for {fmt_range(m.start, m.end)} "
                                f"at {m.location}. It's in your calendar with a reminder. If the time doesn't work, "
                                f"reply with a time that does.{extra}\n\nThanks,\n{fac.name}",
                                now, thread_id=m.thread_id, kind="notification", meta={"meeting_id": m.id}))
            if self.notifier:
                self.notifier.push(s.id, f"Proctor check-in with {fac.name}: {fmt_range(m.start, m.end)}")
            booked.append(m)
        return booked
