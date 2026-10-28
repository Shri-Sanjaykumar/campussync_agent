"""Scores mail sorting on data/eval/triage.jsonl.

'category' is right when it matches the expected core category (or one of several acceptable ones);
"new" means any non-core category counts (placement, events, clubs...), since those are open-ended.
Also scored: needs_reply, and whether a deadline was found when there is one. A row's optional "meta"
is copied onto the mail (e.g. a Google group it was posted to, or bulk-mail headers)."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from ..core.triage import CORE, Triage
from ..models import Mail
from ..store import StateStore


class _Sender:
    def __init__(self, name, role):
        self.name, self.role = name, role


class _Portal:
    def __init__(self, people):
        self.people = people

    def get_person(self, pid):
        return self.people.get(pid)


def run(rows: list[dict], llm=None) -> dict:
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    store = StateStore(tmp)
    people, mails = {}, []
    for i, r in enumerate(rows):
        name, role = r["from"].rsplit(" (", 1)
        people[f"P{i}"] = _Sender(name, role.rstrip(")"))
        m = Mail(f"E{i}", f"P{i}", ["ME"], r["subject"], r["body"], datetime.fromisoformat(r["sent_at"]), kind="human",
                 meta=dict(r.get("meta", {})))
        store.upsert_mail(m)
        mails.append(m)
    t = Triage(store, None, _Portal(people), llm, lambda: datetime.fromisoformat(rows[0]["sent_at"]))
    t.college_domains = ("vit.ac.in", "vitstudent.ac.in")
    if llm is not None:
        t.classify_many(mails)
    score = {"category": 0, "needs_reply": 0, "deadline": 0}
    wrong, created = [], set()
    for m, r in zip(mails, rows):
        got = t.classify(m)
        exp = r["expect"]
        want = exp["category"] if isinstance(exp["category"], list) else [exp["category"]]
        cat_ok = any((got["category"] not in CORE) if w == "new" else got["category"] == w for w in want)
        if got["category"] not in CORE:
            created.add(got["category"])
        checks = {"category": cat_ok, "needs_reply": bool(got["action"]) == exp["needs_reply"],
                  "deadline": bool(got["deadline"]) == exp["deadline"]}
        for k, ok in checks.items():
            score[k] += ok
        if not all(checks.values()):
            wrong.append({"subject": r["subject"], "want": exp, "got": {"category": t.label(got["category"]),
                                                                       "needs_reply": got["action"],
                                                                       "deadline": str(got["deadline"]) if got["deadline"] else None}})
    n = len(rows)
    return {"n": n, **{k: round(100 * v / n) for k, v in score.items()}, "created": sorted(created), "wrong": wrong,
            "error": t.last_error}
