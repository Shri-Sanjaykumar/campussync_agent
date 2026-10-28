"""Mail sorting and the daily summary.

With DeepSeek: each mail is sorted into a category, with a one-line summary, whether it needs a reply,
and any deadline. A few core categories are always there because the app acts on them (Meeting,
Leave request, Deadline, Question, Announcement, Update, Promotion). Beyond those there is no fixed list:
the model reuses categories it already made, and creates a new short one only when nothing fits
("Placement", "Event Invite", "Internship"...). Near-duplicates are folded into the existing category,
and the number of custom categories is capped so the list stays short.

The model's answer is then checked by a few hard rules, so the inbox stays calm:
- Mail to a group or mailing list never "needs a reply", and is never treated as a meeting request.
- "Deadline" is only for something the reader must do by a date (coursework, fees, a library book).
  Internships, hackathons, events and other optional opportunities are not deadlines.
- Promotions are hidden from the app; mail from the college is never a promotion.

Results are saved per mail (with a version, so a change in these rules re-sorts old mail), and each
mail is sent to DeepSeek once. Without DeepSeek (or if a call fails) the rule-based sorter is used."""
from __future__ import annotations

import difflib
import json
import re
from datetime import date, datetime, time, timedelta

from ..llm.rules import _strip_greeting, clean_subject, parse_date, parse_request, parse_times
from ..models import Mail
from ..utils import WEEKDAY_FULL, fmt_date, fmt_dt, fmt_range

VERSION = 2  # bump when the sorting rules change: saved results from older versions are sorted again

DEADLINE_RE = re.compile(r"\b(deadline|due (on|by|date)|is due|overdue|submit(ted)?|submission|last date|on or before|"
                         r"upload(ed)? by|return (it |the book )?by|pay (it )?(by|before))\b", re.I)
NOREPLY_RE = re.compile(r"(no-?reply|do-?not-?reply|notifications?@|alerts?@|mailer-daemon)", re.I)
MEETING_WORDS = re.compile(r"\b(meet|meeting|appointment|cabin|see you)\b", re.I)
ANNOUNCE_RE = re.compile(r"\b(circular|notice|announcement|all (the )?students|all faculty|kindly note|please note|hereby|"
                         r"(is|are) (hereby )?informed|will be (published|held|conducted)|has been (postponed|rescheduled|"
                         r"cancelled|extended))\b", re.I)
DATE_CLUE = re.compile(r"\d|\b(today|tonight|tomorrow|tmrw|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                       r"mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
                       r"week|month|eod|end of (the )?day|midnight|noon)\b", re.I)
# Signs a mail went to a crowd rather than to the reader.
BULK_RE = re.compile(r"\b(dear (all|students|vitians|faculty( members)?|colleagues|participants|members|everyone)|"
                     r"all are (invited|welcome|requested)|hello everyone|hey everyone|to all (the )?students)\b", re.I)
# Optional opportunities: a "last date" here is not the reader's deadline.
OPPORTUNITY_RE = re.compile(r"\b(internships?|placements?|campus drive|recruit(ment|ing)?|hiring|job openings?|"
                            r"hackathons?|competitions?|contests?|workshops?|webinars?|seminars?|fests?|events?|"
                            r"scholarships?|nptel|mooc|certification course|registrations?|register|invit(ation|ed|e)|"
                            r"volunteers?|apply now|opportunit(y|ies))\b", re.I)
# ...unless the mail is about something the reader owes anyway.
OWED_RE = re.compile(r"\b(internship (report|certificate|diary)|(hostel|tuition|semester|exam|mess) fees?|fee payment|"
                     r"assignment|digital assignment|da ?-? ?\d|lab record|review \d|project report)\b", re.I)
PROMO_RE = re.compile(r"(\d+ ?% off|\b(discount|coupon|promo ?code|cashback|offer ends|limited[- ]time|flash sale|"
                      r"shop now|order now|buy now|free trial|upgrade now|newsletter|unsubscribe|deals? of the|"
                      r"new arrivals|best price|match your profile|recommended for you)\b)", re.I)
