"""Iteration 2: holds, group meetings, preferences, chat booking, office-hours queue, timetable import,
dashboard, hybrid retrieval, Telegram, neural intent model, evaluation sets, speed."""
import random
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from campussync.core.office_hours import OfficeHoursError
from campussync.models import Commitment, Meeting, to_dict
from campussync.utils import overlaps

from conftest import NOW, ROOT


def _offer(svc, sid="S006", fid="F101", body="Ma'am, when are you free this week for 30 minutes?"):
    r = svc.send_mail(sid, [fid], "Doubt", body)
    prop = svc.process_mail(r["mail"].id, fid)
    assert prop.decision == "offer"
    svc.confirm(prop)
    return prop, svc.mail.inbox(sid)[0]


# ---------------------------------------------------------------- holds
def test_offered_slots_are_held_and_released(svc):
    prop, offer_mail = _offer(svc)
    held = prop.options[0]
    # another student asking for exactly that time can't get it while it's held
    chk = svc.engine.check_slot(["F101"], held.start, held.end)
    assert any(c["kind"] == "hold" for c in chk["hard"])
    # the student who got the offer can still pick it
    pick = svc.send_mail("S006", ["F101"], "", "Option 1 works", in_reply_to=offer_mail.id)
    p2 = svc.process_mail(pick["mail"].id, "F101")
    assert p2.decision == "accept" and p2.chosen.start == held.start
    svc.confirm(p2)
    assert not svc.store.active_holds("F101", svc.now())


def test_holds_expire(svc):
    prop, _ = _offer(svc)
    held = prop.options[-1]
    svc.set_clock(NOW + timedelta(hours=5))
    chk = svc.engine.check_slot(["F101"], held.start, held.end)
    assert not any(c["kind"] == "hold" for c in chk["hard"])


def test_new_offer_releases_old_holds(svc):
    prop, offer_mail = _offer(svc)
    first_offer = offer_mail.meta["offer_id"]
    again = svc.send_mail("S006", ["F101"], "", "None of those work, what about next week?", in_reply_to=offer_mail.id)
    p2 = svc.process_mail(again["mail"].id, "F101")
    if p2.decision == "offer":
        svc.confirm(p2)
        assert not [h for h in svc.store.data["holds"] if h["offer_id"] == first_offer and h["status"] == "active"]


# ---------------------------------------------------------------- group meetings
def test_team_mail_includes_teammates(svc):
    m = next(x for x in svc.mail.inbox("F101") if x.subject == "Prototype demo")
    prop = svc.process_mail(m.id, "F101")
    assert set(prop.participants) == {"S001", "S002", "S003", "F101"}
    assert prop.reply_body.startswith("Hi all")
    res = svc.confirm(prop)
    if res["meeting"]:
        assert set(res["meeting"].participant_ids) == {"S001", "S002", "S003", "F101"}
        for pid in ("S001", "S003"):
            assert svc.mail.inbox(pid)[0].kind == "agent"


def test_group_fallback_leaves_out_one_member(svc):
    # Rohan (S003) is busy every working day all week -> no time for the whole team
    for i in range(0, 11):
        d = date(2026, 10, 15) + timedelta(days=i)
        svc.store.upsert_commitment(Commitment(svc.store.new_id("C"), "S003", "Internship", datetime.combine(d, time(8)),
                                               datetime.combine(d, time(19)), "official"))
    r = svc.send_mail("S002", ["F101"], "Team check-in", "Ma'am, can our team meet you next week for 30 minutes?")
    prop = svc.process_mail(r["mail"].id, "F101")
    assert prop.decision == "offer" and prop.options
    assert all(o.missing == ["S003"] for o in prop.options)
    assert "everyone except Rohan" in prop.reply_body
    svc.confirm(prop)
    offer_mail = svc.mail.inbox("S002")[0]
    pick = svc.send_mail("S002", ["F101"], "", "Option 1 works", in_reply_to=offer_mail.id)
    p2 = svc.process_mail(pick["mail"].id, "F101")
    assert p2.decision == "accept"
    res = svc.confirm(p2)
    assert "S003" not in res["meeting"].participant_ids and "S001" in res["meeting"].participant_ids


# ---------------------------------------------------------------- preferences
def test_preferences_shape_suggestions(svc):
    svc.set_prefs("F101", earliest="10:00", latest="16:00", gap_min=15, no_meeting_days=[5])
    slots = svc.engine.find_common_slots(["F101"], date(2026, 10, 16), date(2026, 10, 24), 30, max_results=10)
    assert slots
    meetings = svc.store.meetings("F101")
    for s in slots:
        assert time(10) <= s.start.time() and s.end.time() <= time(16)
        assert s.start.weekday() != 5
        for m in meetings:
            assert not overlaps(s.start, s.end, m.start - timedelta(minutes=15), m.end + timedelta(minutes=15))


