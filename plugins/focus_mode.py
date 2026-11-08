"""
ERA AI plugin: focus_mode

Closes distracting apps and runs a focus timer. When the time is over you hear a
beep and see a message box. Works offline.
"""

PLUGIN = {
    "name": "focus_mode",
    "description": (
        "Starts or stops a focus session. 'start' closes distracting desktop apps and runs "
        "a timer; 'status' tells the time left; 'stop' ends the session early. Use for "
        "'focus mode', 'I want to study for 25 minutes', 'close distractions'. If the user "
        "names apps to close, pass them in apps_to_close, otherwise the default list "
        "(Discord, Steam, Epic Games, Telegram, WhatsApp, Netflix, TikTok, Instagram) is used. "
        "This does not block websites. Do NOT use this for reminders, that belongs to reminder."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: start, status, stop"},
            "minutes": {"type": "INTEGER", "description": "Length of the focus session in minutes, default 25"},
            "apps_to_close": {
                "type": "ARRAY",
                "description": "Optional names of apps to close, for example Discord",
                "items": {"type": "STRING"},
            },
        },
        "required": ["action"],
    },
}

import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent


def _desktop_folder() -> Path:
    for candidate in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop", Path.home() / "Documents"):
        if candidate.exists():
            return candidate
    return Path.home()


def _data_folder() -> Path:
    folder = Path.home() / "Documents" / "Jarvis"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        folder = _desktop_folder()
    return folder


def _safe_name(text, default="File") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _]+", "", str(text)).strip().replace(" ", "_")
    return cleaned[:50] or default


def _unique_path(folder: Path, stem: str, ext: str) -> Path:
    path = folder / f"{stem}{ext}"
    number = 2
    while path.exists():
        path = folder / f"{stem}_{number}{ext}"
        number += 1
    return path


def _open_file(path: Path) -> None:
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def _log(player, message: str) -> None:
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass



_DEFAULT_APPS = ["discord", "steam", "epicgameslauncher", "telegram", "whatsapp", "netflix", "tiktok", "instagram"]
_PROTECTED = {"python", "pythonw", "explorer", "svchost", "system", "winlogon", "csrss", "services", "lsass", "dwm"}
_STATE = {"end": 0.0, "stop": None, "thread": None, "minutes": 0}


def _clean_app_name(name: str) -> str:
    n = str(name).strip().lower().replace(".exe", "").replace(" ", "")
    return {"epic": "epicgameslauncher", "epicgames": "epicgameslauncher", "googlechrome": "chrome"}.get(n, n)


def _close_apps(names: list) -> list:
    try:
        import psutil
    except ImportError:
        return []
    targets = {_clean_app_name(n) for n in names} - _PROTECTED
    me = os.getpid()
    victims, closed = [], set()
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            raw = (proc.info["name"] or "").lower().replace(".exe", "")
            if proc.info["pid"] != me and raw in targets:
                proc.terminate()
                victims.append(proc)
                closed.add(raw)
        except Exception:
            continue
    try:
        _gone, alive = psutil.wait_procs(victims, timeout=3)
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass
    return sorted(closed)


def _notify(text: str) -> None:
    try:
        if platform.system() == "Windows":
            import ctypes
            import winsound
            try:
                winsound.Beep(1000, 400)
                winsound.Beep(1300, 400)
            except Exception:
                pass
            ctypes.windll.user32.MessageBoxW(0, text, "Focus mode", 0x40 | 0x1000)
        elif platform.system() == "Darwin":
            subprocess.Popen(["osascript", "-e", f'display notification "{text}" with title "Focus mode"'])
        else:
            subprocess.Popen(["notify-send", "Focus mode", text])
    except Exception:
        print(f"[Focus] {text}")


def _timer(minutes: int, stop_event, player) -> None:
    finished = not stop_event.wait(minutes * 60)
    if finished:
        message = f"Your {minutes} minute focus session is over. Well done, take a short break."
        _log(player, message)
        _notify(message)
    _STATE["end"] = 0.0


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "")).strip().lower()
    try:
        running = _STATE["end"] > time.time()

        if action == "start":
            if running:
                left = int((_STATE["end"] - time.time()) // 60) + 1
                return f"A focus session is already running, about {left} minutes are left."
            minutes = max(1, min(int(parameters.get("minutes") or 25), 240))
            apps = parameters.get("apps_to_close") or _DEFAULT_APPS
            closed = _close_apps([str(a) for a in apps])
            stop_event = threading.Event()
            _STATE.update(end=time.time() + minutes * 60, stop=stop_event, minutes=minutes)
            thread = threading.Thread(target=_timer, args=(minutes, stop_event, player), daemon=True, name="focus-timer")
            _STATE["thread"] = thread
            thread.start()
            closed_text = f"I closed {', '.join(closed)}." if closed else "No distracting apps were open."
            result = f"Focus mode is on for {minutes} minutes. {closed_text} I will beep when time is up."

        elif action == "status":
            if not running:
                return "No focus session is running."
            left = _STATE["end"] - time.time()
            result = f"{int(left // 60)} minutes and {int(left % 60)} seconds are left in your focus session."

        elif action == "stop":
            if not running:
                return "No focus session is running."
            _STATE["stop"].set()
            _STATE["end"] = 0.0
            result = "Focus mode is stopped."

        else:
            return "I can start a focus session, tell the time left, or stop it."

        _log(player, result)
        return result
    except Exception as error:
        return f"Sorry, focus mode failed: {error}"
