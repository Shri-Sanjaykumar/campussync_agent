"""'Ask' assistant: plain questions like "when is my guide free this week?".
With DeepSeek: the model plans, calls the scheduling tools below, and writes the answer from
their results plus retrieved documents. Without it: a rule-based router calls the same tools."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from ..core.attendance import attendance_summary
from ..llm.rules import PARTS, parse_date_range, parse_duration, parse_times
from ..utils import WEEKDAY_FULL, fmt_date, fmt_range, fmt_time, first_name

TOOLS = [
    {"type": "function", "function": {
        "name": "find_common_free_time",
        "description": "Find times when ALL the given people are free (uses timetables, exams, holidays, commitments, meetings).",
        "parameters": {"type": "object", "properties": {
            "people": {"type": "array", "items": {"type": "string"},
                       "description": "Who must be free: 'me', 'my guide', 'my proctor', a name, or an id like F101"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            "duration_min": {"type": "integer", "default": 30},
            "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening"]}},
            "required": ["people", "date_from", "date_to"]}}},
    {"type": "function", "function": {
        "name": "get_schedule",
        "description": "Busy blocks for one person between two dates. Details are shown only for the asking user.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}},
            "required": ["person", "date_from", "date_to"]}}},
    {"type": "function", "function": {
        "name": "get_my_attendance",
        "description": "The asking student's attendance per course and how many classes they can still miss.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_exams",
        "description": "The asking user's exams between two dates.",
        "parameters": {"type": "object", "properties": {"date_from": {"type": "string"}, "date_to": {"type": "string"}},
                       "required": ["date_from", "date_to"]}}},
    {"type": "function", "function": {
        "name": "get_academic_calendar",
        "description": "Holidays, exam weeks and non-instructional days between two dates.",
        "parameters": {"type": "object", "properties": {"date_from": {"type": "string"}, "date_to": {"type": "string"}},
                       "required": ["date_from", "date_to"]}}},
    {"type": "function", "function": {
        "name": "list_my_meetings",
        "description": "The asking user's booked meetings between two dates.",
        "parameters": {"type": "object", "properties": {"date_from": {"type": "string"}, "date_to": {"type": "string"}},
                       "required": ["date_from", "date_to"]}}},
    {"type": "function", "function": {
        "name": "propose_meeting",
        "description": "When the user wants to BOOK/schedule a meeting: find up to 3 times free for everyone. "
                       "The app shows them as buttons; tapping one sends a request to the other people.",
        "parameters": {"type": "object", "properties": {
            "people": {"type": "array", "items": {"type": "string"}, "description": "Other people (not 'me')"},
            "date_from": {"type": "string"}, "date_to": {"type": "string"},
            "duration_min": {"type": "integer", "default": 30},
            "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
            "title": {"type": "string", "description": "Short meeting title"}},
            "required": ["people", "date_from", "date_to"]}}},
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": "Search timetables, cabin hours, policies, calendar and the user's own commitments.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]


class Assistant:
    def __init__(self, svc):
        self.svc = svc
        self._actions: list[dict] = []

    # ================================================================ people resolution
    def resolve(self, ref: str, user) -> list[str]:
        portal = self.svc.portal
        low = (ref or "").lower().strip()
        out: list[str] = []
        if re.search(r"\b(my )?guide\b", low) and user.guide_id:
            out.append(user.guide_id)
        if re.search(r"\b(my )?proctor\b", low) and user.proctor_id:
            out.append(user.proctor_id)
        for p in portal.list_people():
            if p.id == user.id:
                continue
            name = re.sub(r"^(dr|prof)\.?\s+", "", p.name.lower())
            parts = name.split()
            if re.search(rf"\b{re.escape(p.id.lower())}\b", low) or (p.reg_no and p.reg_no.lower() in low) \
                    or name in low or (parts and re.search(rf"\b{re.escape(parts[0])}\b", low) and len(parts[0]) > 3):
                if p.id not in out:
                    out.append(p.id)
        if re.search(r"\b(me|my|i|myself|us|we|both)\b", low) and not out:
            out.append(user.id)
        return out

    def _people_arg(self, people: list[str], user) -> list[str]:
        pids: list[str] = []
        for ref in people:
            r = ref.strip().lower()
            if r in ("me", "i", "myself", "self", user.id.lower()):
                pids.append(user.id)
            else:
                pids += self.resolve(ref, user) or []
        return list(dict.fromkeys(pids))

    # ================================================================ tools
    def tool(self, name: str, args: dict, user) -> dict:
        try:
            return getattr(self, f"_t_{name}")(user, **args)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _dates(self, a, b):
        today = self.svc.now().date()
        da = date.fromisoformat(a) if a else today
        db = date.fromisoformat(b) if b else da
        return max(da, today) if da < today and db >= today else da, max(db, da)

    def _t_find_common_free_time(self, user, people, date_from=None, date_to=None, duration_min=30, part_of_day=None):
        pids = self._people_arg(people, user)
        if not pids:
            return {"error": "couldn't work out who you mean"}
        a, b = self._dates(date_from, date_to)
        window = PARTS.get(part_of_day) if part_of_day else None
        eng = self.svc.engine
        names = [self.svc.portal.get_person(p).name for p in pids]
        days = []
        for i in range((b - a).days + 1):
            d = a + timedelta(days=i)
            wins = eng.free_windows(pids, d, min_minutes=int(duration_min or 30), window=window)
            if wins:
                days.append({"date": fmt_date(d), "free": [f"{fmt_time(s)}-{fmt_time(e)}" for s, e in wins]})
        best = eng.find_common_slots(pids, a, b, int(duration_min or 30), window=window)
        cabin = []
        for pid in pids:
            for w in self.svc.portal.office_hours(pid):
                cabin.append(f"{self.svc.portal.get_person(pid).name}: {WEEKDAY_FULL[w.weekday]} {fmt_time(w.start)}-{fmt_time(w.end)}")
        return {"people": names, "from": fmt_date(a), "to": fmt_date(b), "duration_min": duration_min,
                "free_by_day": days, "best_slots": [fmt_range(s.start, s.end) for s in best], "cabin_hours": cabin}

    def _t_get_schedule(self, user, person, date_from=None, date_to=None):
        pids = self._people_arg([person], user)
        if not pids:
            return {"error": "unknown person"}
        pid = pids[0]
        target = self.svc.portal.get_person(pid)
        a, b = self._dates(date_from, date_to)
        evs = self.svc.engine.person_events(pid, a, b)
        out = []
        for e in evs:
            if pid == user.id:
                title = e.title
            elif e.kind in ("holiday",) or (target.role == "faculty" and e.kind in ("class", "duty")):
                title = e.title if e.kind != "class" else "Teaching"
            else:
                title = "Busy"
            out.append({"when": fmt_range(e.start, e.end), "what": title, "kind": e.kind if pid == user.id else
                        ("holiday" if e.kind == "holiday" else "busy")})
        return {"person": target.name, "from": fmt_date(a), "to": fmt_date(b), "blocks": out}

    def _t_get_my_attendance(self, user):
        if user.role != "student":
            return {"error": "attendance applies to students"}
        recs = self.svc.portal.attendance(user.id)
        return {"cgpa": user.cgpa, "exempt": bool(user.cgpa and user.cgpa >= float(self.svc.settings.get("attendance.exemption_cgpa", 99))),
                "minimum_percent": self.svc.settings.get("attendance.minimum_percent"),
                "courses": attendance_summary(user, recs, self.svc.settings)}

    def _t_get_exams(self, user, date_from=None, date_to=None):
        today = self.svc.now().date()
        a = date.fromisoformat(date_from) if date_from else today
        b = date.fromisoformat(date_to) if date_to else today + timedelta(days=60)
        ex = sorted(self.svc.portal.exams(user.id, a, b), key=lambda e: e.start)
        return {"exams": [{"title": e.title, "when": fmt_range(e.start, e.end), "venue": e.venue} for e in ex]}

    def _t_get_academic_calendar(self, user, date_from=None, date_to=None):
        a, b = self._dates(date_from, date_to)
        days = [c for c in self.svc.portal.academic_calendar(a, b) if c.kind not in ("instructional", "weekend")]
        return {"days": [{"date": fmt_date(c.date), "kind": c.kind, "label": c.label} for c in days]}

    def _t_list_my_meetings(self, user, date_from=None, date_to=None):
        a, b = self._dates(date_from, date_to)
        ms = [m for m in self.svc.store.meetings(user.id) if a <= m.start.date() <= b]
        return {"meetings": [{"title": m.title, "when": fmt_range(m.start, m.end), "where": m.location,
                              "with": [self.svc.portal.get_person(p).name for p in m.participant_ids if p != user.id]}
                             for m in ms]}

    def _t_propose_meeting(self, user, people, date_from=None, date_to=None, duration_min=30, part_of_day=None,
                           title=None):
        others = [p for p in self._people_arg(people, user) if p != user.id]
        if not others:
            return {"error": "who should the meeting be with?"}
        pids = [user.id] + others
        a, b = self._dates(date_from, date_to)
        window = PARTS.get(part_of_day) if part_of_day else None
        slots = self.svc.engine.find_common_slots(pids, a, b, int(duration_min or 30), window=window)
        title = title or "Meeting"
        actions = [{"type": "request_meeting", "with": others, "start": s.start.isoformat(timespec="minutes"),
                    "end": s.end.isoformat(timespec="minutes"), "title": title, "label": fmt_range(s.start, s.end)}
                   for s in slots]
        self._actions.extend(actions)
        return {"with": [self.svc.portal.get_person(p).name for p in others], "options": [x["label"] for x in actions],
                "note": "The user taps an option to send the request; nothing is booked yet."}

    def _t_search_knowledge(self, user, query):
        kb = self.svc.knowledge()
        hits = kb.search(query, user)
        return {"results": [{"title": d.title, "text": d.text[:600]} for d, _s in hits]}

    # ================================================================ answering
    def answer(self, question: str, user_id: str, history: list[dict] | None = None) -> dict:
        user = self.svc.portal.get_person(user_id)
        self._actions = []
        if self.svc.llm.enabled:
            try:
                out = self._answer_llm(question, user, history or [])
                out["actions"] = list(self._actions)
                return out
            except Exception:
                self._actions = []
        out = self._answer_rules(question, user)
        out["actions"] = list(self._actions)
        return out

    def _system_prompt(self, user) -> str:
        now = self.svc.now()
        extra = ""
        if user.guide_id:
            extra += f" Their project guide is {self.svc.portal.get_person(user.guide_id).name} ({user.guide_id})."
        if user.proctor_id:
            extra += f" Their proctor is {self.svc.portal.get_person(user.proctor_id).name} ({user.proctor_id})."
        return (f"You are CampusSync, a scheduling assistant for {self.svc.settings.get('institution.name')}. "
                f"Now is {WEEKDAY_FULL[now.weekday()]} {now:%Y-%m-%d %H:%M}. You are talking to {user.name} "
                f"({user.role}, id {user.id}).{extra} 'This week' means today until Saturday. "
                "Always call tools to get times; never invent a date, time, room or name. "
                "Never reveal why another person is busy, only that they are busy. "
                "If they want to book a meeting, call propose_meeting; the app turns the options into buttons. "
                "Answer briefly in plain words.")

    def _answer_llm(self, question, user, history):
        llm = self.svc.llm
        messages = [{"role": "system", "content": self._system_prompt(user)}]
        for h in history[-6:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": question})
        used = []
        for _ in range(6):
            msg = llm.chat(messages, tools=TOOLS)
            calls = getattr(msg, "tool_calls", None) or []
            if not calls:
                return {"text": (msg.content or "").strip(), "mode": "deepseek", "tools": used}
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": c.id, "type": "function",
                                             "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                            for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                used.append(c.function.name)
                res = self.tool(c.function.name, args, user)
                messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(res, default=str)})
        raise RuntimeError("too many tool rounds")

    def _answer_rules(self, q: str, user) -> dict:
        low = q.lower()
        today = self.svc.now().date()
        rng = parse_date_range(q, today) or (today, today + timedelta(days=self.svc.engine.search_days - 1))
        a, b = rng[0].isoformat(), rng[1].isoformat()
        _s, _e, part, _rest = parse_times(q)
        dur = parse_duration(q) or 30
        others = [p for p in self.resolve(q, user) if p != user.id]

        if re.search(r"\b(book|schedule|set up|arrange|fix up|fix a)\b", low) and others:
            title = "Project discussion" if re.search(r"\bproject|review\b", low) else "Meeting"
            r = self._t_propose_meeting(user, others, a, b, dur, part, title)
            if "error" in r or not r["options"]:
                return self._out(r.get("error") or f"No time works for you and {', '.join(r['with'])} in that period. "
                                 "Try a different day.", ["propose_meeting"])
            return self._out(f"These times work for you and {', '.join(r['with'])} ({dur} min):\n" +
                             "\n".join(f"- {o}" for o in r["options"]) +
                             "\n\nTap one to send the request. Their agent confirms it.", ["propose_meeting"])
        if re.search(r"\b(attendance|bunk|skip|miss)\b", low):
            r = self._t_get_my_attendance(user)
            if "error" in r:
                return self._out(r["error"].capitalize() + ".", ["get_my_attendance"])
            lines = [f"{c['course_code']} {c['course_title']}: {c['attended']}/{c['total']} = {c['percent']}%, "
                     f"can miss {c['can_miss']} more" + (f" ({c['status']})" if c["status"] != "ok" else "")
                     for c in r["courses"]]
            head = ("You're exempt from the attendance minimum (CGPA rule). Your numbers anyway:" if r["exempt"]
                    else f"Your attendance (minimum {r['minimum_percent']}%):")
            return self._out(head + "\n" + "\n".join(f"- {x}" for x in lines), ["get_my_attendance"])
        if re.search(r"\b(exam|exams|cat|fat|cat-2|cat2)\b", low):
            r = self._t_get_exams(user, None, None)
            if not r["exams"]:
                return self._out("No upcoming exams for you in the calendar.", ["get_exams"])
            return self._out("Your upcoming exams:\n" + "\n".join(f"- {e['title']}: {e['when']} ({e['venue']})" for e in r["exams"]), ["get_exams"])
        if re.search(r"\b(holiday|holidays|calendar|working day|leave)\b", low):
            if not parse_date_range(q, today):
                b = (today + timedelta(days=45)).isoformat()
            r = self._t_get_academic_calendar(user, a, b)
            if not r["days"]:
                return self._out("No holidays or exam days in that period.", ["get_academic_calendar"])
            return self._out("From the academic calendar:\n" + "\n".join(f"- {d['date']}: {d['label']}" for d in r["days"]),
                             ["get_academic_calendar"])
        if re.search(r"\b(free|available|availability|slot|meet|when can)\b", low):
            people = others + ([user.id] if others and not re.search(r"\b(only|just|alone)\b", low) else [])
            people = people or [user.id]
            r = self._t_find_common_free_time(user, people, a, b, dur, part)
            if "error" in r:
                return self._out(r["error"], ["find_common_free_time"])
            names = ["you" if n == user.name else n for n in r["people"]]
            who = " and ".join(names) if len(names) > 1 else names[0]
            if not r["free_by_day"]:
                return self._out(f"No common free time for {who} between {r['from']} and {r['to']}.", ["find_common_free_time"])
            lines = [f"- {d['date']}: {', '.join(d['free'])}" for d in r["free_by_day"]]
            text = f"Free time for {who} ({r['from']} to {r['to']}, at least {dur} min):\n" + "\n".join(lines)
            if r["best_slots"]:
                text += "\n\nBest picks: " + "; ".join(r["best_slots"])
            if r["cabin_hours"]:
                text += "\nCabin hours: " + "; ".join(r["cabin_hours"])
            return self._out(text, ["find_common_free_time"])
        if re.search(r"\b(meeting|meetings|appointments?)\b", low):
            if not parse_date_range(q, today):
                b = (today + timedelta(days=30)).isoformat()
            r = self._t_list_my_meetings(user, a, b)
            if not r["meetings"]:
                return self._out("You have no meetings booked in that period.", ["list_my_meetings"])
            return self._out("Your meetings:\n" + "\n".join(f"- {m['when']}: {m['title']} with {', '.join(m['with'])} ({m['where']})"
                                                           for m in r["meetings"]), ["list_my_meetings"])
        if re.search(r"\b(schedule|timetable|classes|busy|class)\b", low):
            target = others[0] if others else user.id
            r = self._t_get_schedule(user, target, a, b)
            if not r["blocks"]:
                return self._out(f"{r['person']} has nothing scheduled in that period.", ["get_schedule"])
            return self._out(f"{'Your schedule' if target == user.id else r['person']}, {r['from']} to {r['to']}:\n" +
                             "\n".join(f"- {x['when']}: {x['what']}" for x in r["blocks"][:25]), ["get_schedule"])
        r = self._t_search_knowledge(user, q)
        if not r["results"]:
            return self._out("I couldn't find anything on that. Try asking about free time, meetings, exams, "
                             "holidays or attendance.", ["search_knowledge"])
        return self._out("Here's what I found:\n" + "\n".join(f"- {x['title']}: {x['text'][:240]}" for x in r["results"][:3]),
                         ["search_knowledge"])

    @staticmethod
    def _out(text: str, tools: list[str]) -> dict:
        return {"text": text, "mode": "rules", "tools": tools}
