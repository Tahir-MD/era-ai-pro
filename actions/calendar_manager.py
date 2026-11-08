"""
actions/calendar_manager.py - Google Calendar by voice (official API).

    "What's on my calendar today?"                 "What is my next meeting?"
    "Schedule a meeting with Ali tomorrow at 3 pm for one hour, add a Google Meet link"
    "Remind me every Monday at 9 about the team standup"     (recurring)
    "When am I free tomorrow for 45 minutes?"
    "Move the dentist appointment to Friday 5 pm"            "Cancel the lunch with Sara" (asks you to confirm)
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time as dtime, timedelta

from core import confirm as confirm_gate
from core import contacts, google_auth, services

_RRULES = {"daily": "RRULE:FREQ=DAILY", "weekly": "RRULE:FREQ=WEEKLY", "monthly": "RRULE:FREQ=MONTHLY", "yearly": "RRULE:FREQ=YEARLY",
           "weekdays": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── time helpers (pure, unit-tested) ─────────────────────────────────────────
def parse_dt(raw: str, base: datetime | None = None) -> tuple[datetime, bool]:
    """(aware datetime, date_only). Accepts ISO date/datetime or 'HH:MM' (= today). Naive means the user's local time."""
    s = (raw or "").strip().replace("Z", "+00:00")
    if not s:
        raise ValueError("empty time")
    base = base or datetime.now().astimezone()
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        h, m = map(int, s.split(":"))
        return base.replace(hour=h, minute=m, second=0, microsecond=0), False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.combine(date.fromisoformat(s), dtime(0, 0)).astimezone(), True
    dt = datetime.fromisoformat(s.replace(" ", "T", 1))
    return (dt if dt.tzinfo else dt.astimezone()), False


