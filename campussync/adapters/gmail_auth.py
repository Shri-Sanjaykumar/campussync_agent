"""Connecting a Gmail account. Two ways:

1. Sign in with Google (OAuth). Needs a one-time OAuth client file (client_secret.json) from Google
   Cloud. That file is the app's identity, not anyone's mailbox: after it's added, anyone runs the app
   and signs in with their own Gmail.
2. Gmail address + app password (IMAP/SMTP). No Google Cloud setup, but the account needs 2-Step
   Verification and app passwords allowed (college Workspace accounts often block this).

Everything is kept in data/secrets/ on the machine running the app (git-ignored, never zipped)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # read, send, label. No permanent delete.

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class GmailAuthError(Exception):
    pass


class GmailAuth:
    def __init__(self, settings):
        self.dir = settings.path_of("secrets") if settings.get("paths.secrets") else settings.root / "data" / "secrets"
        self.redirect_uri = os.getenv("CAMPUSSYNC_REDIRECT_URI") or settings.get("gmail.redirect_uri", "http://localhost:8501/")
        self.client_file = self.dir / "client_secret.json"
        self.token_file = self.dir / "gmail_token.json"
        self.pending_file = self.dir / "oauth_pending.json"
        self.imap_file = self.dir / "gmail_app_password.json"
        self.last_error: str | None = None

    # ---------------------------------------------------------------- state
    @property
    def method(self) -> str | None:
        if self.token_file.exists():
            return "oauth"
        if self.imap_file.exists():
            return "app_password"
        return None

    @property
    def connected(self) -> bool:
        return self.method is not None

    @property
    def has_client(self) -> bool:
        return self.client_file.exists()

    def _write(self, path: Path, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def disconnect(self) -> None:
        for f in (self.token_file, self.imap_file, self.pending_file):
            if f.exists():
                f.unlink()

    # ---------------------------------------------------------------- OAuth client file
    def save_client_file(self, raw: bytes | str) -> str:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise GmailAuthError("That isn't a JSON file. Download the OAuth client JSON from Google Cloud.")
        kind = "installed" if "installed" in data else "web" if "web" in data else None
        if not kind or not data[kind].get("client_id") or not data[kind].get("client_secret"):
            raise GmailAuthError("This JSON has no OAuth client in it. In Google Cloud create an "
                                 "'OAuth client ID' of type 'Desktop app' and download that file.")
        self._write(self.client_file, data)
        return kind

    def client_kind(self) -> str | None:
        if not self.has_client:
            return None
        data = json.loads(self.client_file.read_text(encoding="utf-8"))
        return "installed" if "installed" in data else "web"

    # ---------------------------------------------------------------- OAuth flow
    def _flow(self, state: str | None = None):
        from google_auth_oauthlib.flow import Flow
        return Flow.from_client_secrets_file(str(self.client_file), scopes=SCOPES, redirect_uri=self.redirect_uri,
                                             state=state)

    def auth_url(self) -> str:
        """Google sign-in link. The PKCE verifier is saved to disk because the redirect back from Google
        lands in a new browser session."""
        if not self.has_client:
            raise GmailAuthError("Add the OAuth client file first.")
        flow = self._flow()
        url, state = flow.authorization_url(access_type="offline", prompt="consent")
        pending = self._pending()
        pending[state] = {"verifier": flow.code_verifier, "at": time.time()}
        pending = {k: v for k, v in pending.items() if time.time() - v["at"] < 1800}
        self._write(self.pending_file, pending)
        return url

    def _pending(self) -> dict:
        if self.pending_file.exists():
            try:
                return json.loads(self.pending_file.read_text(encoding="utf-8"))
            except ValueError:
                return {}
        return {}

    def handle_callback(self, code: str, state: str | None) -> None:
        pending = self._pending()
        entry = pending.get(state or "")
        if not entry:
            raise GmailAuthError("This sign-in link has expired or was opened in another app. Click 'Sign in with Google' again.")
        flow = self._flow(state=state)
        flow.code_verifier = entry["verifier"]
        try:
            flow.fetch_token(code=code)
        except Exception as e:
            raise GmailAuthError(f"Google didn't accept the sign-in: {e}")
        creds = flow.credentials
        granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
        if granted and not set(SCOPES) <= granted:
            raise GmailAuthError("Gmail access wasn't granted. Sign in again and tick the Gmail permission box.")
        self._write(self.token_file, json.loads(creds.to_json()))
        pending.pop(state, None)
        self._write(self.pending_file, pending)
        if self.imap_file.exists():
            self.imap_file.unlink()

    def credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        if not self.token_file.exists():
            return None
        creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    raise GmailAuthError(f"Your Gmail sign-in expired ({type(e).__name__}). Connect again.")
                self._write(self.token_file, json.loads(creds.to_json()))
            else:
                raise GmailAuthError("Your Gmail sign-in is no longer valid. Connect again.")
        return creds

    # ---------------------------------------------------------------- app password
    def save_app_password(self, address: str, password: str) -> None:
        address, password = address.strip(), password.replace(" ", "").strip()
        if "@" not in address or len(password) < 16:
            raise GmailAuthError("Enter your Gmail address and the 16-character app password (spaces are fine).")
        self._write(self.imap_file, {"address": address, "password": password})
        if self.token_file.exists():
            self.token_file.unlink()

    def app_password(self) -> dict | None:
        if not self.imap_file.exists():
            return None
        return json.loads(self.imap_file.read_text(encoding="utf-8"))
