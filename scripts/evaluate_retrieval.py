"""Compare retrieval backends on data/eval/retrieval.jsonl (16 questions with a known right document).
    python scripts/evaluate_retrieval.py [--misses]"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campussync.ml.evaluate import evaluate_retrieval, load_rows  # noqa: E402
from campussync.rag.store import KnowledgeBase  # noqa: E402
from campussync.service import CampusSync  # noqa: E402

svc = CampusSync()
if not svc.portal.list_people():
    svc.reset_demo()
docs = svc._build_docs()
people = {p.id: p for p in svc.people()}
rows = load_rows(ROOT / "data/eval/retrieval.jsonl")
print(f"{len(rows)} questions over {len(docs)} documents\n")
print(f"{'backend':38}{'recall@1':>10}{'recall@3':>10}{'MRR':>7}")
for backend in ("tfidf", "dense", "hybrid"):
    kb = KnowledgeBase(svc.settings, backend)
    kb.build(docs)
    r = evaluate_retrieval(kb, people, rows)
    print(f"{kb.active_backend:38}{r['recall@1']:>9}%{r['recall@3']:>9}%{r['mrr']:>7}")
    if "--misses" in sys.argv:
        for m in r["misses"]:
            print("     miss:", m)
