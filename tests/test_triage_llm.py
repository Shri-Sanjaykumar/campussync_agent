"""Mail sorting with DeepSeek (faked): open-ended categories on top of the core ones."""
import json
from datetime import datetime, timedelta

from campussync.models import Mail

from conftest import NOW


class FakeDeepSeek:
    """Plays the model: labels each mail by keywords, the way a model might (incl. near-duplicate names)."""

    def __init__(self, labels=None):
        self.calls, self.labels = [], labels or {}

    def __call__(self, system, user):
        self.calls.append((system, user))
        emails = json.loads(user)["emails"]
        out = []
        for e in emails:
            text = (e["subject"] + " " + e["body"]).lower()
            cat, due, reply = "Other", None, False
            for key, (c, d, r) in self.labels.items():
                if key in text:
                    cat, due, reply = c, d, r
                    break
            out.append({"id": e["id"], "category": cat, "summary": f"About {e['subject']}", "needs_reply": reply,
                        "deadline": due, "priority": "normal"})
        return {"results": out}


def _on(svc, fake):
    svc.llm.mode, svc.llm.api_key = "auto", "test"
    svc.llm.json = fake
    return fake


def _mail(svc, subject, body, frm="OFF1", to=("F101",)):
    return svc.mail.send(Mail("", frm, list(to), subject, body, NOW, kind="human"))


def test_core_and_new_categories_with_folding(svc):
    fake = _on(svc, FakeDeepSeek({
        "placement drive": ("Placement", "2026-10-20T17:00", True),
        "campus recruitment": ("Placements", None, False),        # near-duplicate -> folded
        "pay the hostel fee": ("Fee Payment", "2026-10-25T23:59", True),
        "meet": ("Meeting request", None, True),                  # synonym of a core category
        "lost and found": ("Lost & Found", None, False),
    }))
    ms = [_mail(svc, "Placement drive: Acme", "Register for the placement drive by 20 Oct 5 pm."),
          _mail(svc, "Campus recruitment update", "Shortlist for campus recruitment is out."),
          _mail(svc, "Hostel fee", "Please pay the hostel fee by 25 October."),
          _mail(svc, "Doubt", "Can we meet tomorrow at 3 pm?", frm="S002"),
          _mail(svc, "Lost and found", "A blue water bottle was found in SJT.")]
    assert svc.triage.classify_many(ms) == 5
    assert len(fake.calls) == 1  # one call for the batch
    cats = [svc.triage.classify(m)["category"] for m in ms]
    assert cats == ["Placement", "Placement", "Fee Payment", "meeting", "Lost & Found"]
    assert set(svc.triage.custom_categories()) == {"Placement", "Fee Payment", "Lost & Found"}
    assert svc.triage.classify(ms[3])["action"] is True  # meetings always need action
    # saved: no new call when classified again, and the next prompt lists the categories so they get reused
    svc.triage.classify(ms[0])
    assert len(fake.calls) == 1
    m6 = _mail(svc, "Another placement drive", "Placement drive for Beta Corp, register by 22 Oct.")
    svc.triage.classify(m6)
    assert '"Placement"' in fake.calls[-1][0]


def test_deadline_kept_only_when_mail_states_a_date(svc):
    _on(svc, FakeDeepSeek({"report": ("Deadline", "2026-10-20T17:00", True),
                           "slides": ("Deadline", "2026-10-21T23:59", True)}))
    a = _mail(svc, "Report", "Submit the report soon please.")          # no date in the text: drop the model's date
    b = _mail(svc, "Slides", "Upload the slides by Wednesday.")
    svc.triage.classify_many([a, b])
    assert svc.triage.classify(a)["deadline"] is None
    assert svc.triage.classify(b)["deadline"] == datetime(2026, 10, 21, 23, 59)
    svc.triage.capture_deadlines(b)
    assert any(d["mail_id"] == b.id for d in svc.triage.deadlines("F101"))


