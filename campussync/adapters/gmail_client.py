"""Talking to Gmail. Both clients expose the same small interface, so the mailbox adapter doesn't
care which one is in use:

    profile_email()                       -> "you@gmail.com"
    recent(max_n, days)                   -> list[RawMessage]   (newest first)
    send(EmailMessage, thread_id)         -> {"id", "threadId"}
    mark_handled(gmail_id)                -> adds the 'CampusSync' label

GmailAPIClient uses the Gmail API (Sign in with Google). ImapSmtpClient uses IMAP + SMTP with an
app password. Messages are fetched as raw RFC 822 in both cases and parsed by the same function."""
from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import html as _html
import imaplib
import re
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

LABEL = "CampusSync"


@dataclass
class RawMessage:
    gmail_id: str
    thread_id: str
    from_name: str
    from_email: str
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    sent_at: datetime  # local naive
    message_id: str = ""
    references: str = ""
    labels: list[str] = field(default_factory=list)
    bulk: bool = False  # sent to a mailing list or in bulk (List-Id, List-Unsubscribe, Precedence: bulk...)


# Gmail inbox tabs that never reach the agent (marketing and social-network mail).
SKIP_TABS = ("promotions", "social")


def gmail_query(days: int, skip_tabs=SKIP_TABS) -> str:
    """Gmail search used to pull mail: the inbox for the last few days, minus the skipped tabs."""
    return " ".join([f"in:inbox newer_than:{days}d"] + [f"-category:{t}" for t in skip_tabs])


def is_bulk_header(msg) -> bool:
    if msg.get("List-Id") or msg.get("List-Unsubscribe") or msg.get("Mailing-list"):
        return True
    if str(msg.get("Precedence", "")).strip().lower() in ("bulk", "list", "junk"):
        return True
    auto = str(msg.get("Auto-Submitted", "")).strip().lower()
    return bool(auto) and auto != "no"


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"[ \t]+", " ", _html.unescape(h)).strip()


def _strip_quoted(text: str) -> str:
    """Drop the quoted earlier mail under a reply ("On Mon, ... wrote:" and "> " lines)."""
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*On .{5,200} wrote:\s*$", line) or re.match(r"^-{2,}\s*Original Message", line, re.I):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    out = "\n".join(lines).strip()
    return out or text.strip()


def parse_rfc822(raw: bytes, gmail_id: str, thread_id: str, labels: list[str] | None = None,
                 tz=None) -> RawMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = email.utils.parseaddr(str(msg.get("From", "")))
    to = [a.lower() for _n, a in email.utils.getaddresses([str(x) for x in msg.get_all("To", [])]) if a]
    cc = [a.lower() for _n, a in email.utils.getaddresses([str(x) for x in msg.get_all("Cc", [])]) if a]
    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except Exception:
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        body = _html_to_text(content) if part.get_content_type() == "text/html" else content
    try:
        dt = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(tz).replace(tzinfo=None) if tz else dt.astimezone().replace(tzinfo=None)
    except Exception:
        dt = datetime.now()
    return RawMessage(gmail_id=gmail_id, thread_id=thread_id, from_name=name or addr.split("@")[0],
                      from_email=addr.lower(), to=to, cc=cc, subject=str(msg.get("Subject", "")).strip(),
                      body=_strip_quoted(body), sent_at=dt.replace(second=0, microsecond=0),
                      message_id=str(msg.get("Message-ID", "")).strip(), references=str(msg.get("References", "")).strip(),
                      labels=list(labels or []), bulk=is_bulk_header(msg))


def build_message(from_addr: str, to: list[str], subject: str, body: str, in_reply_to: str = "",
                  references: str = "") -> EmailMessage:
    m = EmailMessage()
    m["From"] = from_addr
    m["To"] = ", ".join(to)
    m["Subject"] = subject
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = (references + " " + in_reply_to).strip()
    m.set_content(body)
    return m


