"""Real Gmail mailbox, with the college side still mocked.

- Incoming: the owner's Gmail inbox is synced into the agent. Each sender is mapped to a VTOP profile
  (if their address is linked) or to an outside contact.
- Outgoing: mail the owner (or the owner's agent) sends to someone with a real address goes out from
  the owner's Gmail, threaded under the original mail. Mail to mock VTOP profiles (*.mock addresses)
  stays inside the app, like before.
- Handled mails get a 'CampusSync' label in Gmail, so a reset or a second machine knows they're done.

Without a connected account it behaves exactly like the mock mailbox ("demo mailbox")."""
from __future__ import annotations

import email.utils
import re
from datetime import datetime

from ..core.directory import is_real_address
from ..models import Mail
from .gmail_auth import GmailAuth, GmailAuthError
from .gmail_client import SKIP_TABS, GmailAPIClient, ImapSmtpClient, build_message

VIA_RE = re.compile(r"^\s*['\"]?(?P<name>.+?)['\"]?\s+via\s+(?P<group>.+?)\s*$", re.I)


def split_via(from_name: str) -> tuple[str, str]:
    """"'Director TBI' via M.Tech. - Software Engineering 2023 Group" -> ("Director TBI", "M.Tech. - ...")."""
    m = VIA_RE.match(from_name or "")
    return (m.group("name").strip(), m.group("group").strip()) if m else ((from_name or "").strip(), "")
from .mock_mail import MockMailbox


