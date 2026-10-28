import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from campussync.llm.rules import parse_request
from campussync.utils import WEEKDAY_FULL, fmt_time, overlaps

from conftest import NOW


def _free_slot(svc, pids, d, dur=30):
    return svc.engine.find_common_slots(pids, d, d + timedelta(days=5), dur, max_results=1)[0]


def test_parser_cases():
    ref = NOW
    p = parse_request("Report", "Good morning Ma'am, can we meet tomorrow at 3 pm? 20 minutes.", ref)
    assert (p.intent, p.date, p.start, p.duration_min, p.part_of_day) == ("meeting_request", date(2026, 10, 16), time(15), 20, None)
    p = parse_request("x", "When are you free this week for a 30 minute discussion?", ref)
    assert p.intent == "availability_query" and p.duration_min == 30
    p = parse_request("Urgent: issue", "Need to meet you today itself", ref)
    assert p.urgent and p.date == ref.date()
    p = parse_request("Re: x", "Option 2 works for me", ref)
    assert p.intent == "option_choice" and p.option_choice == 2
    p = parse_request("x", "Can we meet between 2 and 4 pm on 20/10?", ref)
    assert (p.start, p.end, p.date) == (time(14), time(16), date(2026, 10, 20))
    p = parse_request("x", "I need to reschedule our meeting on 19 Oct. Could we move it to Tuesday afternoon?", ref)
    assert p.intent == "reschedule" and p.date == date(2026, 10, 20) and p.part_of_day == "afternoon"


def test_accept_books_meeting_everywhere(svc):
    slot = _free_slot(svc, ["F101", "S005"], date(2026, 10, 16))
    body = f"Ma'am, can we meet on {slot.start.day} Oct at {fmt_time(slot.start)} for 30 minutes?"
    r = svc.send_mail("S005", ["F101"], "Doubt clearing", body)
    prop = svc.process_mail(r["mail"].id, "F101")
    assert prop.decision == "accept" and prop.chosen.start == slot.start
    res = svc.confirm(prop)
    m = res["meeting"]
    assert m.participant_ids == ["S005", "F101"] or set(m.participant_ids) == {"S005", "F101"}
    for pid in ("F101", "S005"):
        assert any(e.ref == f"meeting:{m.id}" for e in svc.calendar.list_events(pid))
        assert any(r.meeting_id == m.id for r in svc.reminders.upcoming(pid))
    assert svc.mail.inbox("S005")[0].in_reply_to == r["mail"].id
    # the booked time is now busy for both
    assert not svc.engine.check_slot(["F101"], m.start, m.end)["ok"]


def test_busy_time_gets_alternatives_then_pick_option(svc):
    # F101 teaches on Monday 08:00 (slot A1) - ask for exactly that
    r = svc.send_mail("S006", ["F101"], "Project doubt", "Can we meet on Monday at 8 am?")
    prop = svc.process_mail(r["mail"].id, "F101")
    assert prop.decision == "offer" and prop.options
    assert any("busy" in n.lower() for n in prop.private_notes)
    assert "SWE3001" not in prop.reply_body  # never reveal why
    svc.confirm(prop)
    offer_mail = svc.mail.inbox("S006")[0]
    assert offer_mail.meta.get("options")
    pick = svc.send_mail("S006", ["F101"], "", "Option 2 works for me, thanks", in_reply_to=offer_mail.id)
    prop2 = svc.process_mail(pick["mail"].id, "F101")
    assert prop2.decision == "accept"
    assert prop2.chosen.start == prop.options[1].start
    res = svc.confirm(prop2)
    assert res["meeting"].start == prop.options[1].start


