"""Connector interfaces. This is the 'adapter' contract.

To plug the agent into a new college / company / system you only write classes that
implement these three interfaces and register them in registry.py. Nothing in
campussync/core or campussync/agent has to change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..models import (AttendanceRecord, CalendarDay, ClassSession, Event, ExamEntry, Mail,
                      Person, WeeklyWindow)


class PortalAdapter(ABC):
    """Read-only access to the institution's portal (VTOP, ERP, LMS, HR system...)."""

    @abstractmethod
    def list_people(self) -> list[Person]: ...

    @abstractmethod
    def get_person(self, person_id: str) -> Person | None: ...

    @abstractmethod
    def weekly_classes(self, person_id: str) -> list[ClassSession]:
        """Recurring weekly classes a person attends (student) or teaches (faculty)."""

    def attendance(self, person_id: str) -> list[AttendanceRecord]:
        """Optional. Return [] if the system has no attendance data."""
        return []

    def academic_calendar(self, start: date, end: date) -> list[CalendarDay]:
        """Optional. Holidays, exam days, instructional days."""
        return []

    def exams(self, person_id: str, start: date, end: date) -> list[ExamEntry]:
        """Optional. Exams the person writes."""
        return []

    def office_hours(self, person_id: str) -> list[WeeklyWindow]:
        """Optional. Preferred meeting windows (faculty cabin hours)."""
        return []

    def duties(self, person_id: str) -> list[WeeklyWindow]:
        """Optional. Other fixed weekly busy blocks (department meetings, committees)."""
        return []

    def teams(self, person_id: str) -> list[dict]:
        """Optional. Project teams the person belongs to or guides:
        [{id, name, member_ids, guide_id}]"""
        return []

    def describe(self) -> str:
        return self.__class__.__name__


class MailAdapter(ABC):
    @abstractmethod
    def send(self, mail: Mail) -> Mail: ...

    @abstractmethod
    def get(self, mail_id: str) -> Mail | None: ...

    @abstractmethod
    def inbox(self, person_id: str) -> list[Mail]: ...

    @abstractmethod
    def sent(self, person_id: str) -> list[Mail]: ...

    @abstractmethod
    def mark(self, mail_id: str, status: str) -> None: ...

    @abstractmethod
    def thread(self, thread_id: str) -> list[Mail]: ...


class CalendarAdapter(ABC):
    @abstractmethod
    def add_event(self, person_id: str, event: Event) -> None: ...

    @abstractmethod
    def remove_by_ref(self, person_id: str, ref: str) -> None: ...

    @abstractmethod
    def list_events(self, person_id: str) -> list[Event]: ...