VIA_RE = re.compile(r"\svia\s", re.I)
HIDDEN = {"promotion"}

# key -> (label, what belongs in it). Keys stay fixed because the app acts on them.
CORE = {
    "meeting": ("Meeting", "a person wants to meet the reader, asks when the reader is free, reschedules or cancels "
                           "a meeting with the reader, or picks one of the offered times (not a class-wide session or event)"),
    "leave": ("Leave request", "a student or a parent asks for leave or says the student will be absent"),
    "deadline": ("Deadline", "the reader must submit, finish, return or pay something required of them by a date: "
                             "an assignment, a project or review submission, a lab record, a fee, a library book, a form "
                             "they have to fill. NOT optional opportunities (internships, jobs, placement drives, "
                             "hackathons, competitions, events, workshops, courses, scholarships) even with a last date"),
    "question": ("Question", "a person asks the reader something and waits for the reader's answer (not a meeting)"),
    "announcement": ("Announcement", "information for a group: circulars, notices, schedule changes, forwarded mails, "
                                     "events and opportunities when no custom category fits better"),
    "notification": ("Update", "automatic notifications, confirmations, receipts, security alerts, system messages"),
    "promotion": ("Promotion", "marketing from companies or services: offers, discounts, newsletters, product news, "
                               "job-portal or course-platform suggestions, anything selling or asking for sign-ups. "
                               "Mail from the reader's own college, teachers or classmates is never a Promotion"),
    "other": ("Other", "anything that fits nothing else"),
}
CATEGORY_LABEL = {k: v[0] for k, v in CORE.items()}
_LABEL_TO_KEY = {v[0].lower(): k for k, v in CORE.items()}
_SYNONYMS = {"meeting request": "meeting", "meetings": "meeting", "appointment": "meeting", "scheduling": "meeting",
             "leave": "leave", "leave application": "leave", "absence": "leave", "deadlines": "deadline",
             "submission": "deadline", "questions": "question", "query": "question", "announcements": "announcement",
             "circular": "announcement", "notice": "announcement", "updates": "notification", "notification": "notification",
             "notifications": "notification", "misc": "other", "miscellaneous": "other", "general": "other",
             "promotions": "promotion", "marketing": "promotion", "advertisement": "promotion", "ads": "promotion",
             "newsletter": "promotion", "newsletters": "promotion", "spam": "promotion", "offers": "promotion"}


def is_bulk(m: Mail) -> bool:
    """Was this mail sent to a crowd (a mailing list, a Google group, many people, "Dear all")?"""
    meta = m.meta or {}
    if meta.get("bulk") or meta.get("via") or VIA_RE.search(meta.get("from_name", "")):
        return True
    if int(meta.get("n_recipients", len(m.to_ids))) > 6:
        return True
    return bool(BULK_RE.search(f"{m.subject}\n{(m.body or '')[:600]}"))


def _summary(body: str) -> str:
    text = _strip_greeting(body)
    text = re.sub(r"^\s*(dear|hi|hello|respected)\b[^,\n]*[,\n]", "", text.strip(), flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    first = re.split(r"(?<=[.!?])\s", text)[0] if text else ""
    return (first[:117] + "...") if len(first) > 120 else first


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).strip()
    n = re.sub(r"\s+", " ", n)
    return re.sub(r"(ies)$", "y", re.sub(r"(?<!s)s$", "", n))


def _tidy(name: str) -> str:
    name = re.sub(r"[_/|]+", " ", str(name or "")).strip(" .:-")
    name = re.sub(r"\s+", " ", name)[:30]
    return " ".join(w if w.isupper() and len(w) <= 4 else w.capitalize() for w in name.split())


