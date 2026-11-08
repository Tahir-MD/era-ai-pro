"""
actions/routines.py - voice-created automations ("macros") that chain ANY other tool.

    "Create a routine called study mode: open VS Code, start focus mode for 25 minutes,
     then say good luck."                      -> saved once
    "Run study mode"                           -> executes every step in order
    "Run study mode every weekday at 9:00"     -> runs by itself, even if you forgot

Steps are plain data, stored in memory/routines.json:
    {"tool": "open_app", "args": {"app_name": "chrome"}}   call any action or plugin
    {"say": "Good luck!"}                                  the assistant says it
    {"wait": 5}                                            pause (seconds)
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from core import services

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "memory" / "routines.json"      # tests monkeypatch this
_lock = threading.RLock()
_running: set[str] = set()

# Tools a routine may never call: no recursion, no self-shutdown, nothing that needs a human gate.
_BLOCKED = {"routines", "shutdown_jarvis", "agent_task", "run_python"}
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_MAX_STEPS = 25


# ── storage ──────────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DATA_FILE)          # atomic: a crash can never leave half a file


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


# ── parsing ──────────────────────────────────────────────────────────────────
def parse_steps(raw) -> tuple[list[dict], str]:
    """Normalise whatever the model sent into clean steps. Returns (steps, error)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return [], "steps must be a list."
    if not isinstance(raw, list) or not raw:
        return [], "A routine needs at least one step."
    if len(raw) > _MAX_STEPS:
        return [], f"A routine can have at most {_MAX_STEPS} steps."
    steps = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            return [], f"Step {i} is not valid."
        if s.get("say"):
            steps.append({"say": str(s["say"])[:300]})
        elif s.get("wait_seconds") or s.get("wait"):
            try:
                steps.append({"wait": max(0.0, min(float(s.get("wait_seconds") or s.get("wait")), 300.0))})
            except (TypeError, ValueError):
                return [], f"Step {i}: wait must be a number."
        elif s.get("tool"):
            tool = str(s["tool"]).strip()
            if tool in _BLOCKED:
                return [], f"Step {i}: the tool '{tool}' cannot be used inside a routine."
            args = s.get("args")
            if args is None and s.get("args_json"):
                try:
                    args = json.loads(s["args_json"])
                except Exception:
                    return [], f"Step {i}: args_json is not valid JSON."
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return [], f"Step {i}: args must be an object."
            steps.append({"tool": tool, "args": args})
        else:
            return [], f"Step {i} has no tool, say or wait."
    return steps, ""


def parse_days(raw) -> list[str]:
    if not raw:
        return list(_DAYS)
    text = " ".join(raw) if isinstance(raw, list) else str(raw)
    text = text.lower()
    if "weekday" in text:
        return _DAYS[:5]
    if "weekend" in text:
        return _DAYS[5:]
    if "daily" in text or "every day" in text or "everyday" in text:
        return list(_DAYS)
    found = [d for d in _DAYS if d in text or d in re.sub(r"[^a-z ]", "", text)]
    return found or list(_DAYS)


def parse_time(raw: str) -> str | None:
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", (raw or "").lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


# ── execution ────────────────────────────────────────────────────────────────
def run_routine(name: str, announce: bool = True) -> str:
    """Execute synchronously. Returns a short report of what happened."""
    k = _key(name)
    with _lock:
        data = _load()
        if k not in data:
            close = [n for n in data if k in n or n in k]
            return f"No routine called '{name}'." + (f" Did you mean: {', '.join(close)}?" if close else "")
        if k in _running:
            return f"'{name}' is already running."
        _running.add(k)
        steps = list(data[k]["steps"])
    results = []
    try:
        for i, s in enumerate(steps, 1):
            if "say" in s:
                services.inject(f"[ROUTINE] Say exactly this to the user, nothing else: {s['say']}")
                results.append(f"{i}. said it")
            elif "wait" in s:
                time.sleep(s["wait"])
                results.append(f"{i}. waited {s['wait']:g}s")
            else:
                out = services.dispatch(s["tool"], s["args"])
                results.append(f"{i}. {s['tool']}: {str(out)[:80]}")
                time.sleep(0.6)             # let apps open before the next step
        with _lock:
            data = _load()
            if k in data:
                data[k]["runs"] = data[k].get("runs", 0) + 1
                data[k]["last_run"] = datetime.now().isoformat(timespec="seconds")
                _save(data)
    finally:
        with _lock:
            _running.discard(k)
    services.log(f"SYS: Routine '{name}' finished ({len(steps)} steps).")
    return f"Ran '{name}': " + "; ".join(results)


def _run_async(name: str) -> None:
    threading.Thread(target=run_routine, args=(name,), daemon=True, name=f"routine-{name}").start()


# ── scheduler (started once by core.services.start_all) ──────────────────────
def due_routines(now: datetime, data: dict, fired: dict) -> list[str]:
    """Pure function (unit-tested): which routines should start at `now`."""
    out = []
    stamp = now.strftime("%Y-%m-%d %H:%M")
    for k, r in data.items():
        sch = r.get("schedule")
        if not sch:
            continue
        if _DAYS[now.weekday()] in sch.get("days", _DAYS) and sch.get("time") == now.strftime("%H:%M") and fired.get(k) != stamp:
            out.append(k)
            fired[k] = stamp
    return out