def test_daily_meeting_limit(svc):
    review_day = next(m for m in svc.store.meetings("F101") if m.title == "Project review 2").start.date()
    svc.set_prefs("F101", max_per_day=2)
    slots = svc.engine.find_common_slots(["F101"], review_day, review_day, 30)
    assert slots == []
    assert any(e.kind == "pref" for e in svc.engine.person_events("F101", review_day, review_day))


# ---------------------------------------------------------------- chat booking
def test_chat_booking_sends_request_that_gets_booked(svc):
    a = svc.ask("S003", "Book 30 min with my guide tomorrow afternoon")
    assert a["actions"], a["text"]
    act = a["actions"][0]
    r = svc.request_meeting("S003", act["with"], datetime.fromisoformat(act["start"]),
                            datetime.fromisoformat(act["end"]), act["title"])
    prop = svc.process_mail(r["mail"].id, "F101")
    assert prop.decision == "accept" and prop.chosen.start == datetime.fromisoformat(act["start"])


# ---------------------------------------------------------------- office hours
def _free_session(svc, fid):
    return next(s for s in svc.office_hours.sessions(fid, date(2026, 10, 16), 10) if s["free"] >= 2)


def test_book_cabin_token(svc):
    sess = _free_session(svc, "F101")
    tok = next(t for t in sess["tokens"] if t["status"] == "free")
    student = next(sid for sid in ("S010", "S011", "S012", "S013", "S015")
                   if svc.engine.check_slot([sid], tok["start"], tok["end"])["ok"])
    m = svc.office_hours.book(student, "F101", tok["start"], "Backprop doubt")
    assert m.kind == "office_hours" and m.participant_ids == ["F101", student]
    with pytest.raises(OfficeHoursError):
        svc.office_hours.book(student, "F101", tok["start"], "again")
    other = next(t for t in svc.office_hours.sessions("F101", tok["start"].date(), 1, viewer=student)
                 if t["start"] == sess["start"])["tokens"]
    nxt = next(t for t in other if t["status"] == "free")
    with pytest.raises(OfficeHoursError, match="already have a token"):
        svc.office_hours.book(student, "F101", nxt["start"], "second")


def test_waitlist_gets_token_when_someone_cancels(svc):
    full = next(s for s in svc.office_hours.sessions("F102", date(2026, 10, 16), 10) if s["free"] == 0
                and all(t["status"] == "booked" for t in s["tokens"]))
    waiter = next(sid for sid in ("S015", "S016", "S017", "S018", "S019", "S020", "S001")
                  if svc.engine.check_slot([sid], full["tokens"][0]["start"], full["tokens"][0]["end"])["ok"])
    svc.office_hours.join_waitlist(waiter, "F102", full["start"], "Lab doubt")
    with pytest.raises(OfficeHoursError):
        svc.office_hours.join_waitlist(waiter, "F102", full["start"], "Lab doubt")
    first = full["tokens"][0]
    res = svc.cancel_meeting(first["meeting_id"], first["student_id"])
    assert res["waitlist"] and res["waitlist"]["assigned_to"] == waiter
    assert any(m.subject == "You got a cabin-hours token" for m in svc.mail.inbox(waiter))


# ---------------------------------------------------------------- timetable import
def test_timetable_import_overrides_portal(svc):
    text = ("1 | SWE3001 - Deep Learning | A1+TA1 | SJT 301 | Dr. Kavitha Raman\n"
            "2 | SWE3002 Software Metrics | B2+TB2 | TT 205\n"
            "3 | SWE3001P Deep Learning Lab | L31+L32 | SJT 805\n")
    rows, problems = svc.preview_timetable(text)
    assert len(rows) == 3 and not problems
    res = svc.import_timetable("S009", text)
    assert res["courses"] == 3
    classes = svc.portal.weekly_classes("S009")
    assert {c.course_code for c in classes} == {"SWE3001", "SWE3002", "SWE3001P"}
    mon = date(2026, 10, 19)
    evs = [e for e in svc.engine.person_events("S009", mon, mon) if e.kind == "class"]
    assert any(e.start.time() == time(8) for e in evs) and any(e.start.time() == time(14) for e in evs)
    svc.clear_imported_timetable("S009")
    assert {c.course_code for c in svc.portal.weekly_classes("S009")} != {"SWE3001", "SWE3002", "SWE3001P"}


def test_timetable_import_flags_problems(svc):
    rows, problems = svc.preview_timetable("SWE3001 Deep Learning A1+TA1\nSWE3009 Clashing course A1\nSWE3010 Bad Q9\n")
    assert any("clashes" in p for p in problems) and any("no slot" in p for p in problems)


