"""Attendance-aware rules: can this student skip this class for a meeting?"""
from __future__ import annotations

import math

from ..models import AttendanceRecord, Person


def classes_can_miss(rec: AttendanceRecord, minimum: float) -> int:
    """How many more class units can be missed and still stay at or above the minimum."""
    if rec.total == 0:
        return 0
    k = math.floor(rec.attended * 100 / minimum - rec.total)
    return max(0, k)


def skip_status(person: Person, rec: AttendanceRecord | None, units: int, settings) -> dict:
    minimum = float(settings.get("attendance.minimum_percent", 75))
    margin = float(settings.get("attendance.safety_margin_percent", 2))
    exempt_cgpa = settings.get("attendance.exemption_cgpa")
    exempt = (exempt_cgpa is not None and person.cgpa is not None and person.cgpa >= float(exempt_cgpa))
    if rec is None or rec.total == 0:
        return {"skippable": exempt, "level": "exempt" if exempt else "unknown", "exempt": exempt,
                "percent": None, "percent_if_missed": None, "can_miss": None}
    pct = rec.attended / rec.total * 100
    after = rec.attended / (rec.total + units) * 100
    if exempt:
        level, ok = "exempt", True
    elif after >= minimum + margin:
        level, ok = "safe", True
    elif after >= minimum:
        level, ok = "borderline", False
    else:
        level, ok = "unsafe", False
    return {"skippable": ok, "level": level, "exempt": exempt, "percent": round(pct, 1),
            "percent_if_missed": round(after, 1), "can_miss": classes_can_miss(rec, minimum),
            "course": rec.course_code, "course_title": rec.course_title}


def attendance_summary(person: Person, records: list[AttendanceRecord], settings) -> list[dict]:
    minimum = float(settings.get("attendance.minimum_percent", 75))
    out = []
    for r in records:
        st = skip_status(person, r, 1, settings)
        if st["exempt"]:
            status = "exempt (CGPA)"
        elif st["percent"] is not None and st["percent"] < minimum:
            status = "below minimum"
        elif st["can_miss"] == 0:
            status = "can't miss any"
        else:
            status = "ok"
        out.append({"course_code": r.course_code, "course_title": r.course_title, "attended": r.attended,
                    "total": r.total, "percent": st["percent"], "can_miss": st["can_miss"], "status": status})
    return sorted(out, key=lambda x: (x["percent"] or 0))
