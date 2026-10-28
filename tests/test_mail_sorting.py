"""Mail sorting rules on top of DeepSeek: group mail never needs a reply, opportunities aren't deadlines,
promotions never show up."""
from datetime import datetime

from campussync.adapters.gmail_client import gmail_query, parse_rfc822
from campussync.core.triage import VERSION
from campussync.llm.rules import parse_times
from campussync.models import Mail

from conftest import NOW
from test_gmail import FakeGmail, connect
from test_triage_llm import FakeDeepSeek, _on

GROUP = {"source": "gmail", "from_email": "23mis@vitstudent.ac.in", "from_name": "Director TBI",
         "via": "M.Tech. - Software Engineering 2023 Group", "bulk": True}


def _mail(svc, subject, body, frm="OFF1", to=("F101",), meta=None):
    return svc.mail.send(Mail("", frm, list(to), subject, body, NOW, kind="human", meta=dict(meta or {})))


def test_group_invite_never_needs_reply(svc):
    _on(svc, FakeDeepSeek({"hackathon": ("Event Invite", None, True)}))  # the model over-eagerly says "reply"
    m = _mail(svc, "Invitation - One day Hackathon - reg", "Dear all VITians, all are invited. Register by 26 Oct.", meta=GROUP)
    t = svc.triage.classify(m)
    assert t["category"] == "Event Invite" and t["action"] is False and t["bulk"] is True


def test_dear_all_meeting_notice_is_not_a_request(svc):
    _on(svc, FakeDeepSeek({"department meeting": ("Meeting", None, True)}))
    m = _mail(svc, "Department meeting", "Dear all faculty members, the department meeting is at 3 pm tomorrow in SJT 101.")
    t = svc.triage.classify(m)
    assert t["category"] == "announcement" and not t["action"]
    assert not svc.is_request(m)
    assert all(r["mail"].id != m.id for r in svc.process_all_unread("F101"))


def test_internship_last_date_is_not_a_deadline(svc):
    _on(svc, FakeDeepSeek({"internship": ("Deadline", "2026-10-30T23:59", True)}))
    m = _mail(svc, "Fwd: Build with Jolenergy | 6-month internships in Vellore",
              "Dear students, Jolenergy is hiring interns. Last date to apply: 30 October.", meta=GROUP)
    svc.after_send(m)
    t = svc.triage.classify(m)
    assert t["category"] == "announcement" and t["deadline"] is None and not t["action"]
    assert not any(d["mail_id"] == m.id for d in svc.triage.deadlines("F101"))


def test_coursework_still_is_a_deadline(svc):
    _on(svc, FakeDeepSeek({"review 2": ("Deadline", "2026-10-23T17:00", False)}))
    m = _mail(svc, "Review 2 slides", "Dear all, submit your Review 2 slides on the portal by Friday 5 pm.",
              frm="F101", to=("S001", "S002"))
    svc.after_send(m)
    assert svc.triage.classify(m)["deadline"] == datetime(2026, 10, 23, 17, 0)
    assert any(d["mail_id"] == m.id for d in svc.triage.deadlines("S001"))


def test_promotions_are_hidden_but_college_mail_never_is(svc):
    _on(svc, FakeDeepSeek({"coursera": ("Promotion", None, False), "library week": ("Promotion", None, False)}))
    promo = _mail(svc, "40% off Coursera Plus", "Coursera Plus is 40% off for a limited time.",
                  frm=svc.directory.resolve("no-reply@m.learn.coursera.org", "Coursera"),
                  meta={"source": "gmail", "from_email": "no-reply@m.learn.coursera.org", "from_name": "Coursera"})
    college = _mail(svc, "Library week", "Library week starts Monday: book fair and talks.",
                    frm=svc.directory.resolve("library@vit.ac.in", "Central Library"),
                    meta={"source": "gmail", "from_email": "library@vit.ac.in", "from_name": "Central Library"})
    assert svc.triage.hidden(svc.triage.classify(promo))
    assert svc.triage.classify(college)["category"] == "announcement"
    ids = [m.id for m in svc.visible_inbox("F101")]
    assert promo.id not in ids and college.id in ids


def test_older_sorting_is_redone_and_its_deadlines_dropped(svc):
    m = _mail(svc, "Fwd: Missed NPTEL Course Registration", "Students who missed NPTEL registration can register "
              "on or before 24 October.", meta=GROUP)
    # what an older version saved: a deadline that needs a reply
    svc.store.data["triage"][m.id] = {"category": "deadline", "summary": "x", "deadline": "2026-10-24T23:59",
                                      "action": True, "priority": "normal", "source": "deepseek"}
    svc.store.data["deadlines"].append({"id": "D999", "person_id": "F101", "title": "NPTEL", "due": "2026-10-24T23:59",
                                        "mail_id": m.id, "from_id": "OFF1", "done": False})
    assert not any(d["id"] == "D999" for d in svc.triage.deadlines("F101"))  # hidden at once
    _on(svc, FakeDeepSeek({"nptel": ("Course Registration", None, False)}))
    assert svc.triage.classify_many([m]) == 1
    t = svc.triage.classify(m)
    assert t["category"] == "Course Registration" and not t["action"]
    assert svc.store.data["triage"][m.id]["v"] == VERSION
    assert not any(d["id"] == "D999" for d in svc.store.data["deadlines"])  # and removed for good


