"""Real-Gmail path, tested without the network: a fake Gmail client for the end-to-end flow, and the
real Gmail API client against recorded API responses."""
import base64
import json
from datetime import date, datetime, timedelta

import pytest

from campussync.adapters.gmail_client import (GmailAPIClient, ImapSmtpClient, RawMessage, build_message,
                                              parse_rfc822)
from campussync.utils import fmt_time

from conftest import NOW

ME = "me.student@gmail.com"
FRIEND = "friend.prof@gmail.com"


class FakeGmail:
    def __init__(self, email=ME):
        self.email, self.inbox, self.sent, self.handled, self.n = email, [], [], set(), 0
        self.base_labels = {}

    def profile_email(self):
        return self.email

    def deliver(self, frm, subject, body, thread=None, to=None, name="", msgid=None, when=None, labels=(), bulk=False):
        self.n += 1
        gid = f"g{self.n:03d}"
        self.inbox.append(RawMessage(gid, thread or f"t{self.n:03d}", name or frm.split("@")[0], frm, to or [self.email], [],
                                     subject, body, when or NOW, msgid or f"<{gid}@mail.gmail.com>", "", [], bulk))
        self.base_labels[gid] = list(labels)
        return gid

    def recent(self, max_n=30, days=14, skip_ids=None):
        out = []
        for m in self.inbox:
            if skip_ids and m.gmail_id in skip_ids:
                continue
            m.labels = self.base_labels.get(m.gmail_id, []) + (["CAMPUSSYNC"] if m.gmail_id in self.handled else [])
            out.append(m)
        return out[::-1]

    def handled_label_id(self):
        return "CAMPUSSYNC"

    def send(self, msg, thread_id=None):
        self.sent.append({"msg": msg, "thread": thread_id})
        return {"id": f"s{len(self.sent)}", "threadId": thread_id or f"new{len(self.sent)}"}

    def mark_handled(self, gid):
        self.handled.add(gid)


def connect(svc, profile="F101", name="Test Owner"):
    fake = FakeGmail()
    svc.gmail.set_client(fake)
    svc.directory.set_owner(fake.email, profile, name)
    return fake


def test_not_connected_means_demo_mailbox(gsvc):
    assert not gsvc.live_mail and not gsvc.gmail.connected
    assert gsvc.sync_gmail(force=True)["new"] == []
    r = gsvc.send_mail("S002", ["F101"], "x", "Can we meet tomorrow at 3 pm?")
    assert r["mail"].meta.get("delivered") is None


def test_sync_maps_senders_and_is_idempotent(gsvc):
    fake = connect(gsvc)
    fake.deliver("stranger@outside.org", "Hello", "Hi, when are you free this week for 30 minutes?", name="Stranger One")
    fake.deliver(FRIEND, "Doubt", "Can we meet tomorrow at 3 pm?")
    new = gsvc.sync_gmail(force=True)["new"]
    assert len(new) == 2 and all(m.to_ids[0] == "F101" for m in new)
    ext = gsvc.person(next(m.from_id for m in new if m.meta["from_email"] == "stranger@outside.org"))
    assert ext.role == "external" and ext.name == "Stranger One"
    assert gsvc.sync_gmail(force=True)["new"] == []
    # linking the friend's address to a VTOP profile moves their mails over
    gsvc.link_contact(FRIEND, "S002")
    m = gsvc.store.get_mail(next(x.id for x in new if x.meta["from_email"] == FRIEND))
    assert m.from_id == "S002" and gsvc.person("S002").email == FRIEND


def test_owner_profile_shows_real_name_and_address(gsvc):
    connect(gsvc, "F101", "Asha Menon")
    p = gsvc.person("F101")
    assert p.name == "Asha Menon" and p.email == ME


def test_reply_goes_out_from_gmail_in_the_same_thread(gsvc):
    fake = connect(gsvc)
    fake.deliver(FRIEND, "Project report", "Ma'am, can we meet tomorrow at 3 pm for 20 minutes?", thread="T1",
                 msgid="<abc@mail.gmail.com>")
    gsvc.sync_gmail(force=True)
    gsvc.link_contact(FRIEND, "S002")
    mail = next(m for m in gsvc.mail.inbox("F101") if m.meta.get("gmail_id"))
    prop = gsvc.process_mail(mail.id, "F101")
    assert prop.decision in ("accept", "offer")
    gsvc.confirm(prop)
    assert len(fake.sent) == 1
    out = fake.sent[0]
    assert out["thread"] == "T1"
    assert out["msg"]["To"] == FRIEND and out["msg"]["In-Reply-To"] == "<abc@mail.gmail.com>"
    assert out["msg"]["Subject"] == "Re: Project report" and ME in out["msg"]["From"]
    assert mail.meta["gmail_id"] in fake.handled  # labelled CampusSync in Gmail