# ===================================================================== Gmail API
class GmailAPIClient:
    def __init__(self, credentials=None, service=None, tz=None, skip_tabs=SKIP_TABS):
        if service is None:
            from googleapiclient.discovery import build
            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        self.svc = service
        self.tz = tz
        self.skip_tabs = tuple(skip_tabs)
        self._label_id: str | None = None
        self._email: str | None = None

    def profile_email(self) -> str:
        if not self._email:
            self._email = self.svc.users().getProfile(userId="me").execute()["emailAddress"].lower()
        return self._email

    def recent(self, max_n: int = 30, days: int = 14, skip_ids: set[str] | None = None) -> list[RawMessage]:
        res = self.svc.users().messages().list(userId="me", q=gmail_query(days, self.skip_tabs), maxResults=max_n).execute()
        out = []
        for ref in res.get("messages", []):
            if skip_ids and ref["id"] in skip_ids:
                continue
            full = self.svc.users().messages().get(userId="me", id=ref["id"], format="raw").execute()
            raw = base64.urlsafe_b64decode(full["raw"].encode())
            out.append(parse_rfc822(raw, full["id"], full.get("threadId", full["id"]), full.get("labelIds", []), self.tz))
        return out

    def labels_of(self, gmail_id: str) -> list[str]:
        r = self.svc.users().messages().get(userId="me", id=gmail_id, format="minimal").execute()
        return r.get("labelIds", [])

    def send(self, msg: EmailMessage, thread_id: str | None = None) -> dict:
        body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            body["threadId"] = thread_id
        return self.svc.users().messages().send(userId="me", body=body).execute()

    def _label(self) -> str:
        if self._label_id:
            return self._label_id
        for lb in self.svc.users().labels().list(userId="me").execute().get("labels", []):
            if lb["name"] == LABEL:
                self._label_id = lb["id"]
                return self._label_id
        lb = self.svc.users().labels().create(userId="me", body={"name": LABEL, "labelListVisibility": "labelShow",
                                                                  "messageListVisibility": "show"}).execute()
        self._label_id = lb["id"]
        return self._label_id

    def handled_label_id(self) -> str:
        return self._label()

    def mark_handled(self, gmail_id: str) -> None:
        self.svc.users().messages().modify(userId="me", id=gmail_id, body={"addLabelIds": [self._label()]}).execute()


# ===================================================================== IMAP + SMTP (app password)
class ImapSmtpClient:
    IMAP_HOST, SMTP_HOST = "imap.gmail.com", "smtp.gmail.com"

    def __init__(self, address: str, password: str, tz=None, imap_factory=None, smtp_factory=None, skip_tabs=SKIP_TABS):
        self.address, self.password, self.tz = address.lower(), password, tz
        self.skip_tabs = tuple(skip_tabs)
        self.imap_factory = imap_factory or (lambda: imaplib.IMAP4_SSL(self.IMAP_HOST, 993))
        self.smtp_factory = smtp_factory or (lambda: smtplib.SMTP_SSL(self.SMTP_HOST, 465, timeout=20))

    def profile_email(self) -> str:
        return self.address

    def _imap(self):
        im = self.imap_factory()
        im.login(self.address, self.password)
        im.select("INBOX")
        return im

    def check(self) -> None:
        im = self._imap()
        im.logout()

    def recent(self, max_n: int = 30, days: int = 14, skip_ids: set[str] | None = None) -> list[RawMessage]:
        im = self._imap()
        try:
            # Gmail's own search over IMAP skips the Promotions and Social tabs; plain SINCE is the fallback.
            try:
                typ, data = im.uid("SEARCH", None, "X-GM-RAW", '"' + gmail_query(days, self.skip_tabs) + '"')
                if typ != "OK":
                    raise imaplib.IMAP4.error(typ)
            except imaplib.IMAP4.error:
                since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
                typ, data = im.uid("SEARCH", None, f"SINCE {since}")
            uids = (data[0] or b"").split()[-max_n:][::-1]
            out = []
            for uid in uids:
                typ, parts = im.uid("FETCH", uid, "(X-GM-MSGID X-GM-THRID X-GM-LABELS RFC822)")
                meta, raw = None, None
                for p in parts:
                    if isinstance(p, tuple):
                        meta, raw = p[0].decode(errors="replace"), p[1]
                if raw is None:
                    continue
                gid = re.search(r"X-GM-MSGID (\d+)", meta)
                tid = re.search(r"X-GM-THRID (\d+)", meta)
                lbl = re.search(r"X-GM-LABELS \(([^)]*)\)", meta)
                gmail_id = format(int(gid.group(1)), "x") if gid else f"uid{uid.decode()}"
                if skip_ids and gmail_id in skip_ids:
                    continue
                labels = [x.strip('"') for x in lbl.group(1).split()] if lbl else []
                out.append(parse_rfc822(raw, gmail_id, format(int(tid.group(1)), "x") if tid else gmail_id, labels, self.tz))
            return out
        finally:
            try:
                im.logout()
            except Exception:
                pass

    def handled_label_id(self) -> str:
        return LABEL

    def send(self, msg: EmailMessage, thread_id: str | None = None) -> dict:
        s = self.smtp_factory()
        try:
            s.login(self.address, self.password)
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
        return {"id": "", "threadId": thread_id or ""}

    def mark_handled(self, gmail_id: str) -> None:
        im = self._imap()
        try:
            typ, data = im.uid("SEARCH", None, f"X-GM-MSGID {int(gmail_id, 16)}")
            for uid in (data[0] or b"").split():
                im.uid("STORE", uid, "+X-GM-LABELS", f"({LABEL})")
        except ValueError:
            pass
        finally:
            try:
                im.logout()
            except Exception:
                pass
