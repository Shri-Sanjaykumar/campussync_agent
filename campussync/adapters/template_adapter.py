"""Starting point for a real connector (live VTOP, another college ERP, Google Calendar, Outlook...).

Steps:
1. Copy this file, rename the class, fill in the methods.
2. Register it in registry.py, e.g. PORTALS["vtop_live"] = LiveVTOPPortal
3. Set adapters.portal: vtop_live in your settings YAML.

Only the methods marked @abstractmethod in base.py are required. The optional ones
(attendance, exams, academic_calendar, office_hours, duties) switch the matching
agent features on when they return data.
"""
from __future__ import annotations

from .base import PortalAdapter


class TemplatePortal(PortalAdapter):
    def __init__(self, settings):
        self.settings = settings
        # e.g. self.session = login(settings.get("portal.url"), token_from_env)

    def list_people(self):
        raise NotImplementedError("Return Person objects from the directory / roster")

    def get_person(self, person_id):
        raise NotImplementedError

    def weekly_classes(self, person_id):
        raise NotImplementedError("Return ClassSession objects (weekday + start/end times)")
