"""Reads a meeting mail, checks BOTH people's timetables, and drafts the reply:
'yes, that works' (and books it) or 'I'm not free then, here's when we both are'.

Privacy rule: replies never reveal why someone is busy. The detailed reasons are shown
only to the person approving the reply, and only for their own schedule."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..llm.rules import ParsedRequest
from ..models import Mail, Meeting, Slot, from_dict, to_dict
from ..utils import combine, fmt_date, fmt_range, fmt_time, salutation


class SlotTaken(Exception):
    pass


@dataclass
class Proposal:
    mail_id: str
    responder_id: str
    requester_id: str
    participants: list[str]
    parsed: ParsedRequest
    decision: str  # accept | offer | cancel | nothing_to_cancel | no_slots | not_meeting
    title: str
    duration_min: int
    summary: str = ""
    requested: str | None = None
    chosen: Slot | None = None
    options: list[Slot] = field(default_factory=list)
    private_notes: list[str] = field(default_factory=list)
    meeting_id: str | None = None
    offer_id: str | None = None
    reply_subject: str = ""
    reply_body: str = ""
    drafted_by: str = "template"
    leave: dict | None = None  # the leave case this mail belongs to


TEAM_RE = re.compile(r"\b(our|my) (project )?(team|group)\b|\ball of us\b|\bteam ?mates\b|\bteam members\b|\bwhole team\b", re.I)

PUBLIC_PROBLEMS = {"holiday": "that day is a holiday", "not a working day for meetings": "that's not a working day",
                   "that time has already passed": "that time has already passed"}


class MailHandler:
    def __init__(self, engine, store, mail, reminders, portal, parser, llm, settings, clock, notifier=None, leave=None):
        self.engine, self.store, self.mail, self.reminders = engine, store, mail, reminders
        self.portal, self.parser, self.llm, self.settings, self.clock = portal, parser, llm, settings, clock
        self.notifier = notifier
        self.leave = leave
        self.default_duration = int(settings.get("scheduling.default_duration_min", 30))
        self.hold_hours = float(settings.get("scheduling.hold_hours", 4))

    # ================================================================ public API
    def process(self, mail_id: str, responder_id: str) -> Proposal:
        mail = self.mail.get(mail_id)
        if mail is None:
            raise KeyError(f"No mail {mail_id}")
        requester = mail.from_id
        participants = list(dict.fromkeys([requester, responder_id] +
                                          [x for x in mail.to_ids if x != responder_id and self.portal.get_person(x)]))
        if mail.kind != "human" or not self.portal.get_person(requester):
            p = ParsedRequest(intent="other", topic=mail.subject)
            return Proposal(mail_id, responder_id, requester, participants, p, "not_meeting", mail.subject,
                            self.default_duration, summary="System message - nothing to schedule.")
        parsed = self.parser.parse(mail.subject, mail.body, mail.sent_at)
        duration = parsed.duration_min or self.default_duration
        prop = Proposal(mail_id, responder_id, requester, participants, parsed, "not_meeting",
                        parsed.topic or mail.subject, duration)
        if TEAM_RE.search(f"{mail.subject}\n{mail.body}"):
            for t in self.portal.teams(requester):
                if requester in t["member_ids"]:
                    new = [m for m in t["member_ids"] if m not in prop.participants]
                    prop.participants += new
                    prop.private_notes.append(f"Group meeting: included {t['name']} "
                                              f"({', '.join(self.portal.get_person(m).name for m in t['member_ids'])})")
                    break
        offer = self._open_offer(mail.thread_id, offered_by=responder_id, offered_to=requester)

        if parsed.intent == "leave_request" and self.leave:
            self._handle_leave(prop, mail, parsed)
        elif offer and (parsed.intent == "option_choice" or (parsed.start and self._match_offer(offer, parsed))):
            self._handle_option(prop, offer, parsed)
        elif parsed.intent == "cancel":
            self._handle_cancel(prop, mail)
        elif parsed.intent in ("meeting_request", "reschedule", "availability_query", "option_choice"):
            if parsed.intent == "reschedule":
                m = self._existing_meeting(requester, responder_id, mail.thread_id)
                if m:
                    prop.meeting_id, prop.title = m.id, m.title
                    prop.duration_min = parsed.duration_min or int((m.end - m.start).total_seconds() // 60)
                    prop.private_notes.append(f"Moving existing meeting: {m.title} ({fmt_range(m.start, m.end)})")
            self._handle_request(prop, parsed)
        else:
            prop.summary = "This mail doesn't look like a scheduling request. No reply drafted."
        self._draft(prop)
        return prop

    def choose_option(self, prop: Proposal, index: int) -> Proposal:
        """Responder books one of the offered options directly instead of mailing the list."""
        prop.chosen = prop.options[index]
        prop.decision = "accept"
        prop.summary = f"Booking option {index + 1}: {fmt_range(prop.chosen.start, prop.chosen.end)}"
        self._draft(prop)
        return prop

    def confirm(self, prop: Proposal, body: str | None = None) -> dict:
        """Send the reply and carry out the action (book / offer / cancel)."""
        mail = self.mail.get(prop.mail_id)
        now = self.clock()
        result: dict = {"meeting": None, "reply": None}
        if prop.decision == "not_meeting":
            self.mail.mark(prop.mail_id, "processed")
            return result
        meta: dict = {"decision": prop.decision}
        if prop.decision == "accept":
            chk = self.engine.check_slot(self._attending(prop), prop.chosen.start, prop.chosen.end,
                                         exclude_meeting_ids=[prop.meeting_id] if prop.meeting_id else [],
                                         ignore_offers=[prop.offer_id] if prop.offer_id else [])
            if chk["hard"]:
                raise SlotTaken("That slot is no longer free for everyone. Process the mail again.")
            if prop.offer_id:
                self.store.release_holds(prop.offer_id)
                self.store.close_offer(prop.offer_id)
            m = self._book(prop, mail, now)
            meta["meeting_id"] = m.id
            result["meeting"] = m
        elif prop.decision == "offer":
            offer_id = self.store.new_id("O")
            self.store.add_offer({
                "id": offer_id, "thread_id": mail.thread_id, "offered_by": prop.responder_id,
                "offered_to": prop.requester_id, "participants": prop.participants, "title": prop.title,
                "duration_min": prop.duration_min, "meeting_id": prop.meeting_id, "status": "open",
                "options": [[s.start.isoformat(timespec="minutes"), s.end.isoformat(timespec="minutes"), list(s.missing)]
                            for s in prop.options],
                "created_at": now.isoformat(timespec="minutes")})
            expires = (now + timedelta(hours=self.hold_hours)).isoformat(timespec="minutes")
            self.store.add_holds([{
                "id": self.store.new_id("H"), "offer_id": offer_id, "thread_id": mail.thread_id,
                "participants": [p for p in prop.participants if p not in s.missing],
                "start": s.start.isoformat(timespec="minutes"), "end": s.end.isoformat(timespec="minutes"),
                "expires_at": expires, "status": "active"} for s in prop.options])
            meta.update({"offer_id": offer_id, "options": [to_dict(s) for s in prop.options], "hold_until": expires})
        elif prop.decision == "cancel":
            m = self.store.get_meeting(prop.meeting_id)
            m.status = "cancelled"
            m.history.append({"at": now.isoformat(timespec="minutes"), "action": "cancelled", "by": prop.requester_id})
            self.store.upsert_meeting(m)
            self.reminders.sync_calendar(m)
            self.reminders.cancel(m.id)
            meta["meeting_id"] = m.id
            result["meeting"] = m
        recipients = [p for p in prop.participants if p != prop.responder_id]
        reply = self.mail.send(Mail("", prop.responder_id, recipients, prop.reply_subject, body or prop.reply_body,
                                    now, thread_id=mail.thread_id, in_reply_to=mail.id, kind="agent", meta=meta))
        self.mail.mark(prop.mail_id, "processed")
        result["reply"] = reply
        if self.notifier:
            ping = {"accept": f"Booked: {prop.title} on {fmt_range(prop.chosen.start, prop.chosen.end)}" if prop.chosen else "",
                    "offer": f"{self.portal.get_person(prop.responder_id).name} sent you times to pick for '{prop.title}'",
                    "cancel": f"Cancelled: {prop.title}"}.get(prop.decision)
            if ping:
                for pid in recipients:
                    self.notifier.push(pid, ping)
        return result

    # ================================================================ handlers
    def _handle_request(self, prop: Proposal, p: ParsedRequest) -> None:
        now = self.clock()
        pids, dur = prop.participants, prop.duration_min
        exclude = [prop.meeting_id] if prop.meeting_id else []
        days = self.engine.search_days
        window = p.window
        is_range = bool(p.start and p.end and (combine(now.date(), p.end) - combine(now.date(), p.start)) > timedelta(minutes=dur))

        # 1) a specific time was asked for
        if p.date and p.start and not is_range:
            req_s = combine(p.date, p.start)
            req_e = req_s + timedelta(minutes=dur)
            prop.requested = fmt_range(req_s, req_e)
            chk = self.engine.check_slot(pids, req_s, req_e, exclude_meeting_ids=exclude)
            if chk["ok"]:
                prop.decision, prop.chosen = "accept", Slot(req_s, req_e, 0, ["requested time is free for everyone"],
                                                            chk["soft"])
                prop.summary = f"Requested time {prop.requested} is free for everyone."
                self._soft_notes(prop, chk["soft"])
                return
            self._explain_conflicts(prop, chk)
            prop.options = self.engine.find_common_slots(pids, p.date, p.date + timedelta(days=days), dur,
                                                         preferred_start=req_s, exclude_meeting_ids=exclude)
            prop.decision = "offer" if prop.options else "no_slots"
            prop.summary = f"Requested time {prop.requested} doesn't work. " + (
                f"Found {len(prop.options)} common alternatives." if prop.options else "No common slot found.")
            if not prop.options:
                self._group_fallback(prop, p.date, p.date + timedelta(days=days), preferred=req_s)
            return

        # 2) a day (maybe with a window like "morning" or "2-4 pm")
        if p.date:
            prop.requested = fmt_date(p.date) + (f", {p.part_of_day}" if p.part_of_day and not is_range else "") + (
                f", {fmt_time(p.start)}-{fmt_time(p.end)}" if is_range else "")
            same_day = self.engine.find_common_slots(pids, p.date, p.date, dur, window=window,
                                                     allow_soft=p.urgent, exclude_meeting_ids=exclude)
            if same_day and window:
                best = min(same_day, key=lambda x: (x.score, x.start))
                prop.decision, prop.chosen = "accept", best
                prop.options = [x for x in same_day if x is not best]
                prop.summary = f"Free inside the requested window: picked {fmt_range(best.start, best.end)}."
                self._soft_notes(prop, best.soft_conflicts)
                return
            if same_day:
                prop.decision, prop.options = "offer", same_day
                prop.summary = f"Found {len(same_day)} common slots on {fmt_date(p.date)}."
                return
            if not self.engine.is_meeting_day(p.date):
                prop.private_notes.append(f"{fmt_date(p.date)} is not a meeting day ({self.engine.day_kind(p.date)}).")
            prop.options = self.engine.find_common_slots(pids, p.date + timedelta(days=1), p.date + timedelta(days=days),
                                                         dur, window=window, exclude_meeting_ids=exclude) or \
                self.engine.find_common_slots(pids, p.date + timedelta(days=1), p.date + timedelta(days=days), dur,
                                              exclude_meeting_ids=exclude)
            prop.decision = "offer" if prop.options else "no_slots"
            prop.summary = f"No common slot on {prop.requested}. " + (
                "Offering the nearest days." if prop.options else "Nothing in the next days either.")
            if not prop.options:
                self._group_fallback(prop, p.date, p.date + timedelta(days=days), window=window)
            return

        # 3) no date: availability question or open request
        start = now.date()
        if p.urgent:
            prop.requested = "as soon as possible"
            urgent = self.engine.find_common_slots(pids, start, start + timedelta(days=1), dur, allow_soft=True,
                                                   exclude_meeting_ids=exclude)
            if urgent:
                best = min(urgent, key=lambda x: (x.score, x.start))
                prop.decision, prop.chosen, prop.options = "accept", best, [x for x in urgent if x is not best]
                prop.summary = f"Urgent: best common slot soon is {fmt_range(best.start, best.end)}."
                self._soft_notes(prop, best.soft_conflicts)
                return
        end = start + timedelta(days=days)
        if p.range_start:  # "this week" / "next week"
            start, end = max(p.range_start, now.date()), p.range_end
            prop.requested = f"{fmt_date(start)} to {fmt_date(end)}"
        prop.options = self.engine.find_common_slots(pids, start, end, dur, window=window, exclude_meeting_ids=exclude)
        prop.decision = "offer" if prop.options else "no_slots"
        prop.summary = (f"Found {len(prop.options)} common slots between {fmt_date(start)} and {fmt_date(end)}."
                        if prop.options else "No common slot in that period.")
        if not prop.options:
            self._group_fallback(prop, start, end, window=window)

    def _group_fallback(self, prop: Proposal, start, end, preferred=None, window=None) -> bool:
        """No time works for everyone in a 3+ person meeting: offer times that miss exactly one optional member
        (never the person asking or the person replying)."""
        optional = [p for p in prop.participants if p not in (prop.requester_id, prop.responder_id)]
        if len(prop.participants) < 3 or not optional:
            return False
        exclude = [prop.meeting_id] if prop.meeting_id else []
        cands = []
        for m in optional:
            rest = [p for p in prop.participants if p != m]
            for sl in self.engine.find_common_slots(rest, start, end, prop.duration_min, preferred_start=preferred,
                                                    window=window, max_results=2, exclude_meeting_ids=exclude):
                sl.missing = [m]
                sl.score += 30
                cands.append(sl)
        cands.sort(key=lambda x: (x.score, x.start))
        chosen = []
        for c in cands:
            if not any(c.start == x.start for x in chosen):
                chosen.append(c)
            if len(chosen) >= self.engine.max_suggestions:
                break
        if not chosen:
            return False
        prop.options = sorted(chosen, key=lambda x: x.start)
        prop.decision = "offer"
        prop.private_notes.append("No time works for the whole group; each option leaves out one person.")
        prop.summary += " No time fits everyone, so the options each miss one member."
        return True

    def _attending(self, prop: Proposal) -> list[str]:
        missing = prop.chosen.missing if prop.chosen else []
        return [p for p in prop.participants if p not in missing]

    def _handle_option(self, prop: Proposal, offer: dict, p: ParsedRequest) -> None:
        opts = [Slot(datetime.fromisoformat(o[0]), datetime.fromisoformat(o[1]), missing=list(o[2]) if len(o) > 2 else [])
                for o in offer["options"]]
        idx = (p.option_choice - 1) if p.option_choice else self._match_offer(offer, p) - 1
        prop.offer_id, prop.meeting_id = offer["id"], offer.get("meeting_id")
        prop.title, prop.duration_min = offer["title"], offer["duration_min"]
        prop.participants = offer["participants"]
        if not (0 <= idx < len(opts)):
            prop.decision, prop.options = "offer", opts
            prop.summary = "The reply picks an option number that wasn't offered. Re-sending the options."
            return
        slot = opts[idx]
        attending = [p for p in prop.participants if p not in slot.missing]
        chk = self.engine.check_slot(attending, slot.start, slot.end,
                                     exclude_meeting_ids=[prop.meeting_id] if prop.meeting_id else [],
                                     ignore_offers=[offer["id"]])
        prop.requested = f"option {idx + 1} ({fmt_range(slot.start, slot.end)})"
        if chk["ok"]:
            prop.decision = "accept"
            prop.chosen = Slot(slot.start, slot.end, 0, [f"picked option {idx + 1}"], chk["soft"], missing=slot.missing)
            prop.summary = f"They picked option {idx + 1}. It was held for them, booking it."
            return
        prop.private_notes.append("That option is no longer free; offering fresh times.")
        prop.options = self.engine.find_common_slots(prop.participants, slot.start.date(),
                                                     slot.start.date() + timedelta(days=self.engine.search_days),
                                                     prop.duration_min, preferred_start=slot.start,
                                                     ignore_offers=[offer["id"]])
        prop.decision = "offer" if prop.options else "no_slots"
        prop.summary = "The picked option got taken in the meantime. New options found." if prop.options else \
            "The picked option got taken and nothing else is free."

    def _handle_leave(self, prop: Proposal, mail: Mail, p: ParsedRequest) -> None:
        """Leave goes parent mail -> VTOP application -> proctor approval. Mails only feed that process."""
        sender = self.portal.get_person(mail.from_id)
        prop.participants = [mail.from_id, prop.responder_id]
        if sender.role == "parent":
            case = self.leave.on_parent_mail(mail, p)
            prop.decision, prop.leave = "leave_parent", case
            ward = self.portal.get_person(sender.ward_id) if sender.ward_id else None
            prop.summary = (f"Parent's mail for {ward.name if ward else 'their ward'}'s leave"
                            + (f" ({self.leave.describe(case)}). Recorded as step 1 of 3." if case else
                               ". No date found - ask the parent for the dates."))
        elif sender.role == "student":
            cases = [c for c in self.leave.for_student(sender.id) if c["status"] in ("awaiting_application", "pending")]
            case = None
            if p.date:
                end = p.end_date or p.date
                case = next((c for c in cases if c["start_date"] <= end.isoformat() and p.date.isoformat() <= c["end_date"]), None)
            case = case or (cases[0] if cases else None)
            prop.decision, prop.leave = "leave_info", case
            prop.summary = "Student asked for leave by mail. Leave is applied on VTOP, so the reply explains what's still missing."
        else:
            prop.summary = "Leave mail from someone who isn't a student or parent."
            prop.decision = "not_meeting"

    def _handle_cancel(self, prop: Proposal, mail: Mail) -> None:
        m = self._existing_meeting(prop.requester_id, prop.responder_id, mail.thread_id)
        if not m:
            prop.decision = "nothing_to_cancel"
            prop.summary = "Asked to cancel, but there's no upcoming meeting between you two."
            return
        prop.decision, prop.meeting_id, prop.title = "cancel", m.id, m.title
        prop.chosen = Slot(m.start, m.end)
        prop.summary = f"Cancel {m.title} on {fmt_range(m.start, m.end)}."

    # ================================================================ helpers
    def _open_offer(self, thread_id: str, offered_by: str, offered_to: str) -> dict | None:
        for o in reversed(self.store.offers()):
            if o["thread_id"] == thread_id and o["offered_by"] == offered_by and o["offered_to"] == offered_to \
                    and o["status"] == "open":
                return o
        return None

    def _match_offer(self, offer: dict, p: ParsedRequest) -> int:
        """If they replied with a time instead of a number, find which option it is (1-based, 0 = none)."""
        for i, (s, _e) in enumerate(offer["options"], start=1):
            st = datetime.fromisoformat(s)
            if p.start and st.time() == p.start and (p.date is None or p.date == st.date()):
                return i
        return 0

    def _existing_meeting(self, a: str, b: str, thread_id: str | None) -> Meeting | None:
        now = self.clock()
        ms = [m for m in self.store.meetings(a) if b in m.participant_ids and m.status == "confirmed" and m.end > now]
        same = [m for m in ms if thread_id and m.thread_id == thread_id]
        return (same or ms or [None])[0]

    def _explain_conflicts(self, prop: Proposal, chk: dict) -> None:
        for prob in chk["problems"]:
            prop.private_notes.append(f"Requested time: {prob}")
        seen = set()
        for c in chk["hard"]:
            if c["person_id"] == prop.responder_id:
                prop.private_notes.append(f"You're busy: {c['title']} ({fmt_time(c['start'])}-{fmt_time(c['end'])})")
            elif c["person_id"] not in seen:
                who = self.portal.get_person(c["person_id"])
                prop.private_notes.append(f"{who.name if who else c['person_id']} is busy then")
                seen.add(c["person_id"])

    def _soft_notes(self, prop: Proposal, softs: list[dict]) -> None:
        for s in softs:
            who = self.portal.get_person(s["person_id"])
            prop.private_notes.append(
                f"{who.name if who else s['person_id']} would miss {s['title']} - attendance "
                f"{s.get('percent')}% -> {s.get('percent_if_missed')}% ({s.get('level')})")

    def _location(self, pids: list[str]) -> str:
        for pid in pids:
            p = self.portal.get_person(pid)
            if p and p.role == "faculty" and p.cabin:
                return p.cabin
        return "To be decided"

    def _book(self, prop: Proposal, mail: Mail, now: datetime) -> Meeting:
        loc = self._location(prop.participants)
        if prop.meeting_id and self.store.get_meeting(prop.meeting_id):
            m = self.store.get_meeting(prop.meeting_id)
            m.history.append({"at": now.isoformat(timespec="minutes"), "action": "moved",
                              "from": fmt_range(m.start, m.end), "by": prop.requester_id})
            m.start, m.end, m.status = prop.chosen.start, prop.chosen.end, "confirmed"
            m.participant_ids = self._attending(prop)
        else:
            m = Meeting(self.store.new_id("MT"), prop.title, prop.requester_id, self._attending(prop),
                        prop.chosen.start, prop.chosen.end, "confirmed", loc, mail.id, mail.thread_id,
                        [{"at": now.isoformat(timespec="minutes"), "action": "booked", "by": prop.responder_id}])
        self.store.upsert_meeting(m)
        self.reminders.sync_calendar(m)
        self.reminders.schedule(m)
        return m

    # ================================================================ drafting
    def _draft(self, prop: Proposal) -> None:
        mail = self.mail.get(prop.mail_id)
        me = self.portal.get_person(prop.responder_id)
        them = self.portal.get_person(prop.requester_id)
        prop.reply_subject = mail.subject if mail.subject.lower().startswith("re:") else f"Re: {mail.subject}"
        body = self._template(prop, me, them)
        prop.reply_body, prop.drafted_by = body, "template"
        if body and self.llm.enabled:
            polished = self._polish(body, prop)
            if polished:
                prop.reply_body, prop.drafted_by = polished, "deepseek"

    def _template(self, prop: Proposal, me, them) -> str:
        hi = (f"Dear {them.name}," if them.role == "faculty" else f"Hi {salutation(them)},") if them else "Hello,"
        if len([p for p in prop.participants if p != prop.responder_id]) > 1:
            hi = "Hi all,"
        sign = me.name + (f"\n{me.reg_no}" if me and me.reg_no else "")
        d = prop.decision
        if d == "not_meeting":
            return ""
        if d == "accept":
            when = fmt_range(prop.chosen.start, prop.chosen.end)
            loc = self._location(prop.participants)
            opening = {"option_choice": "Great, booked.", "reschedule": "Sure, let's move it."}.get(prop.parsed.intent, "Yes, that works.")
            extra = ""
            if prop.chosen.missing:
                extra += (f"\n{', '.join(salutation(self.portal.get_person(x)) for x in prop.chosen.missing)} "
                          f"can't make this time, so please share the notes with them.\n")
            mine = [s for s in prop.chosen.soft_conflicts if s["person_id"] == prop.requester_id]
            if mine:
                extra += (f"\nNote: this overlaps your {mine[0]['title']} class. Your attendance allows it "
                         f"({mine[0].get('percent')}% -> {mine[0].get('percent_if_missed')}%).\n")
            return (f"{hi}\n\n{opening} See you on {when}" + (f" at {loc}" if loc != "To be decided" else "") +
                    f". I've added it to the calendar and you'll get a reminder before it.\n{extra}\nThanks,\n{sign}")
        if d == "offer":
            def miss(sl):
                return f" (everyone except {', '.join(salutation(self.portal.get_person(x)) for x in sl.missing)})" \
                    if sl.missing else ""
            lines = "\n".join(f"Option {i}: {fmt_range(s.start, s.end)}{miss(s)}" for i, s in enumerate(prop.options, 1))
            public = [PUBLIC_PROBLEMS[x.split(': ', 1)[-1]] for x in prop.private_notes
                      if x.split(': ', 1)[-1] in PUBLIC_PROBLEMS]
            us = "all of us" if len(prop.participants) > 2 else "both of us"
            if prop.offer_id and prop.summary.startswith("The reply picks an option number"):
                lead = "I couldn't match that to an option. These are the times I offered:"
            elif prop.offer_id:
                lead = f"Sorry, that time just got taken. These are free for {us}:"
            elif prop.requested and prop.parsed.start:
                lead = (f"{prop.requested} doesn't work for everyone" if len(prop.participants) > 2 else
                        f"I'm not free at {prop.requested}") + (f" ({public[0]})" if public else "") + \
                    f". These times work for {us}:"
            elif prop.parsed.range_start and not prop.parsed.date:
                lead = f"These times between {prop.requested.replace(' to ', ' and ')} work for {us}:"
            elif prop.requested and prop.options and prop.options[0].start.date() == prop.parsed.date:
                lead = f"Here's when I'm free on {fmt_date(prop.parsed.date)}:"
            elif prop.requested and prop.parsed.date:
                lead = (f"{prop.requested} doesn't work for everyone" if len(prop.participants) > 2 else
                        f"I'm not free on {prop.requested}") + (f" ({public[0]})" if public else "") + \
                    f". The nearest times that work for {us}:"
            else:
                lead = f"Here are times that work for {us}:"
            hold = f" I'll keep these free for {self.hold_hours:g} hours." if self.hold_hours else ""
            return (f"{hi}\n\n{lead}\n\n{lines}\n\nReply with the option number that suits you "
                    f"(for example \"Option 1 works\") and I'll book it.{hold}\n\nThanks,\n{sign}")
        if d == "cancel":
            return (f"{hi}\n\nNo problem. I've cancelled our meeting '{prop.title}' on "
                    f"{fmt_range(prop.chosen.start, prop.chosen.end)}.\n\nThanks,\n{sign}")
        if d == "leave_parent":
            ward = self.portal.get_person(them.ward_id) if them and them.ward_id else None
            wn = ward.name if ward else "your ward"
            case = prop.leave
            if not case:
                return (f"Dear {them.name},\n\nThank you for your mail. Could you tell me the dates of the leave?"
                        f"\n\nRegards,\n{sign}")
            tail = ("I can see the leave application on VTOP and will review it." if case.get("applied_at") else
                    f"{salutation(ward) if ward else 'Your ward'} also needs to apply for the leave on VTOP, and I'll approve it there.")
            return (f"Dear {them.name},\n\nThank you for letting me know. Noted that {wn} will be on leave "
                    f"{self.leave.describe(case)} ({case['reason']}). {tail}\n\nRegards,\n{sign}")
        if d == "leave_info":
            case = prop.leave
            if not case:
                need = "Please apply for the leave on VTOP, and ask your parent to mail me as well."
            elif case.get("applied_at") and case.get("parent_mail_id"):
                need = "I have your VTOP application and your parent's mail. I'll review it and you'll hear back soon."
            elif case.get("applied_at"):
                need = "I have your VTOP application. I still need a mail from your parent before I can approve it."
            elif case.get("parent_mail_id"):
                need = "I've received your parent's mail. Please apply for the leave on VTOP so I can approve it."
            else:
                need = "Please apply for the leave on VTOP, and ask your parent to mail me as well."
            return f"{hi}\n\n{need}\n\nThanks,\n{sign}"
        if d == "nothing_to_cancel":
            return f"{hi}\n\nI couldn't find an upcoming meeting between us to cancel. Could you tell me which one you mean?\n\nThanks,\n{sign}"
        if d == "no_slots":
            return (f"{hi}\n\nI couldn't find a time that works for both of us in the next "
                    f"{self.engine.search_days} days. Could you suggest a time next week?\n\nThanks,\n{sign}")
        return ""

    def _polish(self, body: str, prop: Proposal) -> str | None:
        system = ("You rewrite short scheduling emails between university students and faculty so they read "
                  "naturally and politely. Keep it short. Keep EVERY date, time, option number and room exactly as "
                  "written. Do not add new facts, times, reasons, places or promises. Do not explain why anyone is busy. "
                  "Return only the email text.")
        try:
            out = self.llm.text(system, body, temperature=0.3)
        except Exception:
            return None
        must = set(re.findall(r"\b\d{2}:\d{2}\b", body)) | set(re.findall(r"Option \d", body))
        if not out or any(x not in out for x in must) or len(out) > 3 * len(body) + 200:
            return None
        if set(re.findall(r"\b\d{2}:\d{2}\b", out)) - set(re.findall(r"\b\d{2}:\d{2}\b", body)):
            return None  # model introduced a time that wasn't in the draft
        return out
