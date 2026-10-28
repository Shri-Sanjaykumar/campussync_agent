"""Mass rescheduling: when someone becomes unavailable, move every affected meeting at once."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import Alert, Mail, Meeting, Slot
from ..utils import fmt_range, overlaps, salutation


@dataclass
class PlanItem:
    meeting: Meeting
    new_slot: Slot | None
    alternatives: list[Slot] = field(default_factory=list)


class Rescheduler:
    def __init__(self, engine, store, mail, reminders, portal, clock, notifier=None):
        self.engine, self.store, self.mail, self.reminders, self.portal, self.clock = (
            engine, store, mail, reminders, portal, clock)
        self.notifier = notifier

    def affected_meetings(self, person_id: str, start: datetime, end: datetime) -> list[Meeting]:
        now = self.clock()
        return [m for m in self.store.meetings(person_id)
                if m.status == "confirmed" and m.end > now and overlaps(m.start, m.end, start, end)]

    def plan_unavailability(self, person_id: str, start: datetime, end: datetime, reason: str) -> list[PlanItem]:
        extra = {person_id: [(start, end, reason or "Unavailable")]}
        return self.plan(self.affected_meetings(person_id, start, end), extra)

    def plan(self, meetings: list[Meeting], extra_busy: dict | None = None) -> list[PlanItem]:
        extra = {k: list(v) for k, v in (extra_busy or {}).items()}
        items = []
        today = self.clock().date()
        for m in sorted(meetings, key=lambda x: x.start):
            dur = int((m.end - m.start).total_seconds() // 60)
            start_d = max(today, m.start.date())
            slots = self.engine.find_common_slots(
                m.participant_ids, start_d, start_d + timedelta(days=self.engine.search_days), dur,
                preferred_start=m.start, extra_busy=extra, exclude_meeting_ids=[m.id], max_results=3)
            new = slots[0] if slots else None
            if new:  # reserve it so the next meeting in this batch doesn't take the same time
                for pid in m.participant_ids:
                    extra.setdefault(pid, []).append((new.start, new.end, f"Planned: {m.title}"))
            items.append(PlanItem(m, new, slots[1:]))
        return items

    def apply(self, items: list[PlanItem], actor_id: str, reason: str = "") -> dict:
        now = self.clock()
        moved, stuck = 0, 0
        actor = self.portal.get_person(actor_id)
        for it in items:
            m = it.meeting
            old = fmt_range(m.start, m.end)
            if it.new_slot:
                m.history.append({"at": now.isoformat(timespec="minutes"), "action": "moved",
                                  "from": old, "by": actor_id, "reason": reason})
                m.start, m.end, m.status = it.new_slot.start, it.new_slot.end, "confirmed"
                moved += 1
                body_tail = f"New time: {fmt_range(m.start, m.end)}\nPrevious time: {old}"
                subject = f"Moved: {m.title}"
            else:
                m.status = "needs_attention"
                m.history.append({"at": now.isoformat(timespec="minutes"), "action": "could_not_move",
                                  "from": old, "by": actor_id})
                stuck += 1
                body_tail = (f"Original time: {old}\nThe agent couldn't find a common free slot in the next "
                             f"{self.engine.search_days} days. Please reply with a time that works.")
                subject = f"Needs a new time: {m.title}"
            self.store.upsert_meeting(m)
            self.reminders.sync_calendar(m)
            self.reminders.schedule(m)
            for pid in m.participant_ids:
                if pid == actor_id:
                    continue
                p = self.portal.get_person(pid)
                who = actor.name if actor else "The organiser"
                body = (f"Hi {salutation(p) if p else ''},\n\n{who} can't make the original time for "
                        f"'{m.title}', so it has been rescheduled.\n\n{body_tail}\n\n"
                        f"(Sent by CampusSync on behalf of {who})")
                self.mail.send(Mail("", actor_id, [pid], subject, body, now, thread_id=m.thread_id or "",
                                    kind="notification", meta={"meeting_id": m.id}))
                if self.notifier:
                    self.notifier.push(pid, f"{subject} - " + (fmt_range(m.start, m.end) if it.new_slot else "needs a new time"))
            if not it.new_slot:
                self.store.add_alert(Alert(self.store.new_id("A"), actor_id, now, "warning",
                                           f"Couldn't find a new slot for '{m.title}' ({old})", {"meeting_id": m.id}))
        return {"moved": moved, "needs_attention": stuck}