def test_rules_without_deepseek(svc):
    invite = _mail(svc, "Invitation - Poster Exhibition", "Dear students, you are invited to the poster exhibition. "
                   "Submit posters on or before 27 October.", meta=GROUP)
    promo = _mail(svc, "Flash sale", "Get 50% off this weekend only. Shop now!",
                  frm=svc.directory.resolve("offers@shop.example.com", "Shop"),
                  meta={"source": "gmail", "from_email": "offers@shop.example.com"})
    ask = _mail(svc, "Doubt", "Sir, can we meet tomorrow at 3 pm?", frm="S002")
    t1, t2, t3 = (svc.triage.classify(x) for x in (invite, promo, ask))
    assert t1["category"] == "announcement" and not t1["action"] and t1["deadline"] is None
    assert t2["category"] == "promotion"
    assert t3["category"] == "meeting" and t3["action"]


def test_by_a_date_is_not_a_time():
    assert parse_times("please return it by 23 Oct")[0] is None
    assert parse_times("can we meet by 4")[0] is not None


# ---------------------------------------------------------------- Gmail
def test_gmail_query_skips_promotions_and_social():
    q = gmail_query(14)
    assert "in:inbox" in q and "-category:promotions" in q and "-category:social" in q


def test_sync_drops_promotions_tab_and_marks_group_mail(gsvc):
    fake = connect(gsvc)
    fake.deliver("deals@shop.com", "Big sale", "50% off", labels=["CATEGORY_PROMOTIONS", "INBOX"])
    fake.deliver("23mis@vitstudent.ac.in", "Invitation - Hackathon", "Dear all VITians, all are invited.",
                 name="'Director TBI' via M.Tech. - Software Engineering 2023 Group", to=["23mis@vitstudent.ac.in"], bulk=True)
    new = gsvc.sync_gmail(force=True)["new"]
    assert [m.subject for m in new] == ["Invitation - Hackathon"]
    m = new[0]
    assert m.meta["from_name"] == "Director TBI" and m.meta["via"].startswith("M.Tech.") and m.meta["bulk"]
    assert not m.meta["direct"]
    assert gsvc.triage.classify(m)["action"] is False


def test_bulk_headers_detected():
    raw = (b"From: Group <g@vit.ac.in>\r\nTo: g@vit.ac.in\r\nSubject: Notice\r\nList-Id: <g.vit.ac.in>\r\n"
           b"Date: Thu, 15 Oct 2026 02:00:00 +0000\r\n\r\nHello")
    assert parse_rfc822(raw, "i", "t").bulk
    personal = b"From: a@b.com\r\nTo: c@d.com\r\nSubject: x\r\nDate: Thu, 15 Oct 2026 02:00:00 +0000\r\n\r\nHi"
    assert not parse_rfc822(personal, "i", "t").bulk


def test_promotions_synced_by_older_versions_get_hidden(gsvc):
    fake = connect(gsvc)
    fake.labels_of = lambda gid: ["INBOX", "CATEGORY_PROMOTIONS"] if gid == "old1" else ["INBOX"]
    old = Mail("Gold1", gsvc.directory.resolve("deals@shop.com", "Shop"), [gsvc.owner_id], "Sale", "Big sale", NOW,
               kind="human", meta={"source": "gmail", "gmail_id": "old1", "from_email": "deals@shop.com", "from_name": "Shop"})
    grp = Mail("Gold2", gsvc.directory.resolve("23mis@vitstudent.ac.in"), [gsvc.owner_id], "Notice", "Hello", NOW,
               kind="human", meta={"source": "gmail", "gmail_id": "old2", "from_email": "23mis@vitstudent.ac.in",
                                   "from_name": "'Director TBI' via M.Tech. Group"})
    gsvc.store.upsert_mail(old)
    gsvc.store.upsert_mail(grp)
    gsvc.sync_gmail(force=True)
    ids = [m.id for m in gsvc.visible_inbox(gsvc.owner_id)]
    assert "Gold1" not in ids and "Gold2" in ids
    g = gsvc.mail.get("Gold2")
    assert g.meta["from_name"] == "Director TBI" and g.meta["bulk"]
