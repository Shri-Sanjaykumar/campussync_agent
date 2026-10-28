"""Neural intent classifier: a small multi-layer perceptron (2 hidden layers) on word + character
n-gram features. Character n-grams make it tolerant of typos and slang ("tmrw", "mtg", "pls").

It runs offline in milliseconds and acts as a second opinion next to the rule parser:
it only overrides the rules when the rules found nothing or the network is very confident."""
from __future__ import annotations

import time
from pathlib import Path

MODEL_VERSION = "intent-mlp-v2"


class IntentModel:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.pipe = None
        self.info: dict = {}

    def _build(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.neural_network import MLPClassifier
        from sklearn.pipeline import FeatureUnion, Pipeline
        feats = FeatureUnion([
            ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)),
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True)),
        ])
        mlp = MLPClassifier(hidden_layer_sizes=(128, 64), alpha=1e-3, max_iter=300, early_stopping=True,
                            validation_fraction=0.15, n_iter_no_change=10, random_state=0)
        return Pipeline([("features", feats), ("mlp", mlp)])

    def train(self, n_per_class: int = 220, seed: int = 0) -> dict:
        from .synth import generate
        X, y = generate(n_per_class, seed)
        t0 = time.time()
        self.pipe = self._build()
        self.pipe.fit(X, y)
        mlp = self.pipe.named_steps["mlp"]
        self.info = {"version": MODEL_VERSION, "samples": len(X), "classes": [str(c) for c in mlp.classes_],
                     "epochs": int(mlp.n_iter_), "val_accuracy": round(float(max(mlp.validation_scores_ or [0])), 3),
                     "train_seconds": round(time.time() - t0, 1), "layers": f"features -> 128 -> 64 -> {len(mlp.classes_)} (ReLU, Adam)"}
        try:
            import joblib
            self.path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump({"pipe": self.pipe, "info": self.info}, self.path)
        except Exception:
            pass
        return self.info

    def load(self) -> bool:
        try:
            import joblib
            obj = joblib.load(self.path)
            if obj.get("info", {}).get("version") != MODEL_VERSION:
                return False
            self.pipe, self.info = obj["pipe"], obj["info"]
            return True
        except Exception:
            return False

    def ensure(self) -> None:
        if self.pipe is None and not self.load():
            self.train()

    def predict(self, text: str) -> tuple[str, float]:
        self.ensure()
        proba = self.pipe.predict_proba([text])[0]
        i = int(proba.argmax())
        return str(self.pipe.classes_[i]), float(proba[i])
