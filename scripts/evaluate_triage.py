"""Compare mail sorting: built-in rules vs DeepSeek, on the labelled inbox mails in data/eval/triage.jsonl.
    python scripts/evaluate_triage.py [--wrong]
DeepSeek is used when DEEPSEEK_API_KEY is set in .env."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campussync.config import load_settings  # noqa: E402
from campussync.llm.client import LLMClient  # noqa: E402
from campussync.ml.eval_triage import run  # noqa: E402

rows = [json.loads(x) for x in (ROOT / "data/eval/triage.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
llm = LLMClient(load_settings())
runs = {"Rules": run(rows)}
if llm.enabled:
    ds = run(rows, llm)
    if ds.get("error"):
        print(f"DeepSeek couldn't be reached ({ds['error']}), so it isn't scored.\n")
    else:
        runs["DeepSeek"] = ds
print(f"{len(rows)} labelled mails\n")
print(f"{'':12}{'category':>10}{'needs reply':>13}{'deadline':>10}")
for name, r in runs.items():
    print(f"{name:12}{r['category']:>9}%{r['needs_reply']:>12}%{r['deadline']:>9}%")
for name, r in runs.items():
    if r["created"]:
        print(f"\nCategories {name} created: {', '.join(r['created'])}")
if not llm.enabled:
    print("\nDeepSeek not scored: set DEEPSEEK_API_KEY in .env.")
if "--wrong" in sys.argv:
    for name, r in runs.items():
        print(f"\n--- {name}: {len(r['wrong'])} mails with a mistake")
        for w in r["wrong"]:
            print(f"  {w['subject']}: want {w['want']} got {w['got']}")
