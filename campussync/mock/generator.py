"""Builds the mock VTOP export (data/mock/vtop.json) relative to an anchor date,
so the demo always has 'tomorrow', 'this week' and an upcoming CAT-2 window.
All names, codes, rooms and dates are invented."""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from ..adapters.ffcs import SlotGrid
from ..utils import overlaps, combine

FACULTY = [
    ("F101", "Dr. Kavitha Raman", "SJT 213 Cabin"),
    ("F102", "Dr. Arjun Menon", "SJT 215 Cabin"),
    ("F103", "Prof. Sneha Iyer", "TT 412 Cabin"),
    ("F104", "Dr. Rahul Verma", "TT 418 Cabin"),
    ("F105", "Dr. Meera Nair", "SJT 310 Cabin"),
    ("F106", "Prof. Vikram Das", "SMV 120 Cabin"),
]
STUDENTS = ["Aarav Sharma", "Diya Krishnan", "Rohan Pillai", "Ananya Reddy", "Karthik Subramanian",
            "Ishita Gupta", "Aditya Rajan", "Neha Joseph", "Vivek Rao", "Meghna Bose",
            "Rithvik Thomas", "Priya Venkatesh", "Aditi Das", "Kavya Suresh", "Siddharth Jain",
            "Nikhila Varma", "Harish Kumar", "Lakshmi Prasad", "Tanvi Shah", "Joel Mathew"]
THEORY = [("SWE3001", "Deep Learning", "A"), ("SWE3002", "Software Metrics", "B"),
          ("SWE3003", "Design Patterns", "C"), ("SWE3004", "Cyber Security", "D"),
          ("SWE3005", "UI/UX and HCI", "E"), ("SWE3006", "Java and J2EE", "F")]
LABS = [("SWE3001P", "Deep Learning Lab", ["L31+L32", "L53+L54", "L9+L10"], 0),
        ("SWE3006P", "Java and J2EE Lab", ["L36+L37", "L48+L49", "L19+L20"], 5)]
ROOMS = ["SJT 301", "SJT 304", "TT 205", "TT 208", "SMV 110", "SJT 402"]
GUIDES = {**{f"S{i:03d}": "F101" for i in range(1, 7)}, **{f"S{i:03d}": "F102" for i in range(7, 11)},
          **{f"S{i:03d}": "F103" for i in range(11, 14)}, **{f"S{i:03d}": "F104" for i in range(14, 17)},
          "S017": "F105", "S018": "F105", "S019": "F106", "S020": "F106"}


def _sessions(grid: SlotGrid, slot: str):
    return grid.resolve(slot)


def _clash(a: list, b: list) -> bool:
    for wd1, s1, e1, _ in a:
        for wd2, s2, e2, _ in b:
            if wd1 == wd2 and s1 < e2 and s2 < e1:
                return True
    return False


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _next_monday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7)


def build_calendar(anchor: date) -> tuple[list[dict], dict]:
    sem_start = anchor - timedelta(days=84 + anchor.weekday())
    cat_start = _next_monday(anchor + timedelta(days=11))
    cat_days = [cat_start + timedelta(days=i) for i in range(5)]
    fat_start = _next_monday(anchor + timedelta(days=46))
    fat_days = [d for d in (fat_start + timedelta(days=i) for i in range(8)) if d.weekday() < 5][:6]
    holidays = {}
    for offset, label in [(-40, "Institute holiday (mock)"), (6, "Institute holiday (mock)"),
                          (27, "Festival holiday (mock)")]:
        h = _next_weekday(anchor + timedelta(days=offset))
        while h in cat_days or h in fat_days or h == anchor:
            h = _next_weekday(h + timedelta(days=1))
        holidays[h] = label
    cal = []
    d = sem_start
    end = anchor + timedelta(days=75)
    while d <= end:
        if d in holidays:
            kind, label = "holiday", holidays[d]
        elif d in cat_days:
            kind, label = "exam", "CAT-2 exam week (mock)"
        elif d in fat_days:
            kind, label = "exam", "FAT exams (mock)"
        elif d.weekday() >= 5:
            kind, label = "weekend", "Saturday" if d.weekday() == 5 else "Sunday"
        elif d > fat_days[-1]:
            kind, label = "break", "Semester break (mock)"
        else:
            kind, label = "instructional", "Instructional day"
        cal.append({"date": d.isoformat(), "kind": kind, "label": label})
        d += timedelta(days=1)
    meta = {"semester_start": sem_start, "cat_days": cat_days, "fat_days": fat_days, "holidays": holidays}
    return cal, meta


