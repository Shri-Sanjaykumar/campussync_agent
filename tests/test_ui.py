"""Clicks through the Streamlit app headlessly: every page renders for every role and the main flows work."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    cfg = yaml.safe_load((ROOT / "config/settings.yaml").read_text())
    cfg["paths"]["portal_data"] = str(tmp_path / "vtop.json")
    cfg["paths"]["state"] = str(tmp_path / "state.json")
    cfg["llm"]["use_llm"] = "off"
    cfg["adapters"]["mail"] = "mock_mail"
    cfg["paths"]["secrets"] = str(tmp_path / "secrets")
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("CAMPUSSYNC_CONFIG", str(p))
    monkeypatch.chdir(ROOT)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)
    at.run()
    assert not at.exception, at.exception
    return at


def ok(at):
    assert not at.exception, at.exception
    return at


def as_user(at, pid):
    return ok(at.sidebar.selectbox(key="acting_as").set_value(pid).run())


def go(at, page, view_key=None, view=None):
    ok(at.sidebar.radio(key="nav").set_value(page).run())
    if view_key:
        ok(at.button_group(key=view_key).set_value(view).run())
    return at


def btn(at, label, startswith=False):
    for b in at.button:
        if (b.label.startswith(label) if startswith else b.label == label) and not b.disabled:
            return b
    raise AssertionError(f"button {label!r} not found; have {[b.label for b in at.button][:40]}")


def toasts(at):
    return " ".join(t.value for t in at.toast)


def test_pages_follow_the_role(app):
    assert app.sidebar.radio(key="nav").options[0] == "Home"
    assert "Students" in app.sidebar.radio(key="nav").options  # F101 is faculty
    as_user(app, "S001")
    assert "Leave" in app.sidebar.radio(key="nav").options and "Students" not in app.sidebar.radio(key="nav").options
    as_user(app, "P001")
    assert [o.split("  (")[0] for o in app.sidebar.radio(key="nav").options] == ["Inbox", "Leave", "Settings"]


@pytest.mark.parametrize("pid", ["F101", "S001", "P001"])
def test_every_page_renders(app, pid):
    as_user(app, pid)
    for page in [o.split("  (")[0] for o in app.sidebar.radio(key="nav").options]:
        go(app, page)
    views = {"Calendar": ("cal_view", ["Upcoming", "Deadlines", "Busy times"]),
             "Settings": ("settings_view", ["Notifications", "Mail and contacts", "Mail categories", "Demo and system"])}
    for page, (key, vs) in views.items():
        if page in app.sidebar.radio(key="nav").options:
            for v in vs:
                go(app, page, key, v)


def test_inbox_agent_drafts_and_approve_books(app):
    go(app, "Inbox")
    r = app.radio(key="sel_mail")
    for mid in r.options:
        app.radio(key="sel_mail").set_value(mid).run()
        ok(app)
        if any(m.value.startswith("##### Book ") for m in app.markdown):
            break
    else:
        raise AssertionError("no mail where the agent proposes a booking")
    btn(app, "Approve and send").click().run()
    ok(app)
    assert "Booked" in toasts(app)


def test_home_open_jumps_to_that_mail(app):
    opens = [b for b in app.button if b.key and b.key.startswith("home_open_")]
    assert opens
    mid = opens[0].key.removeprefix("home_open_")
    opens[0].click().run()
    ok(app)
    assert app.session_state["nav"] == "Inbox"
    assert app.radio(key="sel_mail").value == mid


def test_assistant_suggestion(app):
    as_user(app, "S003")
    go(app, "Assistant")
    ok(app.pills(key="ask_sugg").set_value("When is my guide free this week?").run())
    assert any("Kavitha" in m.value for m in app.markdown)


def test_busy_time_quick_add(app):
    as_user(app, "S001")
    go(app, "Calendar", "cal_view", "Busy times")
    next(t for t in app.text_input if t.label == "Add in plain words").input("Hackathon on 19 Oct 9am-1pm")
    btn(app, "Add").click().run()
    ok(app)
    assert any("Hackathon" in m.value for m in app.markdown) or app.success


def test_cant_make_it_preview(app):
    go(app, "Calendar", "cal_view", "Upcoming")
    btn(app, "Preview").click().run()
    ok(app)


def test_book_cabin_token(app):
    as_user(app, "S015")
    go(app, "Cabin hours")
    free = [b for b in app.button if b.label.endswith("\nfree") and not b.disabled]
    assert free
    free[0].click().run()
    ok(app)
    assert "Booked" in toasts(app)


def test_run_evaluations(app):
    go(app, "Settings", "settings_view", "Demo and system")
    btn(app, "Run the evaluation sets").click().run()
    ok(app)
    assert any("Mail understanding" in m.value for m in app.markdown)


def test_leave_approval_and_student_views(app):
    go(app, "Students")
    assert any("Meghna Bose" in m.value for m in app.markdown)  # F101 proctors her and her leave is pending
    btn(app, "Approve").click().run()
    ok(app)
    assert "Leave approved" in toasts(app)
    as_user(app, "S010")
    go(app, "Leave")
    as_user(app, "P010")
    go(app, "Leave")


def test_plan_checkins(app):
    go(app, "Students", "students_view", "Proctor check-ins")
    btn(app, "Plan check-ins").click().run()
    ok(app)
    assert len(app.dataframe) >= 2  # the overview and the plan


def open_mail(at, pred):
    """Select each mail in the list until pred(at) holds; returns the mail id."""
    for mid in at.radio(key="sel_mail").options:
        ok(at.radio(key="sel_mail").set_value(mid).run())
        if pred(at):
            return mid
    raise AssertionError("no matching mail; headings seen: " + str([m.value for m in at.markdown if m.value.startswith("#")]))


def has(at, test):
    return any(test(m.value) for m in at.markdown)


def test_review_demo_offer_pick_book(app):
    """Steps 1-3 of the README demo: offer times, the student picks one, the faculty's agent books it."""
    go(app, "Inbox")
    open_mail(app, lambda a: has(a, lambda v: v == "#### Project report discussion"))
    assert has(app, lambda v: v.startswith("##### Offer"))
    btn(app, "Approve and send").click().run()
    ok(app)
    as_user(app, "S002")
    go(app, "Inbox")
    open_mail(app, lambda a: has(a, lambda v: v == "##### Pick a time"))
    next(b for b in app.button if b.key and b.key.startswith("pick_")).click().run()
    ok(app)
    assert "option 1 works" in toasts(app)
    as_user(app, "F101")
    go(app, "Inbox")
    open_mail(app, lambda a: has(a, lambda v: "Option 1 works" in v) and has(a, lambda v: v.startswith("##### Book ")))
    btn(app, "Approve and send").click().run()
    ok(app)
    assert "Booked" in toasts(app)
