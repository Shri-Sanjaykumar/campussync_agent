"""CampusSync - campus scheduling agent.  Run:  streamlit run app.py"""
from __future__ import annotations

import html
import re
from datetime import date, datetime, time, timedelta

import pandas as pd
import streamlit as st

from campussync.adapters.gmail_auth import GmailAuthError
from campussync.adapters.gmail_client import ImapSmtpClient
from campussync.adapters.gmail_mailbox import split_via
from campussync.agent.mail_handler import SlotTaken
from campussync.core.attendance import attendance_summary
from campussync.core.directory import is_real_address
from campussync.core.office_hours import OfficeHoursError
from campussync.core.triage import CORE
from campussync.service import CampusSync
from campussync.utils import WEEKDAY_FULL, WEEKDAYS, fmt_date, fmt_dt, fmt_range, fmt_time

st.set_page_config(page_title="CampusSync", layout="wide", initial_sidebar_state="expanded")

CSS = """
<style>
:root {
  --ink:#18212F; --muted:#5B6576; --line:#E3E7ED; --panel:#F3F5F8;
  --accent:#2C4BA0; --accent-soft:#E8EDF9;
  --amber:#8A5300; --amber-soft:#FBEFD9; --red:#A3271C; --red-soft:#FCE9E7; --green:#1C6E48; --green-soft:#E3F2EA;
}
.block-container {padding-top: 2.4rem; padding-bottom: 4rem; max-width: 1320px;}
h1, h2, h3, h4, h5 {letter-spacing: -0.012em;}
[data-testid="stSidebar"] h2 {font-size: 1.35rem; margin-bottom: 0;}

/* Two radios become plain lists: the sidebar pages and the mail list. The label itself is the whole row,
   so a click anywhere on it selects (the caption sits on top of the label's padding and lets clicks through). */
.st-key-nav, .st-key-sel_mail, .st-key-nav .stRadio, .st-key-sel_mail .stRadio {width: 100% !important;}
.st-key-nav [role="radiogroup"], .st-key-sel_mail [role="radiogroup"] {width: 100%;}
.st-key-nav [role="radiogroup"] > div, .st-key-sel_mail [role="radiogroup"] > div {position: relative; width: 100%;}
.st-key-nav [data-testid="stRadioOption"], .st-key-sel_mail [data-testid="stRadioOption"] {width: 100%; box-sizing: border-box; cursor: pointer; margin: 0;}
.st-key-nav [data-testid="stRadioOption"] > div > div:first-child,
.st-key-sel_mail [data-testid="stRadioOption"] > div > div:first-child {display: none;}
.st-key-nav [role="radiogroup"] > div[data-focus-visible="true"],
.st-key-sel_mail [role="radiogroup"] > div[data-focus-visible="true"] {outline: 2px solid var(--accent); outline-offset: -2px;}

.st-key-nav [role="radiogroup"] {gap: 2px;}
.st-key-nav [role="radiogroup"] > div {border-radius: 6px;}
.st-key-nav [data-testid="stRadioOption"] {padding: 6px 10px;}
.st-key-nav [role="radiogroup"] > div:hover {background: #E6E9EF;}
.st-key-nav [role="radiogroup"] > div[data-selected="true"] {background: var(--accent-soft);}
.st-key-nav [role="radiogroup"] > div[data-selected="true"] p {color: var(--accent); font-weight: 600;}

.st-key-maillist {padding: 0 !important; gap: 0 !important;}
.st-key-sel_mail [role="radiogroup"] {gap: 0;}
.st-key-sel_mail [role="radiogroup"] > div {border-bottom: 1px solid var(--line);}
.st-key-sel_mail [data-testid="stRadioOption"] {padding: 11px 14px 53px 16px;}
.st-key-sel_mail [data-testid="stRadioOption"] p {white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.st-key-sel_mail [role="radiogroup"] > div:hover {background: #F6F7F9;}
.st-key-sel_mail [role="radiogroup"] > div[data-selected="true"] {background: var(--accent-soft); box-shadow: inset 3px 0 0 var(--accent);}
.st-key-sel_mail [data-testid="stRadioCaption"] {position: absolute; top: 35px; left: 16px; right: 14px;
  padding: 0 !important; margin: 0 !important; pointer-events: none;}
.st-key-sel_mail [data-testid="stRadioCaption"] p {font-size: .84rem; line-height: 1.45; margin: 2px 0 0 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}

.tag {display:inline-block; padding:1px 7px; border-radius:4px; font-size:.76rem; font-weight:600; line-height:1.5;
      background:#EEF1F5; color:#3A4556; margin:0 6px 4px 0; white-space:nowrap;}
.tag-accent {background:var(--accent-soft); color:var(--accent);}
.tag-amber {background:var(--amber-soft); color:var(--amber);}
.tag-red {background:var(--red-soft); color:var(--red);}
.tag-green {background:var(--green-soft); color:var(--green);}
.muted {color: var(--muted); font-size: .88rem;}
.mailbody {white-space: pre-wrap; line-height: 1.55; font-size: .97rem;}

/* week grid */
.dayhead {border-top: 3px solid var(--line); padding-top: 6px; margin-bottom: 8px;}
.dayhead.today {border-top-color: var(--accent);}
.dayhead b {font-size: .95rem;} .dayhead span {display:block; font-size: .75rem; color: var(--muted);}
.ev {border-radius: 5px; padding: 5px 8px; margin: 0 0 6px 0; font-size: .8rem; line-height: 1.3;
     border-left: 3px solid #9AA3B2; background: #F5F6F8;}
.ev b {font-variant-numeric: tabular-nums; font-weight: 600;}
.ev small {display:block; color: var(--muted);}
.ev-class {border-color:#5C7BC9;} .ev-class.soft {border-left-style: dashed; background:#FAFBFC;}
.ev-exam {border-color:var(--red); background:var(--red-soft);} .ev-due {border-color:var(--amber); background:var(--amber-soft);}
.ev-holiday {border-color:#8A93A3;} .ev-duty {border-color:#7B61B8;}
.ev-commitment {border-color:#C98A1E;} .ev-meeting {border-color:var(--green); background:var(--green-soft);}
.ev-external {border-color:#2B8A99;} .ev-hold {border-color:#9AA3B2; border-left-style: dotted;}
.freeday {font-size:.8rem; color: var(--muted); padding: 4px 0;}

.tok {display:inline-block; min-width:72px; text-align:center; padding:5px 6px; margin:0 6px 6px 0; border-radius:5px;
      font-size:.78rem; background:#F3F5F8; border:1px solid var(--line); font-variant-numeric: tabular-nums;}
.tok-booked {background:var(--accent-soft); border-color:#C9D3EE;} .tok-mine {background:var(--green-soft);}
.tok-past, .tok-unavailable, .tok-held {opacity:.45;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ====================================================================== service and helpers
def get_svc() -> CampusSync:
    if "svc" not in st.session_state:
        s = CampusSync()
        if not s.portal.list_people():
            s.reset_demo()
        st.session_state.svc = s
    return st.session_state.svc


svc = get_svc()
svc.store.load()  # pick up changes made by other browser tabs


def name(pid: str) -> str:
    if pid == "agent":
        return "CampusSync"
    p = svc.person(pid)
    return p.name if p else pid


def sender_name(m) -> str:
    """Who wrote this mail: the real name for Gmail senders outside VTOP (not the group they posted through)."""
    fn = split_via((m.meta or {}).get("from_name", ""))[0]
    p = svc.person(m.from_id)
    if fn and (p is None or p.role == "external"):
        return fn
    return name(m.from_id)


def first(pid: str) -> str:
    n = name(pid)
    return n if n.startswith(("Dr.", "Prof.", "Mr.", "Mrs.")) else n.split()[0]


def flash(msg: str, kind: str = "success") -> None:
    st.session_state["flash"] = (kind, msg)


def tag(text: str, kind: str = "") -> str:
    return f"<span class='tag{' tag-' + kind if kind else ''}'>{html.escape(str(text))}</span>"


def go(page: str, **state) -> None:
    """Callback: jump to another page (and set things like the selected mail)."""
    st.session_state["nav"] = page
    for k, v in state.items():
        st.session_state[k] = v


def short_when(dt: datetime) -> str:
    today = svc.now().date()
    if dt.date() == today:
        return fmt_time(dt)
    if dt.date() == today - timedelta(days=1):
        return "Yesterday"
    return f"{dt.day} {dt.strftime('%b')}"


# ====================================================================== connect Gmail first
SETUP_STEPS = """
Google needs a one-time setup before any app can read Gmail. Anyone on the team can do it once (about 10 minutes):

