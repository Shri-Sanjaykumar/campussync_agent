"""Mock calendar. Stands in for Google Calendar / Outlook Calendar.
Events whose ref starts with 'meeting:' were written by the agent; anything else
counts as an outside event (and is treated as busy time)."""
from __future__ import annotations

from ..models import Event, from_dict, to_dict
from .base import CalendarAdapter


class MockCalendar(CalendarAdapter):
    def __init__(self, settings, store):
        self.store = store

    def describe(self) -> str:
        return "Mock calendar (stored in data/state)"

    def add_event(self, person_id, event):
        cal = self.store.data["calendar"].setdefault(person_id, [])
        if event.ref:
            cal[:] = [e for e in cal if e.get("ref") != event.ref]
        cal.append(to_dict(event))
        self.store.save()

    def remove_by_ref(self, person_id, ref):
        cal = self.store.data["calendar"].get(person_id, [])
        self.store.data["calendar"][person_id] = [e for e in cal if e.get("ref") != ref]
        self.store.save()

    def list_events(self, person_id):
        return [from_dict(Event, e) for e in self.store.data["calendar"].get(person_id, [])]
