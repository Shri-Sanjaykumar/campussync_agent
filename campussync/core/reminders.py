"""Reminders, calendar writes and .ics export for booked meetings."""
from __future__ import annotations

from datetime import datetime, timedelta

from ..models import Event, Mail, Meeting, Reminder
from ..utils import fmt_range


class ReminderService:
    def __init__(self, store, mail, calendar, portal, settings, clock, notifier=None):
        self.store, self.mail, self.calendar, self.portal = store, mail, calendar, portal
        self.notifier = notifier
        self.offsets = list(settings.get("reminders.offsets_min", [1440, 30]))
        self.clock = clock

    # ---------------------------------------------------------------- calendar sync
    def sync_calendar(self, m: Meeting) -> None:
        ref = f"meeting:{m.id}"
        for pid in m.participant_ids:
            if m.status == "confirmed":
                self.calendar.add_event(pid, Event(m.start, m.end, m.title, "meeting", "agent", ref=ref,
                                                   meta={"location": m.location}))
            else:
                self.calendar.remove_by_ref(pid, ref)

    # ---------------------------------------------------------------- reminders
    def cancel(self, meeting_id: str) -> None:
        for r in self.store.reminders():
            if r.meeting_id == meeting_id and r.status == "scheduled":
                r.status = "cancelled"
                self.store.upsert_reminder(r)

    def schedule(self, m: Meeting) -> list[Reminder]:
        self.cancel(m.id)
        if m.status != "confirmed":
            return []
        now = self.clock()
        out = []
        for pid in m.participant_ids:
            for off in self.offsets:
                at = m.start - timedelta(minutes=off)
                if at > now:
                    r = Reminder(self.store.new_id("R"), m.id, pid, at)
                    self.store.upsert_reminder(r)
                    out.append(r)
        return out

    def upcoming(self, person_id: str) -> list[Reminder]:
        return sorted([r for r in self.store.reminders() if r.person_id == person_id and r.status == "scheduled"],
                      key=lambda r: r.send_at)

    def dispatch_due(self, now: datetime | None = None) -> list[Mail]:
        now = now or self.clock()
        sent = []
        for r in self.store.reminders():
            if r.status != "scheduled" or r.send_at > now:
                continue
            m = self.store.get_meeting(r.meeting_id)
            r.status = "sent" if m and m.status == "confirmed" else "cancelled"
            self.store.upsert_reminder(r)
            if r.status != "sent":
                continue
            others = [self.portal.get_person(p).name for p in m.participant_ids if p != r.person_id and self.portal.get_person(p)]
            body = (f"Reminder: {m.title}\nWhen: {fmt_range(m.start, m.end)}\n"
                    f"With: {', '.join(others) or '-'}\nWhere: {m.location or 'TBD'}")
            mail = self.mail.send(Mail("", "agent", [r.person_id], f"Reminder: {m.title}", body, now,
                                       kind="reminder", meta={"meeting_id": m.id}))
            sent.append(mail)
            if self.notifier:
                self.notifier.push(r.person_id, f"Reminder: {m.title}, {fmt_range(m.start, m.end)} at {m.location or 'TBD'}")
        return sent


def meeting_to_ics(m: Meeting, portal, tz: str = "Asia/Kolkata") -> str:
    def f(dt: datetime) -> str:
        return dt.strftime("%Y%m%dT%H%M%S")
    attendees = []
    for pid in m.participant_ids:
        p = portal.get_person(pid)
        if p:
            attendees.append(f"ATTENDEE;CN={p.name}:mailto:{p.email}")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CampusSync Agent//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VTIMEZONE", f"TZID:{tz}", "BEGIN:STANDARD", "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0530", "TZOFFSETTO:+0530", "TZNAME:IST", "END:STANDARD", "END:VTIMEZONE",
        "BEGIN:VEVENT", f"UID:{m.id}@campussync", f"DTSTAMP:{f(datetime.now())}",
        f"DTSTART;TZID={tz}:{f(m.start)}", f"DTEND;TZID={tz}:{f(m.end)}",
        f"SUMMARY:{m.title}", f"LOCATION:{m.location}", *attendees,
        "BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY", f"DESCRIPTION:{m.title}", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def parse_ics(text: str) -> list[dict]:
    """Minimal .ics reader for uploads: returns [{title, start, end}] (local times)."""
    events, cur = [], None
    unfolded = []
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line.strip())
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if "start" in cur and "end" in cur:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            name = key.split(";")[0].upper()
            if name == "SUMMARY":
                cur["title"] = val
            elif name in ("DTSTART", "DTEND"):
                v = val.rstrip("Z")
                fmt = "%Y%m%dT%H%M%S" if "T" in v else "%Y%m%d"
                try:
                    dt = datetime.strptime(v[:15] if "T" in v else v[:8], fmt)
                except ValueError:
                    continue
                if val.endswith("Z"):
                    dt += timedelta(hours=5, minutes=30)
                cur["start" if name == "DTSTART" else "end"] = dt
    for e in events:
        e.setdefault("title", "Imported event")
    return events