# ---------------------------------------------------------------- dashboard
def test_dashboard_numbers(svc):
    for m in svc.mail.inbox("F101"):
        if m.kind == "human":
            svc.confirm(svc.process_mail(m.id, "F101"))
    st = svc.faculty_stats("F101", date(2026, 10, 12), date(2026, 10, 24))
    assert st["tokens"] == 3 and st["meetings"] >= 4
    assert st["cabin_use_pct"] and 0 < st["cabin_use_pct"] <= 100
    assert st["answered"] >= 1 and st["median_reply_min"] is not None
    assert sum(st["decisions"].values()) == st["answered"]
    table = svc.department_table(date(2026, 10, 12), date(2026, 10, 24))
    assert len(table) == 6


# ---------------------------------------------------------------- retrieval
def test_hybrid_retrieval_with_fake_neural_encoder(svc):
    from campussync.rag.store import KnowledgeBase
    kb = KnowledgeBase(svc.settings, "hybrid")
    rng = np.random.default_rng(0)
    vocab = {}

    def enc(texts):  # bag-of-words random projection: stands in for sentence-transformers
        out = []
        for t in texts:
            v = np.zeros(32)
            for w in t.lower().split():
                if w not in vocab:
                    vocab[w] = rng.normal(size=32)
                v += vocab[w]
            out.append(v)
        return np.array(out)

    kb.encoder = enc
    kb.build(svc._build_docs())
    assert "custom encoder" in kb.active_backend
    hits = kb.search("cabin hours of Kavitha Raman", svc.person("S003"))
    assert hits and hits[0][0].id == "oh-F101"


def test_retrieval_eval_runs(svc):
    from campussync.ml.evaluate import evaluate_retrieval, load_rows
    from campussync.rag.store import KnowledgeBase
    rows = load_rows(ROOT / "data/eval/retrieval.jsonl")
    kb = KnowledgeBase(svc.settings, "hybrid")
    kb.build(svc._build_docs())
    r = evaluate_retrieval(kb, {p.id: p for p in svc.people()}, rows)
    assert r["recall@3"] >= 80


# ---------------------------------------------------------------- telegram
def test_telegram_push_on_booking(svc):
    sent = []
    svc.notifier.sender = lambda chat, text: (sent.append((chat, text)) or (True, ""))
    svc.set_prefs("S005", telegram_chat_id="12345", notify_telegram=True)
    slot = svc.engine.find_common_slots(["F101", "S005"], date(2026, 10, 16), date(2026, 10, 22), 30, max_results=1)[0]
    r = svc.send_mail("S005", ["F101"], "Doubt", f"Can we meet on {slot.start.day} Oct at {slot.start:%H:%M}?")
    svc.confirm(svc.process_mail(r["mail"].id, "F101"))
    assert sent and sent[0][0] == "12345" and "Booked" in sent[0][1]
    assert svc.notifier.log_for("S005")[0]["status"] == "sent"


def test_telegram_without_token_only_logs(svc):
    svc.set_prefs("S005", telegram_chat_id="12345", notify_telegram=True)
    e = svc.notifier.push("S005", "hello")
    assert e["status"].startswith("logged")


# ---------------------------------------------------------------- neural intent model + evaluation
def test_intent_model_and_ensemble(svc):
    label, prob = svc.intent_model.predict("pls cancel tmrw mtg sir")
    assert label == "cancel"
    p = svc.parser.parse("x", "r u free today evening mam? need 10 mins", NOW)
    assert p.intent in ("meeting_request", "availability_query")
    assert p.model_intent is not None


def test_parser_scores_do_not_regress(svc):
    from campussync.ml.evaluate import evaluate, load_rows
    main = evaluate(svc.parser.parse, load_rows(ROOT / "data/eval/mails.jsonl"))
    hold = evaluate(svc.parser.parse, load_rows(ROOT / "data/eval/mails_holdout.jsonl"))
    assert main["exact"] >= 95 and hold["exact"] >= 80, (main["exact"], hold["exact"])


# ---------------------------------------------------------------- speed
def test_scales_to_thousands_of_meetings(svc):
    rnd = random.Random(1)
    people = [p.id for p in svc.people()]
    for i in range(2000):
        a, b = rnd.sample(people, 2)
        d = date(2026, 9, 1) + timedelta(days=rnd.randint(0, 120))
        s = datetime(d.year, d.month, d.day, rnd.randint(8, 17), 0)
        svc.store.data["meetings"].append(to_dict(Meeting(f"X{i}", "bulk", a, [a, b], s, s + timedelta(minutes=30))))
    svc.store.save()
    t0 = _time.time()
    for sid in [f"S{i:03d}" for i in range(1, 21)]:
        svc.engine.find_common_slots(["F101", sid], date(2026, 10, 16), date(2026, 10, 23), 30)
    assert _time.time() - t0 < 3
