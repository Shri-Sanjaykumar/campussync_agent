"""Mock VTOP connector. Reads a JSON export that looks like what a VTOP integration would give
(registrations by FFCS slot, attendance, academic calendar, exam schedule).
A real VTOP connector would implement the same methods against the live portal."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from ..models import (AttendanceRecord, CalendarDay, ClassSession, ExamEntry, Person,
                      WeeklyWindow)
from ..utils import parse_hhmm
from .base import PortalAdapter
from .ffcs import SlotGrid


class MockVTOPPortal(PortalAdapter):
    def __init__(self, settings):
        self.settings = settings
        self.data_path = settings.path_of("portal_data")
        self.grid = SlotGrid(settings.path_of("slot_grid"))
        self.reload()

    def reload(self) -> None:
        if not self.data_path.exists():
            self.data = {"people": [], "sections": [], "registrations": {}, "attendance": {},
                         "office_hours": {}, "duties": {}, "calendar": [], "exams": []}
        else:
            self.data = json.loads(Path(self.data_path).read_text(encoding="utf-8"))
        self._people = {p["id"]: Person(**p) for p in self.data["people"]}
        self._sections = {s["section_id"]: s for s in self.data["sections"]}
        self._class_cache: dict[str, list[ClassSession]] = {}

    def describe(self) -> str:
        return f"Mock VTOP ({self.grid.name}) from {self.data_path.name}"

    # ---------------------------------------------------------------- people
    def list_people(self) -> list[Person]:
        return list(self._people.values())

    def get_person(self, person_id: str) -> Person | None:
        return self._people.get(person_id)

    # ---------------------------------------------------------------- timetable
    def _sections_for(self, person_id: str) -> list[dict]:
        p = self._people.get(person_id)
        if not p:
            return []
        if p.role == "student":
            return [self._sections[s] for s in self.data["registrations"].get(person_id, [])]
        return [s for s in self._sections.values() if s["faculty_id"] == person_id]

    def weekly_classes(self, person_id: str) -> list[ClassSession]:
        if person_id in self._class_cache:
            return self._class_cache[person_id]
        out = []
        for sec in self._sections_for(person_id):
            for wd, s, e, units in self.grid.resolve(sec["slot"]):
                out.append(ClassSession(course_code=sec["course_code"], course_title=sec["course_title"],
                                        slot=sec["slot"], weekday=wd, start=s, end=e, kind=sec["kind"],
                                        venue=sec.get("venue", ""), faculty_id=sec["faculty_id"],
                                        units=units))
        out.sort(key=lambda c: (c.weekday, c.start))
        self._class_cache[person_id] = out
        return out

    def attendance(self, person_id: str) -> list[AttendanceRecord]:
        titles = {s["course_code"]: s["course_title"] for s in self._sections.values()}
        recs = self.data["attendance"].get(person_id, {})
        return [AttendanceRecord(code, titles.get(code, code), a, t) for code, (a, t) in recs.items()]

    # ---------------------------------------------------------------- calendar & exams
    def academic_calendar(self, start: date, end: date) -> list[CalendarDay]:
        out = []
        for c in self.data["calendar"]:
            d = date.fromisoformat(c["date"])
            if start <= d <= end:
                out.append(CalendarDay(d, c["kind"], c.get("label", "")))
        return out

    def exams(self, person_id: str, start: date, end: date) -> list[ExamEntry]:
        p = self._people.get(person_id)
        if not p or p.role != "student":
            return []
        courses = {s["course_code"] for s in self._sections_for(person_id)}
        out = []
        for e in self.data["exams"]:
            d = date.fromisoformat(e["date"])
            if e["course_code"] in courses and start <= d <= end:
                out.append(ExamEntry(e["course_code"], e["title"],
                                     datetime.combine(d, parse_hhmm(e["start"])),
                                     datetime.combine(d, parse_hhmm(e["end"])), e.get("venue", "")))
        return out

    def office_hours(self, person_id: str) -> list[WeeklyWindow]:
        return [WeeklyWindow(w["weekday"], parse_hhmm(w["start"]), parse_hhmm(w["end"]), w.get("title", "Cabin hours"))
                for w in self.data["office_hours"].get(person_id, [])]

    def teams(self, person_id: str) -> list[dict]:
        return [t for t in self.data.get("teams", []) if person_id in t["member_ids"] or t["guide_id"] == person_id]

    def duties(self, person_id: str) -> list[WeeklyWindow]:
        return [WeeklyWindow(w["weekday"], parse_hhmm(w["start"]), parse_hhmm(w["end"]), w.get("title", "Duty"))
                for w in self.data["duties"].get(person_id, [])]
