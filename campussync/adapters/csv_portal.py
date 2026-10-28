"""Generic CSV connector for any college that can export its timetable as plain files.
Proves the adapter idea: a completely different data format, same agent.

Folder layout (see examples/other_college/):
  people.csv     id,name,email,role,department,cgpa,guide_id,cabin
  timetable.csv  person_id,weekday,start,end,course_code,course_title,kind,venue
  calendar.csv   date,kind,label            (optional)
  exams.csv      person_id,course_code,title,date,start,end,venue   (optional)
  office_hours.csv person_id,weekday,start,end   (optional)
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from ..models import CalendarDay, ClassSession, ExamEntry, Person, WeeklyWindow
from ..utils import WEEKDAYS, parse_hhmm
from .base import PortalAdapter


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def _weekday(v: str) -> int:
    return int(v) if v.isdigit() else WEEKDAYS.index(v[:3].title())


class CSVPortal(PortalAdapter):
    def __init__(self, settings):
        self.folder = settings.path_of("portal_data")
        self._people = {}
        for r in _read(self.folder / "people.csv"):
            self._people[r["id"]] = Person(
                id=r["id"], name=r["name"], email=r.get("email", ""), role=r.get("role", "student"),
                department=r.get("department", ""), cgpa=float(r["cgpa"]) if r.get("cgpa") else None,
                guide_id=r.get("guide_id") or None, cabin=r.get("cabin") or None)
        self._tt = _read(self.folder / "timetable.csv")
        self._cal = _read(self.folder / "calendar.csv")
        self._exams = _read(self.folder / "exams.csv")
        self._oh = _read(self.folder / "office_hours.csv")

    def describe(self) -> str:
        return f"CSV export from {self.folder}"

    def list_people(self):
        return list(self._people.values())

    def get_person(self, person_id):
        return self._people.get(person_id)

    def weekly_classes(self, person_id):
        out = []
        for r in self._tt:
            if r["person_id"] != person_id:
                continue
            out.append(ClassSession(course_code=r.get("course_code", ""), course_title=r.get("course_title", ""),
                                    slot="", weekday=_weekday(r["weekday"]), start=parse_hhmm(r["start"]),
                                    end=parse_hhmm(r["end"]), kind=r.get("kind", "theory"), venue=r.get("venue", "")))
        return sorted(out, key=lambda c: (c.weekday, c.start))

    def academic_calendar(self, start, end):
        out = []
        for r in self._cal:
            d = date.fromisoformat(r["date"])
            if start <= d <= end:
                out.append(CalendarDay(d, r.get("kind", "holiday"), r.get("label", "")))
        return out

    def exams(self, person_id, start, end):
        out = []
        for r in self._exams:
            d = date.fromisoformat(r["date"])
            if r["person_id"] == person_id and start <= d <= end:
                out.append(ExamEntry(r["course_code"], r.get("title", "Exam"),
                                     datetime.combine(d, parse_hhmm(r["start"])),
                                     datetime.combine(d, parse_hhmm(r["end"])), r.get("venue", "")))
        return out

    def office_hours(self, person_id):
        return [WeeklyWindow(_weekday(r["weekday"]), parse_hhmm(r["start"]), parse_hhmm(r["end"]), "Office hours")
                for r in self._oh if r["person_id"] == person_id]
