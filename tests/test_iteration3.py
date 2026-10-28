"""Iteration 3: leave (parent mail -> VTOP application -> proctor), mail triage + daily summary, proctor check-ins."""
from datetime import date, datetime, time, timedelta

import pytest

from campussync.utils import overlaps

from conftest import NOW


def _case(svc, sid):
    return next(r for r in svc.leave.all() if r["student_id"] == sid)


# ---------------------------------------------------------------- leave
def test_seeded_leave_cases_follow_the_procedure(svc):
    full, parent_only, no_parent = _case(svc, "S010"), _case(svc, "S012"), _case(svc, "S015")
    assert svc.leave.checklist(full)["ready"] and full["status"] == "pending"
    assert parent_only["status"] == "awaiting_application" and not svc.leave.checklist(parent_only)["applied"]
    assert no_parent["status"] == "pending" and not svc.leave.checklist(no_parent)["parent_mail"]
    assert full["approver_id"] == svc.person("S010").proctor_id


def test_parent_mail_then_vtop_application_match_up(svc):
    proctor = svc.person("S005").proctor_id
    svc.send_mail("P005", [proctor], "Leave", "Respected Sir, my son Karthik has fever. Kindly grant him leave on 22 Oct.")
    case = _case(svc, "S005")
    assert case["status"] == "awaiting_application" and case["parent_mail_id"]
    svc.apply_leave("S005", date(2026, 10, 22), date(2026, 10, 22), "Fever")
    case = _case(svc, "S005")
    assert case["status"] == "pending" and svc.leave.checklist(case)["ready"]
    assert any("Leave application on VTOP" in m.subject for m in svc.mail.inbox(proctor))
    with pytest.raises(ValueError):
        svc.apply_leave("S005", date(2026, 10, 22), date(2026, 10, 22), "again")


def test_leave_impact_counts_only_working_days(svc):
    holiday = next(c.date for c in svc.portal.academic_calendar(date(2026, 10, 15), date(2026, 11, 30)) if c.kind == "holiday")
    an = svc.leave.analyse("S001", holiday, holiday)
    assert an["days"] == 0 and an["classes"] == []
    mon = date(2026, 10, 19)
    an = svc.leave.analyse("S001", mon, mon)
    assert an["days"] == 1 and an["classes"]
    for a in an["attendance"]:
        assert a["after"] <= a["now"]


def test_approve_needs_application_and_parent_mail(svc):
    with pytest.raises(ValueError, match="parent"):
        svc.approve_leave(_case(svc, "S015")["id"], svc.person("S015").proctor_id)
    with pytest.raises(ValueError, match="VTOP"):
        svc.approve_leave(_case(svc, "S012")["id"], svc.person("S012").proctor_id)


def test_approve_blocks_time_tells_faculty_and_moves_meetings(svc):
    case = _case(svc, "S010")
    ws, we = svc.leave.window(case)
    # give the student a meeting inside the leave window so it has to move
    slot = svc.engine.find_common_slots(["F101", "S010"], ws.date(), ws.date(), 30, max_results=1)
    if slot:
        from campussync.models import Meeting
        m = Meeting(svc.store.new_id("MT"), "Lab doubt", "S010", ["F101", "S010"], slot[0].start, slot[0].end)
        svc.store.upsert_meeting(m)
    an = svc.leave.analysis_for(case)
    res = svc.approve_leave(case["id"], case["approver_id"], "Get well soon")
    assert res["request"]["status"] == "approved"
    assert not svc.engine.check_slot(["S010"], ws.replace(hour=10), ws.replace(hour=10, minute=30))["ok"]
    fac = {c["faculty_id"] for c in an["classes"] if c["faculty_id"]}
    for fid in fac:
        assert any(x.subject.startswith("Leave approved: Meghna") for x in svc.mail.inbox(fid))
    assert any(x.subject.startswith("Re: Leave for Meghna") for x in svc.mail.inbox("P010"))
    if slot:
        moved = svc.store.get_meeting(m.id)
        assert not overlaps(moved.start, moved.end, ws, we)


def test_reject(svc):
    case = _case(svc, "S015")
    svc.reject_leave(case["id"], case["approver_id"], "Please ask your parent to mail me first")
    assert _case(svc, "S015")["status"] == "rejected"
    assert svc.mail.inbox("S015")[0].subject.startswith("Leave not approved")


def test_leave_mails_get_procedure_replies(svc):
    proctor = svc.person("S010").proctor_id
    pm = next(m for m in svc.mail.inbox(proctor) if m.from_id == "P010")
    prop = svc.process_mail(pm.id, proctor)
    assert prop.decision == "leave_parent" and "on VTOP" in prop.reply_body
    r = svc.send_mail("S004", [svc.person("S004").proctor_id], "Leave", "Sir, I need leave on 23 Oct for my sister's wedding.")
    prop = svc.process_mail(r["mail"].id, svc.person("S004").proctor_id)
    assert prop.decision == "leave_info" and "apply for the leave on VTOP" in prop.reply_body


# ---------------------------------------------------------------- triage + summary
def test_triage_categories_and_deadlines(svc):
    cats = {m.subject: svc.triage.classify(m)["category"] for m in svc.mail.inbox("S001")}
    assert cats["Review 2 slides deadline"] == "deadline"
    assert cats["Circular: CAT-2 seating"] == "announcement"
    assert cats["Lab record submission"] == "meeting"
    q = next(m for m in svc.mail.inbox("F102") if m.subject == "Java lab")
    assert svc.triage.classify(q)["category"] == "question"
    dl = svc.triage.deadlines("S001")
    assert dl and dl[0]["title"] == "Review 2 slides deadline" and dl[0]["due"].endswith("17:00")
    svc.triage.set_done(dl[0]["id"])
    assert not svc.triage.deadlines("S001")


def test_daily_summary(svc):
    d = svc.digest("F101")
    assert "Needs your reply" in d["text"] and d["pending_leave"]
    m = svc.send_digest("S001")
    assert m.subject.startswith("Your CampusSync summary") and "Review 2 slides" in m.body


# ---------------------------------------------------------------- proctor
def test_proctor_risk_and_plan(svc):
    fid = "F101"
    rows = svc.proctor.overview(fid)
    assert rows and [r["score"] for r in rows] == sorted([r["score"] for r in rows], reverse=True)
    items = svc.proctor.plan(fid, date(2026, 10, 16), date(2026, 10, 24), 15)
    slots = [i["slot"] for i in items if i["slot"]]
    assert slots
    for i, a in enumerate(slots):
        for b in slots[i + 1:]:
            assert not overlaps(a.start, a.end, b.start, b.end)
    first_high = [i for i in items if i["level"] == "high" and i["slot"]]
    if first_high and len(slots) > 1:
        assert first_high[0]["slot"].start <= max(s.start for s in slots)
    booked = svc.proctor.apply(fid, items)
    assert booked and all(m.kind == "proctor" for m in booked)
    s = [p for p in booked[0].participant_ids if p != fid][0]
    assert any(x.subject == "Proctor check-in" for x in svc.mail.inbox(s))
    # planning again skips students who already have one booked
    again = svc.proctor.plan(fid, date(2026, 10, 16), date(2026, 10, 24), 15)
    assert len(again) == len(items) - len(booked)