1. Open [console.cloud.google.com](https://console.cloud.google.com/) and create a project.
2. *APIs & Services > Library*: search **Gmail API** and click **Enable**.
3. *APIs & Services > OAuth consent screen*: choose **External**, add the app name and your email, save.
   Under **Test users**, add every Gmail address that will sign in.
4. *APIs & Services > Credentials > Create credentials > OAuth client ID*: type **Desktop app**, create, **Download JSON**.
5. Upload that file below. It identifies the app, not a mailbox.

Google will say the app isn't verified while it's in testing: click **Continue**. Sign-ins last 7 days in testing.
"""


def gmail_gate() -> None:
    gm, auth = svc.gmail, svc.gmail.auth
    qp = st.query_params
    if "error" in qp:
        st.error(f"Google sign-in was cancelled ({qp.get('error')}).")
        st.query_params.clear()
    elif "code" in qp:
        try:
            auth.handle_callback(qp.get("code"), qp.get("state"))
            gm.set_client(None)
            st.query_params.clear()
            st.rerun()
        except GmailAuthError as e:
            st.query_params.clear()
            st.error(str(e))

    _, mid, _ = st.columns([1, 2, 1])
    if not gm.connected:
        with mid:
            st.title("Connect your Gmail")
            st.write("CampusSync reads meeting requests in your Gmail, checks timetables and drafts replies. Nothing is sent "
                     "until you approve it. College data (VTOP) is a mock in this version.")
            t1, t2 = st.tabs(["Sign in with Google", "Use an app password"])
            with t1:
                if auth.has_client:
                    try:
                        st.link_button("Sign in with Google", auth.auth_url(), type="primary")
                        st.caption("Google opens in a new tab and the app continues there.")
                        st.button("I've signed in, refresh", type="tertiary")
                    except Exception as e:
                        st.error(f"Couldn't build the Google sign-in link: {e}")
                    if auth.client_kind() == "web":
                        st.info(f"This is a 'Web application' client. Add {auth.redirect_uri} to its redirect URIs in Google "
                                "Cloud, or create a 'Desktop app' client instead.")
                    with st.expander("Use a different OAuth client file"):
                        up = st.file_uploader("OAuth client JSON", type=["json"], key="client_json_replace")
                        if up and st.button("Replace"):
                            auth.save_client_file(up.read())
                            st.rerun()
                else:
                    st.markdown(SETUP_STEPS)
                    up = st.file_uploader("OAuth client JSON", type=["json"], key="client_json")
                    if up:
                        try:
                            auth.save_client_file(up.read())
                            flash("OAuth client added. Now sign in with Google.")
                            st.rerun()
                        except GmailAuthError as e:
                            st.error(str(e))
            with t2:
                st.write("Needs 2-Step Verification and an app password from "
                         "[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). "
                         "College accounts often block this; use Sign in with Google there.")
                with st.form("app_pw"):
                    addr = st.text_input("Gmail address")
                    pw = st.text_input("App password", type="password")
                    if st.form_submit_button("Connect", type="primary"):
                        try:
                            cand = ImapSmtpClient(addr.strip(), pw.replace(" ", ""), tz=svc.tz)
                            cand.check()
                            auth.save_app_password(addr, pw)
                            gm.set_client(None)
                            st.rerun()
                        except GmailAuthError as e:
                            st.error(str(e))
                        except Exception as e:
                            st.error(f"Gmail refused the login ({type(e).__name__}). Check the address and app password, "
                                     "and that IMAP is on in Gmail settings.")
            st.caption("The app can read, send and label mail. It never deletes mail. Sign-in files stay in data/secrets on "
                       "this computer.")
            st.button("Skip for now and use the demo mailbox", type="tertiary",
                      on_click=lambda: svc.store.set_setting("mail_mode", "demo"))
        st.stop()

    account = gm.account()
    if not account:
        with mid:
            st.title("Can't reach Gmail")
            st.error(gm.last_error or "Gmail didn't answer.")
            a, b = st.columns(2)
            if a.button("Try again", type="primary"):
                gm.set_client(None)
                st.rerun()
            if b.button("Sign in again"):
                gm.disconnect()
                svc.directory.clear_owner()
                st.rerun()
        st.stop()

    owner = svc.directory.owner
    if not owner or owner["email"] != account:
        with mid:
            st.title("Which VTOP profile is yours?")
            st.write(f"Connected as **{account}**. VTOP is mocked, so pick the profile whose timetable, attendance and "
                     "cabin hours should count as yours.")
            people_ = sorted([p for p in svc.base_portal.list_people() if p.role in ("faculty", "student")],
                             key=lambda p: (p.role != "faculty", p.id))
            with st.form("owner_form"):
                pid = st.selectbox("Your profile", [p.id for p in people_],
                                   format_func=lambda i: f"{svc.base_portal.get_person(i).name} ({svc.base_portal.get_person(i).role})")
                nm = st.text_input("Your name (replies are signed with it)", "")
                if st.form_submit_button("Continue", type="primary"):
                    svc.directory.set_owner(account, pid, nm or svc.base_portal.get_person(pid).name)
                    st.session_state.pop("acting_as", None)
                    svc.sync_gmail(force=True)
                    st.rerun()
            st.button("Use a different Gmail", type="tertiary", on_click=gm.disconnect)
        st.stop()

    res = svc.sync_gmail()
    if res["new"]:
        st.toast(f"{len(res['new'])} new mail from Gmail")
    for a in res["auto"]:
        if a.get("summary"):
            st.toast(f"Auto-replied: {a['mail'].subject}")


if svc.gmail and svc.store.get_setting("mail_mode") != "demo":
    gmail_gate()


# ====================================================================== sidebar: who, where
PAGES = {
    "faculty": ["Home", "Inbox", "Calendar", "Students", "Cabin hours", "Assistant", "Settings"],
    "student": ["Home", "Inbox", "Calendar", "Leave", "Cabin hours", "Assistant", "Settings"],
    "parent": ["Inbox", "Leave", "Settings"],
}
role_order = {"faculty": 0, "student": 1, "parent": 2, "staff": 3}
people = sorted(svc.people(), key=lambda p: (role_order.get(p.role, 9), p.id))

with st.sidebar:
    st.markdown("## CampusSync")
    st.caption(svc.settings.get("institution.name"))
    ids = [p.id for p in people]
    owner_id = svc.owner_id
    if owner_id in ids:
        ids.remove(owner_id)
        ids.insert(0, owner_id)
    if st.session_state.get("acting_as") not in ids:
        st.session_state["acting_as"] = owner_id or ("F101" if "F101" in ids else ids[0])
    uid = st.selectbox("Viewing as", ids, key="acting_as",
                       format_func=lambda i: (f"{name(i)} (you)" if i == owner_id else f"{name(i)}, {svc.person(i).role}"))
    me = svc.person(uid)
    pages = PAGES.get(me.role, ["Inbox", "Settings"])
    if st.session_state.get("nav") not in pages:
        st.session_state["nav"] = pages[0]
    unread = len([m for m in svc.mail.inbox(uid) if m.status == "unread" and m.kind == "human" and m.from_id != uid
                  and svc.triage.peek(m)["action"]])
    page = st.radio("Go to", pages, key="nav", label_visibility="collapsed",
                    format_func=lambda p: f"{p}  ({unread})" if p == "Inbox" and unread else p)

    st.divider()
    if svc.live_mail:
        gm = svc.gmail
        st.caption(f"Gmail: {svc.directory.owner_email}  \nChecked {gm.last_sync.strftime('%H:%M') if gm.last_sync else 'not yet'}")
        if gm.last_error:
            st.warning(gm.last_error)
        if st.button("Check for new mail", type="tertiary"):
            r_ = svc.sync_gmail(force=True)
            flash(f"{len(r_['new'])} new mail." if r_["new"] else "No new mail.")
            st.rerun()
        if uid != owner_id:
            st.caption("This is a mock profile: its mail stays inside the app.")

        @st.fragment(run_every=60)
        def _poll_gmail():
            if svc.sync_gmail()["new"]:
                st.rerun(scope="app")
        _poll_gmail()
    elif svc.gmail:
        st.caption("Mail: demo mailbox")
        st.button("Connect Gmail", type="tertiary", on_click=lambda: svc.store.set_setting("mail_mode", None))
    if svc.store.get_setting("clock"):
        st.caption(f"Clock set to {fmt_dt(svc.now())}")
    if svc.auto_mode:
        st.caption("Auto mode is on: requests are answered without review.")

if "flash" in st.session_state:
    kind, msg = st.session_state.pop("flash")
    if kind == "success" and len(msg) < 160 and "\n" not in msg:
        st.toast(msg)
    else:
        getattr(st, kind if kind in ("info", "warning", "error", "success") else "info")(msg)


# ====================================================================== shared pieces
def event_html(e) -> str:
    soft = "" if e.hard else " soft"
    sub = []
    if e.kind == "class":
        lvl = e.meta.get("level")
        if lvl and lvl not in ("unknown",):
            sub.append(f"{e.meta.get('percent')}% attendance" + (", safe to skip" if not e.hard else ""))
        if e.meta.get("venue"):
            sub.append(e.meta["venue"])
    elif e.kind == "meeting":
        sub.append("with " + ", ".join(first(p) for p in e.meta.get("participants", []) if p != uid))
    elif e.kind == "hold":
        sub.append(f"held until {e.meta.get('expires_at', '')[11:16]}")
    elif e.kind == "exam" and e.meta.get("venue"):
        sub.append(e.meta["venue"])
    t = "All day" if e.kind == "holiday" else f"{fmt_time(e.start)}-{fmt_time(e.end)}"
    return (f"<div class='ev ev-{e.kind}{soft}'><b>{t}</b> {html.escape(e.title)}"
            + (f"<small>{html.escape(', '.join(x for x in sub if x))}</small>" if sub else "") + "</div>")


def empty(text: str) -> None:
    st.markdown(f"<p class='muted'>{html.escape(text)}</p>", unsafe_allow_html=True)


TEMPLATES = {
    "Blank": ("", ""),
    "Ask for a specific time": ("Quick meeting", "Hi, can we meet tomorrow at 3 pm for 20 minutes?"),
    "Ask when they're free": ("Discussion", "Hi, when are you free this week for a 30 minute discussion?"),
    "A day and part of day": ("Project discussion", "Can we meet on Monday afternoon for half an hour?"),
    "Team meeting": ("Team meeting", "Can our team meet you next week for 30 minutes to show the prototype?"),
    "Urgent, today": ("Urgent: need to talk", "This is urgent, I need to meet you today itself. 15 minutes is enough."),
    "Cancel a meeting": ("Cancel meeting", "Sorry, I can't make it to our meeting. Please cancel it."),
    "Leave mail (parent)": ("Leave for my ward", "Respected Sir/Madam, my son is unwell. Kindly grant him leave from 21st to 22nd October."),
}


@st.dialog("New mail", width="large")
def compose_dialog():
    tpl = st.selectbox("Start from", list(TEMPLATES), key="cmp_tpl")
    sub0, body0 = TEMPLATES[tpl]
    others = [p.id for p in people if p.id != uid]
    default_to = []
    if not svc.live_mail:
        if me.role == "student" and me.guide_id:
            default_to = [me.guide_id]
        elif me.role == "parent" and me.ward_id and svc.person(me.ward_id).proctor_id:
            default_to = [svc.person(me.ward_id).proctor_id]
    to = st.multiselect("To", others, default=default_to,
                        format_func=lambda i: name(i) + ("" if (not svc.live_mail or svc.directory.real_email(i)) else " (in-app only)"))
    real_addrs = ""
    if svc.live_mail and uid == svc.owner_id:
        real_addrs = st.text_input("Other email addresses", placeholder="friend@gmail.com, prof@vit.ac.in",
                                   help="Sent from your Gmail. Separate with commas.")
    subject = st.text_input("Subject", sub0, key=f"cmp_sub_{tpl}")
    body = st.text_area("Message", body0, height=150, key=f"cmp_body_{tpl}")
    if st.button("Send", type="primary"):
        extra = [svc.directory.resolve(a.strip()) for a in real_addrs.replace(";", ",").split(",") if "@" in a]
        to_all = list(dict.fromkeys(list(to) + extra))
        if not to_all or not body.strip():
            st.error("Add a recipient and a message.")
            return
        r = svc.send_mail(uid, to_all, subject or "Meeting request", body)
        if r["mail"].meta.get("delivered") == "failed":
            flash(svc.gmail.last_error, "warning")
        elif r["auto"]:
            flash("Sent. " + " ".join(f"{name(a['responder'])}'s agent replied: {a.get('summary', a.get('error'))}"
                                      for a in r["auto"]), "info")
        else:
            flash(f"Sent to {', '.join(name(x) for x in to_all)}.")
        st.rerun()


# ====================================================================== Home
def page_home():
    now = svc.now()
    today = now.date()
    greet = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
    c1, c2 = st.columns([4, 1])
    c1.title(f"{greet}, {first(uid)}")
    c1.caption(f"{WEEKDAY_FULL[today.weekday()]}, {today.day} {today.strftime('%B')}")
    with c2:
        st.write("")
        if st.button("Email me today's summary", type="tertiary"):
            svc.send_digest(uid)
            flash("Summary sent to your inbox.")
            st.rerun()

    inbox = svc.mail.inbox(uid)
    svc.triage.classify_many(inbox)
    needs = [m for m in inbox if m.status == "unread" and m.kind == "human" and m.from_id != uid
             and svc.triage.classify(m)["action"]]
    meetings_today = [m for m in svc.store.meetings(uid) if m.start.date() == today and m.end >= now]
    week_end = today + timedelta(days=7)
    dls = [d for d in svc.triage.deadlines(uid) if datetime.fromisoformat(d["due"]).date() <= week_end]

    tiles = [("Need your reply", len(needs)), ("Meetings left today", len(meetings_today)),
             ("Deadlines this week", len(dls))]
    if me.role == "faculty":
        tiles.append(("Leave to approve", len(svc.leave.for_approver(uid, "pending"))))
    else:
        recs = attendance_summary(me, svc.portal.attendance(uid), svc.settings)
        tiles.append(("Courses you can't skip", len([r for r in recs if r["status"] in ("below minimum", "can't miss any")])))
    for col, (label, val) in zip(st.columns(len(tiles)), tiles):
        with col.container(border=True):
            st.metric(label, val)

    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Today")
        evs = [e for e in svc.engine.person_events(uid, today, today) if e.kind not in ("exam_prep", "pref") and e.end >= now]
        if not evs:
            empty("Nothing else on today.")
        st.markdown("".join(event_html(e) for e in evs), unsafe_allow_html=True)
        st.subheader("Deadlines this week")
        if not dls:
            empty("No deadlines in the next 7 days.")
        for d in dls:
            due = datetime.fromisoformat(d["due"])
            st.markdown(f"{tag(fmt_dt(due), 'red' if due < now else 'amber')} {html.escape(d['title'])}",
                        unsafe_allow_html=True)
    with right:
        st.subheader("Waiting for your reply")
        if not needs:
            empty("You're all caught up.")
        for m in needs[:6]:
            t = svc.triage.classify(m)
            a, b = st.columns([5, 1], vertical_alignment="center")
            a.markdown(f"**{html.escape(sender_name(m))}** {tag(svc.triage.label(t['category']), 'accent')}  \n"
                       f"<span class='muted'>{html.escape(m.subject)}</span>", unsafe_allow_html=True)
            b.button("Open", key=f"home_open_{m.id}", type="tertiary", on_click=go,
                     args=("Inbox",), kwargs={"sel_mail": m.id, "inbox_filter": None})
        alerts = svc.store.alerts(uid)[:4]
        if alerts:
            st.subheader("Alerts")
            for x in alerts:
                kind = {"critical": "red", "warning": "amber"}.get(x.level, "accent")
                st.markdown(f"{tag(x.level.capitalize(), kind)} {html.escape(x.message)}", unsafe_allow_html=True)

    if me.role == "faculty":
        st.write("")
        with st.expander("Your week in numbers"):
            monday = today - timedelta(days=today.weekday())
            stt = svc.faculty_stats(uid, monday, monday + timedelta(days=5))
            c = st.columns(4)
            c[0].metric("Meetings", stt["meetings"])
            c[1].metric("Cabin tokens", stt["tokens"])
            c[2].metric("Cabin hours used", f"{stt['cabin_use_pct']}%" if stt["cabin_use_pct"] is not None else "-")
            c[3].metric("Median reply", f"{stt['median_reply_min']} min" if stt["median_reply_min"] is not None else "-")
            import altair as alt
            order = list(stt["by_day"].keys())
            chart = (alt.Chart(pd.DataFrame({"Day": order, "Meetings": list(stt["by_day"].values())}))
                     .mark_bar(color="#2C4BA0", size=24, cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                     .encode(x=alt.X("Day:N", sort=order, axis=alt.Axis(labelAngle=0, title=None)),
                             y=alt.Y("Meetings:Q", axis=alt.Axis(tickMinStep=1, title=None, gridOpacity=0.35)),
                             tooltip=["Day", "Meetings"]).properties(height=180))
            st.altair_chart(chart, width="stretch")
            st.caption(f"Time this week: teaching {stt['busy_pct']['class']}%, meetings {stt['busy_pct']['meeting']}%, "
                       f"other {stt['busy_pct']['other']}%, free {stt['free_pct']}%.")
            st.dataframe(pd.DataFrame(svc.department_table(monday, monday + timedelta(days=5))).astype(object)
                         .where(lambda x: x.notna(), "-"), hide_index=True, width="stretch")


# ====================================================================== Inbox
DECISION_HEAD = {
    "accept": "Book it",
    "offer": "Offer other times",
    "cancel": "Cancel the meeting",
    "nothing_to_cancel": "Ask which meeting",
    "no_slots": "No common time found",
    "not_meeting": "Nothing to schedule",
    "leave_parent": "Parent's leave mail recorded",
    "leave_info": "Point them to VTOP",
}


def render_proposal(prop, mail_id: str) -> None:
    head = DECISION_HEAD.get(prop.decision, prop.decision)
    if prop.decision == "accept" and prop.chosen:
        head = f"Book {fmt_range(prop.chosen.start, prop.chosen.end)}"
    elif prop.decision == "offer" and prop.offer_id:
        head = "That time was taken. Offer fresh times"
    st.markdown(f"##### {head}")
    st.markdown(f"<span class='muted'>{html.escape(prop.summary)}</span>", unsafe_allow_html=True)
    if prop.private_notes:
        st.caption("Only you see this: " + "; ".join(prop.private_notes))
    if prop.leave:
        ck = svc.leave.checklist(prop.leave)
        st.markdown(tag("Parent mail " + ("received" if ck["parent_mail"] else "missing"), "green" if ck["parent_mail"] else "red")
                    + tag("VTOP application " + ("done" if ck["applied"] else "missing"), "green" if ck["applied"] else "red"),
                    unsafe_allow_html=True)
        if me.role == "faculty":
            st.button("Open leave requests", type="tertiary", key=f"golv_{mail_id}", on_click=go, args=("Students",),
                      kwargs={"students_view": "Leave requests"})
    if prop.decision == "not_meeting":
        if st.button("Mark as done", key=f"done_{mail_id}"):
            svc.confirm(prop)
            st.session_state.pop(f"prop_{mail_id}", None)
            st.rerun()
        return
    body = st.text_area("Reply", prop.reply_body, height=210, key=f"body_{mail_id}", label_visibility="collapsed")
    real_to = []
    if svc.live_mail and prop.responder_id == svc.owner_id:
        real_to = [a for a in (svc.directory.real_email(p) for p in prop.participants if p != prop.responder_id)
                   if is_real_address(a)]
    row = st.container(horizontal=True)
    send = row.button("Approve and send", type="primary", key=f"send_{mail_id}")
    if prop.decision == "offer" and prop.options:
        with row.popover("Book one now"):
            st.caption("Skip the back-and-forth and book one of the offered times.")
            for i, s in enumerate(prop.options):
                lab = fmt_range(s.start, s.end) + (f" (without {', '.join(first(x) for x in s.missing)})" if s.missing else "")
                if st.button(lab, key=f"book_{mail_id}_{i}", type="tertiary"):
                    st.session_state[f"prop_{mail_id}"] = svc.handler.choose_option(prop, i)
                    st.session_state.pop(f"body_{mail_id}", None)
                    st.rerun()
    if real_to:
        st.caption(f"Sends from {svc.directory.owner_email} to {', '.join(real_to)} in the same Gmail thread.")
    with st.expander("How the agent read this"):
        p = prop.parsed
        bits = [p.intent.replace("_", " ")]
        if p.date:
            bits.append(fmt_date(p.date))
        if p.start:
            bits.append(fmt_time(p.start) + (f"-{fmt_time(p.end)}" if p.end else ""))
        if p.part_of_day:
            bits.append(p.part_of_day)
        bits.append(f"{prop.duration_min} min")
        if p.urgent:
            bits.append("urgent")
        st.write(f"Read by {'DeepSeek' if p.source == 'deepseek' else 'the built-in parser'}: " + ", ".join(bits) + ".")
        if p.model_intent:
            st.write(f"Neural check: {p.model_intent.replace('_', ' ')} ({p.model_conf:.0%} sure).")
        if len(prop.participants) > 2:
            st.write("Group: " + ", ".join(name(x) for x in prop.participants) + ".")
        if prop.decision == "offer":
            st.write(f"Offered times are held for {svc.handler.hold_hours:g} hours once sent.")
        st.write("Draft by " + ("DeepSeek, checked so no times changed." if prop.drafted_by == "deepseek" else "template."))
    if send:
        try:
            res = svc.confirm(prop, body)
            st.session_state.pop(f"prop_{mail_id}", None)
            if res.get("meeting") and prop.decision == "accept":
                msg = f"Booked {fmt_range(res['meeting'].start, res['meeting'].end)} and replied."
            elif prop.decision == "cancel":
                msg = "Meeting cancelled and replied."
            else:
                msg = "Reply sent" + (" from Gmail." if real_to else ".")
            if real_to and res.get("reply") and res["reply"].meta.get("delivered") == "failed":
                flash(f"Saved, but Gmail didn't send it: {res['reply'].meta.get('delivery_error')}", "warning")
            else:
                flash(msg)
            st.rerun()
        except SlotTaken as e:
            st.error(str(e))


def mail_detail(m) -> None:
    tri = svc.triage.classify(m)
    cat = svc.triage.label(tri["category"])
    is_new = m.status == "unread" and m.kind == "human"
    st.markdown(f"#### {html.escape(m.subject)}")
    sender_addr = m.meta.get("from_email")
    to_txt = ", ".join("you" if x == uid else name(x) for x in m.to_ids[:3]) + (f" and {len(m.to_ids) - 3} more" if len(m.to_ids) > 3 else "")
    via = m.meta.get("via") or split_via(m.meta.get("from_name", ""))[1]
    st.markdown(f"<span class='muted'>{html.escape(sender_name(m))}"
                + (f" via {html.escape(via)}" if via else "")
                + (f" &lt;{html.escape(sender_addr)}&gt;" if sender_addr else "")
                + f" to {html.escape(to_txt)}, {fmt_dt(m.sent_at)}</span>", unsafe_allow_html=True)
    chips = [tag(cat, "accent")]
    if tri.get("priority") == "high":
        chips.append(tag("High priority", "red"))
    if tri["deadline"]:
        chips.append(tag(f"Due {fmt_dt(tri['deadline'])}", "amber"))
    if m.meta.get("delivered") == "gmail":
        chips.append(tag("Sent from Gmail", "green"))
    elif m.meta.get("delivered") == "failed":
        chips.append(tag("Gmail send failed", "red"))
    if m.kind == "agent":
        chips.append(tag("Written by their agent"))
    st.markdown("".join(chips), unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown(f"<div class='mailbody'>{html.escape(m.body)}</div>", unsafe_allow_html=True)

    # time options another agent offered: pick one
    opts = m.meta.get("options")
    if opts and m.kind == "agent" and m.from_id != uid:
        st.markdown("##### Pick a time")
        cols = st.columns(len(opts))
        for i, o in enumerate(opts):
            s, e = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
            if cols[i].button(fmt_range(s, e), key=f"pick_{m.id}_{i}", width="stretch"):
                r = svc.send_mail(uid, [m.from_id], "", f"Option {i + 1} works for me. Thanks!", in_reply_to=m.id)
                flash(f"Sent: option {i + 1} works." + (" Their agent booked it." if r["auto"] else ""))
                st.rerun()

    mid = m.meta.get("meeting_id")
    if mid and svc.store.get_meeting(mid):
        mt = svc.store.get_meeting(mid)
        a, b = st.columns([3, 1])
        a.caption(f"Meeting {mt.status}: {fmt_range(mt.start, mt.end)}, {mt.location}")
        b.download_button("Add to calendar", svc.ics_for(mid), file_name=f"{mid}.ics", mime="text/calendar",
                          key=f"ics_{m.id}", type="tertiary")

    # the agent
    # the agent: only for mail written to this person (group notices and newsletters are just for reading)
    personal = m.kind == "human" and m.from_id != uid and not tri.get("bulk")
    if personal:
        pk = f"prop_{m.id}"
        agent_cat = tri["category"] in ("meeting", "leave")
        if agent_cat and tri["action"] and is_new and pk not in st.session_state:
            with st.spinner("Checking timetables..."):
                st.session_state[pk] = svc.process_mail(m.id, uid)
        if pk in st.session_state:
            st.markdown("<span class='muted'>Agent's suggestion</span>", unsafe_allow_html=True)
            render_proposal(st.session_state[pk], m.id)
        else:
            row = st.container(horizontal=True)
            label = "Draft a reply with the agent" if agent_cat else "Check it for a meeting request"
            if row.button(label, key=f"run_{m.id}", type="secondary"):
                st.session_state[pk] = svc.process_mail(m.id, uid)
                st.session_state.pop(f"body_{m.id}", None)
                st.rerun()
            if tri["action"] and is_new and row.button("Mark as done", key=f"markdone_{m.id}", type="tertiary"):
                svc.mail.mark(m.id, "processed")
                st.rerun()

        with st.expander("Reply yourself"):
            with st.form(f"reply_{m.id}", clear_on_submit=True):
                txt = st.text_area("Message", height=90, label_visibility="collapsed",
                                   placeholder="e.g. Option 1 works, or: Can we do Friday 4 pm instead?")
                if st.form_submit_button("Send") and txt.strip():
                    r = svc.send_mail(uid, [m.from_id], "", txt, in_reply_to=m.id)
                    svc.mail.mark(m.id, "processed")
                    flash("Reply sent." + (" Their agent answered." if r["auto"] else ""))
                    st.rerun()
    sender = svc.person(m.from_id)
    if m.meta.get("source") == "gmail" and sender_addr and sender is not None and sender.role == "external":
        with st.expander("Link this sender to VTOP"):
            st.caption("They aren't in VTOP, so only your timetable is checked. Link them to check theirs too.")
            vt = [p for p in svc.base_portal.list_people() if p.role in ("faculty", "student", "parent") and p.id != svc.owner_id]
            choice = st.selectbox("VTOP profile", [p.id for p in vt], key=f"link_{m.id}",
                                  format_func=lambda i: f"{svc.base_portal.get_person(i).name} ({svc.base_portal.get_person(i).role})")
            if st.button("Link", key=f"linkbtn_{m.id}"):
                svc.link_contact(sender_addr, choice)
                st.session_state.pop(f"prop_{m.id}", None)
                flash(f"Linked to {svc.base_portal.get_person(choice).name}.")
                st.rerun()


def page_inbox():
    inbox = svc.mail.inbox(uid)
    live_owner = svc.live_mail and uid == svc.owner_id
    todo = svc.triage.pending(inbox)
    if todo and svc.triage.llm_on:
        with st.spinner(f"Sorting {len(todo)} mail with DeepSeek..."):
            svc.triage.classify_many(inbox)
    tri_of = {m.id: svc.triage.classify(m) for m in inbox}
    inbox = [m for m in inbox if not svc.triage.hidden(tri_of[m.id])]  # promotions never show up

    h1, h2, h3 = st.columns([5, 1.3, 1.6])
    h1.title("Inbox")
    with h2:
        st.write("")
        if st.button("New mail", width="stretch"):
            compose_dialog()
    new_requests = [m for m in inbox if m.status == "unread" and m.kind == "human" and m.from_id != uid
                    and tri_of[m.id]["action"] and tri_of[m.id]["category"] in ("meeting", "leave")]
    with h3:
        st.write("")
        if st.button("Answer all requests", width="stretch", disabled=not new_requests,
                     help="The agent replies to every new meeting and leave mail without showing you each draft"):
            res = svc.process_all_unread(uid)
            flash("\n".join(f"- {r['mail'].subject}: {r['summary']}" for r in res) or "Nothing to answer.", "info")
            st.rerun()

    source = "All"
    if live_owner:
        source = st.segmented_control("Source", ["Gmail", "Demo mail", "All"], default="Gmail", key="inbox_source",
                                      label_visibility="collapsed") or "Gmail"
    if source == "Gmail":
        inbox = [m for m in inbox if m.meta.get("source") == "gmail"]
    elif source == "Demo mail":
        inbox = [m for m in inbox if m.meta.get("source") != "gmail"]

    counts: dict[str, int] = {}
    for m in inbox:
        lab = svc.triage.label(tri_of[m.id]["category"])
        counts[lab] = counts.get(lab, 0) + 1
    order = ["Meeting", "Leave request", "Deadline", "Question", "Announcement"]
    cats = [c for c in order if c in counts] + sorted([c for c in counts if c not in order + ["Update", "Other"]],
                                                      key=lambda c: -counts[c]) + [c for c in ("Update", "Other") if c in counts]
    needs_n = len([m for m in inbox if m.status == "unread" and m.kind == "human" and tri_of[m.id]["action"]])
    opts = ["All", "Needs reply"] + cats
    if st.session_state.get("inbox_filter") not in opts + [None]:
        st.session_state["inbox_filter"] = None
    show = st.pills("Filter", opts, key="inbox_filter", label_visibility="collapsed",
                    format_func=lambda o: f"{o} {needs_n}" if o == "Needs reply" else (f"{o} {counts[o]}" if o in counts else o))
    show = show or "All"

    visible = []
    for m in inbox:
        t = tri_of[m.id]
        if show == "Needs reply" and not (t["action"] and m.status == "unread" and m.kind == "human"):
            continue
        if show not in ("All", "Needs reply") and svc.triage.label(t["category"]) != show:
            continue
        visible.append(m)
    if not visible:
        empty("No mail here." + ("" if inbox else " Send yourself a request from another profile with New mail."))
        return

    ids_ = [m.id for m in visible]
    if st.session_state.get("sel_mail") not in ids_:
        waiting = [m.id for m in visible if tri_of[m.id]["action"] and m.status == "unread" and m.kind == "human"]
        st.session_state["sel_mail"] = (waiting or ids_)[0]
    by_id = {m.id: m for m in visible}

    pos = {mid: i for i, mid in enumerate(ids_)}

    def row_label(mid):
        m = by_id[mid]
        sender = html.escape(sender_name(m)).replace("*", "")
        # The radio matches a click back to its option by the shown text, so two mails from the same person
        # would collide. A different number of zero-width spaces keeps every label unique and invisible.
        tail = "\u200b" * pos[mid]
        return (f"**{sender}**" if m.status == "unread" and m.kind == "human" else sender) + tail

    def row_caption(mid):
        m = by_id[mid]
        return f"{m.subject}  \n{svc.triage.label(tri_of[mid]['category'])}, {short_when(m.sent_at)}"

    left, right = st.columns([4, 7], gap="large")
    with left:
        with st.container(height=640 if len(ids_) > 8 else "content", key="maillist", border=True):
            sel = st.radio("Mail", ids_, key="sel_mail", label_visibility="collapsed", format_func=row_label,
                           captions=[row_caption(i) for i in ids_])
    with right:
        mail_detail(by_id[sel])


# ====================================================================== Calendar
def page_calendar():
    st.title("Calendar")
    views = ["Week", "Upcoming", "Deadlines", "Busy times"] + (["Attendance"] if me.role == "student" else [])
    view = st.segmented_control("View", views, default="Week", key="cal_view", label_visibility="collapsed") or "Week"
    today = svc.now().date()

    if view == "Week":
        a, b = st.columns([1, 4])
        pick = a.date_input("Week of", today, key="week_of", label_visibility="collapsed", format="DD/MM/YYYY")
        b.caption("Classes, exams, holidays, meetings and your own busy times. A dashed class is one you can safely skip."
                  + (" Using your pasted timetable." if svc.store.imported_timetable(uid) else ""))
        monday = pick - timedelta(days=pick.weekday())
        days = [monday + timedelta(days=i) for i in range(6)]
        cal = svc.engine.calendar_days(days[0], days[-1])
        dues = svc.triage.deadlines(uid)
        for col, d in zip(st.columns(6, gap="small"), days):
            kind = svc.engine.day_kind(d, cal)
            label = cal[d].label if d in cal and kind != "instructional" else ""
            evs = [e for e in svc.engine.person_events(uid, d, d) if e.kind not in ("exam_prep", "pref")]
            due = "".join(f"<div class='ev ev-due'><b>Due {x['due'][11:16]}</b> {html.escape(x['title'])}</div>"
                          for x in dues if x["due"][:10] == d.isoformat())
            with col:
                st.markdown(f"<div class='dayhead{' today' if d == today else ''}'><b>{WEEKDAYS[d.weekday()]} {d.day}</b>"
                            f"<span>{html.escape(label) or '&nbsp;'}</span></div>"
                            + (due + "".join(event_html(e) for e in evs) or "<div class='freeday'>Free</div>"),
                            unsafe_allow_html=True)

    elif view == "Upcoming":
        ms = [m for m in svc.store.meetings(uid) if m.end >= svc.now()]
        if not ms:
            empty("No meetings booked.")
        for m in ms:
            with st.container(border=True):
                a, b, c = st.columns([5, 1.3, 1], vertical_alignment="center")
                kind = {"office_hours": "Cabin token", "proctor": "Proctor check-in"}.get(m.kind)
                a.markdown(f"**{fmt_range(m.start, m.end)}**  \n{html.escape(m.title)} with "
                           f"{html.escape(', '.join(name(x) for x in m.participant_ids if x != uid))}, {html.escape(m.location)}"
                           + ("  \n" + tag("Needs a new time", "red") if m.status == "needs_attention" else "")
                           + ("  \n" + tag(kind) if kind else ""), unsafe_allow_html=True)
                b.download_button("Add to calendar", svc.ics_for(m.id), file_name=f"{m.id}.ics", mime="text/calendar",
                                  key=f"wk_ics_{m.id}", type="tertiary")
                if c.button("Cancel", key=f"wk_cancel_{m.id}", type="tertiary"):
                    res = svc.cancel_meeting(m.id, uid)
                    msg = f"Cancelled '{m.title}'. Everyone was told."
                    if res.get("waitlist"):
                        msg += f" The slot went to {name(res['waitlist']['assigned_to'])} from the waitlist."
                    flash(msg)
                    st.rerun()
        st.write("")
        with st.expander("I can't make it on a day"):
            st.caption("Every meeting in that window moves to a time that works for everyone, and they're all told.")
            with st.form("unavail_form"):
                a, b, c = st.columns(3)
                d = a.date_input("Date", today + timedelta(days=1), format="DD/MM/YYYY")
                s = b.time_input("From", time(8, 0), step=timedelta(minutes=10))
                e = c.time_input("To", time(19, 0), step=timedelta(minutes=10))
                reason = st.text_input("Reason (only you see it)", "Unavailable")
                go_ = st.form_submit_button("Preview")
            if go_:
                st.session_state["unavail"] = (datetime.combine(d, s), datetime.combine(d, e), reason,
                                               svc.plan_unavailability(uid, datetime.combine(d, s), datetime.combine(d, e), reason))
            if "unavail" in st.session_state:
                s_, e_, reason_, items = st.session_state["unavail"]
                if not items:
                    st.info(f"No meetings in {fmt_range(s_, e_)}.")
                else:
                    st.dataframe(pd.DataFrame([{
                        "Meeting": it.meeting.title, "With": ", ".join(name(p) for p in it.meeting.participant_ids if p != uid),
                        "Now": fmt_range(it.meeting.start, it.meeting.end),
                        "Moves to": fmt_range(it.new_slot.start, it.new_slot.end) if it.new_slot else "no common time"}
                        for it in items]), hide_index=True, width="stretch")
                    n_people = sum(len(it.meeting.participant_ids) - 1 for it in items)
                    if st.button(f"Move them and tell {n_people} people", type="primary"):
                        res = svc.apply_unavailability(uid, s_, e_, reason_, items)
                        st.session_state.pop("unavail")
                        flash(f"Moved {res['moved']} meeting(s)." + (f" {res['needs_attention']} need a new time." if res["needs_attention"] else ""))
                        st.rerun()

    elif view == "Deadlines":
        dls = svc.triage.deadlines(uid)
        if not dls:
            empty("No open deadlines. Deadlines in your mail are added here automatically.")
        for d in dls:
            due = datetime.fromisoformat(d["due"])
            a, b = st.columns([6, 1])
            a.markdown(f"{tag(fmt_dt(due), 'red' if due < svc.now() else 'amber')} {html.escape(d['title'])}"
                       + (f" <span class='muted'>from {html.escape(name(d['from_id']))}</span>" if d["from_id"] != uid else ""),
                       unsafe_allow_html=True)
            if b.button("Done", key=f"dl_done_{d['id']}", type="tertiary"):
                svc.triage.set_done(d["id"])
                st.rerun()
        with st.expander("Add a deadline"):
            with st.form("add_deadline", clear_on_submit=True):
                a, b, c = st.columns([3, 2, 1])
                t_ = a.text_input("What", placeholder="DA-2 Software Metrics")
                dd = b.date_input("Due", today + timedelta(days=3), format="DD/MM/YYYY")
                tt = c.time_input("Time", time(23, 59), step=timedelta(minutes=30))
                if st.form_submit_button("Add") and t_.strip():
                    svc.triage.add_deadline(uid, t_.strip(), datetime.combine(dd, tt))
                    st.rerun()

    elif view == "Busy times":
        page_busy_times()

    elif view == "Attendance":
        rows = attendance_summary(me, svc.portal.attendance(uid), svc.settings)
        st.caption(f"Minimum {svc.settings.get('attendance.minimum_percent')}%. CGPA {svc.settings.get('attendance.exemption_cgpa')}+ "
                   f"is exempt. Your CGPA: {me.cgpa}.")
        if rows:
            st.dataframe(pd.DataFrame(rows).rename(columns={
                "course_code": "Code", "course_title": "Course", "attended": "Attended", "total": "Held",
                "percent": "%", "can_miss": "Can still miss", "status": "Status"}), hide_index=True, width="stretch")
        else:
            empty("This portal doesn't share attendance.")


def show_clashes(c, clashes):
    if not clashes:
        st.success(f"Added '{c.title}'. No clashes.")
        return
    with st.container(border=True):
        st.markdown(f"**'{html.escape(c.title)}' clashes with {len(clashes)} thing(s)**")
        for cl in clashes:
            kind = {"critical": "red", "warning": "amber"}.get(cl["severity"], "accent")
            st.markdown(tag(cl["severity"].capitalize(), kind) + f" {html.escape(cl['with'])}, {cl['when']}"
                        + (f" <span class='muted'>{html.escape(cl['note'])}</span>" if cl["note"] else ""),
                        unsafe_allow_html=True)
        mids = [cl["meeting_id"] for cl in clashes if cl["meeting_id"]]
        if mids and st.button("Move the clashing meetings and tell everyone", type="primary", key=f"fix_{c.id}"):
            summary, items = svc.reschedule_clashing_meetings(uid, c, mids)
            flash(f"Moved {summary['moved']} meeting(s)." + (f" {summary['needs_attention']} need a new time." if summary["needs_attention"] else ""))
            st.session_state.pop("last_commitment", None)
            st.rerun()


def page_busy_times():
    st.caption("Things VTOP doesn't know about, like club work or appointments. The agent treats them as busy. Only you see them.")
    with st.form("quick_add", clear_on_submit=True, border=False):
        a, b = st.columns([5, 1])
        quick = a.text_input("Add in plain words", label_visibility="collapsed",
                             placeholder="Club practice every Tue 5-6:30pm, or Dentist tomorrow 10am")
        if b.form_submit_button("Add", width="stretch") and quick.strip():
            try:
                st.session_state["last_commitment"] = svc.add_commitment_from_text(uid, quick)
            except ValueError as e:
                st.error(str(e))
    if "last_commitment" in st.session_state and st.session_state["last_commitment"][0].person_id == uid:
        show_clashes(*st.session_state["last_commitment"])
    mine = sorted(svc.store.commitments(uid), key=lambda x: x.start)
    if not mine:
        empty("Nothing added yet.")
    for c in mine:
        a, b = st.columns([6, 1])
        rec = f", every week until {fmt_date(c.until)}" if c.recurrence and c.until else (", weekly" if c.recurrence else "")
        a.markdown(f"**{html.escape(c.title)}** <span class='muted'>{fmt_range(c.start, c.end)}{rec}</span>",
                   unsafe_allow_html=True)
        if b.button("Remove", key=f"del_{c.id}", type="tertiary"):
            svc.delete_commitment(c.id)
            st.rerun()
    st.write("")
    with st.expander("More ways to add"):
        how = st.segmented_control("How", ["Form", "Calendar file", "Paste timetable"], default="Form",
                                   key="busy_how", label_visibility="collapsed") or "Form"
        if how == "Form":
            with st.form("commit_form"):
                t = st.text_input("Title")
                a, b, cc = st.columns(3)
                d = a.date_input("Date", svc.now().date(), format="DD/MM/YYYY")
                s = b.time_input("Start", time(17, 0), step=timedelta(minutes=10))
                e = cc.time_input("End", time(18, 0), step=timedelta(minutes=10))
                a2, b2, c2 = st.columns(3)
                cat = a2.selectbox("Type", ["unofficial", "urgent", "official"])
                weekly = b2.checkbox("Every week")
                until = c2.date_input("Until", svc.now().date() + timedelta(days=60), format="DD/MM/YYYY")
                if st.form_submit_button("Add"):
                    try:
                        st.session_state["last_commitment"] = svc.add_commitment(
                            uid, t or "Busy", datetime.combine(d, s), datetime.combine(d, e), cat,
                            "weekly" if weekly else None, until if weekly else None)
                        st.rerun()
                    except ValueError as ex:
                        st.error(str(ex))
        elif how == "Calendar file":
            up = st.file_uploader("An .ics file from Google Calendar or Outlook", type=["ics"])
            if up and st.button("Import"):
                res = svc.import_ics(uid, up.read().decode("utf-8", "ignore"))
                flash(f"Imported {len(res)} event(s).")
                st.rerun()
        else:
            st.caption("One course per line with its code and slot, e.g. SWE3001 Deep Learning A1+TA1 SJT 301.")
            if svc.store.imported_timetable(uid):
                st.info(f"Using your pasted timetable ({len(svc.store.imported_timetable(uid))} courses).")
                if st.button("Go back to the VTOP timetable"):
                    svc.clear_imported_timetable(uid)
                    st.rerun()
            tt = st.text_area("Timetable", height=130, key="tt_text", label_visibility="collapsed",
                              placeholder="SWE3001 Deep Learning A1+TA1 SJT 301\nSWE3001P Deep Learning Lab L31+L32 SJT 805")
            row = st.container(horizontal=True)
            prev_ = row.button("Preview")
            use_ = row.button("Use it", type="primary")
            if prev_ and tt.strip():
                rows, probs = svc.preview_timetable(tt)
                if rows:
                    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                for pr in probs:
                    st.warning(pr)
            if use_ and tt.strip():
                try:
                    res = svc.import_timetable(uid, tt)
                    flash(f"Using {res['courses']} courses from your timetable.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


# ====================================================================== Leave / Students
def leave_status(req: dict) -> str:
    return {"awaiting_application": "Waiting for VTOP application", "pending": "Waiting for proctor",
            "approved": "Approved", "rejected": "Not approved"}[req["status"]]


def impact_block(an: dict) -> None:
    st.markdown(f"**{an['days']} working day(s), {len(an['classes'])} class(es) missed**"
                + (" <span class='muted'>(exempt from the attendance minimum)</span>" if an["exempt"] else ""),
                unsafe_allow_html=True)
    if an["attendance"]:
        st.dataframe(pd.DataFrame([{"Course": f"{a['course_code']} {a['course_title']}", "Missed": a["missed"],
                                    "Now": f"{a['now']}%", "After": f"{a['after']}%",
                                    "Below 75% after": "Yes" if a["below"] else ""} for a in an["attendance"]]),
                     hide_index=True, width="stretch")
    for e in an["exams"]:
        st.error(f"Exam during the leave: {e['title']}, {e['when']}")
    if an["meetings"]:
        st.caption("Meetings that move on approval: " + "; ".join(f"{m.title} ({fmt_range(m.start, m.end)})" for m in an["meetings"]))


def leave_approver():
    pend = svc.leave.for_approver(uid, "pending")
    waiting = svc.leave.for_approver(uid, "awaiting_application")
    st.caption("Parent mails the proctor, the student applies on VTOP, you approve. Only working days count.")
    if not pend:
        empty("No leave applications to approve.")
    for req in pend:
        ck = svc.leave.checklist(req)
        with st.container(border=True):
            st.markdown(f"**{html.escape(name(req['student_id']))}** <span class='muted'>{svc.leave.describe(req)}, "
                        f"{html.escape(req['reason'])}</span>", unsafe_allow_html=True)
            st.markdown(tag("Parent mail " + ("received" if ck["parent_mail"] else "missing"), "green" if ck["parent_mail"] else "red")
                        + tag("Applied on VTOP", "green")
                        + (tag("Dates match the parent's mail", "green") if ck["dates_match"] else
                           tag("Dates differ from the parent's mail", "amber") if ck["parent_mail"] else ""),
                        unsafe_allow_html=True)
            if req.get("parent_mail_id"):
                pm = svc.mail.get(req["parent_mail_id"])
                if pm:
                    with st.expander(f"Parent's mail ({name(pm.from_id)})"):
                        st.markdown(f"<div class='mailbody'>{html.escape(pm.body)}</div>", unsafe_allow_html=True)
            impact_block(svc.leave.analysis_for(req))
            note = st.text_input("Note to the student", key=f"lnote_{req['id']}", placeholder="Optional")
            force = False
            if not ck["parent_mail"]:
                force = st.checkbox("Approve without the parent's mail", key=f"lforce_{req['id']}")
            row = st.container(horizontal=True)
            if row.button("Approve", type="primary", key=f"lapp_{req['id']}", disabled=not (ck["parent_mail"] or force)):
                res = svc.approve_leave(req["id"], uid, note, without_parent_mail=force)
                flash(f"Leave approved. {res['faculty_told']} course faculty told, {res['moved']['moved']} meeting(s) moved.")
                st.rerun()
            if row.button("Reject", key=f"lrej_{req['id']}"):
                svc.reject_leave(req["id"], uid, note)
                flash("Leave not approved. The student was told.")
                st.rerun()
    if waiting:
        st.markdown("##### Parent mailed, student hasn't applied yet")
        for req in waiting:
            st.markdown(f"{html.escape(name(req['student_id']))} <span class='muted'>{svc.leave.describe(req)}, "
                        f"{html.escape(req['reason'])}</span>", unsafe_allow_html=True)
    done = [r for r in svc.leave.for_approver(uid) if r["status"] in ("approved", "rejected")]
    if done:
        with st.expander(f"Decided ({len(done)})"):
            for req in done[:20]:
                st.markdown(f"{html.escape(name(req['student_id']))} <span class='muted'>{svc.leave.describe(req)}</span> "
                            + tag(leave_status(req), "green" if req["status"] == "approved" else "red"), unsafe_allow_html=True)


def proctor_view():
    ov = svc.proctor.overview(uid)
    if not ov:
        empty("You have no proctees.")
        return
    st.caption("Risk comes from attendance and time since the last check-in.")
    st.dataframe(pd.DataFrame([{
        "Student": r["student"].name, "Risk": r["level"].capitalize(), "Why": "; ".join(r["reasons"]) or "-",
        "Lowest attendance": f"{r['lowest']}%" if r["lowest"] is not None else "-",
        "Next check-in": fmt_range(r["next"].start, r["next"].end) if r["next"] else "-"} for r in ov]),
        hide_index=True, width="stretch")
    with st.form("plan_checkins"):
        a, b, c, d_ = st.columns(4)
        p_from = a.date_input("From", svc.now().date() + timedelta(days=1), format="DD/MM/YYYY")
        p_to = b.date_input("To", svc.now().date() + timedelta(days=8), format="DD/MM/YYYY")
        p_dur = c.number_input("Minutes each", 10, 60, 15, step=5)
        d_.write("")
        p_risk = d_.checkbox("Only at-risk students")
        plan_go = st.form_submit_button("Plan check-ins", type="primary")
    if plan_go:
        st.session_state["checkin_plan"] = svc.proctor.plan(uid, p_from, p_to, int(p_dur), p_risk)
    plan = st.session_state.get("checkin_plan")
    if plan is not None:
        if not plan:
            st.info("Everyone already has a check-in booked.")
        else:
            st.dataframe(pd.DataFrame([{"Student": i["student"].name, "Risk": i["level"].capitalize(),
                                        "Time": fmt_range(i["slot"].start, i["slot"].end) if i["slot"] else "no common free time"}
                                       for i in plan]), hide_index=True, width="stretch")
            if st.button("Book these and mail each student", type="primary"):
                booked = svc.proctor.apply(uid, plan)
                st.session_state.pop("checkin_plan", None)
                flash(f"Booked {len(booked)} check-in(s).")
                st.rerun()


def page_students():
    st.title("Students")
    n = len(svc.leave.for_approver(uid, "pending"))
    view = st.segmented_control("View", ["Leave requests", "Proctor check-ins"], default="Leave requests",
                                key="students_view", label_visibility="collapsed",
                                format_func=lambda v: f"{v} ({n})" if v == "Leave requests" and n else v) or "Leave requests"
    if view == "Leave requests":
        leave_approver()
    else:
        proctor_view()


def page_leave():
    st.title("Leave")
    sid = uid if me.role == "student" else me.ward_id
    stu = svc.person(sid)
    proctor = svc.person(stu.proctor_id) if stu.proctor_id else None
    st.caption("Leave needs two things: a mail from a parent to the proctor"
               + (f" ({proctor.name})" if proctor else "") + ", and an application on VTOP.")
    if me.role == "student":
        box = st.columns([3, 2])[0]
        part = box.toggle("Only part of a day", key="leave_part")
        with box.form("leave_form"):
            a, b = st.columns(2)
            l_from = a.date_input("From", svc.now().date() + timedelta(days=1), format="DD/MM/YYYY")
            l_to = b.date_input("To", svc.now().date() + timedelta(days=1), format="DD/MM/YYYY")
            l_s, l_e = time(8, 0), time(13, 0)
            if part:
                c, d_ = st.columns(2)
                l_s = c.time_input("From time", time(8, 0), step=timedelta(minutes=10))
                l_e = d_.time_input("To time", time(13, 0), step=timedelta(minutes=10))
            reason = st.text_input("Reason", "Unwell")
            row = st.container(horizontal=True)
            check = row.form_submit_button("See what it costs")
            go_ = row.form_submit_button("Apply on VTOP", type="primary")
        if check:
            impact_block(svc.leave.analyse(sid, l_from, l_to, l_s if part else None, l_e if part else None))
        if go_:
            try:
                req = svc.apply_leave(sid, l_from, l_to, reason, l_s if part else None, l_e if part else None)
                flash("Applied on VTOP." + (" Your parent's mail is in, so it's with your proctor."
                                            if svc.leave.checklist(req)["parent_mail"] else " Ask your parent to mail your proctor."))
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    else:
        st.button("Write to the proctor", type="primary", on_click=go, args=("Inbox",))
    st.subheader("Status")
    mine = svc.leave.for_student(sid)
    if not mine:
        empty("No leave on record.")
    for req in mine:
        ck = svc.leave.checklist(req)
        st.markdown(f"**{svc.leave.describe(req)}** <span class='muted'>{html.escape(req['reason'])}</span>  \n"
                    + tag(leave_status(req), {"approved": "green", "rejected": "red"}.get(req["status"], "accent"))
                    + tag("Parent mail " + ("received" if ck["parent_mail"] else "missing"), "green" if ck["parent_mail"] else "")
                    + tag("VTOP " + ("applied" if ck["applied"] else "not applied"), "green" if ck["applied"] else ""),
                    unsafe_allow_html=True)


# ====================================================================== Cabin hours
def page_cabin():
    st.title("Cabin hours")
    legend = {"free": "free", "booked": "taken", "mine": "yours", "past": "past", "unavailable": "busy", "held": "on hold"}
    if me.role == "faculty":
        st.caption(f"Students book {svc.office_hours.token_min}-minute slots in your cabin hours. A freed slot goes to the "
                   "first student on the waitlist.")
        fac_id = uid
    else:
        facs = [p.id for p in svc.people("faculty")]
        a, b = st.columns(2)
        fac_id = a.selectbox("Faculty", facs, index=facs.index(me.guide_id) if me.guide_id in facs else 0,
                             format_func=name, key="cabin_fac")
        topic = b.text_input("About", "Doubt clearing", key="cabin_topic")
    sessions = svc.office_hours.sessions(fac_id, svc.now().date(), 8, viewer=uid)
    if not sessions:
        empty("No cabin hours in the next week.")
    for sess in sessions:
        st.markdown(f"**{fmt_date(sess['date'])}, {fmt_time(sess['start'])}-{fmt_time(sess['end'])}** "
                    f"<span class='muted'>{sess['free']} of {len(sess['tokens'])} free"
                    + (f", {len(sess['waitlist'])} waiting" if sess["waitlist"] else "") + "</span>", unsafe_allow_html=True)
        if me.role == "faculty":
            st.markdown("".join(f"<span class='tok tok-{t['status']}'>{fmt_time(t['start'])}<br>"
                                f"{html.escape(first(t['student_id']) if t['student_id'] else legend[t['status']])}</span>"
                                for t in sess["tokens"]), unsafe_allow_html=True)
            if sess["waitlist"]:
                st.caption("Waitlist: " + ", ".join(name(w["student_id"]) for w in sess["waitlist"]))
        else:
            cols = st.columns(len(sess["tokens"]))
            for col, t in zip(cols, sess["tokens"]):
                if col.button(f"{fmt_time(t['start'])}\n{legend[t['status']]}", key=f"tok_{fac_id}_{t['start'].isoformat()}",
                              disabled=t["status"] != "free", width="stretch"):
                    try:
                        m = svc.office_hours.book(uid, fac_id, t["start"], topic)
                        flash(f"Booked {fmt_range(m.start, m.end)} with {name(fac_id)}.")
                        st.rerun()
                    except OfficeHoursError as e:
                        st.error(str(e))
            if sess["free"] == 0 and not any(t["status"] == "mine" for t in sess["tokens"]):
                if st.button("Join the waitlist", key=f"wl_{fac_id}_{sess['start'].isoformat()}", type="tertiary"):
                    try:
                        svc.office_hours.join_waitlist(uid, fac_id, sess["start"], topic)
                        flash("You're on the waitlist.")
                        st.rerun()
                    except OfficeHoursError as e:
                        st.error(str(e))
        st.write("")


# ====================================================================== Assistant
def _ask_suggestion():
    v = st.session_state.get("ask_sugg")
    if v:
        st.session_state["pending_q"] = v
        st.session_state["ask_sugg"] = None


def page_assistant():
    st.title("Assistant")
    hk = f"chat_{uid}"
    hist = st.session_state.setdefault(hk, [])
    sugg = (["When is my guide free this week?", "Book 30 min with my guide tomorrow", "How is my attendance?",
             "When are my exams?"] if me.role == "student" else
            ["What meetings do I have this week?", "Show my schedule for tomorrow", "Any holidays coming up?",
             "When is Aarav Sharma free tomorrow?"])
    if not hist:
        st.caption("Ask about free times, meetings, exams, holidays or attendance. Answers come from the timetables, "
                   "not guesses.")
    st.pills("Try", sugg, key="ask_sugg", on_change=_ask_suggestion, label_visibility="collapsed")
    for i, h in enumerate(hist):
        with st.chat_message(h["role"]):
            st.markdown(h["content"].replace("\n", "  \n"))
            acts = h.get("actions") or []
            if acts and not h.get("used"):
                for j, (col, act) in enumerate(zip(st.columns(len(acts)), acts)):
                    if col.button(f"Request {act['label']}", key=f"act_{uid}_{i}_{j}", width="stretch"):
                        r = svc.request_meeting(uid, act["with"], datetime.fromisoformat(act["start"]),
                                                datetime.fromisoformat(act["end"]), act["title"])
                        h["used"] = True
                        flash(f"Request sent for {act['label']}." + (" Their agent booked it." if r["auto"] else ""))
                        st.rerun()
    q = st.chat_input("Ask something") or st.session_state.pop("pending_q", None)
    if q:
        hist.append({"role": "user", "content": q})
        with st.spinner("Checking timetables..."):
            ans = svc.ask(uid, q, hist[:-1])
        hist.append({"role": "assistant", "content": ans["text"] or "I couldn't answer that.", "actions": ans.get("actions", [])})
        st.rerun()
    if hist:
        st.button("Clear the chat", type="tertiary", on_click=lambda: st.session_state.update({hk: []}))


# ====================================================================== Settings
def page_settings():
    st.title("Settings")
    views = ["Preferences", "Notifications", "Mail and contacts", "Mail categories", "Demo and system"]
    view = st.segmented_control("Section", views, default="Preferences", key="settings_view",
                                label_visibility="collapsed") or "Preferences"
    pr = svc.prefs(uid)

    if view == "Preferences":
        st.caption("The agent never offers or accepts a time that breaks these. Meetings already booked stay.")
        with st.columns([3, 2])[0], st.form("prefs_form"):
            a, b = st.columns(2)
            use_e = a.checkbox("No meetings before", value=bool(pr["earliest"]))
            e_t = a.time_input("Earliest", datetime.strptime(pr["earliest"] or "09:00", "%H:%M").time(),
                               step=timedelta(minutes=30), label_visibility="collapsed")
            use_l = b.checkbox("No meetings after", value=bool(pr["latest"]))
            l_t = b.time_input("Latest", datetime.strptime(pr["latest"] or "17:00", "%H:%M").time(),
                               step=timedelta(minutes=30), label_visibility="collapsed")
            c1, c2 = st.columns(2)
            maxd = c1.number_input("Most meetings in a day (0 for no limit)", 0, 10, int(pr["max_per_day"] or 0))
            gap = c2.number_input("Gap between meetings, minutes", 0, 60, int(pr["gap_min"] or 0), step=5)
            nod = st.multiselect("No meetings on", list(range(6)), default=pr["no_meeting_days"] or [],
                                 format_func=lambda i: WEEKDAY_FULL[i])
            if st.form_submit_button("Save", type="primary"):
                svc.set_prefs(uid, earliest=e_t.strftime("%H:%M") if use_e else None,
                              latest=l_t.strftime("%H:%M") if use_l else None, max_per_day=int(maxd) or None,
                              gap_min=int(gap), no_meeting_days=nod)
                flash("Preferences saved.")
                st.rerun()

    elif view == "Notifications":
        with st.form("tg_form"):
            st.markdown("**Telegram**")
            st.caption("Pings for bookings, times to pick, reminders and moved meetings. " + svc.notifier.status().capitalize() + ".")
            a, b = st.columns([2, 1])
            chat = a.text_input("Telegram chat id", pr["telegram_chat_id"] or "",
                                help="Message the CampusSync bot once, then find the id on the bot's getUpdates page")
            b.write("")
            tg = b.checkbox("Send me pings", value=bool(pr["notify_telegram"]))
            if st.form_submit_button("Save"):
                svc.set_prefs(uid, telegram_chat_id=chat.strip(), notify_telegram=tg)
                flash("Saved.")
                st.rerun()
        if pr["telegram_chat_id"] and st.button("Send a test ping", type="tertiary"):
            e = svc.notifier.push(uid, "CampusSync test: notifications are working.", force=True)
            flash(f"Test ping: {e['status'] if e else 'not sent'}", "info")
            st.rerun()
        st.markdown("**Reminders coming up**")
        rs = svc.reminders.upcoming(uid)
        if not rs:
            empty("None scheduled.")
        for r in rs[:10]:
            m = svc.store.get_meeting(r.meeting_id)
            if m:
                st.markdown(f"<span class='muted'>{fmt_dt(r.send_at)}</span> {html.escape(m.title)}, {fmt_range(m.start, m.end)}",
                            unsafe_allow_html=True)
        if st.button("Send reminders that are due now", type="tertiary"):
            flash(f"Sent {len(svc.dispatch_reminders())} reminder(s).")
            st.rerun()
        with st.expander("Alerts and history"):
            for x in svc.store.alerts(uid)[:20]:
                st.markdown(f"<span class='muted'>{fmt_dt(x.created_at)}</span> {html.escape(x.message)}", unsafe_allow_html=True)
            log = svc.notifier.log_for(uid)
            if log:
                st.markdown("**Telegram pings**")
                for n in log[:10]:
                    st.markdown(f"<span class='muted'>{n['at'][:16].replace('T', ' ')}</span> {html.escape(n['text'])} "
                                + tag(n["status"]), unsafe_allow_html=True)
            sent = svc.mail.sent(uid)[:15]
            if sent:
                st.markdown("**Sent by you or your agent**")
                for m in sent:
                    st.markdown(f"<span class='muted'>{fmt_dt(m.sent_at)}</span> to {html.escape(', '.join(name(x) for x in m.to_ids[:3]))}: "
                                f"{html.escape(m.subject)}", unsafe_allow_html=True)

    elif view == "Mail and contacts":
        if not svc.gmail:
            empty("This setup uses the demo mailbox only.")
        elif not svc.live_mail:
            st.write("Gmail isn't connected. Mail stays inside the app.")
            st.button("Connect Gmail", type="primary", on_click=lambda: svc.store.set_setting("mail_mode", None))
        else:
            st.write(f"Connected as **{svc.directory.owner_email}**. Your VTOP profile is "
                     f"{svc.base_portal.get_person(svc.owner_id).name}; replies are signed {svc.directory.owner.get('name')}.")
            prf = svc.prefs(svc.owner_id)
            cp = st.toggle("Email my reminders and daily summary to my Gmail", value=bool(prf.get("copy_to_gmail")))
            if cp != bool(prf.get("copy_to_gmail")):
                svc.set_prefs(svc.owner_id, copy_to_gmail=cp)
                st.rerun()
            row = st.container(horizontal=True)
            row.button("Change profile or name", on_click=svc.directory.clear_owner)
            if row.button("Disconnect Gmail"):
                svc.gmail.disconnect()
                svc.directory.clear_owner()
                st.rerun()
            st.markdown("**People who mailed you**")
            st.caption("Link someone to a VTOP profile so the agent checks their timetable when they mail.")
            vt = [p for p in svc.base_portal.list_people() if p.role in ("faculty", "student", "parent") and p.id != svc.owner_id]
            opts = ["(not in VTOP)"] + [p.id for p in vt]

            def _fmt(i):
                return i if i.startswith("(") else f"{svc.base_portal.get_person(i).name} ({svc.base_portal.get_person(i).role})"
            cts = svc.directory.contacts()
            if not cts:
                empty("Nobody yet.")
            for c in cts:
                a, b = st.columns([2, 3])
                a.markdown(f"{html.escape(c.get('name') or '')}  \n<span class='muted'>{html.escape(c['email'])}</span>",
                           unsafe_allow_html=True)
                cur = c.get("pid") or "(not in VTOP)"
                sel = b.selectbox("Profile", opts, index=opts.index(cur) if cur in opts else 0, key=f"ct_{c['email']}",
                                  format_func=_fmt, label_visibility="collapsed")
                if sel != cur:
                    svc.link_contact(c["email"], None if sel.startswith("(") else sel)
                    st.rerun()
            with st.expander("Link another address"):
                with st.form("add_contact", clear_on_submit=True):
                    a, b = st.columns([2, 3])
                    em = a.text_input("Email address", placeholder="teammate@gmail.com")
                    prf_ = b.selectbox("VTOP profile", [p.id for p in vt], format_func=_fmt)
                    if st.form_submit_button("Link") and "@" in em:
                        svc.directory.resolve(em)
                        svc.link_contact(em, prf_)
                        flash(f"{em} linked.")
                        st.rerun()

    elif view == "Mail categories":
        st.caption(("DeepSeek sorts each mail once. The built-in categories are fixed because the app acts on them; it "
                    f"adds others when mail fits none of them (up to {svc.triage.max_custom})." if svc.triage.llm_on else
                    "Without a DeepSeek key, mail is sorted by built-in rules into the fixed categories.")
                   + " Promotions are hidden from the app, and Gmail's Promotions and Social tabs are never pulled in. "
                   "Only mail written to you personally can need a reply; group mail and invitations never do.")
        counts = svc.triage.category_counts()
        customs = svc.triage.custom_categories()
        rows = [{"Category": lab + (" (hidden)" if k == "promotion" else ""), "Added by": "built in", "Mail": counts.get(k, 0)}
                for k, (lab, _d) in CORE.items()]
        rows += [{"Category": c, "Added by": "DeepSeek", "Mail": counts.get(c, 0)} for c in customs]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if customs:
            with st.form("merge_cat"):
                a, b = st.columns(2)
                src = a.selectbox("Merge", customs)
                dst = b.selectbox("into", list(CORE) + customs, format_func=svc.triage.label)
                if st.form_submit_button("Merge") and src != dst:
                    n_ = svc.triage.merge_category(src, dst)
                    flash(f"Merged {src} into {svc.triage.label(dst)} ({n_} mail).")
                    st.rerun()

    else:
        page_demo_system()


def page_demo_system():
    st.markdown("**Demo controls**")
    a, b = st.columns(2)
    with a:
        sim = st.toggle("Set the clock", value=bool(svc.store.get_setting("clock")),
                        help="Pretend it's another time, to test 'today' or make reminders due")
        if sim:
            cur = svc.now()
            d = st.date_input("Date", cur.date(), key="sim_d", format="DD/MM/YYYY")
            t = st.time_input("Time", cur.time(), step=timedelta(minutes=10), key="sim_t")
            if datetime.combine(d, t) != cur:
                svc.set_clock(datetime.combine(d, t))
        elif svc.store.get_setting("clock"):
            svc.set_clock(None)
        st.caption(f"Agent clock: {fmt_dt(svc.now())}")
    with b:
        auto = st.toggle("Auto mode", value=svc.auto_mode,
                         help="Requests are answered and booked without review" + (
                             ". With Gmail connected, replies go out from your address." if svc.live_mail else ""))
        if auto != svc.auto_mode:
            svc.set_auto_mode(auto)
            st.rerun()
        gen_for = getattr(svc.portal, "data", {}).get("generated_for")
        if st.button("Reset demo data to today"):
            info = svc.reset_demo(svc.now().date())
            svc.sync_gmail(force=True)
            for k in list(st.session_state.keys()):
                if k not in ("svc", "nav", "acting_as"):
                    del st.session_state[k]
            flash(f"Demo data reset for {info.get('anchor')}.")
            st.rerun()
        if gen_for and gen_for != svc.now().date().isoformat():
            st.caption(f"Demo data is from {gen_for}.")
    st.divider()
    st.markdown("**DeepSeek**")
    a, b = st.columns([2, 1])
    key = a.text_input("API key for this session", type="password", placeholder="Uses DEEPSEEK_API_KEY from .env if empty")
    if key and key != svc.llm.api_key:
        svc.llm.set_key(key)
    b.write("")
    b.caption(svc.llm.status())
    st.divider()
    st.markdown("**How this copy is connected**")
    kb = svc.knowledge()
    st.dataframe(pd.DataFrame([
        {"Part": "College portal", "Using": svc.portal.describe()},
        {"Part": "Mail", "Using": svc.mail.describe()},
        {"Part": "Calendar", "Using": svc.calendar.describe()},
        {"Part": "Language model", "Using": svc.llm.status()},
        {"Part": "Search (RAG)", "Using": f"{kb.active_backend}, {len(kb.docs)} documents"},
        {"Part": "Settings file", "Using": svc.settings.path.name},
    ]), hide_index=True, width="stretch")
    with st.expander("Model accuracy"):
        im = svc.intent_model
        if im is not None:
            if im.pipe is None:
                im.ensure()
            st.caption(f"Intent network: {im.info.get('layers', '')}, trained on {im.info.get('samples')} generated mails.")
        if st.button("Run the evaluation sets"):
            from campussync.llm.client import LLMClient
            from campussync.llm.parser import MailParser
            from campussync.llm.rules import parse_request
            from campussync.ml.evaluate import evaluate, evaluate_retrieval, load_rows
            from campussync.rag.store import KnowledgeBase
            root = svc.settings.root / "data" / "eval"
            off = LLMClient(svc.settings)
            off.mode = "off"
            parsers = {"Rules": parse_request, "Rules + network": MailParser(off, svc.intent_model).parse}
            if svc.llm.enabled:
                parsers["DeepSeek"] = svc.parser.parse
            rows = []
            for label, fname in (("Tuned set", "mails.jsonl"), ("Holdout", "mails_holdout.jsonl")):
                data = load_rows(root / fname)
                for pname, fn in parsers.items():
                    r = evaluate(fn, data)
                    rows.append({"Set": f"{label} ({len(data)})", "Reader": pname, "Right action": f"{r['action']}%",
                                 "All fields right": f"{r['exact']}%"})
            st.markdown("Mail understanding")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            docs = svc._build_docs()
            ppl = {p.id: p for p in svc.people()}
            qs = load_rows(root / "retrieval.jsonl")
            rr = []
            for bk in ("tfidf", "dense", "hybrid"):
                kb_ = KnowledgeBase(svc.settings, bk)
                kb_.build(docs)
                r = evaluate_retrieval(kb_, ppl, qs)
                rr.append({"Search": kb_.active_backend, "Right doc first": f"{r['recall@1']}%", "In top 3": f"{r['recall@3']}%"})
            st.markdown(f"Search ({len(qs)} questions)")
            st.dataframe(pd.DataFrame(rr), hide_index=True, width="stretch")
    with st.expander("Rules in use"):
        st.json({"scheduling": svc.settings.get("scheduling"), "attendance": svc.settings.get("attendance"),
                 "reminders": svc.settings.get("reminders")})


# ====================================================================== route
{
    "Home": page_home,
    "Inbox": page_inbox,
    "Calendar": page_calendar,
    "Students": page_students,
    "Leave": page_leave,
    "Cabin hours": page_cabin,
    "Assistant": page_assistant,
    "Settings": page_settings,
}[page]()
