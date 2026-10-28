"""Compare mail parsers on the hand-written test set (data/eval/mails.jsonl).
    python scripts/evaluate_parser.py            rules and rules + neural model
    python scripts/evaluate_parser.py --failures  also list every wrong answer
With DEEPSEEK_API_KEY set (in .env), DeepSeek is scored too."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campussync.config import load_settings  # noqa: E402
from campussync.llm.client import LLMClient  # noqa: E402
from campussync.llm.parser import MailParser  # noqa: E402
from campussync.llm.rules import parse_request  # noqa: E402
from campussync.ml.evaluate import FIELDS, evaluate, load_rows  # noqa: E402
from campussync.ml.intent import IntentModel  # noqa: E402

_main, _hold = load_rows(ROOT / "data/eval/mails.jsonl"), load_rows(ROOT / "data/eval/mails_holdout.jsonl")
SETS = {f"Main set ({len(_main)} mails, rules were tuned on these)": _main,
        f"Holdout ({len(_hold)} mails, never used for tuning)": _hold}
settings = load_settings()
model = IntentModel(ROOT / "data/models/intent_mlp.joblib")
off = LLMClient(settings)
off.mode = "off"

runs = {"Rules only": parse_request, "Rules + neural model": MailParser(off, model).parse}
live = LLMClient(settings)
if live.enabled:
    runs["DeepSeek + rules + model"] = MailParser(live, model).parse

head = f"{'':26}" + "".join(f"{k[:12]:>13}" for k in ["action", "exact"] + FIELDS)
all_results = {}
for set_name, rows in SETS.items():
    results = {name: evaluate(fn, rows) for name, fn in runs.items()}
    all_results[set_name] = results
    print(f"\n{set_name}  (none of these were used to train the neural model)")
    print(head)
    for name, r in results.items():
        vals = [r["action"], r["exact"]] + [r["fields"].get(f) for f in FIELDS]
        print(f"{name:26}" + "".join(f"{(str(v) + '%') if v is not None else '-':>13}" for v in vals))
print("\naction = scheduling vs reschedule vs cancel vs option vs other (what the agent actually does)")
print("exact  = every labelled field right for that mail. The holdout number is the honest one.")
if not live.enabled:
    print("DeepSeek not scored: set DEEPSEEK_API_KEY in .env to include it.")
if "--failures" in sys.argv:
    for set_name, results in all_results.items():
        for name, r in results.items():
            print(f"\n--- {set_name} / {name}: {len(r['failures'])} mails with a mistake")
            for f in r["failures"]:
                print(f"  {f['body'][:70]!r}: {f['wrong']}")
