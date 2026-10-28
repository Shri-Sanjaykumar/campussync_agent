from datetime import date, datetime, time, timedelta

from campussync.core.attendance import classes_can_miss, skip_status
from campussync.core.reminders import meeting_to_ics, parse_ics
from campussync.models import AttendanceRecord, Person
from campussync.utils import overlaps


def test_slot_grid(svc):
    g = svc.portal.grid
    a = g.resolve("A1+TA1")
    assert [(wd, s.strftime("%H:%M")) for wd, s, _e, _u in a] == [(0, "08:00"), (2, "09:00"), (4, "10:00")]
    lab = g.resolve("L31+L32")
    assert lab == [(0, time(14, 0), time(15, 50), 2)]


def test_attendance_rules(svc):
    s = svc.settings
    stu = Person("X", "X", "x@x", "student", cgpa=8.0)
    rec = AttendanceRecord("C1", "Course", 30, 36)  # 83.3%
    st = skip_status(stu, rec, 1, s)
    assert st["skippable"] and st["level"] == "safe"
    low = AttendanceRecord("C1", "Course", 27, 35)  # 77.1% -> 75%
    assert not skip_status(stu, low, 1, s)["skippable"]
    topper = Person("Y", "Y", "y@y", "student", cgpa=9.3)
    assert skip_status(topper, low, 1, s)["level"] == "exempt"
    assert classes_can_miss(AttendanceRecord("C", "C", 30, 36), 75) == 4


def test_common_slots_respect_everyones_busy_time(svc):
    pids = ["F101", "S002"]
    d0 = date(2026, 10, 16)
    slots = svc.engine.find_common_slots(pids, d0, d0 + timedelta(days=6), 30, max_results=5)
    assert slots
    for sl in slots:
        for pid in pids:
            for e in svc.engine.person_events(pid, sl.start.date(), sl.start.date()):
                if e.hard:
                    assert not overlaps(sl.start, sl.end, e.start, e.end), (pid, e.title, sl.start)
        assert time(8, 0) <= sl.start.time() and sl.end.time() <= time(19, 0)
        assert sl.start.weekday() != 6


def test_attendance_aware_hardness(svc):
    # the CGPA-exempt student's classes are all skippable (soft); faculty classes are always hard
    topper = next(p for p in svc.people("student") if p.cgpa and p.cgpa >= 9)
    d = date(2026, 10, 16)
    evs = [e for e in svc.engine.person_events(topper.id, d, d + timedelta(days=4)) if e.kind == "class"]
    assert evs and all(not e.hard for e in evs)
    fac = [e for e in svc.engine.person_events("F101", d, d + timedelta(days=4)) if e.kind == "class"]
    assert fac and all(e.hard for e in fac)


def test_holidays_and_exams_block_time(svc):
    hol = [c.date for c in svc.portal.academic_calendar(date(2026, 10, 15), date(2026, 11, 30)) if c.kind == "holiday"][0]
    assert svc.engine.find_common_slots(["F101", "S001"], hol, hol, 30) == []
    exams = svc.portal.exams("S001", date(2026, 10, 15), date(2026, 11, 5))
    assert exams
    ex = exams[0]
    chk = svc.engine.check_slot(["S001"], ex.start - timedelta(minutes=30), ex.start)
    assert not chk["ok"] and any(c["kind"] in ("exam", "exam_prep") for c in chk["hard"])


def test_ics_roundtrip(svc):
    m = svc.store.meetings()[0]
    ics = meeting_to_ics(m, svc.portal)
    assert "BEGIN:VEVENT" in ics and m.start.strftime("%Y%m%dT%H%M") in ics
    ev = parse_ics(ics)
    assert ev and ev[0]["start"] == m.start and ev[0]["end"] == m.end


def test_reset_really_clears_state(svc):
    n_meet, n_mail = len(svc.store.meetings()), len(svc.store.mails())
    svc.send_mail("S001", ["F101"], "x", "hello")
    svc.reset_demo(svc.now().date())
    assert len(svc.store.meetings()) == n_meet and len(svc.store.mails()) == n_mail


def test_save_survives_windows_style_locks(svc, monkeypatch):
    """On Windows, replacing state.json fails while another tab or antivirus has it open. Saves must retry."""
    import os as _os
    import campussync.store as st
    real = _os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 4:
            raise PermissionError(5, "Access is denied")
        return real(a, b)
    monkeypatch.setattr(st.os, "replace", flaky)
    monkeypatch.setattr(st, "_RETRY_DELAYS", [0, 0, 0, 0, 0])
    svc.store.set_setting("probe", 1)
    assert calls["n"] == 4
    monkeypatch.setattr(st.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(5, "Access is denied")))
    svc.store.set_setting("probe", 2)  # never unlocks: falls back to writing in place
    fresh = st.StateStore(svc.store.path)
    assert fresh.get_setting("probe") == 2
    assert not list(svc.store.path.parent.glob("*.tmp"))


def test_two_stores_writing_at_once(svc):
    """Two browser tabs = two stores on two threads, same file."""
    import threading
    import campussync.store as st
    a, b = st.StateStore(svc.store.path), st.StateStore(svc.store.path)
    errors = []

    def hammer(store, key):
        try:
            for i in range(40):
                store.set_setting(key, i)
                store.load()
        except Exception as e:  # pragma: no cover
            errors.append(e)
    t1, t2 = threading.Thread(target=hammer, args=(a, "x")), threading.Thread(target=hammer, args=(b, "y"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors
    assert not list(svc.store.path.parent.glob("*.tmp"))
