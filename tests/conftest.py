import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ANCHOR = date(2026, 10, 15)          # a Thursday
NOW = datetime(2026, 10, 15, 9, 0)


def _make(tmp_path, base_cfg="config/settings.yaml", mail="mock_mail"):
    from campussync.service import CampusSync
    cfg = yaml.safe_load((ROOT / base_cfg).read_text())
    if cfg["adapters"]["portal"] == "mock_vtop":
        cfg["paths"]["portal_data"] = str(tmp_path / "vtop.json")
    cfg["paths"]["state"] = str(tmp_path / "state.json")
    cfg["paths"]["secrets"] = str(tmp_path / "secrets")
    cfg["llm"]["use_llm"] = "off"
    cfg["adapters"]["mail"] = mail
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(cfg))
    svc = CampusSync(str(p))
    svc.set_clock(NOW)
    return svc


@pytest.fixture
def svc(tmp_path):
    s = _make(tmp_path)
    s.reset_demo(ANCHOR)
    return s


@pytest.fixture
def csv_svc(tmp_path):
    s = _make(tmp_path, "config/settings.other_college.yaml")
    s.store.reset()
    s.set_clock(NOW)
    return s


@pytest.fixture
def gsvc(tmp_path):
    """Service with the Gmail adapter (not connected yet)."""
    s = _make(tmp_path, mail="gmail")
    s.reset_demo(ANCHOR)
    return s
