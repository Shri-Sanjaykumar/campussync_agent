"""CampusSync service: wires adapters + core + agent together. The UI (and any other
front end: a Teams bot, a mail daemon, a REST API) only talks to this class."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .adapters.registry import build_adapters
from .agent.assistant import Assistant
from .agent.mail_handler import MailHandler
from .config import load_settings
from .adapters.notifier import NotificationService
from .core.analytics import department_table, faculty_stats
from .adapters.gmail_mailbox import GmailMailbox
from .core.availability import AvailabilityEngine
from .core.directory import Directory
from .core.leave import LeaveDesk
from .core.proctor import ProctorDesk
from .core.triage import Triage
from .core.office_hours import OfficeHours, OfficeHoursError
from .core.timetable_import import TimetableOverlay, parse_timetable_text
from .ml.intent import IntentModel
from .core.clashes import ClashDetector
from .core.reminders import ReminderService, meeting_to_ics, parse_ics
from .core.reschedule import Rescheduler
from .llm.client import LLMClient
from .llm.parser import MailParser
from .models import Commitment, Event, Mail, Meeting
from .rag.store import Doc, KnowledgeBase
from .store import StateStore
from .utils import WEEKDAY_FULL, combine, fmt_dt, fmt_range, fmt_time


class CampusSync:
    def __init__(self, config_path: str | None = None):
        self.settings = load_settings(config_path)
        try:
            self.tz = ZoneInfo(self.settings.get("institution.timezone", "Asia/Kolkata"))
        except Exception:  # Windows without the tzdata package
            from datetime import timezone
            self.tz = timezone(timedelta(hours=5, minutes=30))
        self.store = StateStore(self.settings.path_of("state"))
        base_portal, self.mail, self.calendar = build_adapters(self.settings, self.store)
        self.base_portal = base_portal
        self.directory = Directory(self.store, base_portal)
        # pasted timetables override the portal's; real Gmail senders resolve to profiles or outside contacts
        self.portal = TimetableOverlay(base_portal, self.store, self.directory)
        self.gmail = self.mail if isinstance(self.mail, GmailMailbox) else None
        if self.gmail:
            self.gmail.directory = self.directory
            self.gmail.tz = self.tz
            self.gmail.prefs_of = self.store.prefs
        self.llm = LLMClient(self.settings)
        self.intent_model = IntentModel(self.settings.root / "data" / "models" / "intent_mlp.joblib") \
            if self.settings.get("ml.use_intent_model", True) else None
        self.parser = MailParser(self.llm, self.intent_model)
        self.notifier = NotificationService(self.settings, self.store, self.now)
        self.engine = AvailabilityEngine(self.portal, self.store, self.calendar, self.settings, self.now)
        self.reminders = ReminderService(self.store, self.mail, self.calendar, self.portal, self.settings, self.now,
                                         self.notifier)
        self.clashes = ClashDetector(self.engine, self.store, self.portal, self.now)
        self.rescheduler = Rescheduler(self.engine, self.store, self.mail, self.reminders, self.portal, self.now,
                                       self.notifier)
        self.leave = LeaveDesk(self.engine, self.store, self.portal, self.mail, self.notifier, self.rescheduler,
                               self.reminders, self.settings, self.now)
        self.handler = MailHandler(self.engine, self.store, self.mail, self.reminders, self.portal, self.parser,
                                   self.llm, self.settings, self.now, self.notifier, self.leave)
        self.triage = Triage(self.store, self.mail, self.portal, self.llm, self.now, self.settings)
        self.proctor = ProctorDesk(self.engine, self.store, self.portal, self.mail, self.reminders, self.notifier,
                                   self.settings, self.now)
        self.office_hours = OfficeHours(self.engine, self.store, self.portal, self.reminders, self.mail, self.notifier,
                                        self.settings, self.now)
        self.assistant = Assistant(self)
        self.kb = KnowledgeBase(self.settings)
        self._kb_version = None

    # ---------------------------------------------------------------- clock
    def now(self) -> datetime:
        fixed = self.store.get_setting("clock")
        if fixed:
            return datetime.fromisoformat(fixed)
        return datetime.now(self.tz).replace(tzinfo=None, second=0, microsecond=0)

    def set_clock(self, dt: datetime | None) -> None:
        self.store.set_setting("clock", dt.isoformat(timespec="minutes") if dt else None)

    @property
    def auto_mode(self) -> bool:
        return bool(self.store.get_setting("auto_mode", False))

    def set_auto_mode(self, on: bool) -> None:
        self.store.set_setting("auto_mode", bool(on))

    # ---------------------------------------------------------------- people
    def people(self, role: str | None = None):
        ps = self.portal.list_people()
        return [p for p in ps if role is None or p.role == role]

    def person(self, pid: str):
        return self.portal.get_person(pid)

    # ---------------------------------------------------------------- mail
    # ---------------------------------------------------------------- Gmail
    @property
    def live_mail(self) -> bool:
        return bool(self.gmail and self.gmail.live)

    @property
    def owner_id(self) -> str | None:
        return self.directory.owner_id if self.live_mail else None

    def sync_gmail(self, force: bool = False, min_gap_s: int = 60) -> dict:
        """Pull new Gmail mail. Runs the same steps as an in-app mail (deadlines, parent leave mails),
        and replies automatically when Auto mode is on."""
        if not self.live_mail or not self.directory.owner:
            return {"new": [], "auto": []}
        last = self.gmail.last_sync
        if not force and last and (datetime.now() - last).total_seconds() < min_gap_s:
            return {"new": [], "auto": []}
        new = self.gmail.sync()
        self.triage.classify_many(new)  # one DeepSeek call per batch of new mail
        auto = []
        for m in new:
            self.after_send(m)
            if self.auto_mode and m.status == "unread" and self.is_request(m):
                try:
                    prop = self.handler.process(m.id, self.directory.owner_id)
                    if prop.decision != "not_meeting":
                        res = self.handler.confirm(prop)
                        auto.append({"mail": m, "decision": prop.decision, "summary": prop.summary, "result": res})
                except Exception as e:
                    auto.append({"mail": m, "error": str(e)})
        return {"new": new, "auto": auto}

    def link_contact(self, email: str, pid: str | None) -> None:
        old, new = self.directory.link(email, pid)
        if self.gmail:
            self.gmail.relink(old, new)

    def after_send(self, m: Mail) -> None:
        """Runs on every new human mail: pull deadlines into calendars, record parents' leave mails."""
        self.triage.capture_deadlines(m)
        sender = self.person(m.from_id)
        if sender and sender.role == "parent" and self.triage.classify(m)["category"] == "leave":
            self.leave.on_parent_mail(m, self.parser.parse(m.subject, m.body, m.sent_at))

    def send_mail(self, from_id: str, to_ids: list[str], subject: str, body: str,
                  in_reply_to: str | None = None) -> dict:
        thread = ""
        if in_reply_to:
            parent = self.mail.get(in_reply_to)
            thread = parent.thread_id if parent else ""
            if parent and not subject:
                subject = parent.subject if parent.subject.lower().startswith("re:") else f"Re: {parent.subject}"
        m = self.mail.send(Mail("", from_id, to_ids, subject, body, self.now(), thread_id=thread,
                                in_reply_to=in_reply_to, kind="human"))
        self.after_send(m)
        auto = []
        if self.auto_mode:
            for rid in to_ids:
                try:
                    prop = self.handler.process(m.id, rid)
                    res = self.handler.confirm(prop)
                    auto.append({"responder": rid, "decision": prop.decision, "summary": prop.summary, "result": res})
                except Exception as e:
                    auto.append({"responder": rid, "error": str(e)})
        return {"mail": m, "auto": auto}

    def process_mail(self, mail_id: str, responder_id: str):
        return self.handler.process(mail_id, responder_id)

    def confirm(self, proposal, body: str | None = None):
        return self.handler.confirm(proposal, body)

    def is_request(self, m) -> bool:
        """A meeting or leave mail written to this person (not a group notice): the agent may answer it."""
        t = self.triage.classify(m)
        return bool(t["action"]) and t["category"] in ("meeting", "leave")

    def visible_inbox(self, pid: str) -> list:
        """The inbox without promotions."""
        return [m for m in self.mail.inbox(pid) if not self.triage.hidden(self.triage.peek(m))]

    def process_all_unread(self, responder_id: str) -> list[dict]:  # noqa: C901
        out = []
        for m in self.mail.inbox(responder_id):
            if m.status != "unread" or m.kind != "human" or not self.is_request(m):
                continue
            prop = self.handler.process(m.id, responder_id)
            try:
                res = self.handler.confirm(prop)
                out.append({"mail": m, "decision": prop.decision, "summary": prop.summary, "result": res})
            except Exception as e:
                out.append({"mail": m, "decision": prop.decision, "summary": f"failed: {e}"})
        return out

    # ---------------------------------------------------------------- commitments
    def add_commitment(self, person_id: str, title: str, start: datetime, end: datetime, category: str = "unofficial",
                       recurrence: str | None = None, until: date | None = None, notes: str = "") -> tuple[Commitment, list[dict]]:
        if end <= start:
            raise ValueError("End time must be after start time")
        c = Commitment(self.store.new_id("C"), person_id, title, start, end, category, recurrence, until, notes, self.now())
        self.store.upsert_commitment(c)
        clashes = self.clashes.check(c)
        self.clashes.raise_alerts(c, clashes)
        return c, clashes

    def add_commitment_from_text(self, person_id: str, text: str):
        parsed = self.parser.parse_commitment(text, self.now())
        if not parsed:
            raise ValueError("Couldn't find a day and time in that. Try e.g. 'Club practice every Tue 5-6:30pm'.")
        return self.add_commitment(person_id, parsed["title"], parsed["start"], parsed["end"], parsed["category"],
                                   parsed["recurrence"], notes=f"from text: {text}")

    def import_ics(self, person_id: str, text: str) -> list[tuple[Commitment, list[dict]]]:
        out = []
        for e in parse_ics(text):
            out.append(self.add_commitment(person_id, e["title"], e["start"], e["end"], "official", notes="imported .ics"))
        return out

    def delete_commitment(self, cid: str) -> None:
        self.store.delete_commitment(cid)

    def reschedule_clashing_meetings(self, person_id: str, commitment: Commitment, meeting_ids: list[str]):
        ms = [self.store.get_meeting(mid) for mid in dict.fromkeys(meeting_ids) if mid]
        items = self.rescheduler.plan([m for m in ms if m])
        return self.rescheduler.apply(items, person_id, reason=commitment.title), items

    # ---------------------------------------------------------------- unavailability / mass reschedule
    def plan_unavailability(self, person_id: str, start: datetime, end: datetime, reason: str):
        return self.rescheduler.plan_unavailability(person_id, start, end, reason)

    def apply_unavailability(self, person_id: str, start: datetime, end: datetime, reason: str, items) -> dict:
        self.store.upsert_commitment(Commitment(self.store.new_id("C"), person_id, reason or "Unavailable", start, end,
                                                "official", notes="marked unavailable", created_at=self.now()))
        return self.rescheduler.apply(items, person_id, reason)

    # ---------------------------------------------------------------- meetings: cancel / request
    def cancel_meeting(self, meeting_id: str, by: str, notify: bool = True) -> dict:
        m = self.store.get_meeting(meeting_id)
        if not m or m.status == "cancelled":
            raise ValueError("No such active meeting")
        m.status = "cancelled"
        m.history.append({"at": self.now().isoformat(timespec="minutes"), "action": "cancelled", "by": by})
        self.store.upsert_meeting(m)
        self.reminders.sync_calendar(m)
        self.reminders.cancel(m.id)
        who = self.person(by).name if self.person(by) else by
        if notify:
            for pid in m.participant_ids:
                if pid == by:
                    continue
                self.mail.send(Mail("", by, [pid], f"Cancelled: {m.title}",
                                    f"{who} cancelled '{m.title}' on {fmt_range(m.start, m.end)}.\n\n(Sent by CampusSync)",
                                    self.now(), thread_id=m.thread_id or "", kind="notification", meta={"meeting_id": m.id}))
                self.notifier.push(pid, f"Cancelled: {m.title} ({fmt_range(m.start, m.end)}) by {who}")
        out = {"meeting": m, "waitlist": None}
        if m.kind == "office_hours":
            out["waitlist"] = self.office_hours.on_cancel(m)
        return out

    def request_meeting(self, from_id: str, to_ids: list[str], start: datetime, end: datetime, title: str) -> dict:
        """Send a request for one exact time (used by the Ask chat). The other side's agent checks it
        and books it, so nobody gets a meeting forced on them."""
        dur = int((end - start).total_seconds() // 60)
        body = (f"Hi, can we meet on {fmt_range(start, end)} ({dur} minutes) about {title}?\n\n"
                f"(Requested through CampusSync - this time is free for both of us.)")
        return self.send_mail(from_id, to_ids, title, body)

    # ---------------------------------------------------------------- preferences
    def prefs(self, pid: str) -> dict:
        return self.store.prefs(pid)

    def set_prefs(self, pid: str, **prefs) -> None:
        self.store.set_prefs(pid, prefs)

    # ---------------------------------------------------------------- timetable import
    def preview_timetable(self, text: str):
        return parse_timetable_text(text, getattr(self.base_portal, "grid", None))

    def import_timetable(self, pid: str, text: str) -> dict:
        rows, problems = self.preview_timetable(text)
        if not rows:
            raise ValueError("No courses found. Each line needs a course code and a slot (e.g. SWE3001 ... A1+TA1).")
        self.store.set_imported_timetable(pid, rows)
        alerts = []
        for c in self.store.commitments(pid):
            cl = self.clashes.check(c)
            self.clashes.raise_alerts(c, [x for x in cl if x["kind"] == "class"])
            alerts += [x for x in cl if x["kind"] == "class"]
        for m in self.store.meetings(pid):
            if m.end < self.now():
                continue
            chk = self.engine.check_slot([pid], m.start, m.end, exclude_meeting_ids=[m.id])
            for h in chk["hard"]:
                if h["kind"] == "class":
                    msg = f"Imported timetable: {h['title']} clashes with meeting '{m.title}' on {fmt_range(m.start, m.end)}"
                    from .models import Alert
                    self.store.add_alert(Alert(self.store.new_id("A"), pid, self.now(), "warning", msg, {"meeting_id": m.id}))
                    alerts.append({"with": m.title})
        return {"courses": len(rows), "problems": problems, "clashes": len(alerts)}

    def clear_imported_timetable(self, pid: str) -> None:
        self.store.set_imported_timetable(pid, None)

    # ---------------------------------------------------------------- leave (parent mail -> VTOP application -> proctor)
    def apply_leave(self, sid: str, start: date, end: date, reason: str, start_time=None, end_time=None) -> dict:
        return self.leave.apply_vtop(sid, start, end, reason, start_time, end_time)

    def approve_leave(self, rid: str, approver_id: str, note: str = "", without_parent_mail: bool = False) -> dict:
        return self.leave.approve(rid, approver_id, note, without_parent_mail)

    def reject_leave(self, rid: str, approver_id: str, note: str = "") -> dict:
        return self.leave.reject(rid, approver_id, note)

    # ---------------------------------------------------------------- daily summary
    def digest(self, pid: str) -> dict:
        now = self.now()
        p = self.person(pid)
        since = now - timedelta(days=1)
        inbox = [m for m in self.mail.inbox(pid) if m.sent_at >= since or m.status == "unread"]
        self.triage.classify_many(inbox)
        groups: dict[str, list] = {}
        for m in inbox:
            t = self.triage.classify(m)
            if self.triage.hidden(t):
                continue
            groups.setdefault(t["category"], []).append((m, t))
        action = [(m, t) for m, t in sum(groups.values(), []) if t["action"] and m.status == "unread" and m.kind == "human"]
        deadlines = [d for d in self.triage.deadlines(pid) if datetime.fromisoformat(d["due"]) <= now + timedelta(days=7)]
        today_meetings = [m for m in self.store.meetings(pid) if m.start.date() == now.date() and m.end >= now]
        pending_leave = self.leave.for_approver(pid, "pending") if p.role == "faculty" else []
        risky = [r for r in self.proctor.overview(pid) if r["level"] == "high" and not r["next"]] if p.role == "faculty" else []
        lines = [f"Summary for {p.name}, {fmt_dt(now)}", ""]
        if groups:
            counts = ", ".join(f"{len(v)} {self.triage.label(k).lower()}" for k, v in groups.items())
            lines.append(f"Mail in the last day: {counts}.")
        if action:
            lines.append("\nNeeds your reply:")
            lines += [f"- {self.person(m.from_id).name if self.person(m.from_id) else m.from_id}: {t['summary']}" for m, t in action[:8]]
        if pending_leave:
            lines.append(f"\nLeave applications waiting for you: {len(pending_leave)}")
        if risky:
            lines.append("\nProctees at risk with no check-in booked: " + ", ".join(r["student"].name for r in risky))
        if deadlines:
            lines.append("\nDeadlines in the next 7 days:")
            lines += [f"- {fmt_dt(datetime.fromisoformat(d['due']))}: {d['title']}" for d in deadlines]
        if today_meetings:
            lines.append("\nLater today:")
            lines += [f"- {fmt_range(m.start, m.end)}: {m.title} ({m.location})" for m in today_meetings]
        if len(lines) == 2:
            lines.append("Nothing new. Enjoy the quiet.")
        return {"text": "\n".join(lines), "groups": groups, "action": action, "deadlines": deadlines,
                "today": today_meetings, "pending_leave": pending_leave, "risky": risky}

    def send_digest(self, pid: str) -> Mail:
        d = self.digest(pid)
        m = self.mail.send(Mail("", "agent", [pid], f"Your CampusSync summary, {fmt_dt(self.now())}", d["text"], self.now(),
                                kind="notification", meta={"digest": True}))
        self.notifier.push(pid, d["text"][:900])
        return m

    # ---------------------------------------------------------------- dashboard
    def faculty_stats(self, fid: str, start: date, end: date) -> dict:
        return faculty_stats(self, fid, start, end)

    def department_table(self, start: date, end: date) -> list[dict]:
        return department_table(self, start, end)

    # ---------------------------------------------------------------- reminders / export
    def dispatch_reminders(self):
        return self.reminders.dispatch_due(self.now())

    def ics_for(self, meeting_id: str) -> str:
        return meeting_to_ics(self.store.get_meeting(meeting_id), self.portal, self.settings.get("institution.timezone"))

    # ---------------------------------------------------------------- ask
    def ask(self, user_id: str, question: str, history=None) -> dict:
        return self.assistant.answer(question, user_id, history)

    # ---------------------------------------------------------------- knowledge base (RAG)
    def knowledge(self) -> KnowledgeBase:
        key = (self.store.version, self.now().date())
        if self._kb_version != key:
            self.kb.build(self._build_docs())
            self._kb_version = key
        return self.kb

    def _build_docs(self) -> list[Doc]:
        docs: list[Doc] = []
        today = self.now().date()
        for p in self.portal.list_people():
            classes = self.portal.weekly_classes(p.id)
            lines = [f"{WEEKDAY_FULL[c.weekday]} {fmt_time(c.start)}-{fmt_time(c.end)} {c.course_code} {c.course_title}"
                     f" (slot {c.slot}, {c.venue})" for c in classes]
            vis = "public" if p.role == "faculty" else [p.id, "faculty"]
            if lines:
                docs.append(Doc(f"tt-{p.id}", f"Weekly timetable of {p.name} ({p.role})", "; ".join(lines), "timetable", vis,
                                {"person_id": p.id}))
            oh = self.portal.office_hours(p.id)
            if oh:
                docs.append(Doc(f"oh-{p.id}", f"Cabin hours of {p.name}",
                                f"{p.name} is usually available in {p.cabin or 'their cabin'} during: " +
                                "; ".join(f"{WEEKDAY_FULL[w.weekday]} {fmt_time(w.start)}-{fmt_time(w.end)}" for w in oh),
                                "office_hours", "public", {"person_id": p.id}))
            if p.role == "student":
                g = self.portal.get_person(p.guide_id) if p.guide_id else None
                pr = self.portal.get_person(p.proctor_id) if p.proctor_id else None
                docs.append(Doc(f"rel-{p.id}", f"Guide and proctor of {p.name}",
                                f"{p.name} ({p.reg_no}) - project guide: {g.name if g else 'none'}; proctor: {pr.name if pr else 'none'}.",
                                "relations", [p.id, "faculty"]))
                recs = self.portal.attendance(p.id)
                if recs:
                    docs.append(Doc(f"att-{p.id}", f"Attendance of {p.name}",
                                    "; ".join(f"{r.course_code} {r.course_title}: {r.attended}/{r.total}" for r in recs),
                                    "attendance", [p.id]))
        cal = [c for c in self.portal.academic_calendar(today - timedelta(days=7), today + timedelta(days=75))
               if c.kind in ("holiday", "exam", "break")]
        for c in cal:
            docs.append(Doc(f"cal-{c.date}", f"Academic calendar {c.date.isoformat()}",
                            f"{WEEKDAY_FULL[c.date.weekday()]} {c.date.isoformat()}: {c.label} ({c.kind})", "calendar"))
        seen = set()
        for p in self.people("student"):
            for e in self.portal.exams(p.id, today, today + timedelta(days=75)):
                if (e.course_code, e.title) in seen:
                    continue
                seen.add((e.course_code, e.title))
                docs.append(Doc(f"ex-{e.course_code}-{e.start:%Y%m%d}", e.title,
                                f"{e.title} on {fmt_range(e.start, e.end)} at {e.venue}", "exam"))
        kdir = self.settings.path_of("knowledge")
        if kdir.exists():
            for f in sorted(kdir.glob("*.md")):
                for i, chunk in enumerate(f.read_text(encoding="utf-8").split("\n## ")):
                    title = chunk.strip().splitlines()[0].lstrip("# ").strip() if chunk.strip() else f.stem
                    docs.append(Doc(f"kb-{f.stem}-{i}", f"Policy: {title}", chunk.strip(), "policy"))
        for c in self.store.commitments():
            docs.append(Doc(f"cm-{c.id}", f"Commitment: {c.title}",
                            f"{c.title} on {fmt_range(c.start, c.end)}" + (" every week" if c.recurrence else "") +
                            f" ({c.category})", "commitment", [c.person_id]))
        for m in self.store.meetings():
            names = ", ".join(self.portal.get_person(p).name for p in m.participant_ids if self.portal.get_person(p))
            docs.append(Doc(f"mt-{m.id}", f"Meeting: {m.title}", f"{m.title} on {fmt_range(m.start, m.end)} at {m.location} with {names}",
                            "meeting", list(m.participant_ids)))
        return docs

    # ---------------------------------------------------------------- demo data
    def reset_demo(self, anchor: date | None = None) -> dict:
        from .mock.seed import seed_demo
        return seed_demo(self, anchor)
