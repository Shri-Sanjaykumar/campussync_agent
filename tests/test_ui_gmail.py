"""The app asks to connect Gmail before anything else."""
import json
from pathlib import Path

import pytest
import yaml

from test_gmail import CLIENT, FakeGmail, FRIEND

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    cfg = yaml.safe_load((ROOT / "config/settings.yaml").read_text())
    cfg["paths"]["portal_data"] = str(tmp_path / "vtop.json")
    cfg["paths"]["state"] = str(tmp_path / "state.json")
    cfg["paths"]["secrets"] = str(tmp_path / "secrets")
    cfg["llm"]["use_llm"] = "off"
    cfg["adapters"]["mail"] = "gmail"
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("CAMPUSSYNC_CONFIG", str(p))
    monkeypatch.chdir(ROOT)

    def _make():
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
        at.run()
        assert not at.exception, at.exception
        return at
    return _make, tmp_path


def test_first_screen_asks_to_connect_gmail(make_app):
    make, tmp = make_app
    at = make()
    assert at.title[0].value == "Connect your Gmail"
    assert len(at.tabs) == 2  # only the two connect options, no app yet
    assert any("console.cloud.google.com" in m.value for m in at.markdown)


def test_with_client_file_shows_google_button(make_app):
    make, tmp = make_app
    (tmp / "secrets").mkdir()
    (tmp / "secrets" / "client_secret.json").write_text(json.dumps(CLIENT))
    at = make()
    assert at.title[0].value == "Connect your Gmail"
    assert not at.exception


def test_skip_to_demo_mailbox(make_app):
    make, tmp = make_app
    at = make()
    next(b for b in at.button if b.label.startswith("Skip for now")).click().run()
    assert not at.exception, at.exception
    assert at.sidebar.radio(key="nav").value == "Home"
    assert any("demo mailbox" in c.value for c in at.sidebar.caption)


def test_connected_flow_profile_then_gmail_inbox(make_app, monkeypatch):
    make, tmp = make_app
    fake = FakeGmail()
    fake.deliver(FRIEND, "Doubt", "Can we meet tomorrow at 3 pm?", name="Friend Prof")
    import campussync.adapters.gmail_mailbox as gm
    monkeypatch.setattr(gm, "ImapSmtpClient", lambda *a, **k: fake)
    (tmp / "secrets").mkdir()
    (tmp / "secrets" / "gmail_app_password.json").write_text(json.dumps({"address": fake.email, "password": "x" * 16}))
    at = make()
    assert at.title[0].value == "Which VTOP profile is yours?"
    at.text_input[0].input("Asha Menon")
    at.button[0].click().run()  # form submit
    assert not at.exception, at.exception
    assert any(fake.email in c.value for c in at.sidebar.caption)
    at.sidebar.radio(key="nav").set_value("Inbox").run()
    assert not at.exception, at.exception
    assert at.button_group(key="inbox_source").value == "Gmail"
    assert any(m.value == "#### Doubt" for m in at.markdown)  # the Gmail mail is open