def generate_portal(anchor: date, grid: SlotGrid, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    people, sections = [], []

    for fid, name, cabin in FACULTY:
        first, last = name.split()[-2].lower(), name.split()[-1].lower()
        people.append({"id": fid, "name": name, "email": f"{first}.{last}@vit.mock", "role": "faculty",
                       "department": "SCORE (mock)", "cabin": cabin})

    # theory sections: morning variant (x1) and afternoon variant (x2)
    for i, (code, title, letter) in enumerate(THEORY):
        for v, fac in ((1, FACULTY[i][0]), (2, FACULTY[(i + 1) % 6][0])):
            sections.append({"section_id": f"{code}-{letter}{v}", "course_code": code, "course_title": title,
                             "slot": f"{letter}{v}+T{letter}{v}", "faculty_id": fac, "kind": "theory",
                             "venue": ROOMS[(i + v) % len(ROOMS)]})

    def fac_sessions(fid):
        out = []
        for s in sections:
            if s["faculty_id"] == fid:
                out += _sessions(grid, s["slot"])
        return out

    # lab sections: assign a faculty who is free at that time
    for code, title, slots, course_idx in LABS:
        prefer = [FACULTY[course_idx][0], FACULTY[(course_idx + 1) % 6][0]] + [f[0] for f in FACULTY]
        for j, slot in enumerate(slots):
            ss = _sessions(grid, slot)
            fac = next(f for f in prefer if not _clash(fac_sessions(f), ss))
            sections.append({"section_id": f"{code}-{slot}", "course_code": code, "course_title": title,
                             "slot": slot, "faculty_id": fac, "kind": "lab", "venue": f"SJT 8{j}{course_idx} Lab"})

    by_course: dict[str, list[dict]] = {}
    for s in sections:
        by_course.setdefault(s["course_code"], []).append(s)

    registrations, attendance = {}, {}
    weeks = 12
    high_cgpa = {"S004", "S009", "S015"}
    for idx, name in enumerate(STUDENTS, start=1):
        sid = f"S{idx:03d}"
        reg = f"23MIS{1000 + idx}"
        for _ in range(200):
            chosen, sess = [], []
            for code, _t, _l in THEORY:
                sec = rnd.choice(by_course[code])
                chosen.append(sec)
                sess += _sessions(grid, sec["slot"])
            ok = True
            for code, *_ in LABS:
                options = [s for s in by_course[code] if not _clash(sess, _sessions(grid, s["slot"]))]
                if not options:
                    ok = False
                    break
                sec = rnd.choice(options)
                chosen.append(sec)
                sess += _sessions(grid, sec["slot"])
            if ok:
                break
        registrations[sid] = [s["section_id"] for s in chosen]
        base = rnd.uniform(0.70, 0.97)
        att = {}
        for sec in chosen:
            units = sum(u for *_x, u in _sessions(grid, sec["slot"]))
            total = units * weeks - rnd.randint(0, 2)
            rate = min(1.0, max(0.62, base + rnd.uniform(-0.06, 0.06)))
            att[sec["course_code"]] = [round(total * rate), total]
        attendance[sid] = att
        cgpa = round(rnd.uniform(9.05, 9.6), 2) if sid in high_cgpa else round(rnd.uniform(7.0, 8.95), 2)
        first, last = name.split()[0].lower(), name.split()[-1].lower()
        people.append({"id": sid, "name": name, "email": f"{first}.{last}2023@vitstudent.mock", "role": "student",
                       "department": "M.Tech SE (mock)", "cgpa": cgpa, "reg_no": reg,
                       "guide_id": GUIDES[sid], "proctor_id": FACULTY[(idx + 2) % 6][0]})

    # parents (they mail the proctor for leave) and the SCORE office (sends circulars)
    for idx, name in enumerate(STUDENTS, start=1):
        last = name.split()[-1]
        title = "Mr." if idx % 2 else "Mrs."
        people.append({"id": f"P{idx:03d}", "name": f"{title} {last}", "email": f"{title[:-1].lower()}.{last.lower()}{idx}@parent.mock",
                       "role": "parent", "department": f"Parent of {name}", "ward_id": f"S{idx:03d}"})
    people.append({"id": "OFF1", "name": "SCORE Office", "email": "score.office@vit.mock", "role": "staff",
                   "department": "School office (mock)"})

    # faculty cabin hours (2 x 1 hour where they don't teach) and duties
    office_hours, duties = {}, {}
    for fid, _n, _c in FACULTY:
        busy = fac_sessions(fid)
        cands = []
        for wd in range(5):
            for p in (1, 2, 3, 5, 6, 7):
                s, e = grid.periods[p]
                end = combine(date(2000, 1, 3), s).replace(hour=s.hour + 1).time()
                if not _clash(busy, [(wd, s, end, 1)]):
                    cands.append((wd, s, end))
        picks = rnd.sample(cands, 2)
        office_hours[fid] = [{"weekday": wd, "start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"),
                              "title": "Cabin hours"} for wd, s, e in sorted(picks)]
        duties[fid] = [{"weekday": 5, "start": "10:00", "end": "11:30", "title": "Department meeting"}]
    from datetime import time as _t
    if not _clash(fac_sessions("F101"), [(3, _t(14, 0), _t(15, 0), 1)]):
        duties["F101"].append({"weekday": 3, "start": "14:00", "end": "15:00", "title": "Research committee"})

    calendar, meta = build_calendar(anchor)
    exams = []
    for i, (code, title, _l) in enumerate(THEORY):
        d = meta["cat_days"][i // 2]
        start, end = ("09:30", "11:00") if i % 2 == 0 else ("14:00", "15:30")
        exams.append({"course_code": code, "title": f"CAT-2: {title}", "date": d.isoformat(),
                      "start": start, "end": end, "venue": "SJT exam hall (mock)"})
        fd = meta["fat_days"][i]
        exams.append({"course_code": code, "title": f"FAT: {title}", "date": fd.isoformat(),
                      "start": "09:30", "end": "12:30", "venue": "TT exam hall (mock)"})

    projects = ["Smart attendance tracker", "Campus food demand forecast", "Hostel complaint portal",
                "Library seat booking", "Placement prep chatbot", "Lab equipment tracker", "Bus route optimiser",
                "Event ticketing app"]
    teams, by_guide = [], {}
    for sid, fid in GUIDES.items():
        by_guide.setdefault(fid, []).append(sid)
    for fid, sids in by_guide.items():
        for i in range(0, len(sids), 3):
            n = len(teams) + 1
            teams.append({"id": f"T{n:02d}", "name": f"Team {n}: {projects[(n - 1) % len(projects)]}",
                          "member_ids": sids[i:i + 3], "guide_id": fid})

    return {
        "institution": "VIT Vellore (mock VTOP export)",
        "generated_for": anchor.isoformat(),
        "semester": "Fall 2026-27 (mock)",
        "people": people,
        "sections": sections,
        "registrations": registrations,
        "attendance": attendance,
        "office_hours": office_hours,
        "duties": duties,
        "calendar": calendar,
        "exams": exams,
        "teams": teams,
    }


def write_portal(path: Path, anchor: date, grid: SlotGrid, seed: int = 7) -> dict:
    data = generate_portal(anchor, grid, seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data