def scheduler_loop() -> None:
    fired: dict[str, str] = {}
    time.sleep(8)                                    # let the live session come up first
    for k, r in list(_load().items()):               # "run at startup" routines
        if r.get("on_startup"):
            _run_async(k)
    while True:
        try:
            for k in due_routines(datetime.now(), _load(), fired):
                services.log(f"SYS: Scheduled routine '{k}' starting.")
                _run_async(k)
        except Exception as e:
            print(f"[Routines] scheduler error: {e}")
        time.sleep(20)


# ── the tool ─────────────────────────────────────────────────────────────────
def routines(parameters: dict, player=None, speak=None) -> str:
    action = str(parameters.get("action", "")).lower().strip()
    name = str(parameters.get("name", "")).strip()

    if action == "list":
        data = _load()
        if not data:
            return "You have no routines yet."
        lines = []
        for k, r in sorted(data.items()):
            sch = r.get("schedule")
            when = f" - scheduled {sch['time']} on {','.join(sch['days'])}" if sch else ""
            lines.append(f"{r.get('name', k)} ({len(r['steps'])} steps{when})")
        return "Your routines: " + "; ".join(lines)

    if not name:
        return "Which routine? Give me its name."

    if action == "create":
        steps, err = parse_steps(parameters.get("steps"))
        if err:
            return f"I could not save the routine: {err}"
        with _lock:
            data = _load()
            old = data.get(_key(name), {})
            data[_key(name)] = {"name": name, "steps": steps, "created": old.get("created", datetime.now().isoformat(timespec="seconds")),
                                "runs": old.get("runs", 0), "schedule": old.get("schedule"), "on_startup": old.get("on_startup", False)}
            _save(data)
        return f"Saved routine '{name}' with {len(steps)} steps."

    if action == "run":
        if _key(name) not in _load():
            return run_routine(name)                 # returns the friendly "no routine" message
        _run_async(name)
        return f"Running '{name}' now."

    if action == "delete":
        with _lock:
            data = _load()
            if _key(name) not in data:
                return f"No routine called '{name}'."
            del data[_key(name)]
            _save(data)
        return f"Deleted routine '{name}'."

    if action == "schedule":
        t = parse_time(parameters.get("time", ""))
        if not t:
            return "I need a time like 9:00 or 6 pm."
        days = parse_days(parameters.get("days"))
        with _lock:
            data = _load()
            if _key(name) not in data:
                return f"No routine called '{name}'."
            data[_key(name)]["schedule"] = {"time": t, "days": days}
            _save(data)
        return f"'{name}' will run at {t} on {', '.join(days)} while the assistant is running."

    if action == "unschedule":
        with _lock:
            data = _load()
            if _key(name) not in data:
                return f"No routine called '{name}'."
            data[_key(name)]["schedule"] = None
            data[_key(name)]["on_startup"] = False
            _save(data)
        return f"'{name}' no longer runs automatically."

    if action == "run_at_startup":
        with _lock:
            data = _load()
            if _key(name) not in data:
                return f"No routine called '{name}'."
            data[_key(name)]["on_startup"] = True
            _save(data)
        return f"'{name}' will run every time the assistant starts."

    return "Use action: create, run, list, delete, schedule, unschedule or run_at_startup."


TOOL = {
    "name": "routines",
    "description": (
        "Create, run and schedule named automations (routines) that chain several other tools. "
        "Use for: 'create a routine called X that does A, B and C' (action=create, steps=[...]), "
        "'run X' / 'start my morning routine' (run), 'what routines do I have' (list), 'delete X', "
        "'run X every weekday at 9' (schedule), 'run X when you start' (run_at_startup). "
        "Each step is {tool, args_json} to call any other tool (args_json is a JSON string of that tool's "
        "arguments, e.g. {\"app_name\":\"chrome\"}), or {say} to speak a line, or {wait_seconds}. "
        "Do NOT use this for a single action."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "create | run | list | delete | schedule | unschedule | run_at_startup"},
            "name": {"type": "STRING", "description": "Routine name, e.g. 'study mode'"},
            "steps": {
                "type": "ARRAY",
                "description": "For create: ordered steps",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "tool": {"type": "STRING", "description": "Name of the tool to call"},
                        "args_json": {"type": "STRING", "description": "JSON string with that tool's arguments"},
                        "say": {"type": "STRING", "description": "Instead of a tool: a sentence to say"},
                        "wait_seconds": {"type": "NUMBER", "description": "Instead of a tool: pause this many seconds"},
                    },
                },
            },
            "time": {"type": "STRING", "description": "For schedule: time of day, e.g. 9:00 or 6 pm"},
            "days": {"type": "STRING", "description": "For schedule: daily, weekdays, weekends, or day names like 'mon wed fri'"},
        },
        "required": ["action"],
    },
    "handler": routines,
}
