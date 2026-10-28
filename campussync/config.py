from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"


class Settings:
    """Thin wrapper around the YAML profile with dotted lookups: settings.get('scheduling.day_start')."""

    def __init__(self, raw: dict, path: Path):
        self.raw = raw
        self.path = path
        self.root = PROJECT_ROOT

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path_of(self, key: str) -> Path:
        p = Path(self.get(f"paths.{key}"))
        return p if p.is_absolute() else self.root / p


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass
    p = Path(path or os.getenv("CAMPUSSYNC_CONFIG") or DEFAULT_CONFIG)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Settings(raw, p)
