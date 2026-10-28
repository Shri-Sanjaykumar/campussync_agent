"""Mail understanding: DeepSeek first (when configured), always cross-checked against the rules.
If the model invents a time the mail never mentions, the rules' reading wins."""
from __future__ import annotations

import re
from datetime import date, datetime, time

from ..utils import WEEKDAY_FULL
from .rules import PARTS, ParsedRequest, parse_commitment_text, parse_request

INTENTS = {"meeting_request", "reschedule", "cancel", "availability_query", "option_choice", "leave_request", "other"}

SYSTEM = """You read emails between students and faculty at a university and extract scheduling details.
Return ONLY a JSON object with these keys:
- intent: one of meeting_request, reschedule, cancel, availability_query, option_choice, leave_request, other
- date: "YYYY-MM-DD" or null. Resolve words like today/tomorrow/Friday against the email's sent date. For a reschedule, give the NEW date being asked for.
- start_time: "HH:MM" 24-hour or null
- end_time: "HH:MM" or null (only if a range like 2-4 pm is given)
- part_of_day: morning | afternoon | evening | null
- duration_min: integer or null
- urgent: true/false
- topic: short meeting title, max 6 words
- option_choice: integer if the sender picks a numbered option offered earlier, else null
Never invent a date or time that the email does not state or clearly imply. Assume times without am/pm between 1 and 7 mean pm."""


def _valid_time(v) -> time | None:
    if not v or not isinstance(v, str):
        return None
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)", v)
    return time(int(m.group(1)), int(m.group(2))) if m else None


def _valid_date(v) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


class MailParser:
    def __init__(self, llm, intent_model=None):
        self.llm = llm
        self.intent_model = intent_model
        self.use_model = intent_model is not None

    def _ensemble(self, p: ParsedRequest, subject: str, body: str) -> ParsedRequest:
        """Second opinion from the neural intent model. It only steps in when the rules found nothing,
        or when it is very sure the rules picked the generic 'meeting' label for a cancel/reschedule."""
        if not (self.use_model and self.intent_model) or p.intent == "leave_request":
            return p
        try:
            label, prob = self.intent_model.predict(f"{subject}. {body}")
        except Exception:
            return p
        p.notes.append(f"neural model: {label} ({prob:.0%})")
        p.model_intent, p.model_conf = label, prob
        if label == "option_choice" and not p.option_choice:
            return p
        if p.intent == "other" and label != "other" and prob >= 0.45:
            p.intent = label
            p.notes.append("intent taken from the neural model")
        elif p.intent == "meeting_request" and label in ("reschedule", "cancel") and prob >= 0.8:
            p.intent = label
            p.notes.append("intent taken from the neural model")
        if p.intent == "other" and p.urgent:
            p.intent = "meeting_request"
        return p

    def parse(self, subject: str, body: str, sent_at: datetime) -> ParsedRequest:
        rules = self._ensemble(parse_request(subject, body, sent_at), subject, body)
        if not self.llm.enabled:
            return rules
        try:
            user = (f"Email sent on {WEEKDAY_FULL[sent_at.weekday()]}, {sent_at:%Y-%m-%d %H:%M}.\n"
                    f"Subject: {subject}\n\n{body}")
            j = self.llm.json(SYSTEM, user)
        except Exception:
            rules.notes.append("DeepSeek unavailable, used rule-based parser")
            return rules
        return self._merge(j, rules, f"{subject}\n{body}")

    def _merge(self, j: dict, rules: ParsedRequest, text: str) -> ParsedRequest:
        if rules.intent == "leave_request":
            return rules  # dates and spans for requests come from the rules (checked), not the model
        p = ParsedRequest(source="deepseek")
        p.intent = j.get("intent") if j.get("intent") in INTENTS else rules.intent
        p.date = _valid_date(j.get("date")) or rules.date
        p.start = _valid_time(j.get("start_time"))
        p.end = _valid_time(j.get("end_time"))
        has_clock = bool(re.search(r"\d|noon", text, re.I))
        if p.start and not has_clock:  # model made up a time
            p.notes.append("ignored a time the model invented")
            p.start = p.end = None
        if not p.start:
            p.start, p.end = rules.start, rules.end
        if p.start and p.end and p.end <= p.start:
            p.end = None
        pod = j.get("part_of_day")
        p.part_of_day = pod if pod in PARTS else rules.part_of_day
        try:
            dm = int(j.get("duration_min")) if j.get("duration_min") else None
        except (TypeError, ValueError):
            dm = None
        p.duration_min = dm if dm and 5 <= dm <= 240 else rules.duration_min
        p.urgent = bool(j.get("urgent")) or rules.urgent
        p.topic = (str(j.get("topic") or "").strip() or rules.topic)[:60]
        try:
            oc = int(j.get("option_choice")) if j.get("option_choice") is not None else None
        except (TypeError, ValueError):
            oc = None
        p.option_choice = oc if oc and 1 <= oc <= 5 else rules.option_choice
        if p.option_choice and p.intent in ("other", "meeting_request"):
            p.intent = "option_choice"
        return p

    def parse_commitment(self, text: str, now: datetime) -> dict | None:
        rules = parse_commitment_text(text, now)
        if not self.llm.enabled:
            return rules
        try:
            j = self.llm.json(
                "Extract a calendar commitment from the user's note. Return JSON with keys: title, date (YYYY-MM-DD), "
                "start_time (HH:MM 24h), end_time (HH:MM or null), weekly (true/false), "
                "category (urgent|official|unofficial). Never invent a time that isn't in the note.",
                f"Today is {WEEKDAY_FULL[now.weekday()]} {now:%Y-%m-%d}. Note: {text}")
            d, s = _valid_date(j.get("date")), _valid_time(j.get("start_time"))
            if not d or not s or not re.search(r"\d|noon|morning|afternoon|evening", text, re.I):
                return rules
            e = _valid_time(j.get("end_time"))
            start = datetime.combine(d, s)
            end = datetime.combine(d, e) if e and e > s else (rules["end"].replace(year=d.year, month=d.month, day=d.day)
                                                             if rules and rules["start"].time() == s else
                                                             start.replace(hour=min(23, s.hour + 1)))
            return {"title": str(j.get("title") or (rules or {}).get("title") or "Commitment")[:80],
                    "start": start, "end": end, "recurrence": "weekly" if j.get("weekly") else None,
                    "category": j.get("category") if j.get("category") in ("urgent", "official", "unofficial") else "unofficial"}
        except Exception:
            return rules
