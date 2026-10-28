"""The scheduling brain. Builds everyone's busy blocks from every source
(portal timetable, exams, academic calendar, uploaded commitments, booked meetings,
outside calendar events) and finds common free slots."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable

from ..models import CalendarDay, Commitment, Event, Person, Slot
from ..utils import (combine, daterange, fmt_range, merge_intervals, overlaps, parse_hhmm,
                     round_up, subtract_intervals)
from .attendance import skip_status

ExtraBusy = dict[str, list[tuple[datetime, datetime, str]]]


def expand_commitment(c: Commitment, start: date, end: date) -> list[tuple[datetime, datetime]]:
    """All occurrences of a (possibly weekly) commitment that fall between start and end dates."""
    if not c.recurrence:
        return [(c.start, c.end)] if c.start.date() <= end and c.end.date() >= start else []
    out = []
    dur = c.end - c.start
    last = min(end, c.until) if c.until else end
    d = c.start.date()
    if d < start:
        d += timedelta(days=((start - d).days // 7) * 7)
    while d <= last:
        if d >= start and d >= c.start.date():
            s = combine(d, c.start.time())
            out.append((s, s + dur))
        d += timedelta(days=7)
    return out


class AvailabilityEngine:
    def __init__(self, portal, store, calendar, settings, clock: Callable[[], datetime]):
        self.portal = portal
        self.store = store
        self.calendar = calendar
        self.settings = settings
        self.clock = clock
        s = settings.get
        self.day_start = parse_hhmm(s("scheduling.day_start", "08:00"))
        self.day_end = parse_hhmm(s("scheduling.day_end", "19:00"))
        self.working_days = set(s("scheduling.working_days", [0, 1, 2, 3, 4]))
        self.step = int(s("scheduling.step_min", 10))
        self.lead = int(s("scheduling.lead_time_min", 30))
        self.buffer = int(s("scheduling.buffer_min", 0))
        self.search_days = int(s("scheduling.search_days", 7))
        self.max_suggestions = int(s("scheduling.max_suggestions", 3))
        self.allow_holiday = bool(s("scheduling.allow_meetings_on_holidays", False))
        self.exam_buffer = int(s("scheduling.exam_prep_buffer_min", 60))
        self.lunch = (parse_hhmm(s("scheduling.lunch_start", "13:00")), parse_hhmm(s("scheduling.lunch_end", "14:00")))
        self._memo: dict = {}
        self._memo_key = None

    # ------------------------------------------------------------------ calendar helpers
    def calendar_days(self, start: date, end: date) -> dict[date, CalendarDay]:
        return {c.date: c for c in self.portal.academic_calendar(start, end)}

    def day_kind(self, d: date, cal: dict[date, CalendarDay] | None = None) -> str:
        cal = cal if cal is not None else self.calendar_days(d, d)
        if d in cal:
            return cal[d].kind
        return "weekend" if d.weekday() >= 5 else "instructional"

    def is_meeting_day(self, d: date, cal: dict[date, CalendarDay] | None = None) -> bool:
        if d.weekday() not in self.working_days:
            return False
        return self.allow_holiday or self.day_kind(d, cal) != "holiday"

    # ------------------------------------------------------------------ busy blocks
    def person_events(self, pid: str, start: date, end: date, *, extra_busy: ExtraBusy | None = None,
                      exclude_meeting_ids: Iterable[str] = (), exclude_commitment_ids: Iterable[str] = (),
                      ignore_offers: Iterable[str] = ()) -> list[Event]:
        """All busy blocks for one person. Results without exclusions are memoised until data or clock change."""
        exclude_meeting_ids, exclude_commitment_ids, ignore_offers = (
            tuple(exclude_meeting_ids), tuple(exclude_commitment_ids), tuple(ignore_offers))
        key = (self.store.epoch, self.store.version, self.clock())
        if key != self._memo_key:
            self._memo, self._memo_key = {}, key
        cacheable = not (extra_busy or exclude_meeting_ids or exclude_commitment_ids or ignore_offers)
        mk = (pid, start, end)
        if cacheable and mk in self._memo:
            return list(self._memo[mk])
        ev = self._build_events(pid, start, end, extra_busy, exclude_meeting_ids, exclude_commitment_ids, ignore_offers)
        if cacheable:
            self._memo[mk] = ev
        return list(ev)

    def _build_events(self, pid, start, end, extra_busy, exclude_meeting_ids, exclude_commitment_ids,
                      ignore_offers) -> list[Event]:
        person = self.portal.get_person(pid)
        if person is None:
            return []
        exclude_meeting_ids, exclude_commitment_ids = set(exclude_meeting_ids), set(exclude_commitment_ids)
        ignore_offers = set(ignore_offers)
        cal = self.calendar_days(start, end)
        classes = self.portal.weekly_classes(pid)
        att = {r.course_code: r for r in self.portal.attendance(pid)}
        duties = self.portal.duties(pid)
        ev: list[Event] = []
        for d in daterange(start, end):
            kind = self.day_kind(d, cal)
            if kind == "holiday":
                label = cal[d].label if d in cal else "Holiday"
                ev.append(Event(combine(d, self.day_start), combine(d, self.day_end), label, "holiday",
                                "portal", hard=not self.allow_holiday))
                continue
            if kind == "instructional":
                for c in classes:
                    if c.weekday != d.weekday():
                        continue
                    hard, meta = True, {"course_code": c.course_code, "slot": c.slot, "venue": c.venue}
                    if person.role == "student":
                        st = skip_status(person, att.get(c.course_code), c.units, self.settings)
                        hard = not st["skippable"]
                        meta.update(st)
                    ev.append(Event(combine(d, c.start), combine(d, c.end), f"{c.course_code} {c.course_title}",
                                    "class", "portal", hard=hard, meta=meta))
            if d.weekday() in self.working_days or kind == "instructional":
                for w in duties:
                    if w.weekday == d.weekday():
                        ev.append(Event(combine(d, w.start), combine(d, w.end), w.title, "duty", "portal"))
        for ex in self.portal.exams(pid, start, end):
            ev.append(Event(ex.start, ex.end, ex.title, "exam", "portal", meta={"venue": ex.venue}))
            if self.exam_buffer:
                ev.append(Event(ex.start - timedelta(minutes=self.exam_buffer), ex.start,
                                f"Before exam: {ex.title}", "exam_prep", "portal"))
        for c in self.store.commitments(pid):
            if c.id in exclude_commitment_ids:
                continue
            for s, e in expand_commitment(c, start, end):
                ev.append(Event(s, e, c.title, "commitment", "upload", ref=c.id,
                                meta={"category": c.category}))
        for m in self.store.meetings(pid):
            if m.id in exclude_meeting_ids or m.status != "confirmed":
                continue
            if m.end.date() >= start and m.start.date() <= end:
                ev.append(Event(m.start, m.end, m.title, "meeting", "agent", ref=m.id,
                                meta={"participants": m.participant_ids, "meeting_kind": m.kind}))
        now = self.clock()
        for h in self.store.active_holds(pid, now):
            if h["offer_id"] in ignore_offers:
                continue
            hs, he = datetime.fromisoformat(h["start"]), datetime.fromisoformat(h["end"])
            if he.date() >= start and hs.date() <= end:
                others = [self.portal.get_person(x).name for x in h["participants"] if x != pid and self.portal.get_person(x)]
                ev.append(Event(hs, he, f"Held for {', '.join(others) or 'an offer'}", "hold", "agent", ref=h["offer_id"],
                                meta={"expires_at": h["expires_at"], "participants": h["participants"]}))
        ev += self._preference_blocks(pid, start, end, ev, exclude_meeting_ids)
        for e in self.calendar.list_events(pid):
            if e.ref and e.ref.startswith("meeting:"):
                continue
            if e.end.date() >= start and e.start.date() <= end:
                ev.append(Event(e.start, e.end, e.title, "external", "calendar", hard=True, ref=e.ref))
        for s, e, label in (extra_busy or {}).get(pid, []):
            if e.date() >= start and s.date() <= end:
                ev.append(Event(s, e, label, "blocked", "pending"))
        return sorted(ev, key=lambda x: x.start)

    def _preference_blocks(self, pid, start, end, ev, exclude_meeting_ids) -> list[Event]:
        """Turn a person's meeting preferences into blocks that stop new meetings (never existing ones)."""
        pr = self.store.prefs(pid)
        out: list[Event] = []
        earliest = parse_hhmm(pr["earliest"]) if pr.get("earliest") else None
        latest = parse_hhmm(pr["latest"]) if pr.get("latest") else None
        max_day = int(pr["max_per_day"]) if pr.get("max_per_day") else None
        gap = int(pr.get("gap_min") or 0)
        nodays = set(pr.get("no_meeting_days") or [])
        if not (earliest or latest or max_day or gap or nodays):
            return out
        for d in daterange(start, end):
            day_s, day_e = combine(d, self.day_start), combine(d, self.day_end)
            if d.weekday() in nodays:
                out.append(Event(day_s, day_e, "No meetings on this day (preference)", "pref", "prefs"))
                continue
            if earliest and earliest > self.day_start:
                out.append(Event(day_s, combine(d, earliest), "Before preferred hours", "pref", "prefs"))
            if latest and latest < self.day_end:
                out.append(Event(combine(d, latest), day_e, "After preferred hours", "pref", "prefs"))
            todays = [e for e in ev if e.kind in ("meeting", "hold") and e.start.date() == d]
            if max_day and len([e for e in todays if e.kind == "meeting" and e.meta.get("meeting_kind") != "office_hours"
                                and e.ref not in exclude_meeting_ids]) >= max_day:
                out.append(Event(day_s, day_e, f"Daily limit of {max_day} meetings reached", "pref", "prefs"))
            if gap:
                for e in todays:
                    out.append(Event(e.start - timedelta(minutes=gap), e.start, "Gap before meeting", "pref", "prefs"))
                    out.append(Event(e.end, e.end + timedelta(minutes=gap), "Gap after meeting", "pref", "prefs"))
        return out

    def office_windows(self, pid: str, d: date) -> list[tuple[datetime, datetime]]:
        return [(combine(d, w.start), combine(d, w.end)) for w in self.portal.office_hours(pid) if w.weekday == d.weekday()]

    # ------------------------------------------------------------------ checks
    def check_slot(self, pids: list[str], start: datetime, end: datetime, *, extra_busy: ExtraBusy | None = None,
                   exclude_meeting_ids: Iterable[str] = (), ignore_offers: Iterable[str] = ()) -> dict:
        """Is everyone free? Returns hard conflicts (block), soft ones (skippable classes) and rule problems."""
        d = start.date()
        problems = []
        if not self.is_meeting_day(d):
            problems.append("not a working day for meetings" if self.day_kind(d) != "holiday" else "holiday")
        if start.time() < self.day_start or end.time() > self.day_end or end.date() != d:
            problems.append(f"outside meeting hours ({self.day_start:%H:%M}-{self.day_end:%H:%M})")
        if start < self.clock():
            problems.append("that time has already passed")
        hard, soft = [], []
        for pid in pids:
            for e in self.person_events(pid, d, d, extra_busy=extra_busy, exclude_meeting_ids=exclude_meeting_ids,
                                        ignore_offers=ignore_offers):
                if e.kind == "holiday" and not e.hard:
                    continue
                if overlaps(start - timedelta(minutes=self.buffer), end + timedelta(minutes=self.buffer), e.start, e.end):
                    item = {"person_id": pid, "title": e.title, "kind": e.kind, "start": e.start, "end": e.end,
                            "meta": e.meta}
                    (hard if e.hard else soft).append(item)
        return {"ok": not hard and not problems, "hard": hard, "soft": soft, "problems": problems}

    # ------------------------------------------------------------------ finder
    def find_common_slots(self, pids: list[str], start_date: date, end_date: date, duration_min: int, *,
                          preferred_start: datetime | None = None, window: tuple[time, time] | None = None,
                          allow_soft: bool = False, max_results: int | None = None,
                          extra_busy: ExtraBusy | None = None, exclude_meeting_ids: Iterable[str] = (),
                          ignore_offers: Iterable[str] = ()) -> list[Slot]:
        max_results = max_results or self.max_suggestions
        now = self.clock()
        earliest = round_up(now + timedelta(minutes=self.lead), self.step)
        start_date = max(start_date, now.date())
        if end_date < start_date:
            return []
        dur = timedelta(minutes=duration_min)
        cal = self.calendar_days(start_date, end_date)
        events = {pid: self.person_events(pid, start_date, end_date, extra_busy=extra_busy,
                                          exclude_meeting_ids=exclude_meeting_ids, ignore_offers=ignore_offers)
                  for pid in pids}
        people = {pid: self.portal.get_person(pid) for pid in pids}
        ref_date = preferred_start.date() if preferred_start else start_date
        buf = timedelta(minutes=self.buffer)
        cands: list[Slot] = []
        for d in daterange(start_date, end_date):
            if not self.is_meeting_day(d, cal):
                continue
            ws = combine(d, max(self.day_start, window[0]) if window else self.day_start)
            we = combine(d, min(self.day_end, window[1]) if window else self.day_end)
            ws = max(ws, earliest)
            if ws + dur > we:
                continue
            day_ev = [(pid, e) for pid in pids for e in events[pid]
                      if e.start.date() <= d <= e.end.date() and not (e.kind == "holiday" and not e.hard)]
            hard = [(e.start - buf, e.end + buf) for _p, e in day_ev if e.hard]
            soft = [(p, e) for p, e in day_ev if not e.hard]
            blocking = hard + ([] if allow_soft else [(e.start, e.end) for _p, e in soft])
            for fs, fe in subtract_intervals((ws, we), blocking):
                for s in self._starts(fs, fe, dur):
                    e_ = s + dur
                    softs = [{"person_id": p, "title": ev.title, "percent": ev.meta.get("percent"),
                              "percent_if_missed": ev.meta.get("percent_if_missed"), "level": ev.meta.get("level")}
                             for p, ev in soft if overlaps(s, e_, ev.start, ev.end)]
                    score, reasons = self._score(s, e_, ref_date, preferred_start, softs, people, events)
                    cands.append(Slot(s, e_, score, reasons, softs))
        cands.sort(key=lambda x: (x.score, x.start))
        chosen: list[Slot] = []
        for c in cands:
            if any(overlaps(c.start - timedelta(minutes=60), c.end + timedelta(minutes=60), x.start, x.end) for x in chosen):
                continue
            chosen.append(c)
            if len(chosen) >= max_results:
                break
        return sorted(chosen, key=lambda x: x.start)

    def free_windows(self, pids: list[str], d: date, min_minutes: int = 30,
                     window: tuple[time, time] | None = None) -> list[tuple[datetime, datetime]]:
        """Common free stretches on a day (hard busy only)."""
        if not self.is_meeting_day(d):
            return []
        now = self.clock()
        ws = combine(d, max(self.day_start, window[0]) if window else self.day_start)
        we = combine(d, min(self.day_end, window[1]) if window else self.day_end)
        ws = max(ws, round_up(now, self.step))
        if ws >= we:
            return []
        blocks = []
        for pid in pids:
            for e in self.person_events(pid, d, d):
                if e.hard:
                    blocks.append((e.start, e.end))
        return [(s, e) for s, e in subtract_intervals((ws, we), blocks) if (e - s).total_seconds() >= min_minutes * 60]

    # ------------------------------------------------------------------ internals
    def _starts(self, fs: datetime, fe: datetime, dur: timedelta) -> list[datetime]:
        first = round_up(fs, self.step)
        out = []
        if first + dur <= fe:
            out.append(first)
        t = round_up(first, 30)
        while t + dur <= fe and len(out) < 12:
            if t not in out:
                out.append(t)
            t += timedelta(minutes=30)
        return out

    def _score(self, s, e, ref_date, preferred_start, softs, people, events):
        score, reasons = 0.0, []
        score += abs((s.date() - ref_date).days) * 6
        if preferred_start:
            gap_h = abs((s - preferred_start).total_seconds()) / 3600
            score += min(gap_h, 24) * 1.5
            if s.date() == preferred_start.date():
                score -= 8
                reasons.append("same day as requested")
        if softs:
            score += 40 * len(softs)
            for x in softs:
                who = people[x["person_id"]].name if people.get(x["person_id"]) else x["person_id"]
                reasons.append(f"{who} would miss {x['title']} (attendance {x['percent']}% -> {x['percent_if_missed']}%)")
        if overlaps(s, e, combine(s.date(), self.lunch[0]), combine(s.date(), self.lunch[1])):
            score += 5
        for pid, p in people.items():
            if p and p.role == "faculty":
                for ws, we in self.office_windows(pid, s.date()):
                    if s >= ws and e <= we:
                        score -= 8
                        reasons.append(f"inside {p.name}'s cabin hours")
            if p and p.role == "student" and any(ev.kind == "exam" and ev.start.date() == s.date() for ev in events[pid]):
                score += 25
                reasons.append(f"{p.name} has an exam that day")
        if s.hour >= 18:
            score += 4
        if s.minute not in (0, 30):
            score += 3
        return score, reasons

    def describe_slot(self, slot: Slot) -> str:
        return fmt_range(slot.start, slot.end)
