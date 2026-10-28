"""Maps the adapter names used in config/settings.yaml to classes.
Add your own connector here (see template_adapter.py)."""
from __future__ import annotations

from .csv_portal import CSVPortal
from .gmail_mailbox import GmailMailbox
from .mock_calendar import MockCalendar
from .mock_mail import MockMailbox
from .mock_vtop import MockVTOPPortal

PORTALS = {
    "mock_vtop": MockVTOPPortal,
    "csv": CSVPortal,
}
MAILERS = {
    "mock_mail": MockMailbox,
    "gmail": GmailMailbox,  # real Gmail for the person running the app, mock mailboxes for VTOP profiles
}
CALENDARS = {
    "mock_calendar": MockCalendar,
}


def build_adapters(settings, store):
    def pick(table, kind):
        name = settings.get(f"adapters.{kind}")
        if name not in table:
            raise ValueError(f"Unknown {kind} adapter {name!r}. Available: {', '.join(table)}")
        return table[name]

    portal = pick(PORTALS, "portal")(settings)
    mail = pick(MAILERS, "mail")(settings, store)
    calendar = pick(CALENDARS, "calendar")(settings, store)
    return portal, mail, calendar