def test_reschedule_mail_moves_existing_meeting(svc):
    mail = next(m for m in svc.mail.inbox("F104") if m.subject.startswith("Reschedule"))
    old = next(m for m in svc.store.meetings("F104"))
    prop = svc.process_mail(mail.id, "F104")
    assert prop.parsed.intent == "reschedule" and prop.meeting_id == old.id
    assert prop.decision in ("accept", "offer")
    if prop.decision == "offer":
        prop = svc.handler.choose_option(prop, 0)
    res = svc.confirm(prop)
    moved = svc.store.get_meeting(old.id)
    assert moved.start != old.start and moved.history[-1]["action"] == "moved"
    assert len([m for m in svc.store.meetings("F104")]) == 1


def test_cancel(svc):
    r = svc.send_mail("S002", ["F101"], "Review", "Ma'am, I can't make it to the review, please cancel it.")
    prop = svc.process_mail(r["mail"].id, "F101")
    assert prop.decision == "cancel"
    svc.confirm(prop)
    assert svc.store.get_meeting(prop.meeting_id).status == "cancelled"
    assert not any(e.ref == f"meeting:{prop.meeting_id}" for e in svc.calendar.list_events("S002"))


def test_clash_alert_and_fix(svc):
    review = next(m for m in svc.store.meetings("S001") if m.title == "Project review 2")
    c, clashes = svc.add_commitment("S001", "Hackathon finals", review.start - timedelta(minutes=30),
                                    review.end + timedelta(minutes=30), "urgent")
    meet = [x for x in clashes if x["kind"] == "meeting"]
    assert meet and meet[0]["severity"] == "critical"
    assert svc.store.alerts("S001")
    summary, items = svc.reschedule_clashing_meetings("S001", c, [x["meeting_id"] for x in meet])
    assert summary["moved"] >= 1
    moved = svc.store.get_meeting(review.id)
    assert not overlaps(moved.start, moved.end, c.start, c.end)


def test_mass_reschedule_when_faculty_unavailable(svc):
    reviews = [m for m in svc.store.meetings("F101") if m.title == "Project review 2"]
    day = reviews[0].start.date()
    start, end = datetime.combine(day, time(8)), datetime.combine(day, time(19))
    items = svc.plan_unavailability("F101", start, end, "University committee visit")
    assert len(items) == len(reviews) and all(i.new_slot for i in items)
    new = [i.new_slot for i in items]
    for i, a in enumerate(new):
        assert not overlaps(a.start, a.end, start, end)
        for b in new[i + 1:]:
            assert not overlaps(a.start, a.end, b.start, b.end)
    res = svc.apply_unavailability("F101", start, end, "University committee visit", items)
    assert res["moved"] == len(reviews)
    for m in reviews:
        stu = [p for p in m.participant_ids if p != "F101"][0]
        assert any(x.subject.startswith("Moved:") for x in svc.mail.inbox(stu))


def test_auto_mode_replies_without_approval(svc):
    svc.set_auto_mode(True)
    r = svc.send_mail("S007", ["F102"], "Quick doubt", "Sir, when are you free this week for 15 minutes?")
    assert r["auto"] and r["auto"][0]["decision"] in ("offer", "accept")
    assert svc.mail.inbox("S007")[0].kind == "agent"


def test_reminders_dispatch(svc):
    m = svc.store.meetings()[0]
    svc.set_clock(m.start - timedelta(minutes=29))
    sent = svc.dispatch_reminders()
    assert any(x.meta.get("meeting_id") == m.id for x in sent)


def test_commitment_from_text(svc):
    c, _ = svc.add_commitment_from_text("S010", "Basketball practice every Wed 5-6:30pm")
    assert c.recurrence == "weekly" and c.start.weekday() == 2 and c.start.time() == time(17)
    assert c.end.time() == time(18, 30)


def test_ask_rules(svc):
    a = svc.ask("S003", "When is my guide free this week?")
    assert "Kavitha" in a["text"] and a["mode"] == "rules"
    a = svc.ask("S003", "How is my attendance?")
    assert "SWE3001" in a["text"]
    a = svc.ask("S003", "Any holidays coming up?")
    assert "holiday" in a["text"].lower()
    a = svc.ask("S003", "When are my exams?")
    assert "CAT-2" in a["text"]
    a = svc.ask("S003", "What is the attendance exemption rule?")  # goes to attendance (own data)
    assert a["text"]


