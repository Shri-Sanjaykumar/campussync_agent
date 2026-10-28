"""Mock mail server. Stands in for Outlook / Gmail / the college mail system."""
from __future__ import annotations

from ..models import Mail
from .base import MailAdapter


class MockMailbox(MailAdapter):
    def __init__(self, settings, store):
        self.store = store

    def describe(self) -> str:
        return "Mock mail server (stored in data/state)"

    def send(self, mail: Mail) -> Mail:
        if not mail.id:
            mail.id = self.store.new_id("M")
        if not mail.thread_id:
            mail.thread_id = mail.id
        self.store.upsert_mail(mail)
        return mail

    def get(self, mail_id):
        return self.store.get_mail(mail_id)

    def inbox(self, person_id):
        return sorted([m for m in self.store.mails() if person_id in m.to_ids],
                      key=lambda m: m.sent_at, reverse=True)

    def sent(self, person_id):
        return sorted([m for m in self.store.mails() if m.from_id == person_id],
                      key=lambda m: m.sent_at, reverse=True)

    def mark(self, mail_id, status):
        m = self.store.get_mail(mail_id)
        if m:
            m.status = status
            self.store.upsert_mail(m)

    def thread(self, thread_id):
        return sorted([m for m in self.store.mails() if m.thread_id == thread_id], key=lambda m: m.sent_at)
