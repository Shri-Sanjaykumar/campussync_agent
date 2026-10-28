"""Shared data model. Every connector converts its system's data into these objects,
so the scheduling core never needs to know which college or portal it is talking to."""
from __future__ import annotations

import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, time
from typing import Any, Optional, Union, get_args, get_origin, get_type_hints


# ---------------------------------------------------------------- people / portal
@dataclass
class Person:
    id: str
    name: str
    email: str
    role: str  # "student" | "faculty" | "staff"
    department: str = ""
    cgpa: Optional[float] = None
    reg_no: Optional[str] = None
    guide_id: Optional[str] = None
    proctor_id: Optional[str] = None
    cabin: Optional[str] = None
    ward_id: Optional[str] = None  # parents: the student they're the parent of


@dataclass
class ClassSession:
    """One weekly occurrence of a class (already resolved from slots to clock times)."""
    course_code: str
    course_title: str
    slot: str
    weekday: int  # 0 = Monday
    start: time
    end: time
    kind: str = "theory"  # theory | lab
    venue: str = ""
    faculty_id: Optional[str] = None
    units: int = 1  # attendance units this session counts for


@dataclass
class AttendanceRecord:
    course_code: str
    course_title: str
    attended: int
    total: int


@dataclass
class WeeklyWindow:
    weekday: int
    start: time
    end: time
    title: str = ""


@dataclass
class CalendarDay:
    date: date
    kind: str  # instructional | holiday | exam | weekend
    label: str = ""


@dataclass
class ExamEntry:
    course_code: str
    title: str
    start: datetime
    end: datetime
    venue: str = ""


# ---------------------------------------------------------------- agent-owned records
@dataclass
class Event:
    """A concrete busy (or soft-busy) block on someone's day."""
    start: datetime
    end: datetime
    title: str
    kind: str  # class | exam | exam_prep | holiday | duty | commitment | meeting | external | blocked
    source: str = ""
    hard: bool = True
    ref: Optional[str] = None
    meta: dict = field(default_factory=dict)


@dataclass
class Commitment:
    id: str
    person_id: str
    title: str
    start: datetime
    end: datetime
    category: str = "unofficial"  # urgent | unofficial | official
    recurrence: Optional[str] = None  # None | weekly
    until: Optional[date] = None
    notes: str = ""
    created_at: Optional[datetime] = None


@dataclass
class Meeting:
    id: str
    title: str
    organizer_id: str
    participant_ids: list[str]
    start: datetime
    end: datetime
    status: str = "confirmed"  # confirmed | cancelled | needs_attention
    location: str = ""
    source_mail_id: Optional[str] = None
    thread_id: Optional[str] = None
    history: list[dict] = field(default_factory=list)
    kind: str = "meeting"  # meeting | office_hours


@dataclass
class Mail:
    id: str
    from_id: str
    to_ids: list[str]
    subject: str
    body: str
    sent_at: datetime
    thread_id: str = ""
    in_reply_to: Optional[str] = None
    kind: str = "human"  # human | agent | reminder | notification
    status: str = "unread"  # unread | processed
    meta: dict = field(default_factory=dict)


@dataclass
class Reminder:
    id: str
    meeting_id: str
    person_id: str
    send_at: datetime
    status: str = "scheduled"  # scheduled | sent | cancelled


@dataclass
class Alert:
    id: str
    person_id: str
    created_at: datetime
    level: str  # info | warning | critical
    message: str
    related: dict = field(default_factory=dict)


@dataclass
class Slot:
    start: datetime
    end: datetime
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    soft_conflicts: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # group meetings: who can't make this option


# ---------------------------------------------------------------- (de)serialisation
def _enc(v: Any) -> Any:
    if is_dataclass(v) and not isinstance(v, type):
        return {f.name: _enc(getattr(v, f.name)) for f in fields(v)}
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    return v


def to_dict(obj: Any) -> Any:
    return _enc(obj)


def _dec(tp: Any, v: Any) -> Any:
    if v is None:
        return None
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        return _dec(args[0], v) if args else v
    if origin in (list, typing.List):
        args = get_args(tp)
        return [_dec(args[0], x) for x in v] if args else list(v)
    if origin in (dict, typing.Dict):
        return dict(v)
    if tp is datetime:
        return v if isinstance(v, datetime) else datetime.fromisoformat(v)
    if tp is date:
        return v if isinstance(v, date) else date.fromisoformat(v)
    if tp is time:
        return v if isinstance(v, time) else time.fromisoformat(v)
    if is_dataclass(tp):
        return from_dict(tp, v)
    return v


_HINTS: dict[type, dict] = {}


def from_dict(cls: type, d: dict) -> Any:
    hints = _HINTS.get(cls)
    if hints is None:
        hints = _HINTS[cls] = get_type_hints(cls)
    kwargs = {f.name: _dec(hints[f.name], d[f.name]) for f in fields(cls) if f.name in d}
    return cls(**kwargs)
