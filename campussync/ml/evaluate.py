"""Scores a mail parser against the hand-written set in data/eval/mails.jsonl."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

FIELDS = ["intent", "date", "start", "end", "part_of_day", "duration_min", "urgent", "option"]
SAME_ACTION = {"meeting_request": "schedule", "availability_query": "schedule"}


def load_rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def _value(p, field):
    if field == "date":
        return p.date.isoformat() if p.date else None
    if field in ("start", "end"):
        v = getattr(p, field)
        return v.strftime("%H:%M") if v else None
    if field == "option":
        return p.option_choice
    return getattr(p, field)


def evaluate(parse, rows: list[dict]) -> dict:
    stats = {f: [0, 0] for f in FIELDS}
    action = [0, 0]
    exact = 0
    failures = []
    for r in rows:
        p = parse(r["subject"], r["body"], datetime.fromisoformat(r["sent_at"]))
        ok_all = True
        wrong = {}
        for f, want in r["expect"].items():
            got = _value(p, f)
            stats[f][1] += 1
            if got == want:
                stats[f][0] += 1
            else:
                ok_all = False
                wrong[f] = {"want": want, "got": got}
        action[1] += 1
        if SAME_ACTION.get(p.intent, p.intent) == SAME_ACTION.get(r["expect"]["intent"], r["expect"]["intent"]):
            action[0] += 1
        exact += ok_all
        if wrong:
            failures.append({"body": r["body"], "wrong": wrong})
    return {"n": len(rows),
            "fields": {f: round(100 * c / t) for f, (c, t) in stats.items() if t},
            "action": round(100 * action[0] / action[1]) if action[1] else None,
            "exact": round(100 * exact / len(rows)) if rows else None,
            "failures": failures}


def evaluate_retrieval(kb, people: dict, rows: list[dict]) -> dict:
    """Recall@1, Recall@3 and MRR: does the right document come back for each question?"""
    r1 = r3 = 0
    mrr = 0.0
    misses = []
    for r in rows:
        user = people[r["user"]]
        hits = [d.id for d, _s in kb.search(r["query"], user, k=10)]
        rank = next((i for i, h in enumerate(hits, 1) if h.startswith(r["expect"])), None)
        if rank == 1:
            r1 += 1
        if rank and rank <= 3:
            r3 += 1
        mrr += 1 / rank if rank else 0
        if not rank or rank > 3:
            misses.append({"query": r["query"], "expect": r["expect"], "got": hits[:3]})
    n = len(rows)
    return {"n": n, "recall@1": round(100 * r1 / n), "recall@3": round(100 * r3 / n), "mrr": round(mrr / n, 2),
            "misses": misses}
