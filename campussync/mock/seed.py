"""Fills the demo with a fresh mock VTOP export plus some state: booked project reviews,
an outside calendar event, uploaded commitments and five realistic inbox mails."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..models import Commitment, Event, Mail, Meeting
from ..utils import WEEKDAY_FULL, combine, fmt_date
from .generator import write_portal


def _next_instructional(svc, d: date) -> date:
    for _ in range(30):
        if svc.engine.day_kind(d) == "instructional":
            return d
        d += timedelta(days=1)
    return d


def _book(svc, title, organizer, pids, slot, now, location):
    m = Meeting(svc.store.new_id("MT"), title, organizer, pids, slot.start, slot.end, "confirmed", location,
                history=[{"at": now.isoformat(timespec="minutes"), "action": "booked", "by": "seed"}])
    m.thread_id = m.id
    svc.store.upsert_meeting(m)
    svc.reminders.sync_calendar(m)
    svc.reminders.schedule(m)
    return m


def seed_demo(svc, anchor: date | None = None) -> dict:
    anchor = anchor or svc.now().date()
    if svc.settings.get("adapters.portal") == "mock_vtop":
        write_portal(svc.settings.path_of("portal_data"), anchor, svc.portal.grid, int(svc.settings.get("mock.seed", 7)))
        svc.portal.reload()
    keep = dict(svc.store.data.get("settings", {}))
    svc.store.reset()
    svc.store.data["settings"] = keep
    svc.store.save()
    svc._kb_version = None
    now = svc.now()
    eng = svc.engine
    if svc.settings.get("adapters.portal") != "mock_vtop":
        return {"anchor": anchor.isoformat(), "note": "non-VTOP portal: state cleared, no demo seed"}

    # outside calendar event (e.g. synced from Google Calendar)
    d1 = _next_instructional(svc, anchor + timedelta(days=1))
    # put it over 15:00 on d1 so the demo mail asking for 3 pm gets alternatives
    gl = None
    # every candidate covers 15:00-15:20; longest first, so it still lands when the afternoon is busy
    for st_, mins in ((time(14, 30), 90), (time(14, 0), 90), (time(15, 0), 90), (time(15, 0), 60),
                      (time(14, 30), 60), (time(15, 0), 30), (time(14, 50), 30)):
        a_, b_ = combine(d1, st_), combine(d1, st_) + timedelta(minutes=mins)
        if eng.check_slot(["F101"], a_, b_)["ok"]:
            gl = (a_, b_)
            break
    if not gl:
        s = eng.find_common_slots(["F101"], d1, d1 + timedelta(days=3), 90, window=(time(14, 0), time(18, 30)), max_results=1)
        gl = (s[0].start, s[0].end) if s else None
    if gl:
        svc.calendar.add_event("F101", Event(gl[0], gl[1], "Guest lecture coordination", "external", "calendar",
                                             ref="ext-guest-lecture"))

    # uploaded commitments
    for i in range(5):
        d = anchor + timedelta(days=i + 1)
        wins = eng.free_windows(["S001"], d, 90, window=(time(17, 0), time(19, 0))) if eng.day_kind(d) == "instructional" else []
        if wins:
            st = wins[0][0]
            svc.store.upsert_commitment(Commitment(svc.store.new_id("C"), "S001", "Club practice",
                                                   st - timedelta(days=28), st - timedelta(days=28) + timedelta(minutes=90),
                                                   "unofficial", "weekly", anchor + timedelta(days=60), "seed", now))
            break
    wins = eng.free_windows(["F103"], d1, 60, window=(time(8, 0), time(13, 0)))
    if wins:
        st = wins[0][0]
        svc.store.upsert_commitment(Commitment(svc.store.new_id("C"), "F103", "Personal appointment", st,
                                               st + timedelta(minutes=60), "urgent", None, None, "seed", now))

    # booked meetings: project reviews with the guide
    review_day = _next_instructional(svc, anchor + timedelta(days=3))
    f101 = svc.person("F101")
    reserved: dict = {}
    reviews = []
    for sid in ["S001", "S002", "S003", "S004"]:
        slot = None
        d = review_day
        for _ in range(4):
            got = eng.find_common_slots(["F101", sid], d, d, 30, window=(time(9, 0), time(18, 0)),
                                        extra_busy=reserved, max_results=1)
            if got:
                slot = got[0]
                break
            d = _next_instructional(svc, d + timedelta(days=1))
        if slot:
            m = _book(svc, "Project review 2", "F101", ["F101", sid], slot, now, f101.cabin)
            reviews.append(m)
            for p in ("F101", sid):
                reserved.setdefault(p, []).append((slot.start, slot.end, "reserved"))
    lab_day = _next_instructional(svc, anchor + timedelta(days=2))
    got = eng.find_common_slots(["F104", "S001"], lab_day, lab_day + timedelta(days=3), 30,
                                window=(time(12, 0), time(18, 0)), max_results=1)
    lab_meeting = _book(svc, "Lab record check", "F104", ["F104", "S001"], got[0], now, svc.person("F104").cabin) if got else None

    # cabin-hours tokens: part of F101's next session booked, F102's next session completely full
    from ..core.office_hours import OfficeHoursError
    tokens = 0
    for fid, students, fill_all in (("F101", ["S004", "S005", "S006"], False),
                                    ("F102", ["S007", "S008", "S009", "S010", "S011", "S012", "S013", "S014"], True)):
        sessions = [x for x in svc.office_hours.sessions(fid, anchor + timedelta(days=1), 7) if x["free"]]
        if not sessions:
            continue
        sess = sessions[0]
        free = [t for t in sess["tokens"] if t["status"] == "free"]
        queue = list(students)
        for tok in free:
            if not fill_all and tokens >= 3:
                break
            while queue:
                sid = queue.pop(0)
                try:
                    svc.office_hours.book(sid, fid, tok["start"], "Doubt clearing", notify=False)
                    tokens += 1
                    break
                except OfficeHoursError:
                    continue

    # inbox
    base = combine(anchor, time(7, 30))
    tomorrow_wd = WEEKDAY_FULL[_next_instructional(svc, anchor + timedelta(days=1)).weekday()]
    mails = [
        ("S002", ["F101"], "Project report discussion",
         f"Good morning Ma'am,\n\nCan we meet {'tomorrow' if d1 == anchor + timedelta(days=1) else 'on ' + WEEKDAY_FULL[d1.weekday()]} "
         "at 3 pm to discuss my project report? It should take about 20 minutes.\n\n"
         "Thanks,\nDiya Krishnan (23MIS1002)"),
        ("S003", ["F101"], "Review 2 slides",
         "Hi Ma'am,\n\nWhen are you free this week for a 30 minute discussion on my Review 2 slides?\n\nRegards,\nRohan Pillai"),
        ("S005", ["F103"], "Urgent: CAT-2 seating issue",
         "Ma'am, I have an issue with my CAT-2 seating allotment and need to meet you today itself if possible. "
         "15 minutes is enough.\n\nKarthik Subramanian"),
        ("F102", ["S001"], "Lab record submission",
         f"Dear Aarav,\n\nPlease meet me on {tomorrow_wd} morning regarding your lab record submission.\n\nDr. Arjun Menon"),
    ]
    team_day = _next_instructional(svc, anchor + timedelta(days=4))
    mails.append(("S002", ["F101"], "Prototype demo",
                  f"Ma'am,\n\nCan our team meet you on {WEEKDAY_FULL[team_day.weekday()]} afternoon for 30 minutes "
                  f"to show the prototype?\n\nDiya, Aarav and Rohan"))
    if lab_meeting:
        new_day = _next_instructional(svc, lab_meeting.start.date() + timedelta(days=1))
        mails.append(("S001", ["F104"], "Reschedule our meeting",
                      f"Sir,\n\nI need to reschedule our lab record meeting on {fmt_date(lab_meeting.start.date())} because "
                      f"of a clash. Could we move it to {WEEKDAY_FULL[new_day.weekday()]} afternoon?\n\nAarav Sharma"))
    # leave: parent mail + VTOP application (complete), parent mail only, VTOP application only
    l1 = _next_instructional(svc, anchor + timedelta(days=4))
    l2 = _next_instructional(svc, l1 + timedelta(days=1))
    sp = lambda d: f"{d.day} {d.strftime('%B')}"  # noqa: E731
    mails.append(("P010", [svc.person("S010").proctor_id], "Leave for Meghna",
                  f"Respected Sir/Madam,\n\nMy daughter Meghna (23MIS1010) is unwell and the doctor has advised rest. "
                  f"Kindly grant her leave from {sp(l1)} to {sp(l2)}.\n\nRegards,\nMrs. Bose"))
    mails.append(("P012", [svc.person("S012").proctor_id], "Leave request",
                  f"Dear Sir/Madam,\n\nPriya will be absent on {sp(l1)} as we are travelling to Madurai for a family "
                  f"function.\n\nThank you,\nMrs. Venkatesh"))
    # school office circular, a deadline from a guide, a plain question
    stud = [p.id for p in svc.people("student")]
    mails.append(("OFF1", stud, "Circular: CAT-2 seating",
                  f"Dear students,\n\nPlease note that the CAT-2 seating arrangement will be published on VTOP on "
                  f"{sp(anchor + timedelta(days=7))}. Students are informed to check their venue a day before each exam."
                  f"\n\nSCORE Office"))
    rv = _next_instructional(svc, anchor + timedelta(days=3))
    mails.append(("F101", [f"S{i:03d}" for i in range(1, 7)], "Review 2 slides deadline",
                  f"Dear all,\n\nSubmit your Review 2 slides on the portal by {WEEKDAY_FULL[rv.weekday()]} 5 pm. "
                  f"Late submissions won't be reviewed.\n\nDr. Kavitha Raman"))
    mails.append(("S007", ["F102"], "Java lab", "Sir, is tomorrow's Java lab still on? A few of us heard it might be shifted."))
    sent = []
    for i, (frm, to, subj, body) in enumerate(mails):
        thread = lab_meeting.thread_id if subj.startswith("Reschedule") and lab_meeting else ""
        sent.append(svc.mail.send(Mail("", frm, to, subj, body, base + timedelta(minutes=7 * i), thread_id=thread, kind="human")))
    svc.triage.classify_many(sent)  # sort the demo inbox in a couple of DeepSeek calls, not one per mail
    for m in sent:
        svc.after_send(m)
    svc.leave.apply_vtop("S010", l1, l2, "Unwell, doctor advised rest")
    svc.leave.apply_vtop("S015", l1, l1, "Cousin's wedding")
    svc._kb_version = None
    return {"anchor": anchor.isoformat(), "reviews": len(reviews), "mails": len(mails), "tokens": tokens,
            "people": len(svc.people()), "review_day": review_day.isoformat()}