def test_full_offer_then_pick_over_gmail(gsvc):
    fake = connect(gsvc)
    gsvc.directory.resolve(FRIEND)
    gsvc.link_contact(FRIEND, "S002")
    fake.deliver(FRIEND, "Review", "Ma'am, when are you free this week for 30 minutes?", thread="T2")
    gsvc.sync_gmail(force=True)
    mail = next(m for m in gsvc.mail.inbox("F101") if m.subject == "Review")
    assert mail.from_id == "S002"
    prop = gsvc.process_mail(mail.id, "F101")
    assert prop.decision == "offer"
    gsvc.confirm(prop)
    assert fake.sent and "Option 1" in fake.sent[-1]["msg"].get_content()
    fake.deliver(FRIEND, "Re: Review", "Option 2 works for me, thanks", thread="T2", when=NOW + timedelta(minutes=5))
    gsvc.sync_gmail(force=True)
    pick = next(m for m in gsvc.mail.inbox("F101") if m.subject == "Re: Review")
    p2 = gsvc.process_mail(pick.id, "F101")
    assert p2.decision == "accept" and p2.chosen.start == prop.options[1].start
    res = gsvc.confirm(p2)
    assert res["meeting"] and set(res["meeting"].participant_ids) == {"S002", "F101"}
    assert len(fake.sent) == 2 and fake.sent[-1]["thread"] == "T2"


def test_mock_profiles_never_get_real_mail(gsvc):
    fake = connect(gsvc)
    gsvc.send_mail("F101", ["S003"], "Hello", "Can we meet tomorrow at 3 pm?")
    assert fake.sent == []  # S003 only has a .mock address
    r = gsvc.send_mail("F101", [gsvc.directory.resolve("someone@realmail.com")], "Hi", "Are you free tomorrow?")
    assert len(fake.sent) == 1 and fake.sent[0]["msg"]["To"] == "someone@realmail.com"
    assert r["mail"].meta["delivered"] == "gmail"


def test_handled_label_survives_reset(gsvc):
    fake = connect(gsvc)
    gid = fake.deliver(FRIEND, "Old", "Can we meet tomorrow at 3 pm?")
    fake.handled.add(gid)
    gsvc.sync_gmail(force=True)
    assert gsvc.store.get_mail(f"G{gid}").status == "processed"


def test_send_failure_is_recorded_not_raised(gsvc):
    fake = connect(gsvc)

    def boom(msg, thread_id=None):
        raise ConnectionError("network down")
    fake.send = boom
    r = gsvc.send_mail("F101", [gsvc.directory.resolve("someone@realmail.com")], "Hi", "Hello")
    assert r["mail"].meta["delivered"] == "failed" and "network down" in gsvc.gmail.last_error


def test_outside_contact_meeting_checks_only_owner(gsvc):
    fake = connect(gsvc)
    fake.deliver("guest@company.com", "Guest lecture", "Could we meet on Monday at 8 am?")
    gsvc.sync_gmail(force=True)
    m = next(x for x in gsvc.mail.inbox("F101") if x.subject == "Guest lecture")
    prop = gsvc.process_mail(m.id, "F101")
    assert prop.decision == "offer"  # F101 teaches Monday 08:00
    assert any("You're busy" in n for n in prop.private_notes)


def test_auto_mode_on_sync_replies(gsvc):
    fake = connect(gsvc)
    gsvc.set_auto_mode(True)
    fake.deliver(FRIEND, "Quick chat", "When are you free this week for 15 minutes?")
    res = gsvc.sync_gmail(force=True)
    assert res["auto"] and fake.sent


def test_reminder_copy_to_self(gsvc):
    fake = connect(gsvc)
    gsvc.set_prefs("F101", copy_to_gmail=True)
    m = next(x for x in gsvc.store.meetings("F101"))
    gsvc.set_clock(m.start - timedelta(minutes=29))
    gsvc.dispatch_reminders()
    assert any(s["msg"]["To"] == ME and "Reminder" in s["msg"]["Subject"] for s in fake.sent)


# ---------------------------------------------------------------- parsing and the real clients
RAW = (b"From: Diya K <diya.k@gmail.com>\r\nTo: me.student@gmail.com\r\nCc: Rohan <rohan@gmail.com>\r\n"
       b"Subject: Project report\r\nDate: Thu, 15 Oct 2026 02:00:00 +0000\r\nMessage-ID: <m1@mail.gmail.com>\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\nCan we meet tomorrow at 3 pm?\r\n\r\n"
       b"On Wed, 14 Oct 2026 at 10:00, Me <me.student@gmail.com> wrote:\r\n> old text\r\n")


def test_parse_rfc822(gsvc):
    m = parse_rfc822(RAW, "id1", "th1", [], gsvc.tz)
    assert (m.from_email, m.from_name, m.cc) == ("diya.k@gmail.com", "Diya K", ["rohan@gmail.com"])
    assert m.body == "Can we meet tomorrow at 3 pm?" and m.sent_at == datetime(2026, 10, 15, 7, 30)
    html = (b"From: a@b.com\r\nTo: c@d.com\r\nSubject: x\r\nDate: Thu, 15 Oct 2026 02:00:00 +0000\r\n"
            b"Content-Type: text/html\r\n\r\n<p>Hello <b>there</b></p><br>Free at 4?")
    assert parse_rfc822(html, "i", "t").body.replace("\n", " ").split() == ["Hello", "there", "Free", "at", "4?"]


