"""Leave, following the VIT procedure:
  1. the parent mails the proctor,
  2. the student applies for leave on VTOP (mocked here, like the rest of VTOP),
  3. the proctor checks both and approves or rejects.
The agent matches the parent's mail to the VTOP application (same student, overlapping dates), and
works out what the leave costs: classes missed on working days of the academic calendar, attendance
after, exams in the window, meetings to move. On approval the time is blocked, course faculty are told
which classes will be missed, and clashing meetings move automatically."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..models import Commitment, Mail
from ..utils import combine, daterange, fmt_date, fmt_range, fmt_time, salutation


class LeaveDesk:
    def __init__(self, engine, store, portal, mail, notifier, rescheduler, reminders, settings, clock):
        self.engine, self.store, self.portal, self.mail = engine, store, portal, mail
        self.notifier, self.rescheduler, self.reminders, self.clock = notifier, rescheduler, reminders, clock
        self.minimum = float(settings.get("attendance.minimum_percent", 75))
        self.exempt_cgpa = settings.get("attendance.exemption_cgpa")

    # ---------------------------------------------------------------- analysis
    def window(self, req: dict) -> tuple[datetime, datetime]:
        s = datetime.fromisoformat(req["start_date"]).date() if isinstance(req["start_date"], str) else req["start_date"]
        e = datetime.fromisoformat(req["end_date"]).date() if isinstance(req["end_date"], str) else req["end_date"]
        ts = time.fromisoformat(req["start_time"]) if req.get("start_time") else self.engine.day_start
        te = time.fromisoformat(req["end_time"]) if req.get("end_time") else self.engine.day_end
        return combine(s, ts), combine(e, te)

    def analyse(self, sid: str, start_date: date, end_date: date, start_time: time | None = None,
                end_time: time | None = None) -> dict:
        person = self.portal.get_person(sid)
        ts, te = start_time or self.engine.day_start, end_time or self.engine.day_end
        classes = self.portal.weekly_classes(sid)
        cal = self.engine.calendar_days(start_date, end_date)
        missed, units = [], {}
        for d in daterange(start_date, end_date):
            if self.engine.day_kind(d, cal) != "instructional":
                continue
            for c in classes:
                if c.weekday == d.weekday() and c.start < te and ts < c.end:
                    missed.append({"date": d, "start": c.start, "end": c.end, "course_code": c.course_code,
                                   "course_title": c.course_title, "faculty_id": c.faculty_id, "units": c.units})
                    units[c.course_code] = units.get(c.course_code, 0) + c.units
        exempt = bool(self.exempt_cgpa is not None and person.cgpa is not None and person.cgpa >= float(self.exempt_cgpa))
        att = []
        for r in self.portal.attendance(sid):
            u = units.get(r.course_code, 0)
            if not u or not r.total:
                continue
            now_p = round(100 * r.attended / r.total, 1)
            after = round(100 * r.attended / (r.total + u), 1)
            att.append({"course_code": r.course_code, "course_title": r.course_title, "missed": u, "now": now_p,
                        "after": after, "below": after < self.minimum and not exempt})
        exams = [{"title": e.title, "when": fmt_range(e.start, e.end)} for e in self.portal.exams(sid, start_date, end_date)
                 if e.start.time() < te and ts < e.end.time()]
        ws, we = combine(start_date, ts), combine(end_date, te)
        meetings = [m for m in self.store.meetings(sid) if m.status == "confirmed" and m.start < we and ws < m.end
                    and m.end > self.clock()]
        return {"classes": missed, "attendance": att, "exams": exams, "meetings": meetings, "exempt": exempt,
                "below": [a["course_code"] for a in att if a["below"]],
                "days": sum(1 for d in daterange(start_date, end_date) if self.engine.day_kind(d, cal) == "instructional")}

    # ---------------------------------------------------------------- records
    def all(self) -> list[dict]:
        return list(self.store.data["leave_requests"])

    def get(self, rid: str) -> dict | None:
        return next((r for r in self.store.data["leave_requests"] if r["id"] == rid), None)

    def by_mail(self, mail_id: str) -> dict | None:
        return next((r for r in self.store.data["leave_requests"] if r.get("mail_id") == mail_id), None)

    def for_student(self, sid: str) -> list[dict]:
        return sorted([r for r in self.all() if r["student_id"] == sid], key=lambda r: r["created_at"], reverse=True)

    def for_approver(self, fid: str, status: str | None = None) -> list[dict]:
        return sorted([r for r in self.all() if r["approver_id"] == fid and (status is None or r["status"] == status)],
                      key=lambda r: r["created_at"], reverse=True)

    def _new(self, sid: str, start_date: date, end_date: date, reason: str, start_time=None, end_time=None) -> dict:
        stu = self.portal.get_person(sid)
        approver = stu.proctor_id if stu and stu.proctor_id else None
        req = {"id": self.store.new_id("L"), "student_id": sid, "approver_id": approver,
               "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
               "start_time": start_time.strftime("%H:%M") if start_time else None,
               "end_time": end_time.strftime("%H:%M") if end_time else None,
               "reason": reason or "Personal", "status": "awaiting_application",
               "parent_mail_id": None, "parent_mail_at": None, "parent_dates": None,
               "applied_at": None, "mail_id": None,
               "created_at": self.clock().isoformat(timespec="minutes"), "decided_at": None, "note": ""}
        self.store.data["leave_requests"].append(req)
        return req

    def _match(self, sid: str, start_date: date, end_date: date) -> dict | None:
        for r in self.for_student(sid):
            if r["status"] not in ("awaiting_application", "pending"):
                continue
            if date.fromisoformat(r["start_date"]) <= end_date and start_date <= date.fromisoformat(r["end_date"]):
                return r
        return None

    def on_parent_mail(self, mail, parsed) -> dict | None:
        """Step 1: a parent's mail. Recorded against the student (matched to an existing application if any)."""
        parent = self.portal.get_person(mail.from_id)
        if not parent or parent.role != "parent" or not parent.ward_id or not parsed.date:
            return None
        if any(r.get("parent_mail_id") == mail.id for r in self.all()):
            return next(r for r in self.all() if r.get("parent_mail_id") == mail.id)
        end = parsed.end_date or parsed.date
        req = self._match(parent.ward_id, parsed.date, end) or self._new(parent.ward_id, parsed.date, end, parsed.reason,
                                                                          parsed.start, parsed.end)
        req.update(parent_mail_id=mail.id, parent_mail_at=mail.sent_at.isoformat(timespec="minutes"),
                   parent_dates=[parsed.date.isoformat(), end.isoformat()])
        if req["reason"] in ("", "Personal") and parsed.reason:
            req["reason"] = parsed.reason
        self.store.save()
        return req

    def apply_vtop(self, sid: str, start_date: date, end_date: date, reason: str,
                   start_time: time | None = None, end_time: time | None = None) -> dict:
        """Step 2: the student's leave application on VTOP (mock)."""
        if end_date < start_date:
            raise ValueError("The leave ends before it starts")
        req = self._match(sid, start_date, end_date)
        if req and req["applied_at"]:
            raise ValueError("You already have a leave application for these dates")
        if not req:
            req = self._new(sid, start_date, end_date, reason, start_time, end_time)
        req.update(start_date=start_date.isoformat(), end_date=end_date.isoformat(), reason=reason or req["reason"],
                   start_time=start_time.strftime("%H:%M") if start_time else None,
                   end_time=end_time.strftime("%H:%M") if end_time else None,
                   applied_at=self.clock().isoformat(timespec="minutes"), status="pending")
        self.store.save()
        stu = self.portal.get_person(sid)
        if req["approver_id"]:
            self.mail.send(Mail("", "agent", [req["approver_id"]], f"Leave application on VTOP: {stu.name}",
                                f"{stu.name} ({stu.reg_no or stu.id}) applied for leave on VTOP: {self.describe(req)}, "
                                f"reason: {req['reason']}.\nParent mail: {'received' if req['parent_mail_id'] else 'not yet received'}."
                                f"\n\nReview it under Students > Leave requests.", self.clock(), kind="notification", meta={"leave_id": req["id"]}))
        return req

    def checklist(self, req: dict) -> dict:
        pd = req.get("parent_dates")
        match = bool(pd and pd[0] == req["start_date"] and pd[1] == req["end_date"])
        return {"parent_mail": bool(req.get("parent_mail_id")), "applied": bool(req.get("applied_at")),
                "dates_match": match, "ready": bool(req.get("parent_mail_id")) and bool(req.get("applied_at"))}

    def describe(self, req: dict) -> str:
        s, e = req["start_date"], req["end_date"]
        sd, ed = date.fromisoformat(s), date.fromisoformat(e)
        days = fmt_date(sd) if sd == ed else f"{fmt_date(sd)} to {fmt_date(ed)}"
        if req.get("start_time"):
            days += f", {req['start_time']}-{req['end_time'] or fmt_time(self.engine.day_end)}"
        return days

    def analysis_for(self, req: dict) -> dict:
        return self.analyse(req["student_id"], date.fromisoformat(req["start_date"]), date.fromisoformat(req["end_date"]),
                            time.fromisoformat(req["start_time"]) if req.get("start_time") else None,
                            time.fromisoformat(req["end_time"]) if req.get("end_time") else None)

    # ---------------------------------------------------------------- decisions
    def _update(self, req: dict, **changes) -> None:
        for r in self.store.data["leave_requests"]:
            if r["id"] == req["id"]:
                r.update(changes)
        self.store.save()

    def approve(self, rid: str, approver_id: str, note: str = "", without_parent_mail: bool = False) -> dict:
        req = self.get(rid)
        if not req or req["status"] != "pending":
            raise ValueError("Only a leave the student has applied for on VTOP can be approved")
        if not req.get("parent_mail_id") and not without_parent_mail:
            raise ValueError("No mail from the parent yet")
        an = self.analysis_for(req)
        now = self.clock()
        stu, appr = self.portal.get_person(req["student_id"]), self.portal.get_person(approver_id)
        ws, we = self.window(req)
        self.store.upsert_commitment(Commitment(self.store.new_id("C"), stu.id, f"On leave: {req['reason']}", ws, we,
                                                "official", notes=f"approved leave {rid}", created_at=now))
        moved = {"moved": 0, "needs_attention": 0}
        if an["meetings"]:
            items = self.rescheduler.plan(an["meetings"], {stu.id: [(ws, we, "On leave")]})
            moved = self.rescheduler.apply(items, stu.id, reason="on approved leave")
        by_fac: dict[str, list] = {}
        for c in an["classes"]:
            if c["faculty_id"]:
                by_fac.setdefault(c["faculty_id"], []).append(c)
        for fid, cls in by_fac.items():
            lines = "\n".join(f"- {fmt_date(c['date'])}, {fmt_time(c['start'])}-{fmt_time(c['end'])}: "
                              f"{c['course_code']} {c['course_title']}" for c in cls)
            fac = self.portal.get_person(fid)
            self.mail.send(Mail("", approver_id, [fid], f"Leave approved: {stu.name} ({stu.reg_no or stu.id})",
                                f"Dear {fac.name if fac else ''},\n\n{stu.name} is on approved leave ({self.describe(req)}) "
                                f"and will miss your class(es):\n\n{lines}\n\nApproved by {appr.name}.\n(Sent by CampusSync)",
                                now, kind="notification", meta={"leave_id": rid}))
        self._update(req, status="approved", decided_at=now.isoformat(timespec="minutes"), note=note)
        body = (f"Hi {salutation(stu)},\n\nYour leave for {self.describe(req)} ({req['reason']}) is approved."
                + (f"\n\nNote: {note}" if note else "")
                + (f"\n\n{moved['moved']} meeting(s) during your leave were moved to new times; you'll get separate mails."
                   if moved["moved"] else "")
                + (f"\nCourse faculty for {len(by_fac)} course(s) have been told." if by_fac else "")
                + (f"\n\nPlease note: attendance in {', '.join(an['below'])} will drop below "
                   f"{self.minimum:g}% after this leave." if an["below"] else "")
                + f"\n\nThanks,\n{appr.name}")
        self.mail.send(Mail("", approver_id, [stu.id], f"Leave approved: {self.describe(req)}", body, now,
                            kind="agent",
                            meta={"leave_id": rid, "decision": "leave_approved"}))
        if req.get("parent_mail_id"):
            pm = self.mail.get(req["parent_mail_id"])
            if pm:
                self.mail.send(Mail("", approver_id, [pm.from_id], f"Re: {pm.subject}",
                                    f"Dear {self.portal.get_person(pm.from_id).name},\n\n{stu.name}'s leave for "
                                    f"{self.describe(req)} has been approved.\n\nRegards,\n{appr.name}", now,
                                    thread_id=pm.thread_id, in_reply_to=pm.id, kind="agent",
                                    meta={"leave_id": rid, "decision": "leave_approved"}))
                self.mail.mark(pm.id, "processed")
        if self.notifier:
            self.notifier.push(stu.id, f"Leave approved: {self.describe(req)}")
        return {"request": self.get(rid), "moved": moved, "faculty_told": len(by_fac)}

    def reject(self, rid: str, approver_id: str, note: str = "") -> dict:
        req = self.get(rid)
        if not req or req["status"] not in ("pending", "awaiting_application"):
            raise ValueError("No open leave request with that id")
        now = self.clock()
        stu, appr = self.portal.get_person(req["student_id"]), self.portal.get_person(approver_id)
        self._update(req, status="rejected", decided_at=now.isoformat(timespec="minutes"), note=note)
        body = (f"Hi {salutation(stu)},\n\nI can't approve your leave for {self.describe(req)}."
                + (f"\n\nReason: {note}" if note else "") + f"\n\nThanks,\n{appr.name}")
        self.mail.send(Mail("", approver_id, [stu.id], f"Leave not approved: {self.describe(req)}", body, now,
                            kind="agent", meta={"leave_id": rid, "decision": "leave_rejected"}))
        if self.notifier:
            self.notifier.push(stu.id, f"Leave not approved: {self.describe(req)}")
        return {"request": self.get(rid)}

    def _thread(self, req: dict) -> str:
        for key in ("mail_id", "parent_mail_id"):
            if req.get(key):
                m = self.mail.get(req[key])
                if m and key == "mail_id":
                    return m.thread_id
        return ""
