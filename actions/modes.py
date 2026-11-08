"""
actions/modes.py - assistant personalities you can switch by voice.

    "Switch to study mode"   -> Socratic tutor, short quiz-style answers
    "Coding mode"            -> terse, technical, prefers code tools
    "Focus mode"             -> speaks only when it must, no chit-chat
    "Brainstorm mode"        -> expansive, asks "what if"
    "Back to normal"

The mode is a block of extra instructions added to the system prompt (core: main._build_config calls
``prompt_block()``). Changing mode rebuilds the Live session; session resumption keeps the conversation.
Add your own in config/modes.json:  {"exam": {"label": "Exam prep", "prompt": "..."}}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core import services

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "memory" / "mode.json"           # tests monkeypatch these
CUSTOM_FILE = BASE_DIR / "config" / "modes.json"

BUILTIN = {
    "default": {"label": "Normal", "prompt": ""},
    "study": {
        "label": "Study",
        "prompt": ("STUDY MODE: you are a patient tutor. Explain simply with one concrete example, then check understanding with "
                   "ONE short question. Prefer teaching the user to reason over just giving final answers. Use the quiz and report "
                   "tools when the user asks to be tested. Keep answers under four sentences."),
    },
    "coding": {
        "label": "Coding",
        "prompt": ("CODING MODE: be terse and technical. Lead with the answer or the fix. Name exact functions, flags and files. "
                   "Use code_helper / dev_agent / run_python for real work instead of describing it. Skip pleasantries."),
    },
    "focus": {
        "label": "Focus",
        "prompt": ("FOCUS MODE: the user is concentrating. Answer in ONE short sentence. No greetings, no jokes, no follow-up "
                   "questions, no proactive remarks. Only speak when addressed."),
    },
    "brainstorm": {
        "label": "Brainstorm",
        "prompt": ("BRAINSTORM MODE: be generative and playful. Offer 3 distinct ideas, build on the user's ideas with 'what if', "
                   "challenge assumptions gently, and never shoot an idea down in the first reply."),
    },
}


def _all_modes() -> dict:
    modes = dict(BUILTIN)
    try:
        custom = json.loads(CUSTOM_FILE.read_text(encoding="utf-8"))
        for k, v in custom.items():
            if isinstance(v, dict) and v.get("prompt"):
                modes[str(k).lower().strip()] = {"label": v.get("label", k), "prompt": str(v["prompt"])}
    except Exception:
        pass
    return modes


def current_mode() -> str:
    try:
        m = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("mode", "default")
    except Exception:
        m = "default"
    return m if m in _all_modes() else "default"


def prompt_block() -> str:
    """Text for main._build_config to append to the system prompt ('' in the normal mode)."""
    m = current_mode()
    p = _all_modes()[m]["prompt"]
    return f"[ACTIVE MODE: {m}]\n{p}\n" if p else ""


def _resolve(name: str) -> str | None:
    n = (name or "").lower().strip()
    for filler in ("switch to", "go to", "change to", "back to", "enter", "activate", "turn on", "the", "mode", "please"):
        n = re.sub(rf"\b{filler}\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    modes = _all_modes()
    if n in ("", "normal", "off", "none", "standard", "regular", "default", "usual"):
        return "default"
    if n in modes:
        return n
    for k, v in modes.items():
        if n in k or n in str(v["label"]).lower() or k in n:
            return k
    return None


def assistant_mode(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "set")).lower().strip()
    if action == "list":
        return "Modes: " + ", ".join(f"{k} ({v['label']})" for k, v in _all_modes().items()) + f". Current: {current_mode()}."
    if action == "current":
        return f"Current mode: {current_mode()}."
    target = _resolve(parameters.get("mode", ""))
    if target is None:
        return "I do not know that mode. Available: " + ", ".join(_all_modes()) + "."
    if target == current_mode():
        return f"Already in {target} mode."
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"mode": target}), encoding="utf-8")
    if player:
        try:
            player.write_log(f"SYS: Mode -> {target}")
        except Exception:
            pass
    # Rebuild the live session so the new system prompt applies; the conversation is kept.
    services.request_reconnect(keep_context=True, reason=f"{target} mode")
    return f"Switched to {target} mode. Confirm in one short sentence."


TOOL = {
    "name": "assistant_mode",
    "description": (
        "Switch the assistant's personality/behaviour mode: study, coding, focus, brainstorm, or back to normal. "
        "Use for 'study mode', 'coding mode', 'focus mode for the assistant', 'brainstorm with me', 'back to normal', "
        "and 'what modes do you have'. NOT for the timed focus_mode plugin that closes apps."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "set (default) | list | current"},
            "mode": {"type": "STRING", "description": "default | study | coding | focus | brainstorm | a custom mode name"},
        },
        "required": [],
    },
    "handler": assistant_mode,
}
