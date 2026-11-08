"""
core/google_auth.py - ONE Google sign-in shared by Calendar and Gmail (official Google APIs, no passwords stored).

ONE-TIME SETUP (about 5 minutes, free)
  1. https://console.cloud.google.com -> create a project -> "APIs & Services" -> Library ->
     enable "Google Calendar API" and "Gmail API".
  2. "OAuth consent screen": External, add yourself as a Test user.
  3. "Credentials" -> Create credentials -> OAuth client ID -> type "Desktop app" -> Download JSON.
  4. Save that file as   config/google_credentials.json
  5. Say "connect my Google account". A browser opens once; click Allow. Done - it never asks again
     (the refresh token is kept in config/google_token.json on YOUR computer only).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CRED_FILE = BASE_DIR / "config" / "google_credentials.json"       # tests monkeypatch
TOKEN_FILE = BASE_DIR / "config" / "google_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",      # read, label, draft AND send (no permanent delete)
]
_SERVICE_OVERRIDE: dict = {}                              # tests: {"calendar": fake, "gmail": fake}
_flow_lock = threading.Lock()
_flow_running = False


class NotConnected(Exception):
    """Raised when no valid Google login exists. The message is safe to speak."""


def status() -> dict:
    return {"credentials_file": CRED_FILE.exists(), "authorized": TOKEN_FILE.exists()}


def _load_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds if creds and creds.valid else None


def service(api: str, version: str):
    """Authorised Google API client. Raises NotConnected with a speakable explanation."""
    if api in _SERVICE_OVERRIDE:
        return _SERVICE_OVERRIDE[api]
    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise NotConnected("The Google libraries are missing. Run: pip install google-api-python-client google-auth-oauthlib")
    try:
        creds = _load_creds()
    except Exception as e:
        raise NotConnected(f"Your Google login expired or is broken ({e}). Say 'connect my Google account' to sign in again.")
    if creds is None:
        if not CRED_FILE.exists():
            raise NotConnected("Google is not set up yet. Save your OAuth client file as config/google_credentials.json "
                               "(see ERA_PRO_V2.md), then say 'connect my Google account'.")
        raise NotConnected("Google is not connected yet. Say 'connect my Google account' and approve in the browser.")
    return build(api, version, credentials=creds, cache_discovery=False)


def connect(on_done=None) -> str:
    """Start the one-time browser consent in the background (a tool call must not block for minutes)."""
    global _flow_running
    if not CRED_FILE.exists():
        return ("I need your Google OAuth file first: download it from Google Cloud Console and save it as "
                "config/google_credentials.json. The steps are in ERA_PRO_V2.md.")
    with _flow_lock:
        if _flow_running:
            return "The Google sign-in window is already open. Please approve it in your browser."
        _flow_running = True

    def work():
        global _flow_running
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(CRED_FILE), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300)
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            msg = "Google account connected. Calendar and Gmail are ready."
        except Exception as e:
            msg = f"Google sign-in failed: {str(e)[:120]}"
        finally:
            with _flow_lock:
                _flow_running = False
        if on_done:
            on_done(msg)

    threading.Thread(target=work, daemon=True, name="google-oauth").start()
    return "I opened your browser. Please choose your Google account and click Allow."


def disconnect() -> str:
    try:
        TOKEN_FILE.unlink()
        return "Disconnected from Google. Your files stay on your computer."
    except FileNotFoundError:
        return "You were not connected."