class GmailMailbox(MockMailbox):
    def __init__(self, settings, store):
        super().__init__(settings, store)
        self.settings = settings
        self.auth = GmailAuth(settings)
        self.directory = None  # set by the service
        self.tz = None
        self.prefs_of = None  # callable(pid) -> prefs, set by the service
        self._client = None
        self.last_error: str | None = None
        self.last_sync: datetime | None = None
        self.max_sync = int(settings.get("gmail.sync_max_messages", 30))
        self.sync_days = int(settings.get("gmail.sync_days", 14))
        tabs = settings.get("gmail.skip_tabs", list(SKIP_TABS))
        self.skip_tabs = tuple(str(t).lower() for t in (tabs or []))

    # ---------------------------------------------------------------- connection
    @property
    def demo(self) -> bool:
        return self.store.get_setting("mail_mode") == "demo"

    @property
    def connected(self) -> bool:
        return self._client is not None or self.auth.connected

    @property
    def live(self) -> bool:
        return self.connected and not self.demo

    def set_client(self, client) -> None:  # tests and the app-password check use this
        self._client = client

    def client(self):
        if self._client is None:
            if self.auth.method == "oauth":
                self._client = GmailAPIClient(credentials=self.auth.credentials(), tz=self.tz, skip_tabs=self.skip_tabs)
            elif self.auth.method == "app_password":
                ap = self.auth.app_password()
                self._client = ImapSmtpClient(ap["address"], ap["password"], tz=self.tz, skip_tabs=self.skip_tabs)
            else:
                raise GmailAuthError("Gmail isn't connected")
        return self._client

    def disconnect(self) -> None:
        self.auth.disconnect()
        self._client = None
        self.last_sync = None

    def account(self) -> str | None:
        try:
            return self.client().profile_email() if self.connected else None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    def describe(self) -> str:
        if self.live and self.directory and self.directory.owner_email:
            how = "Sign in with Google" if self.auth.method == "oauth" else "app password"
            return f"Gmail {self.directory.owner_email} ({how}); mock mailboxes for VTOP profiles"
        return "Demo mailbox (Gmail not connected)"

    # ---------------------------------------------------------------- incoming
    def sync(self) -> list[Mail]:
        """Pull new inbox mail from Gmail. Returns the mails that are new to the agent."""
        if not self.live or not self.directory or not self.directory.owner:
            return []
        known = {m.meta.get("gmail_id") for m in self.store.mails() if m.meta.get("gmail_id")}
        try:
            client = self.client()
            self._backfill(client)
            msgs = client.recent(self.max_sync, self.sync_days, skip_ids=known)
            handled = client.handled_label_id()
        except Exception as e:
            self.last_error = f"Couldn't read Gmail: {type(e).__name__}: {e}"
            return []
        self.last_error = None
        self.last_sync = datetime.now()
        owner_email, owner_id = self.directory.owner_email, self.directory.owner_id
        skip_labels = {f"CATEGORY_{t.upper()}" for t in self.skip_tabs}
        new = []
        for rm in sorted(msgs, key=lambda r: r.sent_at):
            if rm.gmail_id in known or rm.from_email == owner_email:
                continue
            if skip_labels & set(rm.labels):
                continue  # Promotions / Social tab: never shown in the app
            person_name, via = split_via(rm.from_name)
            frm = self.directory.resolve(rm.from_email, rm.from_name)
            to_ids = [owner_id]
            for addr in rm.to + rm.cc:
                if addr and addr != rm.from_email:
                    pid = self.directory.resolve(addr)
                    if pid not in to_ids:
                        to_ids.append(pid)
            m = Mail(id=f"G{rm.gmail_id}", from_id=frm, to_ids=to_ids, subject=rm.subject or "(no subject)",
                     body=rm.body, sent_at=rm.sent_at, thread_id=f"g:{rm.thread_id}", kind="human",
                     status="processed" if handled in rm.labels else "unread",
                     meta={"source": "gmail", "gmail_id": rm.gmail_id, "gmail_thread": rm.thread_id,
                           "message_id": rm.message_id, "references": rm.references, "from_email": rm.from_email,
                           "from_name": person_name, "via": via, "labels": list(rm.labels),
                           "bulk": bool(rm.bulk or via), "direct": owner_email in (rm.to + rm.cc),
                           "n_recipients": len(set(rm.to + rm.cc))})
            self.store.upsert_mail(m)
            new.append(m)
        return new

    def _backfill(self, client) -> None:
        """Mail synced by older versions has no Gmail tab or group info: add it once, so promotions pulled in
        back then get hidden and group posts are recognised."""
        old = [m for m in self.store.mails() if m.meta.get("source") == "gmail" and "labels" not in m.meta]
        for m in old:
            labels = []
            if hasattr(client, "labels_of"):
                try:
                    labels = client.labels_of(m.meta["gmail_id"])
                except Exception:
                    labels = []
            name, via = split_via(m.meta.get("from_name", ""))
            m.meta.update({"labels": labels, "from_name": name, "via": via or m.meta.get("via", "")})
            if via:
                m.meta["bulk"] = True
            self.store.upsert_mail(m)

    def relink(self, old_id: str, new_id: str) -> int:
        """After a sender is linked to a VTOP profile, move their existing mails over."""
        if not old_id or old_id == new_id:
            return 0
        n = 0
        for d in self.store.data["mails"]:
            changed = False
            if d["from_id"] == old_id:
                d["from_id"] = new_id
                changed = True
            if old_id in d["to_ids"]:
                d["to_ids"] = [new_id if x == old_id else x for x in d["to_ids"]]
                changed = True
            n += changed
        if n:
            self.store.save()
        return n

    # ---------------------------------------------------------------- outgoing
    def _external_targets(self, mail: Mail) -> list[str]:
        if not self.live or not self.directory or not self.directory.owner:
            return []
        owner = self.directory.owner_id
        if mail.from_id == owner:
            addrs = [self.directory.real_email(p) for p in mail.to_ids if p != owner]
        elif mail.from_id == "agent" and owner in mail.to_ids and self.prefs_of and self.prefs_of(owner).get("copy_to_gmail"):
            addrs = [self.directory.owner_email]  # reminders / summary copied to yourself
        else:
            return []
        return list(dict.fromkeys(a for a in addrs if is_real_address(a)))

    def _parent(self, mail: Mail) -> Mail | None:
        if mail.in_reply_to:
            p = self.store.get_mail(mail.in_reply_to)
            if p and p.meta.get("gmail_thread"):
                return p
        if mail.thread_id and mail.thread_id.startswith("g:"):
            same = [m for m in self.store.mails() if m.thread_id == mail.thread_id and m.meta.get("message_id")]
            if same:
                return max(same, key=lambda m: m.sent_at)
        return None

    def send(self, mail: Mail) -> Mail:
        mail = super().send(mail)
        targets = self._external_targets(mail)
        if not targets:
            return mail
        parent = self._parent(mail)
        owner = self.directory.owner
        from_addr = email.utils.formataddr((owner.get("name") or "", owner["email"]))
        subject = mail.subject
        if parent and not subject.lower().startswith("re:"):
            subject = f"Re: {parent.subject}"
        msg = build_message(from_addr, targets, subject, mail.body,
                            in_reply_to=parent.meta.get("message_id", "") if parent else "",
                            references=parent.meta.get("references", "") if parent else "")
        try:
            res = self.client().send(msg, parent.meta.get("gmail_thread") if parent else None)
            mail.meta.update({"delivered": "gmail", "delivered_to": targets, "sent_gmail_id": res.get("id", "")})
            if parent and parent.meta.get("gmail_id"):
                self._label(parent.meta["gmail_id"])
            elif res.get("threadId") and not mail.thread_id.startswith("g:"):
                mail.thread_id = f"g:{res['threadId']}"
            self.last_error = None
        except Exception as e:
            mail.meta.update({"delivered": "failed", "delivery_error": f"{type(e).__name__}: {e}"})
            self.last_error = f"Couldn't send from Gmail: {type(e).__name__}: {e}. The reply is saved in the app."
        self.store.upsert_mail(mail)
        return mail

    def _label(self, gmail_id: str) -> None:
        try:
            self.client().mark_handled(gmail_id)
        except Exception:
            pass  # a missing label never blocks the actual work

    def mark(self, mail_id, status):
        super().mark(mail_id, status)
        m = self.store.get_mail(mail_id)
        if status == "processed" and m and m.meta.get("gmail_id") and self.live:
            self._label(m.meta["gmail_id"])
