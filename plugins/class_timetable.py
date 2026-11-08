"""
ERA AI plugin: class_timetable

Tell Jarvis your weekly timetable once. After that ask "when is my next class" or
"what do I have tomorrow". Saved on your laptop, works offline.
"""

PLUGIN = {
    "name": "class_timetable",
    "description": (
        "Stores the user's weekly class timetable and answers questions about it. "
        "Actions: add_classes (save one or many classes, use the classes list when the user "
        "dictates the whole week), next_class ('when is my next class', 'agli class kab hai'), "
        "today, tomorrow, day (a named weekday), show_all, remove_class, clear_all. "
        "Times are in the user's local time."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: add_classes, next_class, today, tomorrow, day, show_all, remove_class, clear_all",
            },
            "day": {"type": "STRING", "description": "Weekday name such as Monday. Used for the day and remove_class actions."},
            "subject": {"type": "STRING", "description": "Subject name, used for remove_class"},
            "classes": {
                "type": "ARRAY",
                "description": "Classes to save for add_classes",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "day": {"type": "STRING", "description": "Weekday name, for example Monday"},
                        "subject": {"type": "STRING", "description": "Subject name"},
                        "start_time": {"type": "STRING", "description": "Start time such as 9:30 AM or 14:00"},
                        "end_time": {"type": "STRING", "description": "End time, optional"},
                        "room": {"type": "STRING", "description": "Room or lab, optional"},
                    },
                    "required": ["day", "subject", "start_time"],
                },
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



import datetime as _dt

_LOCK = threading.Lock()
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _path() -> Path:
    return _data_folder() / "class_timetable.json"


def _load() -> list:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list) -> None:
    _path().write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def _norm_day(text):
    s = str(text or "").strip().lower()
    if not s:
        return None
    today = _dt.date.today()
    if s == "today":
        return _DAYS[today.weekday()]
    if s == "tomorrow":
        return _DAYS[(today.weekday() + 1) % 7]
    for name in _DAYS:
        if name.lower().startswith(s[:3]) and s[:3] in name.lower()[:3]:
            return name
    return None


def _parse_time(text):
    s = str(text or "").strip().lower().replace("a.m.", "am").replace("p.m.", "pm")
    s = s.replace(".", ":")
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not match:
        return None
    hour, minute, ampm = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _say_time(value: str) -> str:
    hour, minute = int(value[:2]), int(value[3:])
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def _describe(entry: dict) -> str:
    text = f"{entry['subject']} at {_say_time(entry['start'])}"
    if entry.get("end"):
        text += f" until {_say_time(entry['end'])}"
    if entry.get("room"):
        text += f" in {entry['room']}"
    return text


def _day_list(items: list, day: str) -> list:
    return sorted((c for c in items if c["day"] == day), key=lambda c: c["start"])


def _next_class(items: list) -> str:
    if not items:
        return "You have not told me your timetable yet."
    now = _dt.datetime.now()
    now_text = now.strftime("%H:%M")
    prefix = ""
    for c in _day_list(items, _DAYS[now.weekday()]):
        if c["start"] <= now_text and (c.get("end") or "23:59") > now_text:
            prefix = f"You are in {c['subject']} right now"
            prefix += f", it ends at {_say_time(c['end'])}. " if c.get("end") else ". "
    for offset in range(0, 8):
        day = _DAYS[(now.weekday() + offset) % 7]
        for c in _day_list(items, day):
            if offset == 0 and c["start"] <= now_text:
                continue
            if offset == 0:
                start = _dt.datetime.combine(now.date(), _dt.time(int(c["start"][:2]), int(c["start"][3:])))
                minutes = int((start - now).total_seconds() // 60)
                hours, rest = divmod(minutes, 60)
                when = (f"in {hours} hours and {rest} minutes" if hours else f"in {rest} minutes")
                return prefix + f"Your next class is {_describe(c)} today, {when}."
            label = "tomorrow" if offset == 1 else f"on {day}"
            return prefix + f"Your next class is {_describe(c)} {label}."
    return prefix + "I could not find any upcoming class."


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "")).strip().lower()
    try:
        with _LOCK:
            items = _load()

            if action in ("add_classes", "add_class"):
                incoming = parameters.get("classes") or []
                if not incoming and parameters.get("subject"):
                    incoming = [{
                        "day": parameters.get("day"), "subject": parameters.get("subject"),
                        "start_time": parameters.get("start_time"), "end_time": parameters.get("end_time"),
                        "room": parameters.get("room"),
                    }]
                added, skipped = 0, []
                for c in incoming:
                    day, start = _norm_day(c.get("day")), _parse_time(c.get("start_time"))
                    subject = str(c.get("subject") or "").strip()
                    if not (day and start and subject):
                        skipped.append(subject or "one class")
                        continue
                    items = [x for x in items if not (x["day"] == day and x["start"] == start)]
                    items.append({
                        "day": day, "subject": subject, "start": start,
                        "end": _parse_time(c.get("end_time")) or "",
                        "room": str(c.get("room") or "").strip(),
                    })
                    added += 1
                _save(items)
                result = f"Saved {added} classes in your timetable."
                if skipped:
                    result += f" I could not understand the day or time for: {', '.join(skipped)}."

            elif action == "next_class":
                result = _next_class(items)

            elif action in ("today", "tomorrow", "day"):
                if action == "day":
                    day = _norm_day(parameters.get("day"))
                else:
                    day = _norm_day(action)
                if not day:
                    return "Which day do you mean?"
                classes = _day_list(items, day)
                label = {"today": "today", "tomorrow": "tomorrow"}.get(action, f"on {day}")
                if not classes:
                    result = f"You have no classes {label}."
                else:
                    result = f"You have {len(classes)} classes {label}. " + ". ".join(_describe(c) for c in classes) + "."

            elif action == "show_all":
                if not items:
                    return "Your timetable is empty."
                parts = []
                for day in _DAYS:
                    classes = _day_list(items, day)
                    if classes:
                        parts.append(f"{day}: " + ", ".join(_describe(c) for c in classes) + ".")
                result = " ".join(parts)

            elif action == "remove_class":
                subject = str(parameters.get("subject") or "").strip().lower()
                day = _norm_day(parameters.get("day"))
                if not subject:
                    return "Which subject should I remove?"
                keep = [c for c in items if not (subject in c["subject"].lower() and (not day or c["day"] == day))]
                removed = len(items) - len(keep)
                _save(keep)
                result = f"Removed {removed} classes." if removed else "I could not find that class."

            elif action == "clear_all":
                _save([])
                result = "I cleared your whole timetable."

            else:
                return "I can save classes, tell your next class, read a day, show the whole week or remove a class."

        _log(player, result)
        return result
    except Exception as error:
        return f"Sorry, the timetable failed: {error}"
