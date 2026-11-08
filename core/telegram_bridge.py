"""
core/telegram_bridge.py - control the assistant from ANYWHERE through a private Telegram bot.

SETUP (2 minutes)
  1. In Telegram talk to @BotFather -> /newbot -> copy the token.
  2. Put it in config/api_keys.json:   "telegram_bot_token": "123456:ABC..."
  3. Start the assistant. The HUD log prints a 6-digit pairing code.
  4. Open your bot in Telegram and send:   /pair 123456      (only that chat is ever allowed)

WHAT YOU CAN DO
  * type a command ("open chrome", "what's on my calendar", "run study mode") - the assistant executes it
    on your computer and the answer comes back as a Telegram message
  * send a VOICE NOTE - it is transcribed by Gemini and run like a typed command
  * /screenshot  -> a photo of your computer's screen        * /status  -> is the assistant alive
  * /unpair      -> forget this phone                          * /help

SECURITY
  * nobody is answered until they send the correct one-time /pair code; five wrong codes lock pairing for 10 minutes
  * unpaired chats get NO reply at all (the bot looks dead to strangers)
  * only paired chat ids are stored (config/api_keys.json); the token never leaves your machine
"""
from __future__ import annotations

import json
import secrets
import threading
import time

import requests

from core import services

PAIR_CODE = f"{secrets.randbelow(10**6):06d}"          # new every launch
REPLY_WINDOW_S = 120.0                                 # replies are mirrored for this long after a Telegram command
_state = {"fails": 0, "locked_until": 0.0, "reply_until": 0.0, "reply_chat": None, "stop": False}
_lock = threading.Lock()

# ── config access (tests replace these two) ─────────────────────────────────
def _cfg() -> dict:
    from memory.config_manager import load_api_keys
    return load_api_keys()


def _save(**fields) -> None:
    from memory.config_manager import _patch_config
    _patch_config(**fields)


def _token() -> str:
    return str(_cfg().get("telegram_bot_token", "") or "").strip()


def is_configured() -> bool:
    return len(_token()) > 20


def paired_chats() -> list[int]:
    return [int(c) for c in (_cfg().get("telegram_chat_ids") or [])]


# ── Telegram HTTP (tests replace _call) ──────────────────────────────────────
def _call(method: str, params: dict | None = None, files: dict | None = None, timeout: float = 35.0) -> dict:
    r = requests.post(f"https://api.telegram.org/bot{_token()}/{method}", data=params or {}, files=files, timeout=timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "telegram error"))
    return data["result"]


def send(chat_id: int, text: str) -> None:
    text = (text or "").strip()
    for i in range(0, len(text), 3900) or [0]:
        if text[i:i + 3900]:
            try:
                _call("sendMessage", {"chat_id": chat_id, "text": text[i:i + 3900]})
            except Exception as e:
                print(f"[Telegram] send failed: {e}")


def _download(file_id: str) -> bytes:
    info = _call("getFile", {"file_id": file_id})
    r = requests.get(f"https://api.telegram.org/file/bot{_token()}/{info['file_path']}", timeout=30)
    r.raise_for_status()
    return r.content


def _transcribe(audio: bytes, mime: str = "audio/ogg") -> str:
    """Voice note -> text with Gemini (same key the assistant already uses)."""
    from google import genai
    from google.genai import types
    key = json.loads(_cfg_path_text())["gemini_api_key"]
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model="gemini-flash-latest",
        contents=[types.Part.from_bytes(data=audio, mime_type=mime),
                  "Transcribe this voice message exactly, in its original language. Output only the transcript."])
    return (resp.text or "").strip()


def _cfg_path_text() -> str:
    from memory.config_manager import CONFIG_FILE
    return CONFIG_FILE.read_text(encoding="utf-8")