def test_ask_privacy(svc):
    # a student asking about another student's schedule sees only busy blocks
    r = svc.assistant._t_get_schedule(svc.person("S003"), "Aarav Sharma", "2026-10-16", "2026-10-20")
    assert r["blocks"] and all(b["what"] == "Busy" or b["kind"] == "holiday" for b in r["blocks"])
    hits = svc.knowledge().search("Club practice", svc.person("S003"))
    assert not any(d.kind == "commitment" for d, _ in hits)
    hits = svc.knowledge().search("Club practice", svc.person("S001"))
    assert any(d.kind == "commitment" for d, _ in hits)


# ---------------------------------------------------------------- DeepSeek paths with a fake client
class _Call(SimpleNamespace):
    pass


def test_llm_tool_loop_with_fake_deepseek(svc):
    calls = []

    def fake_chat(messages, tools=None, json_mode=False, temperature=None):
        calls.append(messages)
        if len(calls) == 1:
            tc = _Call(id="c1", function=_Call(name="find_common_free_time",
                                               arguments=json.dumps({"people": ["my guide", "me"], "date_from": "2026-10-16",
                                                                     "date_to": "2026-10-17", "duration_min": 30})))
            return _Call(content="", tool_calls=[tc])
        tool_msg = messages[-1]
        assert tool_msg["role"] == "tool"
        data = json.loads(tool_msg["content"])
        return _Call(content=f"Best: {data['best_slots'][0]}", tool_calls=None)

    svc.llm.mode, svc.llm.api_key = "auto", "test"
    svc.llm.chat = fake_chat
    a = svc.ask("S003", "when is my guide free?")
    assert a["mode"] == "deepseek" and a["tools"] == ["find_common_free_time"] and a["text"].startswith("Best:")


def test_llm_parser_rejects_invented_time(svc):
    svc.llm.mode, svc.llm.api_key = "auto", "test"
    svc.llm.json = lambda system, user: {"intent": "meeting_request", "date": "2026-10-16", "start_time": "11:00",
                                         "part_of_day": None, "duration_min": 30, "urgent": False, "topic": "Chat",
                                         "option_choice": None}
    p = svc.parser.parse("Chat", "Can we meet tomorrow sometime?", NOW)
    assert p.source == "deepseek" and p.start is None and p.date == date(2026, 10, 16)


def test_llm_polish_rejected_if_times_change(svc):
    svc.llm.mode, svc.llm.api_key = "auto", "test"
    svc.llm.text = lambda system, user, temperature=None: user.replace("Option 1", "Choice A")
    r = svc.send_mail("S004", ["F101"], "Chat", "Ma'am, when are you free this week?")
    svc.llm.json = lambda s, u: (_ for _ in ()).throw(RuntimeError("down"))
    prop = svc.process_mail(r["mail"].id, "F101")
    assert prop.drafted_by == "template" and "Option 1" in prop.reply_body


def test_other_college_csv_adapter(csv_svc):
    s = csv_svc
    assert {p.id for p in s.people()} == {"T01", "T02", "P01", "P02", "P03"}
    slots = s.engine.find_common_slots(["T01", "P01"], date(2026, 10, 19), date(2026, 10, 23), 30)
    assert slots
    for sl in slots:
        assert time(9) <= sl.start.time() and sl.end.time() <= time(17) and sl.start.weekday() < 5
        assert sl.start.date() != date(2026, 10, 20)  # holiday from calendar.csv
    r = s.send_mail("P01", ["T01"], "Doubt", "Ma'am, can we meet on Monday at 9 am?")
    prop = s.process_mail(r["mail"].id, "T01")
    assert prop.decision == "offer"  # both are in CS301 then
