"""Office-hours queue. A faculty member's published cabin hours are split into short tokens
(10 minutes by default). Students grab a token directly, no back-and-forth mail, since the
faculty already said they're available then. Full session? Join the waitlist; when someone
cancels, the first waiting student who is free gets the token automatically and is notified."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..models import Mail, Meeting
from ..utils import combine, daterange, fmt_range, overlaps


class OfficeHoursError(Exception):
    pass


class OfficeHours:
    def __init__(self, engine, store, portal, reminders, mail, notifier, settings, clock):
        self.engine, self.store, self.portal, self.reminders = engine, store, portal, reminders
        self.mail, self.notifier, self.clock = mail, notifier, clock
        self.token_min = int(settings.get("office_hours.token_min", 10))
        self.per_student = int(settings.get("office_hours.max_tokens_per_session", 1))

    # ---------------------------------------------------------------- sessions
    def _oh_meetings(self, fid: str, d: date) -> list[Meeting]:
        return [m for m in self.store.meetings(fid) if m.kind == "office_hours" and m.status == "confirmed"
                and m.start.date() == d]

    def sessions(self, fid: str, start: date, days: int = 7, viewer: str | None = None) -> list[dict]:
        now = self.clock()
        out = []
        for d in daterange(start, start + timedelta(days=days - 1)):
            windows = self.engine.office_windows(fid, d)
            if not windows or not self.engine.is_meeting_day(d) or self.engine.day_kind(d) == "exam":
                continue
            booked = self._oh_meetings(fid, d)
            fac_busy = [e for e in self.engine.person_events(fid, d, d)
                        if e.hard and e.kind not in ("pref",) and not (e.kind == "meeting" and e.meta.get("meeting_kind") == "office_hours")]
            for ws, we in windows:
                tokens = []
                t = ws
                while t + timedelta(minutes=self.token_min) <= we:
                    te = t + timedelta(minutes=self.token_min)
                    b = next((m for m in booked if m.start == t), None)
                    if b:
                        student = next((p for p in b.participant_ids if p != fid), None)
                        status = "mine" if viewer and student == viewer else "booked"
                    elif t < now:
                        status = "past"
                    elif any(overlaps(t, te, e.start, e.end) for e in fac_busy if e.kind == "hold"):
                        status = "held"
                    elif any(overlaps(t, te, e.start, e.end) for e in fac_busy):
                        status = "unavailable"
                    else:
                        status = "free"
                        student = None
                    tokens.append({"start": t, "end": te, "status": status,
                                   "student_id": student if b else None, "meeting_id": b.id if b else None})
                    t = te
                wl = [w for w in self.store.waitlist() if w["faculty_id"] == fid and w["session_start"] == ws.isoformat(timespec="minutes")
                      and w["status"] == "waiting"]
                out.append({"faculty_id": fid, "date": d, "start": ws, "end": we, "tokens": tokens,
                            "free": sum(1 for x in tokens if x["status"] == "free"), "waitlist": wl})
        return out

    def _session_of(self, fid: str, t: datetime) -> tuple[datetime, datetime] | None:
        for ws, we in self.engine.office_windows(fid, t.date()):
            if ws <= t < we:
                return ws, we
        return None

    # ---------------------------------------------------------------- booking
    def book(self, student_id: str, fid: str, token_start: datetime, topic: str = "Doubt", notify: bool = True) -> Meeting:
        sess = self._session_of(fid, token_start)
        if not sess:
            raise OfficeHoursError("That time isn't inside the faculty's cabin hours.")
        if token_start < self.clock():
            raise OfficeHoursError("That token has already passed.")
        te = token_start + timedelta(minutes=self.token_min)
        info = next((s for s in self.sessions(fid, token_start.date(), 1, viewer=student_id) if s["start"] == sess[0]), None)
        tok = next((x for x in info["tokens"] if x["start"] == token_start), None) if info else None
        if not tok or tok["status"] != "free":
            raise OfficeHoursError("That token isn't free any more.")
        mine = [x for x in info["tokens"] if x["status"] == "mine"]
        if len(mine) >= self.per_student:
            raise OfficeHoursError("You already have a token in this session.")
        chk = self.engine.check_slot([student_id], token_start, te)
        if chk["hard"]:
            raise OfficeHoursError("You're busy at that time: " + ", ".join(c["title"] for c in chk["hard"][:2]))
        fac = self.portal.get_person(fid)
        m = Meeting(self.store.new_id("MT"), f"Cabin hours: {topic}", student_id, [fid, student_id], token_start, te,
                    "confirmed", fac.cabin or "Cabin", history=[{"at": self.clock().isoformat(timespec="minutes"),
                                                                 "action": "token booked", "by": student_id}],
                    kind="office_hours")
        m.thread_id = m.id
        self.store.upsert_meeting(m)
        self.reminders.sync_calendar(m)
        self.reminders.schedule(m)
        if notify and self.notifier:
            self.notifier.push(student_id, f"Cabin token booked with {fac.name}: {fmt_range(m.start, m.end)}")
        return m

    def join_waitlist(self, student_id: str, fid: str, session_start: datetime, topic: str = "Doubt") -> dict:
        key = session_start.isoformat(timespec="minutes")
        for w in self.store.waitlist():
            if w["faculty_id"] == fid and w["student_id"] == student_id and w["session_start"] == key and w["status"] == "waiting":
                raise OfficeHoursError("You're already on this waitlist.")
        entry = {"id": self.store.new_id("W"), "faculty_id": fid, "student_id": student_id, "session_start": key,
                 "topic": topic, "status": "waiting", "created_at": self.clock().isoformat(timespec="minutes")}
        self.store.add_waitlist(entry)
        return entry

    def on_cancel(self, m: Meeting) -> dict | None:
        """A token was freed: give it to the first waiting student who is free then."""
        fid = next((p for p in m.participant_ids if self.portal.get_person(p) and self.portal.get_person(p).role == "faculty"), None)
        if not fid or m.start < self.clock():
            return None
        sess = self._session_of(fid, m.start)
        if not sess:
            return None
        key = sess[0].isoformat(timespec="minutes")
        queue = sorted([w for w in self.store.waitlist() if w["faculty_id"] == fid and w["session_start"] == key
                        and w["status"] == "waiting"], key=lambda w: w["created_at"])
        for w in queue:
            try:
                nm = self.book(w["student_id"], fid, m.start, w["topic"], notify=False)
            except OfficeHoursError:
                continue
            self.store.update_waitlist(w["id"], status="assigned", meeting_id=nm.id)
            fac = self.portal.get_person(fid)
            text = (f"A cabin-hours token opened up and it's yours: {fmt_range(nm.start, nm.end)} with {fac.name} "
                    f"at {nm.location}. Cancel it from 'My week' if you can't make it.")
            self.mail.send(Mail("", "agent", [w["student_id"]], "You got a cabin-hours token", text, self.clock(),
                                kind="notification", meta={"meeting_id": nm.id}))
            if self.notifier:
                self.notifier.push(w["student_id"], text)
            return {"assigned_to": w["student_id"], "meeting": nm}
        return None