def test_gmail_api_client_against_recorded_responses():
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence
    raw = base64.urlsafe_b64encode(RAW).decode()
    http = HttpMockSequence([
        ({"status": "200"}, json.dumps({"emailAddress": "Me.Student@gmail.com"})),
        ({"status": "200"}, json.dumps({"messages": [{"id": "m1", "threadId": "th1"}]})),
        ({"status": "200"}, json.dumps({"id": "m1", "threadId": "th1", "labelIds": ["INBOX"], "raw": raw})),
        ({"status": "200"}, json.dumps({"labels": [{"id": "Label_9", "name": "CampusSync"}]})),
        ({"status": "200"}, json.dumps({"id": "s1", "threadId": "th1"})),
        ({"status": "200"}, json.dumps({"id": "m1"})),
    ])
    svc = build("gmail", "v1", http=http, cache_discovery=False)
    c = GmailAPIClient(service=svc)
    assert c.profile_email() == "me.student@gmail.com"
    msgs = c.recent()
    assert len(msgs) == 1 and msgs[0].thread_id == "th1" and msgs[0].subject == "Project report"
    assert c.handled_label_id() == "Label_9"
    res = c.send(build_message("Me <me.student@gmail.com>", ["diya.k@gmail.com"], "Re: Project report", "Yes",
                               in_reply_to="<m1@mail.gmail.com>"), "th1")
    assert res["threadId"] == "th1"
    c.mark_handled("m1")


class FakeIMAP:
    def __init__(self):
        self.calls = []

    def login(self, u, p):
        if p != "abcdabcdabcdabcd":
            raise Exception("AUTHENTICATIONFAILED")

    def select(self, box):
        pass

    def uid(self, cmd, *args):
        self.calls.append((cmd, args))
        if cmd == "SEARCH":
            return "OK", [b"7"]
        if cmd == "FETCH":
            return "OK", [(b"7 (X-GM-MSGID 1234567 X-GM-THRID 1234500 X-GM-LABELS (\\\\Inbox) UID 7 RFC822 {300}", RAW), b")"]
        return "OK", [b""]

    def logout(self):
        pass


class FakeSMTP:
    sent = []

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


def test_imap_smtp_client():
    im = FakeIMAP()
    c = ImapSmtpClient("me.student@gmail.com", "abcdabcdabcdabcd", imap_factory=lambda: im, smtp_factory=FakeSMTP)
    msgs = c.recent()
    assert msgs[0].gmail_id == format(1234567, "x") and msgs[0].thread_id == format(1234500, "x")
    c.send(build_message("me.student@gmail.com", ["x@y.com"], "Hi", "Body"))
    assert FakeSMTP.sent[-1]["To"] == "x@y.com"
    c.mark_handled(msgs[0].gmail_id)
    assert any(cmd == "STORE" for cmd, _ in im.calls)
    bad = ImapSmtpClient("me.student@gmail.com", "wrongwrongwrong1", imap_factory=lambda: FakeIMAP())
    with pytest.raises(Exception):
        bad.check()


# ---------------------------------------------------------------- sign-in flow
CLIENT = {"installed": {"client_id": "123.apps.googleusercontent.com", "client_secret": "sec", "project_id": "p",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]}}


def test_oauth_link_and_callback(gsvc, monkeypatch):
    auth = gsvc.gmail.auth
    with pytest.raises(Exception):
        auth.save_client_file(b"{not json")
    with pytest.raises(Exception):
        auth.save_client_file(json.dumps({"foo": 1}))
    assert auth.save_client_file(json.dumps(CLIENT)) == "installed"
    url = auth.auth_url()
    assert "accounts.google.com" in url and "gmail.modify" in url and "code_challenge" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8501%2F" in url
    state = url.split("state=")[1].split("&")[0]
    from google_auth_oauthlib.flow import Flow

    def fake_fetch(self, **kw):
        assert kw["code"] == "the-code" and self.code_verifier
        self.oauth2session.token = {"access_token": "at", "refresh_token": "rt", "token_type": "Bearer",
                                    "expires_in": 3600, "expires_at": 9999999999, "scope": ["https://www.googleapis.com/auth/gmail.modify"]}
        return self.oauth2session.token
    monkeypatch.setattr(Flow, "fetch_token", fake_fetch)
    with pytest.raises(Exception, match="expired"):
        auth.handle_callback("the-code", "wrong-state")
    auth.handle_callback("the-code", state)
    assert auth.connected and auth.method == "oauth"
    tok = json.loads(auth.token_file.read_text())
    assert tok["refresh_token"] == "rt"
    gsvc.gmail.disconnect()
    assert not auth.connected


def test_app_password_saved(gsvc):
    auth = gsvc.gmail.auth
    with pytest.raises(Exception):
        auth.save_app_password("me@gmail.com", "short")
    auth.save_app_password("me@gmail.com", "abcd abcd abcd abcd")
    assert auth.method == "app_password" and auth.app_password()["password"] == "abcdabcdabcdabcd"
