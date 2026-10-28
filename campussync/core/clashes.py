"""Clash alerts: whenever someone uploads a new commitment, check it against everything else."""
from __future__ import annotations

from datetime import timedelta

from ..models import Alert, Commitment
from ..utils import fmt_range, overlaps
from .availability import expand_commitment

SEVERITY = {"hold": "warning", "exam": "critical", "exam_prep": "warning", "meeting": "critical", "duty": "warning",
            "class": "warning", "commitment": "warning", "external": "warning", "holiday": "info"}


class ClashDetector:
    def __init__(self, engine, store, portal, clock, horizon_days: int = 60):
        self.engine, self.store, self.portal, self.clock = engine, store, portal, clock
        self.horizon_days = horizon_days

    def check(self, c: Commitment) -> list[dict]:
        today = self.clock().date()
        start = max(today, c.start.date())
        end = (c.until or (c.start.date() if not c.recurrence else today + timedelta(days=self.horizon_days)))
        end = max(end, c.end.date())
        occ = [o for o in expand_commitment(c, start, end) if o[1] >= self.clock()]
        if not occ:
            return []
        events = self.engine.person_events(c.person_id, start, end, exclude_commitment_ids=[c.id])
        person = self.portal.get_person(c.person_id)
        clashes = []
        for s, e in occ:
            for ev in events:
                if ev.kind == "pref" or not overlaps(s, e, ev.start, ev.end):
                    continue
                sev = SEVERITY.get(ev.kind, "warning")
                note = ""
                if ev.kind == "class":
                    if person and person.role == "faculty":
                        sev, note = "critical", "you teach this class"
                    elif ev.hard:
                        note = (f"attendance {ev.meta.get('percent')}% would drop to "
                                f"{ev.meta.get('percent_if_missed')}% (below the safe line)")
                    else:
                        sev = "info"
                        note = ("you're exempt (CGPA rule)" if ev.meta.get("exempt") else
                                f"you can afford it: {ev.meta.get('percent')}% -> {ev.meta.get('percent_if_missed')}%")
                elif ev.kind == "meeting":
                    note = "this meeting involves other people; the agent can move it"
                clashes.append({"occurrence": (s, e), "with": ev.title, "kind": ev.kind, "severity": sev,
                                "note": note, "meeting_id": ev.ref if ev.kind == "meeting" else None,
                                "when": fmt_range(max(s, ev.start), min(e, ev.end))})
        return clashes

    def raise_alerts(self, c: Commitment, clashes: list[dict]) -> None:
        for cl in clashes:
            level = {"critical": "critical", "warning": "warning"}.get(cl["severity"], "info")
            msg = f"'{c.title}' clashes with {cl['with']} on {cl['when']}"
            if cl["note"]:
                msg += f" - {cl['note']}"
            self.store.add_alert(Alert(self.store.new_id("A"), c.person_id, self.clock(), level, msg,
                                       {"commitment_id": c.id, "meeting_id": cl["meeting_id"]}))
