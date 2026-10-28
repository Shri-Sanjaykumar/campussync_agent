"""Retrieval layer (the 'R' in RAG). Every timetable, cabin-hour list, calendar entry, exam,
policy, uploaded commitment and meeting becomes a small document. Questions pull the most
relevant documents the asking user is allowed to see, and those go to the model as context.

Backends (rag.backend in settings):
  tfidf     keyword match
  dense     dense vectors only: sentence-transformers if installed (neural embeddings),
            otherwise LSA (TruncatedSVD on TF-IDF) so it still runs with no download
  hybrid    both, merged with Reciprocal Rank Fusion (default)"""
from __future__ import annotations

from dataclasses import dataclass, field

import re

import numpy as np

# Campus words people use for the same thing. Applied to questions only.
SYNONYMS = {
    "professor": "faculty", "prof": "faculty", "sir": "faculty", "maam": "faculty", "teacher": "faculty",
    "test": "exam", "cat": "exam", "fat": "exam", "quiz": "exam",
    "cabin": "cabin office", "office": "office cabin", "consultation": "cabin hours",
    "cancels": "unavailable reschedule cancel", "cancel": "unavailable reschedule cancel",
    "attended": "attendance", "bunk": "attendance skip", "skip": "attendance skip",
    "leave": "holiday", "vacation": "holiday break", "timings": "timetable hours",
}
_SUFFIXES = ("ances", "ance", "ings", "ing", "edly", "ed", "es", "s")


def stem(word: str) -> str:
    for suf in _SUFFIXES:
        if len(word) > len(suf) + 3 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def analyzer(text: str) -> list[str]:
    toks = [stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]
    return toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]


def expand_query(q: str) -> str:
    extra = [SYNONYMS[t] for t in re.findall(r"[a-z]+", q.lower()) if t in SYNONYMS]
    return q + (" " + " ".join(extra) if extra else "")


_STOP = set("a an the and or of to in on at for is are am be my me i you your we our it this that what when how "
            "who which with do does did can could will would should if any there by from as".split())


@dataclass
class Doc:
    id: str
    title: str
    text: str
    kind: str
    visibility: object = "public"  # "public" | "faculty" | list of person ids
    meta: dict = field(default_factory=dict)


def can_see(doc: Doc, user) -> bool:
    v = doc.visibility
    if v == "public":
        return True
    if v == "faculty":
        return user.role == "faculty"
    if isinstance(v, (list, tuple, set)):
        return user.id in v or ("faculty" in v and user.role == "faculty")
    return False


class KnowledgeBase:
    RRF_K = 60

    def __init__(self, settings, backend: str | None = None):
        self.backend = backend or str(settings.get("rag.backend", "hybrid"))
        self.model_name = settings.get("rag.embedding_model", "all-MiniLM-L6-v2")
        self.top_k = int(settings.get("rag.top_k", 5))
        self.docs: list[Doc] = []
        self._tfidf = self._vec = self._dense = self._svd = None
        self._st = None
        self.encoder = None  # tests can plug in a fake encoder: callable(list[str]) -> np.ndarray
        self.dense_kind = None

    @property
    def active_backend(self) -> str:
        if self.backend == "tfidf":
            return "tfidf"
        return f"{self.backend} (tfidf + {self.dense_kind})" if self.backend == "hybrid" else f"dense ({self.dense_kind})"

    def _encode(self, texts: list[str]) -> np.ndarray | None:
        if self.encoder is not None:
            return np.asarray(self.encoder(texts), dtype=float)
        if self._st is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._st = SentenceTransformer(self.model_name)
            except Exception:
                self._st = False
        if self._st:
            return self._st.encode(texts, normalize_embeddings=True)
        return None

    def build(self, docs: list[Doc]) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.docs = docs
        texts = [f"{d.title}. {d.text}" for d in docs] or ["empty"]
        self._vec = TfidfVectorizer(analyzer=analyzer, sublinear_tf=True)
        self._tfidf = self._vec.fit_transform(texts)
        self._dense, self._svd = None, None
        if self.backend in ("dense", "hybrid"):
            emb = self._encode(texts)
            if emb is not None:
                self._dense, self.dense_kind = self._norm(emb), "neural embeddings" if self.encoder is None else "custom encoder"
            else:
                from sklearn.decomposition import TruncatedSVD
                n = max(2, min(64, self._tfidf.shape[0] // 3, self._tfidf.shape[1] - 1))
                self._svd = TruncatedSVD(n_components=n, random_state=0)
                self._dense, self.dense_kind = self._norm(self._svd.fit_transform(self._tfidf)), "LSA vectors"

    @staticmethod
    def _norm(m):
        m = np.asarray(m, dtype=float)
        n = np.linalg.norm(m, axis=1, keepdims=True)
        n[n == 0] = 1
        return m / n

    def _rank_tfidf(self, query: str) -> np.ndarray:
        return (self._tfidf @ self._vec.transform([query]).T).toarray().ravel()

    def _rank_dense(self, query: str) -> np.ndarray:
        if self._svd is not None:
            q = self._norm(self._svd.transform(self._vec.transform([query])))
        else:
            q = self._norm(self._encode([query]))
        return (self._dense @ q.T).ravel()

    def search(self, query: str, user, k: int | None = None) -> list[tuple[Doc, float]]:
        if not self.docs:
            return []
        k = k or self.top_k
        query = expand_query(query)
        visible = [i for i, d in enumerate(self.docs) if can_see(d, user)]
        if not visible:
            return []
        if self.backend == "tfidf" or self._dense is None:
            sc = self._rank_tfidf(query)
            order = sorted(visible, key=lambda i: sc[i], reverse=True)
            return [(self.docs[i], float(sc[i])) for i in order[:k] if sc[i] > 0]
        if self.backend == "dense":
            sc = self._rank_dense(query)
            order = sorted(visible, key=lambda i: sc[i], reverse=True)
            return [(self.docs[i], float(sc[i])) for i in order[:k]]
        # hybrid: reciprocal rank fusion of keyword and dense rankings
        kw, de = self._rank_tfidf(query), self._rank_dense(query)
        fused = {}
        for scores in (kw, de):
            order = sorted(visible, key=lambda i: scores[i], reverse=True)
            for rank, i in enumerate(order[:50]):
                if scores is kw and scores[i] <= 0:
                    break
                fused[i] = fused.get(i, 0.0) + 1.0 / (self.RRF_K + rank + 1)
        order = sorted(fused, key=lambda i: fused[i], reverse=True)
        return [(self.docs[i], round(fused[i], 4)) for i in order[:k]]