def day_range(when: str, days: int = 0, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    now = now or datetime.now().astimezone()
    w = (when or "today").strip().lower()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if w in ("today", ""):
        label, span = "today", 1
    elif w == "tomorrow":
        start += timedelta(days=1); label, span = "tomorrow", 1
    elif w in ("week", "this week", "next 7 days"):
        label, span = "the next 7 days", 7
    elif w == "next week":
        start += timedelta(days=(7 - start.weekday())); label, span = "next week", 7
    else:
        d, _ = parse_dt(w)
        start = d.replace(hour=0, minute=0); label, span = d.strftime("%A %d %B"), 1
    span = days or span
    return start, start + timedelta(days=span), label


def fmt_event(e: dict) -> str:
    s, en = e.get("start", {}), e.get("end", {})
    title = e.get("summary", "(no title)")
    if "date" in s:
        d = date.fromisoformat(s["date"])
        when = d.strftime("%a %d %b") + ", all day"
    else:
        a = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")).astimezone()
        b = datetime.fromisoformat(en["dateTime"].replace("Z", "+00:00")).astimezone() if "dateTime" in en else a
        when = a.strftime("%a %d %b, %I:%M %p").replace(" 0", " ") + (" to " + b.strftime("%I:%M %p").lstrip("0") if b != a else "")
    loc = f" at {e['location']}" if e.get("location") else ""
    return f"{when}: {title}{loc}"


def free_slots(events: list[dict], day: datetime, minutes: int, start_hour: int = 9, end_hour: int = 18) -> list[tuple[datetime, datetime]]:
    """Gaps of at least `minutes` inside working hours. All-day events are ignored (they do not block time)."""
    ws = day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    we = day.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    busy = []
    for e in events:
        if "dateTime" not in e.get("start", {}):
            continue
        a = datetime.fromisoformat(e["start"]["dateTime"].replace("Z", "+00:00")).astimezone()
        b = datetime.fromisoformat(e["end"]["dateTime"].replace("Z", "+00:00")).astimezone()
        if b > ws and a < we:
            busy.append((max(a, ws), min(b, we)))
    busy.sort()
    gaps, cur = [], ws
    for a, b in busy:
        if a - cur >= timedelta(minutes=minutes):
            gaps.append((cur, a))
        cur = max(cur, b)
    if we - cur >= timedelta(minutes=minutes):
        gaps.append((cur, we))
    return gaps


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _resolve_attendees(raw: str) -> tuple[list[dict], str]:
    out = []
    for part in re.split(r"[;,]", raw or ""):
        p = part.strip()
        if not p:
            continue
        if _EMAIL.match(p):
            out.append({"email": p})
            continue
        c = contacts.find(p)
        if c and c.get("email"):
            out.append({"email": c["email"]})
        else:
            return [], f"I do not have an email address for {p}. Tell me their email and I will save it."
    return out, ""


def _events(svc, start: datetime, end: datetime, q: str = "", n: int = 25) -> list[dict]:
    kw = dict(calendarId="primary", timeMin=_iso(start), timeMax=_iso(end), singleEvents=True, orderBy="startTime", maxResults=n)
    if q:
        kw["q"] = q
    return svc.events().list(**kw).execute().get("items", [])


def _match(svc, query: str):
    """Find ONE upcoming event by words in its title; returns (event|None, message)."""
    now = datetime.now().astimezone()
    items = [e for e in _events(svc, now - timedelta(hours=6), now + timedelta(days=90), query, 10) if e.get("status") != "cancelled"]
    if not items:
        return None, f"I could not find an event matching '{query}'."
    exact = [e for e in items if e.get("summary", "").strip().lower() == query.strip().lower()]
    pick = exact or ([items[0]] if len({e.get("summary", "").lower() for e in items}) == 1 else [])
    if not pick:
        return None, "Which one? " + "; ".join(fmt_event(e) for e in items[:4])
    return pick[0], ""


# ── the tool ─────────────────────────────────────────────────────────────────
def calendar_manager(parameters: dict, player=None, speak=None) -> str:
    action = str(parameters.get("action", "agenda")).lower().strip()
    p = parameters
    try:
        if action == "connect":
            return google_auth.connect(on_done=lambda m: services.inject(f"[GOOGLE] {m} Tell the user in one short sentence."))
        if action == "disconnect":
            return google_auth.disconnect()
        svc = google_auth.service("calendar", "v3")

        if action in ("agenda", "list", "today", "tomorrow"):
            when = p.get("when") or (action if action in ("today", "tomorrow") else "today")
            a, b, label = day_range(when, int(p.get("days") or 0))
            ev = [e for e in _events(svc, a, b) if e.get("status") != "cancelled"]
            return f"Nothing scheduled for {label}." if not ev else f"{len(ev)} event(s) for {label}:\n" + "\n".join(fmt_event(e) for e in ev)

        if action == "next":
            now = datetime.now().astimezone()
            ev = [e for e in _events(svc, now, now + timedelta(days=60), n=5) if e.get("status") != "cancelled"]
            return "Next up: " + fmt_event(ev[0]) if ev else "You have nothing coming up in the next two months."

        if action == "create":
            title = str(p.get("title", "")).strip()
            if not title or not p.get("start"):
                return "I need at least a title and a start time."
            start, date_only = parse_dt(p["start"])
            body: dict = {"summary": title}
            if date_only:
                body["start"] = {"date": start.date().isoformat()}
                body["end"] = {"date": (parse_dt(p["end"])[0].date() if p.get("end") else start.date() + timedelta(days=1)).isoformat()}
                end = start + timedelta(days=1)
            else:
                end = parse_dt(p["end"])[0] if p.get("end") else start + timedelta(minutes=int(p.get("duration_minutes") or 60))
                if end <= start:
                    return "The end time must be after the start time."
                body["start"], body["end"] = {"dateTime": _iso(start)}, {"dateTime": _iso(end)}
            for key, field in (("location", "location"), ("description", "description")):
                if p.get(key):
                    body[field] = str(p[key])
            att, err = _resolve_attendees(p.get("attendees", ""))
            if err:
                return err
            if att:
                body["attendees"] = att
            if p.get("reminder_minutes") not in (None, ""):
                body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": int(p["reminder_minutes"])}]}
            rep = str(p.get("repeat", "")).strip()
            if rep:
                rule = _RRULES.get(rep.lower(), rep if rep.upper().startswith("RRULE:") else None)
                if not rule:
                    return "Repeat must be daily, weekly, monthly, yearly, weekdays or an RRULE."
                if p.get("repeat_count"):
                    rule += f";COUNT={int(p['repeat_count'])}"
                body["recurrence"] = [rule]
            if p.get("add_meet"):
                body["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
            conflicts = [] if date_only else [e for e in _events(svc, start, end) if "dateTime" in e.get("start", {}) and e.get("status") != "cancelled"]
            made = svc.events().insert(calendarId="primary", body=body, sendUpdates="all" if att else "none",
                                       conferenceDataVersion=1 if p.get("add_meet") else 0).execute()
            msg = f"Created: {fmt_event(made) if made.get('start') else title}."
            if made.get("hangoutLink"):
                msg += f" Meet link: {made['hangoutLink']}"
            if att:
                msg += f" Invitation sent to {', '.join(a['email'] for a in att)}."
            if conflicts:
                msg += " Heads up, it overlaps with " + "; ".join(e.get("summary", "another event") for e in conflicts[:3]) + "."
            return msg

        if action in ("free_time", "free"):
            day, _, label = day_range(p.get("when") or "today")
            ev = _events(svc, day, day + timedelta(days=1))
            gaps = free_slots(ev, day, int(p.get("duration_minutes") or 30), int(p.get("start_hour") or 9), int(p.get("end_hour") or 18))
            if not gaps:
                return f"No free slot of {int(p.get('duration_minutes') or 30)} minutes {label} between working hours."
            return f"Free {label}: " + ", ".join(f"{a.strftime('%I:%M %p').lstrip('0')} to {b.strftime('%I:%M %p').lstrip('0')}" for a, b in gaps[:5])

        if action == "find":
            now = datetime.now().astimezone()
            ev = _events(svc, now - timedelta(days=1), now + timedelta(days=120), str(p.get("title", "")), 6)
            return "\n".join(fmt_event(e) for e in ev) or "No matching events."

        if action in ("reschedule", "move", "update"):
            e, msg = _match(svc, str(p.get("title", "")))
            if not e:
                return msg
            patch: dict = {}
            if p.get("start"):
                s, _ = parse_dt(p["start"])
                old_s = datetime.fromisoformat(e["start"]["dateTime"].replace("Z", "+00:00")) if "dateTime" in e["start"] else s
                old_e = datetime.fromisoformat(e["end"]["dateTime"].replace("Z", "+00:00")) if "dateTime" in e["end"] else s + timedelta(hours=1)
                dur = timedelta(minutes=int(p["duration_minutes"])) if p.get("duration_minutes") else (old_e - old_s)
                patch["start"], patch["end"] = {"dateTime": _iso(s)}, {"dateTime": _iso(s + dur)}
            if p.get("new_title"):
                patch["summary"] = p["new_title"]
            if p.get("location"):
                patch["location"] = p["location"]
            if not patch:
                return "Tell me the new time, title or location."
            done = svc.events().patch(calendarId="primary", eventId=e["id"], body=patch, sendUpdates="all").execute()
            return "Updated: " + fmt_event(done)

        if action in ("delete", "cancel"):
            e, msg = _match(svc, str(p.get("title", "")))
            if not e:
                return msg

            def go() -> str:
                svc.events().delete(calendarId="primary", eventId=e["id"], sendUpdates="all").execute()
                services.inject(f"[CALENDAR] Deleted the event '{e.get('summary')}'. Tell the user in one short sentence.")
                return "deleted"
            return confirm_gate.request(key="calendar_delete", title=f"Delete: {e.get('summary', 'event')}", detail=fmt_event(e), run=go)

        return "Use action: agenda, next, create, free_time, find, reschedule, delete, connect."
    except google_auth.NotConnected as e:
        return str(e)
    except ValueError as e:
        return f"I could not understand that date or time ({e}). Use a format like 2026-11-09T15:00."
    except Exception as e:
        return f"Calendar problem: {str(e)[:200]}"


TOOL = {
    "name": "calendar_manager",
    "description": (
        "The user's Google Calendar. Use for ANY schedule question or change: 'what's on today / tomorrow / this week', 'what's my next meeting', "
        "'schedule/add/book a meeting/appointment/exam/reminder at <time>' (action=create), 'when am I free', 'move/reschedule X', "
        "'cancel/delete X' (asks the user to confirm), recurring events, Google Meet links, inviting people by name or email. "
        "Always pass start/end as ISO 8601 local time (e.g. 2026-11-09T15:00) computed from the current date and time you were given. "
        "First time use: action=connect. For one-off spoken timers use the reminder tool instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "agenda (default) | next | create | free_time | find | reschedule | delete | connect | disconnect"},
            "when": {"type": "STRING", "description": "agenda/free_time: today | tomorrow | week | next week | a date like 2026-11-09"},
            "days": {"type": "NUMBER", "description": "agenda: number of days to show"},
            "title": {"type": "STRING", "description": "Event title (create) or words to find an existing event (find/reschedule/delete)"},
            "start": {"type": "STRING", "description": "ISO start, e.g. 2026-11-09T15:00 (date only 2026-11-09 for all-day)"},
            "end": {"type": "STRING", "description": "ISO end (optional)"},
            "duration_minutes": {"type": "NUMBER", "description": "Length if no end is given (default 60)"},
            "location": {"type": "STRING", "description": "Place"},
            "description": {"type": "STRING", "description": "Notes"},
            "attendees": {"type": "STRING", "description": "Comma-separated emails or saved contact names to invite"},
            "add_meet": {"type": "BOOLEAN", "description": "Add a Google Meet video link"},
            "reminder_minutes": {"type": "NUMBER", "description": "Popup reminder this many minutes before"},
            "repeat": {"type": "STRING", "description": "daily | weekly | monthly | yearly | weekdays"},
            "repeat_count": {"type": "NUMBER", "description": "Stop repeating after N times"},
            "new_title": {"type": "STRING", "description": "reschedule: new title"},
            "start_hour": {"type": "NUMBER", "description": "free_time: working day start hour (default 9)"},
            "end_hour": {"type": "NUMBER", "description": "free_time: working day end hour (default 18)"},
        },
        "required": ["action"],
    },
    "handler": calendar_manager,
}