def test_cap_on_new_categories(svc):
    svc.triage.max_custom = 2
    _on(svc, FakeDeepSeek({"alpha": ("Alpha Things", None, False), "beta": ("Beta Things", None, False),
                           "gamma": ("Gamma Things", None, False)}))
    ms = [_mail(svc, n, n + " mail") for n in ("alpha", "beta", "gamma")]
    svc.triage.classify_many(ms)
    assert [svc.triage.classify(m)["category"] for m in ms] == ["Alpha Things", "Beta Things", "other"]
    assert "No new categories can be created" in svc.triage._prompt()


def test_merge_category(svc):
    fake = _on(svc, FakeDeepSeek({"club": ("Club Events", None, False)}))
    m = _mail(svc, "Robotics club", "The robotics club meets on Friday.")
    svc.triage.classify_many([m])
    assert svc.triage.classify(m)["category"] == "Club Events"
    assert svc.triage.merge_category("Club Events", "announcement") == 1
    assert svc.triage.classify(m)["category"] == "announcement"
    m2 = _mail(svc, "Coding club", "The coding club has a session tonight.")
    svc.triage.classify_many([m2])  # model says "Club Events" again -> goes to the merged target
    assert svc.triage.classify(m2)["category"] == "announcement"
    assert "Club Events" not in svc.triage.custom_categories()


def test_failure_falls_back_to_rules_and_retries_later(svc):
    def boom(system, user):
        raise TimeoutError("deepseek slow")
    _on(svc, boom)
    m = _mail(svc, "Doubt", "Can we meet tomorrow at 3 pm?", frm="S002")
    t = svc.triage.classify(m)
    assert t["source"] == "rules" and t["category"] == "meeting" and "deepseek slow" in svc.triage.last_error
    fake = _on(svc, FakeDeepSeek({"meet": ("Meeting", None, True)}))
    assert svc.triage.classify_many([m]) == 1 and svc.triage.classify(m)["source"] == "deepseek"


def test_parent_leave_mail_sorted_by_model_starts_leave_case(svc):
    _on(svc, FakeDeepSeek({"leave": ("Leave Request", None, True)}))
    proctor = svc.person("S005").proctor_id
    svc.send_mail("P005", [proctor], "Leave", "Respected Sir, kindly grant my son Karthik leave on 22 Oct, he has fever.")
    assert any(r["student_id"] == "S005" and r["parent_mail_id"] for r in svc.leave.all())


def test_digest_sorts_inbox_in_one_go(svc):
    fake = _on(svc, FakeDeepSeek({}))
    svc.store.data["triage"].clear()
    svc.digest("F101")
    n = len(svc.triage.pending(svc.mail.inbox("F101")))
    assert n == 0 and 1 <= len(fake.calls) <= 2


def test_eval_script_scores_a_model():
    """The evaluation harness works end to end with a model (a perfect fake here)."""
    from pathlib import Path
    from campussync.ml.eval_triage import run
    rows = [json.loads(x) for x in (Path(__file__).resolve().parent.parent / "data/eval/triage.jsonl").read_text().splitlines()]
    labels = {"meeting": "Meeting", "leave": "Leave Request", "deadline": "Deadline", "question": "Question",
              "announcement": "Announcement", "notification": "Update", "promotion": "Promotion"}

    class Perfect:
        enabled = True

        def json(self, system, user):
            emails = json.loads(user)["emails"]
            out = []
            for e in emails:
                r = next(r for r in rows if r["subject"] == e["subject"])["expect"]
                want = r["category"][0] if isinstance(r["category"], list) else r["category"]
                out.append({"id": e["id"], "category": labels.get(want, "Campus Life " + e["subject"][:3]),
                            "needs_reply": r["needs_reply"], "summary": "x",
                            "deadline": "2026-10-24T23:59" if r["deadline"] else None})
            return {"results": out}
    res = run(rows, Perfect())
    assert res["category"] == 100 and res["needs_reply"] == 100 and res["deadline"] == 100 and not res["error"]
