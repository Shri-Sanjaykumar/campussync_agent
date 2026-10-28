"""Command-line walkthrough of the agent (no UI needed).
    python scripts/demo.py
Resets the mock data to today, then shows the agent answering the demo mails,
a clash alert, a mass reschedule and a few questions."""
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campussync.service import CampusSync  # noqa: E402
from campussync.utils import fmt_range  # noqa: E402


def line(t=""):
    print("\n" + "=" * 70 + (f"\n{t}" if t else ""))


svc = CampusSync()
info = svc.reset_demo()
print(f"Mock VTOP data generated for {info['anchor']}. LLM: {svc.llm.status()}")

line("1) Faculty inbox: the agent reads each mail and checks both timetables")
for m in svc.mail.inbox("F101"):
    if m.subject == "Prototype demo" or m.kind != "human" or svc.triage.classify(m)["category"] != "meeting":
        continue  # team mail is step 4, leave is step 6
    prop = svc.process_mail(m.id, "F101")
    print(f"\nFrom {svc.person(m.from_id).name}: {m.subject}")
    print(f"  understood: {prop.parsed.intent}, date={prop.parsed.date}, time={prop.parsed.start}, {prop.duration_min} min")
    print(f"  decision:   {prop.decision} - {prop.summary}")
    for n in prop.private_notes:
        print(f"  (private) {n}")
    print("  reply draft:\n    " + prop.reply_body.replace("\n", "\n    "))
    svc.confirm(prop)

line("2) Student uploads a commitment that clashes with a booked review")
review = next(m for m in svc.store.meetings("S001") if m.title == "Project review 2")
c, clashes = svc.add_commitment("S001", "Hackathon finals", review.start - timedelta(minutes=30),
                                review.end + timedelta(minutes=30), "urgent")
for cl in clashes:
    print(f"  [{cl['severity']}] clashes with {cl['with']} on {cl['when']} {cl['note']}")
res, items = svc.reschedule_clashing_meetings("S001", c, [x["meeting_id"] for x in clashes if x["meeting_id"]])
for it in items:
    print(f"  moved '{it.meeting.title}' -> {fmt_range(it.new_slot.start, it.new_slot.end) if it.new_slot else 'no slot'}")

line("3) Faculty can't make it on the review day: everything moves at once")
rev = [m for m in svc.store.meetings("F101") if m.title == "Project review 2"]
if rev:
    d = rev[0].start.date()
    items = svc.plan_unavailability("F101", datetime.combine(d, time(8)), datetime.combine(d, time(19)), "Committee visit")
    for it in items:
        who = [svc.person(p).name for p in it.meeting.participant_ids if p != "F101"]
        print(f"  {who[0]}: {fmt_range(it.meeting.start, it.meeting.end)} -> "
              f"{fmt_range(it.new_slot.start, it.new_slot.end) if it.new_slot else 'no slot'}")
    print("  ", svc.apply_unavailability("F101", datetime.combine(d, time(8)), datetime.combine(d, time(19)),
                                         "Committee visit", items))

line("4) Team meeting: 'our team' pulls in every teammate")
tm = next((m for m in svc.mail.inbox("F101") if m.subject == "Prototype demo" and m.status == "unread"), None)
if tm:
    prop = svc.process_mail(tm.id, "F101")
    print(f"  participants: {[svc.person(p).name for p in prop.participants]}")
    print(f"  decision: {prop.decision} - {prop.summary}")
    print("  reply draft:\n    " + prop.reply_body.replace("\n", "\n    "))

line("5) Cabin-hours queue with waitlist")
full = next((s for s in svc.office_hours.sessions("F102", svc.now().date(), 8) if s["free"] == 0), None)
if full:
    print(f"  Dr. Arjun Menon's session {fmt_range(full['start'], full['end'])} is full")
    svc.office_hours.join_waitlist("S019", "F102", full["start"], "Lab doubt")
    first = full["tokens"][0]
    res = svc.cancel_meeting(first["meeting_id"], first["student_id"])
    got = res["waitlist"]["assigned_to"] if res["waitlist"] else None
    print(f"  {svc.person(first['student_id']).name} cancelled -> token went to {svc.person(got).name if got else 'nobody free'}")

line("6) Leave: parent mail -> VTOP application -> proctor approval")
case = next(r for r in svc.leave.all() if r["student_id"] == "S010")
ck = svc.leave.checklist(case)
an = svc.leave.analysis_for(case)
print(f"  {svc.person('S010').name}, {svc.leave.describe(case)}: parent mail {ck['parent_mail']}, VTOP application {ck['applied']}")
print(f"  {an['days']} working day(s), {len(an['classes'])} classes missed; drops below 75% in: {', '.join(an['below']) or 'none'}")
res = svc.approve_leave(case["id"], case["approver_id"])
print(f"  approved -> {res['faculty_told']} course faculty told, {res['moved']['moved']} meeting(s) moved")

line("7) Proctor check-ins, highest risk first")
plan = svc.proctor.plan("F101", svc.now().date() + timedelta(days=1), svc.now().date() + timedelta(days=8), 15)
for i in plan:
    print(f"  {i['student'].name:18} risk {i['level']:6} -> {fmt_range(i['slot'].start, i['slot'].end) if i['slot'] else 'no slot'}")
svc.proctor.apply("F101", plan)

line("8) Mail triage and daily summary for Dr. Kavitha Raman")
print(svc.digest("F101")["text"])

line("9) Plain questions")
for who, q in [("S003", "When is my guide free this week?"), ("S003", "Book 30 min with my guide tomorrow"),
               ("S001", "When are my exams?")]:
    print(f"\n{svc.person(who).name}: {q}\n" + svc.ask(who, q)["text"])
