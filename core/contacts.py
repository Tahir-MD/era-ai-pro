"""
core/contacts.py - one small address book shared by Gmail, WhatsApp and Instagram.

    "Save Ali's WhatsApp number 0300 1234567"   "Ali's email is ali@gmail.com"   "Ali's instagram is ali.khan"
    then:  "WhatsApp Ali good morning"  /  "email Ali the report"  /  "DM Ali on Instagram"

Stored in memory/contacts.json. Phone numbers are normalised to international form; a number that starts
with 0 gets your default country code (config/api_keys.json -> "default_country_code", default "92" = Pakistan).
"""
from __future__ import annotations

import json
import re
from difflib import get_close_matches
from pathlib import Path

from core.paths import app_dir
BASE_DIR = app_dir()
FILE = BASE_DIR / "memory" / "contacts.json"               # tests monkeypatch
API_CONFIG = BASE_DIR / "config" / "api_keys.json"


def _country() -> str:
    try:
        return re.sub(r"\D", "", str(json.loads(API_CONFIG.read_text(encoding="utf-8")).get("default_country_code", "92"))) or "92"
    except Exception:
        return "92"


def normalize_phone(raw: str, country: str | None = None) -> str | None:
    """'0300-1234567' -> '923001234567'; '+44 7911 123456' -> '447911123456'; junk -> None."""
    s = (raw or "").strip()
    plus = s.startswith("+") or s.startswith("00")
    d = re.sub(r"\D", "", s)
    if s.startswith("00"):
        d = d[2:]
    if not d:
        return None
    if not plus:
        c = country or _country()
        if d.startswith("0"):
            d = c + d.lstrip("0")
        elif not d.startswith(c) and len(d) <= 10:
            d = c + d
    return d if 8 <= len(d) <= 15 else None


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)


def _k(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def upsert(name: str, phone: str = "", email: str = "", instagram: str = "") -> str:
    if not name.strip():
        return "Whose contact is this?"
    data, key = _load(), _k(name)
    c = data.get(key, {"name": name.strip()})
    if phone:
        p = normalize_phone(phone)
        if not p:
            return f"'{phone}' does not look like a valid phone number."
        c["phone"] = p
    if email:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()):
            return f"'{email}' does not look like a valid email address."
        c["email"] = email.strip().lower()
    if instagram:
        c["instagram"] = instagram.strip().lstrip("@").lower()
    data[key] = c
    _save(data)
    return f"Saved {c['name']}: " + ", ".join(f"{k} {v}" for k, v in c.items() if k != "name")


def find(name: str) -> dict | None:
    """Exact, then substring, then fuzzy match - so 'Alee', 'ali bhai' and 'Ali' all find Ali."""
    data, key = _load(), _k(name)
    if not key:
        return None
    if key in data:
        return data[key]
    subs = [k for k in data if key in k or k in key]
    if len(subs) == 1:
        return data[subs[0]]
    if len(subs) > 1:
        return None                                        # ambiguous: let the caller ask which one
    close = get_close_matches(key, list(data), n=1, cutoff=0.8)
    return data[close[0]] if close else None


def ambiguous(name: str) -> list[str]:
    data, key = _load(), _k(name)
    subs = [data[k]["name"] for k in data if key and (key in k or k in key)]
    return subs if len(subs) > 1 and key not in data else []


def remove(name: str) -> str:
    data, key = _load(), _k(name)
    if key in data:
        del data[key]
        _save(data)
        return f"Removed {name}."
    return f"No contact called {name}."


def all_contacts() -> list[dict]:
    return sorted(_load().values(), key=lambda c: c["name"].lower())
