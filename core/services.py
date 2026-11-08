"""
core/services.py - the bridge between background features and the live Gemini session.

WHY
    New advanced features (routines, the Telegram remote, code runner results, modes) need to
    talk to the assistant WITHOUT importing main.py (which would be circular and would drag in
    PyQt). main.py binds a handful of callables here once at startup; everything else just calls
    ``services.submit_text(...)`` / ``services.dispatch(...)``.

    Every function is safe to call before bind() (it returns a harmless default) - that is what
    lets the feature modules be unit-tested without a running assistant.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

_hooks: dict[str, Optional[Callable]] = {
    "dispatch": None,    # (name, args) -> str      run any action/plugin tool synchronously
    "submit": None,      # (text) -> None           type a command to the assistant as if the user said it
    "inject": None,      # (text) -> None           feed text to the model (spoken answer expected)
    "reconnect": None,   # (keep_context, reason)   rebuild the live session (new system prompt)
    "log": None,         # (line) -> None           HUD activity log
    "screenshot": None,  # () -> bytes | None       PNG/JPEG bytes of the screen
}
_output_listeners: list[Callable[[str], None]] = []
_user_listeners: list[Callable[[str], None]] = []
_started: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def bind(**hooks) -> None:
    for k, v in hooks.items():
        if k not in _hooks:
            raise KeyError(f"unknown service hook: {k}")
        _hooks[k] = v


def log(line: str) -> None:
    print(f"[Services] {line}")
    fn = _hooks["log"]
    if fn:
        try:
            fn(line)
        except Exception:
            pass


def dispatch(name: str, args: dict | None = None) -> str:
    fn = _hooks["dispatch"]
    if not fn:
        return f"Cannot run '{name}': the assistant is not ready."
    return str(fn(name, args or {}))


def submit_text(text: str) -> bool:
    fn = _hooks["submit"]
    if not fn:
        return False
    try:
        fn(text)
        return True
    except Exception as e:
        log(f"SYS: submit failed: {e}")
        return False


def inject(text: str) -> bool:
    fn = _hooks["inject"]
    if not fn:
        return False
    try:
        fn(text)
        return True
    except Exception as e:
        log(f"SYS: inject failed: {e}")
        return False


def request_reconnect(keep_context: bool = True, reason: str = "") -> bool:
    fn = _hooks["reconnect"]
    if not fn:
        return False
    try:
        fn(keep_context=keep_context, reason=reason)
        return True
    except Exception:
        return False


def screenshot() -> bytes | None:
    fn = _hooks["screenshot"]
    try:
        return fn() if fn else None
    except Exception:
        return None


# ── conversation listeners (Telegram mirror etc.) ────────────────────────────
def on_output(fn: Callable[[str], None]) -> None:
    with _lock:
        if fn not in _output_listeners:
            _output_listeners.append(fn)


def on_user(fn: Callable[[str], None]) -> None:
    with _lock:
        if fn not in _user_listeners:
            _user_listeners.append(fn)


def emit_output(text: str) -> None:
    with _lock:
        subs = list(_output_listeners)
    for fn in subs:
        try:
            fn(text)
        except Exception as e:
            print(f"[Services] output listener error: {e}")


def emit_user(text: str) -> None:
    with _lock:
        subs = list(_user_listeners)
    for fn in subs:
        try:
            fn(text)
        except Exception as e:
            print(f"[Services] user listener error: {e}")


# ── background services ──────────────────────────────────────────────────────
def start_service(name: str, target: Callable[[], None]) -> bool:
    """Start `target` on a daemon thread exactly once per process (a Gemini reconnect re-runs
    main.run's setup, which must never spawn a second Telegram poller)."""
    with _lock:
        t = _started.get(name)
        if t and t.is_alive():
            return False
        t = threading.Thread(target=target, daemon=True, name=f"svc-{name}")
        _started[name] = t
    t.start()
    return True


def start_all() -> None:
    """Called once by main.run(). Each service decides for itself whether it is configured."""
    try:
        from actions import routines
        start_service("routines", routines.scheduler_loop)
    except Exception as e:
        log(f"SYS: routines scheduler not started: {e}")
    try:
        from core import telegram_bridge
        if telegram_bridge.is_configured():
            start_service("telegram", telegram_bridge.run_forever)
    except Exception as e:
        log(f"SYS: telegram bridge not started: {e}")