class Triage:
    def __init__(self, store, mail, portal, llm, clock, settings=None):
        self.store, self.mail, self.portal, self.llm, self.clock = store, mail, portal, llm, clock
        g = (lambda k, d: settings.get(k, d)) if settings else (lambda k, d: d)
        self.max_custom = int(g("triage.max_custom_categories", 12))
        self.batch = int(g("triage.batch_size", 8))
        self.use_llm = str(g("triage.use_llm", "auto")).lower() != "off"
        self.college_domains = tuple(str(d).lower().lstrip("@") for d in (g("institution.mail_domains", []) or []))
        self._cache: dict[str, dict] = {}  # rule results (not saved, so DeepSeek can redo them later)
        self.last_error: str | None = None

    # ================================================================ categories
    @property
    def _reg(self) -> dict:
        return self.store.data["settings"].setdefault("mail_categories", {"custom": {}, "merged": {}})

    def custom_categories(self) -> list[str]:
        return list(self._reg["custom"].keys())

    def label(self, key: str) -> str:
        return CATEGORY_LABEL.get(key, key)

    @staticmethod
    def hidden(t: dict) -> bool:
        """Promotions never show up in the app."""
        return t.get("category") in HIDDEN

    def _resolve_name(self, raw: str, allow_new: bool = True) -> str:
        """Model output -> core key or custom category name (folding near-duplicates)."""
        name = _tidy(raw)
        if not name:
            return "other"
        low = name.lower()
        if low in _LABEL_TO_KEY:
            return _LABEL_TO_KEY[low]
        if low in CORE:
            return low
        if low in _SYNONYMS:
            return _SYNONYMS[low]
        merged = {k.lower(): v for k, v in self._reg["merged"].items()}
        if low in merged:
            return merged[low]
        n = _norm(name)
        for key, (lab, _d) in CORE.items():
            if _norm(lab) == n:
                return key
        best, best_r = None, 0.0
        for c in self.custom_categories():
            cn = _norm(c)
            if cn == n:
                return c
            r = difflib.SequenceMatcher(None, cn, n).ratio()
            if (len(cn) >= 5 and len(n) >= 5 and (n.startswith(cn) or cn.startswith(n))):
                r = max(r, 0.9)
            if r > best_r:
                best, best_r = c, r
        if best and best_r >= 0.82:
            return best
        if not allow_new or len(self.custom_categories()) >= self.max_custom:
            return "other"
        self._reg["custom"][name] = {"created": self.clock().isoformat(timespec="minutes")}
        return name

    def merge_category(self, name: str, into: str) -> int:
        """Fold a custom category into another one (core key or custom name). Returns mails moved."""
        if name not in self._reg["custom"] or name == into:
            return 0
        self._reg["custom"].pop(name)
        self._reg["merged"][name] = into
        n = 0
        for mid, t in self.store.data["triage"].items():
            if t.get("category") == name:
                t["category"] = into
                n += 1
        self.store.save()
        return n

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.store.data["triage"].values():
            if t.get("v") == VERSION:
                counts[t["category"]] = counts.get(t["category"], 0) + 1
        return counts

    # ================================================================ who sent it
    def _sender(self, m: Mail):
        return self.portal.get_person(m.from_id) if self.portal else None

    def from_college(self, m: Mail) -> bool:
        """Sent by someone in the college: a VTOP profile, or an address on the college's mail domains."""
        p = self._sender(m)
        if p is not None and getattr(p, "role", "external") != "external":
            return True
        addr = (m.meta or {}).get("from_email", "")
        return bool(addr) and any(addr.endswith("@" + d) or addr.endswith("." + d) for d in self.college_domains)

    # ================================================================ classify
    def _system_result(self, m: Mail) -> dict | None:
        if {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"} & set((m.meta or {}).get("labels", [])):
            return {"category": "promotion", "summary": _summary(m.body), "deadline": None, "action": False,
                    "source": "gmail", "priority": "low"}  # Gmail's own Promotions / Social tab
        if m.kind in ("reminder", "notification") or m.from_id == "agent":
            return {"category": "notification", "summary": _summary(m.body), "deadline": None, "action": False,
                    "source": "rules", "priority": "low"}
        if m.kind == "agent":
            cat = "meeting" if (m.meta.get("options") or m.meta.get("meeting_id")) else (
                "leave" if m.meta.get("leave_id") else "notification")
            return {"category": cat, "summary": _summary(m.body), "deadline": None, "action": bool(m.meta.get("options")),
                    "source": "rules", "priority": "normal"}
        return None

    def classify(self, m: Mail) -> dict:
        saved = self._load(m.id)
        if saved:
            return saved
        sysres = self._system_result(m)
        if sysres:
            return sysres
        if self.llm_on:
            self.classify_many([m])
            saved = self._load(m.id)
            if saved:
                return saved
        return self.peek(m)

    def peek(self, m: Mail) -> dict:
        """The current sorting without calling DeepSeek (saved result, else the rules)."""
        saved = self._load(m.id)
        if saved:
            return saved
        sysres = self._system_result(m)
        if sysres:
            return sysres
        if m.id not in self._cache:
            r = self._rules(m)
            r["deadline"] = datetime.fromisoformat(r["deadline"]) if r["deadline"] else None
            self._cache[m.id] = r
        return self._cache[m.id]

    @property
    def llm_on(self) -> bool:
        return self.use_llm and self.llm is not None and self.llm.enabled

    def pending(self, mails: list[Mail]) -> list[Mail]:
        return [m for m in mails if not self._load(m.id) and not self._system_result(m)]

    def classify_many(self, mails: list[Mail]) -> int:
        """Sort several mails with as few DeepSeek calls as possible. Returns how many were sorted."""
        todo = self.pending(mails)
        if not todo or not self.llm_on:
            return 0
        done = 0
        for i in range(0, len(todo), self.batch):
            chunk = todo[i:i + self.batch]
            try:
                results = self._ask_llm(chunk)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                break
            for m in chunk:
                r = results.get(m.id)
                if r:
                    self.store.data["triage"][m.id] = self._validate(m, r)
                    self._cache.pop(m.id, None)
                    self._sync_deadlines(m)
                    done += 1
            self.store.save()
        if done:
            self.last_error = None
        return done

    def _load(self, mid: str) -> dict | None:
        t = self.store.data["triage"].get(mid)
        if not t or t.get("v") != VERSION:
            return None  # sorted under older rules: sort again
        out = dict(t)
        out["deadline"] = datetime.fromisoformat(t["deadline"]) if t.get("deadline") else None
        return out

    # ---------------------------------------------------------------- the hard rules on top of any sorter
    def _finish(self, m: Mail, cat: str, needs_reply: bool, due: datetime | None, summary: str, pri: str,
                source: str) -> dict:
        text = f"{m.subject}\n{m.body}"
        bulk = is_bulk(m)
        college = self.from_college(m)
        if cat == "promotion" and college:
            cat = "announcement"  # the college's own mail is never hidden
        if bulk and cat in ("meeting", "question"):
            cat = "announcement"  # "Dear all, meeting at 3 pm" is a notice, not a request to the reader
        if cat == "deadline" and OPPORTUNITY_RE.search(text) and not OWED_RE.search(text):
            cat = "announcement"  # an internship's last date is not the reader's deadline
        if cat != "deadline" and not (cat not in CORE and OWED_RE.search(text)):
            due = None  # only things the reader owes keep a date (a custom "Fee Payment" category still does)
        if bulk or cat in ("announcement", "notification", "promotion", "other"):
            action = False
        elif cat in ("meeting", "leave"):
            action = True
        else:  # question, deadline, custom categories: only when written to the reader and expecting an answer
            action = bool(needs_reply)
        if cat == "promotion":
            pri = "low"
        return {"category": cat, "summary": summary, "deadline": due.isoformat(timespec="minutes") if due else None,
                "action": action, "priority": pri, "source": source, "bulk": bulk, "v": VERSION}

    # ---------------------------------------------------------------- DeepSeek
    def _prompt(self) -> str:
        core = "\n".join(f'- "{lab}": {desc}' for k, (lab, desc) in CORE.items())
        custom = ", ".join(f'"{c}"' for c in self.custom_categories()) or "(none yet)"
        room = self.max_custom - len(self.custom_categories())
        now = self.clock()
        return (
            "You sort emails in the inbox of a university student or faculty member (the reader). For each email decide:\n"
            "1. category. Use one of these core categories when it fits:\n" + core + "\n"
            f"   Custom categories created earlier: {custom}. Reuse one when it fits.\n"
            + (f"   If nothing above fits well, create ONE new category: 1-3 words, Title Case, general enough to reuse "
               f"for similar mail (e.g. \"Event Invite\", \"Internship\", \"Placement\", \"Course Registration\"). "
               f"Never make a near-duplicate of an existing one. {room} new categories can still be created.\n"
               if room > 0 else "   No new categories can be created: pick the closest existing one or \"Other\".\n")
            + "2. summary: one short plain sentence saying what the mail is about, max 20 words.\n"
            "3. needs_reply: true ONLY when a person wrote to the reader personally and expects the reader to write back. "
            "False for mail sent to a group, list or many people (see the \"sent_to\" field), invitations, notices, "
            "forwards, newsletters, offers, receipts, and anything where the action is to register, apply, attend, pay "
            "or just read.\n"
            "4. deadline: \"YYYY-MM-DDTHH:MM\" only for the Deadline category, when the mail states the date; use 23:59 "
            "when only a date is given. Otherwise null. Never invent a date. A last date to apply for an internship, job, "
            "event or competition is NOT a deadline.\n"
            "5. priority: high, normal or low.\n"
            f"Today is {WEEKDAY_FULL[now.weekday()]} {now:%Y-%m-%d}.\n"
            'Return only JSON: {"results": [{"id": "...", "category": "...", "summary": "...", "needs_reply": false, '
            '"deadline": null, "priority": "normal"}]}')

    def _ask_llm(self, mails: list[Mail]) -> dict[str, dict]:
        items = []
        for m in mails:
            p = self._sender(m)
            meta = m.meta or {}
            sender = f"{p.name} ({p.role})" if p and p.role != "external" else meta.get("from_name") or (p.name if p else m.from_id)
            if meta.get("via"):
                sender += f" (posted to the group {meta['via']})"
            n = int(meta.get("n_recipients", len(m.to_ids)))
            sent_to = ("a mailing list or group" if (meta.get("bulk") or meta.get("via")) else
                       f"{n} people" if n > 6 else "the reader" + (f" and {n - 1} others" if n > 1 else ""))
            body = re.sub(r"\s+\n", "\n", m.body or "").strip()[:1500]
            items.append({"id": m.id, "from": sender, "from_college": self.from_college(m), "sent_to": sent_to,
                          "sent": m.sent_at.strftime("%Y-%m-%d %H:%M"), "subject": m.subject, "body": body})
        out = self.llm.json(self._prompt(), json.dumps({"emails": items}, ensure_ascii=False))
        rows = out.get("results", out if isinstance(out, list) else [])
        return {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id")}

    def _validate(self, m: Mail, r: dict) -> dict:
        cat = self._resolve_name(r.get("category", ""))
        text = f"{m.subject}\n{m.body}"
        due = None
        if r.get("deadline") and DATE_CLUE.search(text):
            try:
                due = datetime.fromisoformat(str(r["deadline"])[:16])
                if due < m.sent_at - timedelta(days=1) or due > m.sent_at + timedelta(days=400):
                    due = None  # a date before the mail was sent, or absurdly far out, is a model slip
            except ValueError:
                due = None
        summary = str(r.get("summary") or "").strip()[:160] or _summary(m.body)
        pri = r.get("priority") if r.get("priority") in ("high", "normal", "low") else "normal"
        return self._finish(m, cat, bool(r.get("needs_reply")), due, summary, pri, "deepseek")

    # ---------------------------------------------------------------- rules (fallback)
    def _rules(self, m: Mail) -> dict:
        text = f"{m.subject}\n{m.body}"
        meta = m.meta or {}
        cat, reply, due = "other", False, None
        if NOREPLY_RE.search(meta.get("from_email", "")):
            cat = "notification"
        elif not self.from_college(m) and (PROMO_RE.search(text) or "CATEGORY_PROMOTIONS" in meta.get("labels", [])):
            cat = "promotion"
        else:
            p = parse_request(m.subject, m.body, m.sent_at)
            when = self._deadline(text, m.sent_at)
            if p.intent == "leave_request":
                cat = "leave"
            elif OPPORTUNITY_RE.search(text) and not OWED_RE.search(text):
                cat = "announcement"
            elif when and DEADLINE_RE.search(text) and not MEETING_WORDS.search(text):
                cat, due = "deadline", when
            elif ANNOUNCE_RE.search(text) or is_bulk(m):
                cat = "announcement"
            elif p.intent in ("meeting_request", "reschedule", "cancel", "availability_query", "option_choice"):
                cat = "meeting"
            elif "?" in m.body:
                cat = "question"
            reply = "?" in (m.body or "")
        return self._finish(m, cat, reply, due, _summary(m.body), "normal", "rules")

    @staticmethod
    def _deadline(text: str, ref: datetime) -> datetime | None:
        s, e, _part, rest = parse_times(text)
        d = parse_date(rest, ref.date())
        if not d:
            return None
        if re.search(r"\b(11:59|midnight|end of (the )?day)\b", text, re.I):
            return datetime.combine(d, time(23, 59))
        return datetime.combine(d, s or time(23, 59))

    # ---------------------------------------------------------------- deadlines
    def capture_deadlines(self, m: Mail) -> int:
        t = self.classify(m)
        if not t["deadline"]:
            return 0
        n = 0
        for pid in m.to_ids:
            if any(d["person_id"] == pid and d["mail_id"] == m.id for d in self.store.data["deadlines"]):
                continue
            self.store.data["deadlines"].append({
                "id": self.store.new_id("D"), "person_id": pid, "title": clean_subject(m.subject) or t["summary"][:60],
                "due": t["deadline"].isoformat(timespec="minutes"), "mail_id": m.id, "from_id": m.from_id, "done": False})
            n += 1
        if n:
            self.store.save()
        return n

    def _sync_deadlines(self, m: Mail) -> None:
        """After a mail is sorted again: drop deadlines it no longer has, add the ones it now has."""
        if not self.peek(m)["deadline"]:
            before = len(self.store.data["deadlines"])
            self.store.data["deadlines"] = [d for d in self.store.data["deadlines"] if d.get("mail_id") != m.id or d["done"]]
            if len(self.store.data["deadlines"]) != before:
                self.store.save()
        elif self.mail is not None:
            self.capture_deadlines(m)

    def _still_deadline(self, d: dict) -> bool:
        if not d.get("mail_id") or self.mail is None:
            return True  # added by hand
        m = self.mail.get(d["mail_id"])
        return m is None or self.peek(m)["deadline"] is not None

    def deadlines(self, pid: str, include_done: bool = False) -> list[dict]:
        return sorted([d for d in self.store.data["deadlines"] if d["person_id"] == pid and (include_done or not d["done"])
                       and self._still_deadline(d)], key=lambda d: d["due"])

    def set_done(self, did: str, done: bool = True) -> None:
        for d in self.store.data["deadlines"]:
            if d["id"] == did:
                d["done"] = done
        self.store.save()

    def add_deadline(self, pid: str, title: str, due: datetime) -> dict:
        d = {"id": self.store.new_id("D"), "person_id": pid, "title": title, "due": due.isoformat(timespec="minutes"),
             "mail_id": None, "from_id": pid, "done": False}
        self.store.data["deadlines"].append(d)
        self.store.save()
        return d
