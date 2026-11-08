"""
ERA AI plugin: notes_and_tasks

"Remember this", "add a task", "read my list". Everything is saved on your laptop
in Documents/Jarvis, so it works offline and survives restarts.
"""

PLUGIN = {
    "name": "notes_and_tasks",
    "description": (
        "Saves and reads the user's notes and to do list. Use for: 'remember this', "
        "'note this down' (add_note), 'read my notes' (read_notes), 'add a task', "
        "'aaj ka kaam likho' (add_task), 'what is on my list', 'meri list sunao' "
        "(list_tasks), 'I finished task 2' (complete_task), 'remove task 3' "
        "(delete_task), 'clear finished tasks' (clear_done). Read lists back in a clear "
        "spoken way. This is for notes and tasks only, NOT for timed reminders or alarms "
        "and NOT for the assistant's own memory about the user."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: add_note, read_notes, add_task, list_tasks, complete_task, delete_task, clear_done",
            },
            "text": {"type": "STRING", "description": "The note or task text, or a few words of an existing task to find it"},
            "task_number": {"type": "INTEGER", "description": "Number of the task in the list, starting from 1"},
            "due": {"type": "STRING", "description": "Optional due date or time in the user's words, for example tomorrow"},
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



_LOCK = threading.Lock()


def _store_path() -> Path:
    return _data_folder() / "notes_and_tasks.json"


def _load() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("notes", [])
            data.setdefault("tasks", [])
            return data
    except Exception:
        pass
    return {"notes": [], "tasks": []}


def _save(data: dict) -> None:
    _store_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    # A readable copy that you can open any time
    lines = ["MY TASKS", ""]
    for i, t in enumerate(data["tasks"], start=1):
        due = f"  (due {t['due']})" if t.get("due") else ""
        lines.append(f"{i}. [{'x' if t.get('done') else ' '}] {t['text']}{due}")
    lines += ["", "MY NOTES", ""]
    for n in data["notes"]:
        lines.append(f"{n['created']}  {n['text']}")
    (_data_folder() / "My Notes and Tasks.txt").write_text("\n".join(lines), encoding="utf-8")


def _now() -> str:
    return time.strftime("%d %b %Y %H:%M")


def _find_task(data: dict, number, text):
    tasks = data["tasks"]
    if number:
        try:
            index = int(number) - 1
            if 0 <= index < len(tasks):
                return index
        except (TypeError, ValueError):
            pass
    needle = str(text or "").strip().lower()
    if needle:
        for i, t in enumerate(tasks):
            if needle in t["text"].lower():
                return i
    return None


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "")).strip().lower()
    text = str(parameters.get("text", "") or "").strip()
    number = parameters.get("task_number")
    due = str(parameters.get("due", "") or "").strip()

    try:
        with _LOCK:
            data = _load()

            if action == "add_note":
                if not text:
                    return "What should I note down?"
                data["notes"].append({"text": text, "created": _now()})
                _save(data)
                result = "Noted. I will keep that safe."

            elif action == "read_notes":
                if not data["notes"]:
                    return "You have no saved notes yet."
                recent = data["notes"][-10:]
                lines = [f"Note {i}: {n['text']}" for i, n in enumerate(recent, start=1)]
                result = f"You have {len(data['notes'])} notes. " + (
                    "The latest ones are. " if len(data["notes"]) > 10 else ""
                ) + " ".join(lines)

            elif action == "add_task":
                if not text:
                    return "What task should I add?"
                data["tasks"].append({"text": text, "done": False, "due": due, "created": _now()})
                _save(data)
                result = f"Added task number {len(data['tasks'])}: {text}."

            elif action == "list_tasks":
                tasks = data["tasks"]
                if not tasks:
                    return "Your task list is empty."
                pending = [(i, t) for i, t in enumerate(tasks, start=1) if not t.get("done")]
                done_count = len(tasks) - len(pending)
                if not pending:
                    return f"All {len(tasks)} tasks are finished."
                parts = []
                for i, t in pending:
                    suffix = f", due {t['due']}" if t.get("due") else ""
                    parts.append(f"Number {i}: {t['text']}{suffix}.")
                result = f"You have {len(pending)} tasks left. " + " ".join(parts)
                if done_count:
                    result += f" {done_count} are already finished."

            elif action in ("complete_task", "delete_task"):
                index = _find_task(data, number, text)
                if index is None:
                    return "I could not find that task. Ask me to read your list first."
                task = data["tasks"][index]
                if action == "complete_task":
                    task["done"] = True
                    _save(data)
                    result = f"Marked as finished: {task['text']}."
                else:
                    data["tasks"].pop(index)
                    _save(data)
                    result = f"Removed the task: {task['text']}."

            elif action == "clear_done":
                before = len(data["tasks"])
                data["tasks"] = [t for t in data["tasks"] if not t.get("done")]
                _save(data)
                result = f"Cleared {before - len(data['tasks'])} finished tasks."

            else:
                return "I can add notes, read notes, add tasks, list tasks, finish tasks, delete tasks or clear finished ones."

        _log(player, result)
        return result
    except Exception as error:
        return f"Sorry, I could not do that: {error}"