# ── update handling (the part with the security logic; fully unit-tested) ────
HELP = ("Commands:\n/status - is the assistant online\n/screenshot - see the screen\n/unpair - forget this phone\n"
        "Anything else is sent to the assistant as a command. You can also send voice notes.")


def handle_update(upd: dict, now: float | None = None) -> str:
    """Process one Telegram update. Returns a tag describing what happened (used by tests)."""
    now = time.time() if now is None else now
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return "ignored"
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None:
        return "ignored"

    # ---- pairing: the ONLY thing an unpaired chat may do ----
    if chat_id not in paired_chats():
        if text.lower().startswith("/pair"):
            with _lock:
                if now < _state["locked_until"]:
                    return "locked"
                code = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
                if secrets.compare_digest(code, PAIR_CODE):
                    _state["fails"] = 0
                    _save(telegram_chat_ids=sorted(set(paired_chats() + [int(chat_id)])))
                    send(chat_id, "Paired. You can now control your assistant from here. Send /help.")
                    services.log("SYS: Telegram phone paired.")
                    return "paired"
                _state["fails"] += 1
                if _state["fails"] >= 5:
                    _state["locked_until"] = now + 600
                    _state["fails"] = 0
                    services.log("SYS: Telegram pairing locked for 10 minutes (5 wrong codes).")
            return "wrong_code"
        return "stranger"                                   # no reply at all

    # ---- paired chat ----
    low = text.lower()
    if low in ("/start", "/help"):
        send(chat_id, HELP)
        return "help"
    if low == "/status":
        send(chat_id, "Online. Send a command or a voice note.")
        return "status"
    if low == "/unpair":
        _save(telegram_chat_ids=[c for c in paired_chats() if c != chat_id])
        send(chat_id, "Unpaired. Send /pair <code> again to reconnect.")
        return "unpaired"
    if low == "/screenshot":
        img = services.screenshot()
        if not img:
            send(chat_id, "I could not capture the screen.")
            return "no_screenshot"
        try:
            _call("sendPhoto", {"chat_id": chat_id}, files={"photo": ("screen.png", img)}, timeout=60)
        except Exception as e:
            send(chat_id, f"Could not send the screenshot: {e}")
        return "screenshot"

    if msg.get("voice") or msg.get("audio"):
        v = msg.get("voice") or msg.get("audio")
        try:
            text = _transcribe(_download(v["file_id"]), v.get("mime_type", "audio/ogg"))
        except Exception as e:
            send(chat_id, f"I could not understand that voice note: {e}")
            return "voice_failed"
        if not text:
            send(chat_id, "The voice note was empty.")
            return "voice_empty"
        send(chat_id, f"Heard: {text}")

    if not text:
        return "ignored"
    with _lock:
        _state["reply_until"] = now + REPLY_WINDOW_S
        _state["reply_chat"] = chat_id
    if services.submit_text(text):
        return "command"
    send(chat_id, "The assistant is not connected right now. Try again in a moment.")
    return "not_ready"


def _on_output(text: str) -> None:
    """Mirror what the assistant says back to the phone - but only right after a Telegram command."""
    with _lock:
        chat, until = _state["reply_chat"], _state["reply_until"]
        if not chat or time.time() > until:
            return
        _state["reply_until"] = time.time() + 20          # keep the window open while it is still talking
    send(chat, text)


def run_forever() -> None:
    services.on_output(_on_output)
    services.log(f"SYS: Telegram remote ready. Pairing code: {PAIR_CODE}  (send '/pair {PAIR_CODE}' to your bot)"
                 if not paired_chats() else "SYS: Telegram remote ready (paired).")
    offset, backoff = None, 2
    while not _state["stop"]:
        try:
            updates = _call("getUpdates", {"timeout": 25, "offset": offset or 0, "allowed_updates": json.dumps(["message"])}, timeout=40)
            backoff = 2
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception as e:
                    print(f"[Telegram] update error: {e}")
        except Exception as e:
            print(f"[Telegram] poll error: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
