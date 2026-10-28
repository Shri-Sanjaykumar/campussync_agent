"""Rule-based understanding of mails and questions.
Used when DeepSeek is not configured, and as a safety net when the model's answer
can't be validated. Handles the common ways students and faculty write."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "oct": 10, "nov": 11, "dec": 12}
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
WD_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)"
WD_IDX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
PARTS = {
    "early morning": (time(8, 0), time(10, 0)),
    "morning": (time(8, 0), time(12, 30)),
    "before lunch": (time(8, 0), time(13, 0)),
    "noon": (time(11, 30), time(14, 0)),
    "after lunch": (time(14, 0), time(17, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(16, 0), time(19, 0)),
    "end of the day": (time(16, 30), time(19, 0)),
}


@dataclass
class ParsedRequest:
    intent: str = "other"  # meeting_request | reschedule | cancel | availability_query | option_choice | leave_request | other
    date: date | None = None
    start: time | None = None
    end: time | None = None
    part_of_day: str | None = None
    duration_min: int | None = None
    urgent: bool = False
    topic: str = ""
    option_choice: int | None = None
    source: str = "rules"
    notes: list[str] = field(default_factory=list)
    model_intent: str | None = None
    model_conf: float | None = None
    range_start: date | None = None  # "this week" / "next week" when no single day is given
    range_end: date | None = None
    end_date: date | None = None  # last day of a leave request
    reason: str = ""

    @property
    def window(self) -> tuple[time, time] | None:
        if self.start and self.end:
            return (self.start, self.end)
        if self.part_of_day and self.part_of_day in PARTS:
            return PARTS[self.part_of_day]
        return None


# ------------------------------------------------------------------ pieces
def _hour(h: int, m: int, ampm: str | None) -> time | None:
    if ampm:
        ampm = ampm.lower().replace(".", "")
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
    elif 1 <= h <= 7:
        h += 12  # "at 3" on campus means 3 pm
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return time(h, m)


AMPM = r"(a\.?m\.?|p\.?m\.?)"
RANGE_RE = re.compile(
    rf"(?:between\s+|from\s+)?\b(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{AMPM}?\s*(?:-|–|to|till|until|and)\s*(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{AMPM}",
    re.I)
RANGE24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(?:-|–|to|till|until)\s*([01]?\d|2[0-3]):([0-5]\d)\b")
SINGLE_RE = re.compile(rf"\b(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{AMPM}(?![a-z])", re.I)
CLOCK_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
AT_RE = re.compile(r"\b(?:at|around|by|@)\s+(\d{1,2})(?:\s*o'?\s*clock)?\b(?![:./\d-])"
                   r"(?!\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b)", re.I)  # "by 23 Oct" is a date
BARE_RE = re.compile(rf"\b(?:today|tomorrow|tmrw|tmr|2moro|tomo|{WD_RE})\s+(\d{{1,2}})\b(?!\s*(?:st|nd|rd|th|[:./-]|min|hour|hr|oct|nov|dec))",
                     re.I)


def parse_times(text: str) -> tuple[time | None, time | None, str | None, str]:
    """Returns (start, end, part_of_day, text_with_times_removed)."""
    t = text
    start = end = None
    m = RANGE_RE.search(t)
    if m:
        h1, m1, ap1, h2, m2, ap2 = m.groups()
        ap1 = ap1 or ap2
        e_ = _hour(int(h2), int(m2 or 0), ap2)
        s_ = _hour(int(h1), int(m1 or 0), ap1)
        if s_ and e_ and s_ > e_ and not m.group(3):  # "11 to 1 pm" -> 11 am
            s_ = _hour(int(h1), int(m1 or 0), "am")
        if s_ and e_ and s_ < e_:
            start, end = s_, e_
            t = t[:m.start()] + " " + t[m.end():]
    if not start:
        m = RANGE24_RE.search(t)
        if m:
            start, end = time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4)))
            t = t[:m.start()] + " " + t[m.end():]
    if not start:
        m = SINGLE_RE.search(t)
        if m:
            start = _hour(int(m.group(1)), int(m.group(2) or 0), m.group(3))
        else:
            m = CLOCK_RE.search(t)
            if m:
                h, mm = int(m.group(1)), int(m.group(2))
                start = _hour(h, mm, None) if h <= 7 else time(h, mm)
            else:
                m = AT_RE.search(t)
                if m:
                    start = _hour(int(m.group(1)), 0, None)
                else:
                    m = BARE_RE.search(t)
                    if m and 1 <= int(m.group(m.lastindex)) <= 12:
                        start = _hour(int(m.group(m.lastindex)), 0, None)
                        t = t[:m.start(m.lastindex)] + " " + t[m.end(m.lastindex):]
                        m = None
        if m:
            t = t[:m.start()] + " " + t[m.end():]
    if not start and re.search(r"\bnoon\b", t, re.I) and not re.search(r"after\s*noon", t, re.I):
        start = time(12, 0)
    part = None
    low = re.sub(r"\bgood\s+(morning|afternoon|evening|day)\b", " ", t.lower())
    for key in sorted(PARTS, key=len, reverse=True):
        if re.search(rf"\b{key}\b", low):
            part = key
            break
    return start, end, part, t


def parse_duration(text: str) -> int | None:
    low = text.lower()
    if re.search(r"half\s+(an\s+)?hour", low):
        return 30
    if re.search(r"quarter\s+(of\s+an\s+)?hour", low):
        return 15
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(hours?|hrs?)\b", low)
    if m:
        return int(float(m.group(1)) * 60)
    m = re.search(r"\b(\d{1,3})\s*(?:-\s*)?(minutes?|mins?)\b", low)
    if m:
        return int(m.group(1))
    if re.search(r"\b(an|one)\s+hour\b", low):
        return 60
    return None


def _year_fix(d: date, ref: date) -> date:
    if d < ref - timedelta(days=60):
        try:
            return d.replace(year=d.year + 1)
        except ValueError:
            return d
    return d


def parse_date(text: str, ref: date) -> date | None:
    low = text.lower()
    if re.search(r"day after tomorrow", low):
        return ref + timedelta(days=2)
    if re.search(r"\b(today|tonight|today itself|this (morning|afternoon|evening))\b", low):
        return ref
    if re.search(r"\b(tomorrow|tmrw|tmr|tom|2moro|tomo|tommorow|tomorow|tmro|tomorrows)\b", low):
        return ref + timedelta(days=1)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?{MONTH_RE}\b", low)
    if m:
        try:
            return _year_fix(date(ref.year, MONTHS[m.group(2)[:3]], int(m.group(1))), ref)
        except ValueError:
            pass
    m = re.search(rf"\b{MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", low)
    if m:
        try:
            return _year_fix(date(ref.year, MONTHS[m.group(1)[:3]], int(m.group(2))), ref)
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", low)
    if m:
        dd, mo = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else ref.year
        if yr < 100:
            yr += 2000
        try:
            d = date(yr, mo, dd)
            return d if m.group(3) else _year_fix(d, ref)
        except ValueError:
            pass
    m = re.search(r"\b(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", low)
    if m and 1 <= int(m.group(1)) <= 31:
        dd = int(m.group(1))
        y, mo = ref.year, ref.month
        if dd < ref.day:  # "the 3rd" said on the 15th means next month
            mo += 1
            if mo == 13:
                y, mo = y + 1, 1
        try:
            return date(y, mo, dd)
        except ValueError:
            pass
    m = re.search(rf"\b(?:(next|this|coming)\s+)?{WD_RE}\b", low)
    if m:
        target = WD_IDX[m.group(2)[:3]]
        delta = (target - ref.weekday()) % 7
        if delta == 0 and m.group(1) != "this":
            delta = 7
        d = ref + timedelta(days=delta)
        if m.group(1) == "next" and d.isocalendar()[1] == ref.isocalendar()[1]:
            d += timedelta(days=7)
        return d
    return None


def parse_date_range(text: str, ref: date) -> tuple[date, date] | None:
    low = text.lower()
    week_end = ref + timedelta(days=5 - ref.weekday()) if ref.weekday() <= 5 else ref
    if "next week" in low:
        mon = ref + timedelta(days=7 - ref.weekday())
        return mon, mon + timedelta(days=5)
    if re.search(r"\b(this week|rest of the week|this weekend)\b", low):
        return ref, week_end
    if re.search(r"\b(next few days|coming days|soon)\b", low):
        return ref, ref + timedelta(days=6)
    d = parse_date(text, ref)
    if d:
        return d, d
    return None


INTENT_RULES = [
    ("cancel", r"\b(cancel|call off|won'?t be able to (come|make|attend)|can'?t make it|cannot make it|not able to (come|attend|make it)|can'?t come|cannot come|not coming|unable to (come|attend))\b"),
    ("reschedule", r"(reschedul|postpone|prepone|\bmove (it|our|the|this|tomorrow'?s?|today'?s?)\b|\bshift (it|our|the|this)\b|another (time|day|slot)|change (the )?(time|slot|date)|push (it|our|the)|\binstead\b|clash with (our|the|my))"),
    ("availability_query", r"(when (are|will|would) you (be )?(free|available)|your (free|available) (time|slot)|availability|what time works|free slots?|when can (i|we) (meet|come|see you)|when (is|are) .* free|(convenient|suitable|good) time|when would be)"),
    ("meeting_request", r"(\bmeet\b|\bmeeting\b|discuss|appointment|\bslot\b|see you|catch up|\btalk\b|\breview\b|doubt|your cabin|ur cabin|\bvisit\b|come and see|get some time|\b(are|r) (you|u) (free|available)\b|\bchat\b|\bcall\b|drop by|spare|\bmins?\b.*\?)"),
]
OPTION_RE = re.compile(r"\b(?:option|choice|slot)\s*(?:no\.?\s*)?#?(\d)\b|\b(first|second|third|1st|2nd|3rd)\s+(?:one|option|slot|time)\b", re.I)
ORD = {"first": 1, "second": 2, "third": 3, "1st": 1, "2nd": 2, "3rd": 3}


def detect_option(text: str) -> int | None:
    m = OPTION_RE.search(text)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else ORD[m.group(2).lower()]


LEAVE_RE = re.compile(r"\b((apply(ing)?|request(ing)?|need|want|grant|sanction|approve|permission|take|taking|on)\s+(for\s+)?"
                      r"(a\s+|the\s+)?(medical\s+|sick\s+|casual\s+)?leave|leave\s+(application|letter|request|on|from|for)|"
                      r"(medical|sick|casual)\s+leave|(grant|sanction|approve|permit|give)\s+(him|her|my\s+(son|daughter|ward)|[a-z]+)\s+"
                      r"(a\s+)?leave|(will|would)\s+be\s+absent|be\s+on\s+leave|leave\s+of\s+absence)\b", re.I)
REASON_RE = re.compile(r"\b(?:for|due to|because of|because|regarding|to attend|as i am|as i have|as i need to)\s+(.+?)(?:[.!\n]|$)", re.I)


def absence_kind(text: str) -> str | None:
    if LEAVE_RE.search(text):
        return "leave_request"
    return None


def parse_span(text: str, ref: date) -> tuple[date | None, date | None]:
    """'from 21st to 23rd October', '20-22 Oct', 'today and tomorrow', 'for 3 days from Monday', single day."""
    low = text.lower()
    if re.search(r"\btoday and tomorrow\b", low):
        return ref, ref + timedelta(days=1)
    m = re.search(r"\bfrom\s+(.{2,25}?)\s+(?:to|till|until)\s+(.{2,25}?)(?=[.,;!\n]|\s+(?:due|for|because|as)\b|$)", low)
    if m:
        a, b = parse_date(m.group(1), ref), parse_date(m.group(2), ref)
        if a and not b and re.fullmatch(r"\s*(\d{1,2})(st|nd|rd|th)?\s*", m.group(2)):
            try:
                b = a.replace(day=int(re.match(r"\s*(\d+)", m.group(2)).group(1)))
            except ValueError:
                b = None
        if a and b and b < a and re.search(MONTH_RE, m.group(2)):
            a = a.replace(month=b.month) if a.day <= b.day else a
        if a and b and b >= a:
            return a, b
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|–|to|till)\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+{MONTH_RE}", low)
    if m:
        mo = MONTHS[m.group(3)[:3]]
        try:
            a, b = _year_fix(date(ref.year, mo, int(m.group(1))), ref), _year_fix(date(ref.year, mo, int(m.group(2))), ref)
            if b >= a:
                return a, b
        except ValueError:
            pass
    a = parse_date(text, ref)
    m = re.search(r"\bfor\s+(\d|two|three|four|five)\s+days\b", low)
    if a and m:
        n = {"two": 2, "three": 3, "four": 4, "five": 5}.get(m.group(1)) or int(m.group(1))
        return a, a + timedelta(days=n - 1)
    return a, a


def parse_reason(subject: str, body: str) -> str:
    body2 = re.sub(r"\bfrom\s+\d.{0,20}?\s+(?:to|till)\s+\d\S*\s*(?:am|pm)?", " ", body, flags=re.I)
    h = re.search(r"\b(?:is|has been|was|am)\s+(unwell|sick|ill|hospitali[sz]ed|down with [^.,\n]+|suffering from [^.,\n]+|having [^.,\n]+)",
                  body2, re.I)
    if h:
        return h.group(1).strip()[:80]
    for m in REASON_RE.finditer(body2):
        r = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
        r = re.sub(r"^(the|a|an)\s+", "", r, flags=re.I)
        if re.match(r"(\d+|one|two|three|four|five)\s+(days?|hours?)\b", r, re.I):
            inner = REASON_RE.search(r)  # "3 days from Monday for the tournament"
            if inner:
                r = re.sub(r"^(the|a|an)\s+", "", inner.group(1).strip(" ,."), flags=re.I)
            else:
                continue
        r = re.sub(r"^((tmrw|tomorrow|today|pls|please|sir|ma'?am|mam)[\s,]*)+", "", r, flags=re.I)
        if len(r) > 2 and not re.fullmatch(r"(leave|tomorrow|today|me)", r, re.I):
            return r[:80]
    return clean_subject(subject)


def detect_intent(text: str) -> str:
    low = text.lower()
    hits = [name for name, rx in INTENT_RULES if re.search(rx, low)]
    if "cancel" in hits and "reschedule" in hits:
        return "reschedule"
    if "cancel" in hits and re.search(r"\b(instead|another|move|shift|other day)\b", low):
        return "reschedule"
    return hits[0] if hits else "other"


def is_urgent(text: str) -> bool:
    return bool(re.search(r"\b(urgent|urgently|asap|as soon as possible|immediately|emergency|today itself|right away)\b", text, re.I))


def clean_subject(subject: str) -> str:
    s = re.sub(r"^\s*((re|fwd?|fw)\s*:\s*)+", "", subject or "", flags=re.I)
    s = re.sub(r"^\s*urgent\s*[:\-]\s*", "", s, flags=re.I)
    return s.strip()


NEW_TIME_MARKERS = re.compile(
    r"\b(move (?:it |this |that |the meeting )?to|moved to|shift (?:it |this )?to|change (?:it )?to|"
    r"reschedule (?:it )?to|instead|how about|what about|could we do|can we do|would .* work|is .* possible|"
    rf"to (?=(?:next |this |coming )?(?:{WD_RE[1:-1]}|tomorrow|today|day after|the evening|the morning|the afternoon|\d)))\b", re.I)


def _strip_greeting(text: str) -> str:
    return re.sub(r"\bgood\s+(morning|afternoon|evening|day)\b", " ", text, flags=re.I)


def parse_request(subject: str, body: str, ref: datetime) -> ParsedRequest:
    body = _strip_greeting(body)
    text = f"{subject}\n{body}"
    p = ParsedRequest()
    kind = absence_kind(text)
    if kind:  # leave request
        p.intent = kind
        no_dur = re.sub(r"\b\d{2}[A-Z]{3}\d{3,5}\b", " ", text)
        p.start, p.end, p.part_of_day, rest = parse_times(no_dur)
        p.date, p.end_date = parse_span(rest, ref.date())
        p.reason = parse_reason(subject, body)
        p.topic = clean_subject(subject) or "Leave request"
        p.urgent = is_urgent(text)
        return p
    intent0 = detect_intent(text)
    if intent0 == "reschedule":
        marks = list(NEW_TIME_MARKERS.finditer(body))
        if marks:  # the new time usually comes after "move it to / how about", or in the clause with "instead"
            mk = marks[-1]
            tail = body[mk.start():]
            q = parse_request("", tail, ref) if tail.strip() != body.strip() else None
            if not (q and (q.date or q.start or q.part_of_day)):
                cs = max(body.rfind(x, 0, mk.start()) for x in ".,;!?\n") + 1
                ce = min([i for i in (body.find(x, mk.end()) for x in ".,;!?\n") if i != -1] or [len(body)])
                clause = body[cs:ce]
                q = parse_request("", clause, ref) if clause.strip() and clause.strip() != body.strip() else None
            if q and (q.date or q.start or q.part_of_day):
                q.intent, q.topic = "reschedule", clean_subject(subject) or "Meeting"
                q.duration_min = q.duration_min or parse_duration(text)
                q.urgent = is_urgent(text)
                return q
    p.option_choice = detect_option(body)
    p.duration_min = parse_duration(text)
    no_dur = re.sub(r"\b\d+(?:\.\d+)?\s*(?:-\s*)?(hours?|hrs?|minutes?|mins?)\b", " ", text, flags=re.I)
    no_dur = re.sub(r"\b\d{2}[A-Z]{3}\d{3,5}\b", " ", no_dur)  # registration numbers like 23MIS1002
    p.start, p.end, p.part_of_day, rest = parse_times(no_dur)
    p.date = parse_date(rest, ref.date()) or parse_date(clean_subject(subject), ref.date())
    p.urgent = is_urgent(text)
    p.intent = detect_intent(text)
    if p.option_choice and p.intent in ("other", "meeting_request", "availability_query"):
        p.intent = "option_choice"
    if p.urgent and p.intent == "other":
        p.intent = "meeting_request"
    if p.intent == "other" and p.start and "?" in text:  # "Is 9:30 tomorrow fine?"
        p.intent = "meeting_request"
    if p.urgent and not p.date:
        p.date = ref.date()
    if not p.date:
        rg = parse_date_range(rest, ref.date())
        if rg and rg[0] != rg[1]:
            p.range_start, p.range_end = rg
    p.topic = clean_subject(subject) or "Meeting"
    return p


def parse_commitment_text(text: str, ref: datetime, default_minutes: int = 60) -> dict | None:
    """'Club practice every Tue 5-6:30pm' -> {title, start, end, recurrence, category}"""
    start, end, part, rest = parse_times(text)
    d = parse_date(rest, ref.date())
    if not d:
        return None
    if not start and part:
        start, end = PARTS[part]
    if not start:
        return None
    dur = parse_duration(rest)
    s = datetime.combine(d, start)
    e = datetime.combine(d, end) if end else s + timedelta(minutes=dur or default_minutes)
    weekly = bool(re.search(r"\b(every|weekly|each|all)\b", text, re.I))
    low = text.lower()
    category = "urgent" if is_urgent(text) else (
        "official" if re.search(r"\b(official|duty|review|exam|class|committee|meeting|viva|invigilation)\b", low) else "unofficial")
    title = re.sub(rf"\b(every|weekly|each|on|at|from|for|between|till|until|this|next|coming|today|tomorrow|{WD_RE}s?)\b",
                   " ", rest, flags=re.I)
    title = re.sub(rf"\b\d{{1,2}}(st|nd|rd|th)?\s*(of\s+)?{MONTH_RE}\b", " ", title, flags=re.I)
    title = re.sub(r"\b(morning|afternoon|evening|urgent)\b", " ", title, flags=re.I)
    title = re.sub(r"\b\d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)\b", " ", title, flags=re.I)
    title = re.sub(r"[\s,;:-]+", " ", title).strip(" .,-") or "Personal commitment"
    return {"title": title[:1].upper() + title[1:], "start": s, "end": e,
            "recurrence": "weekly" if weekly else None, "category": category}
