"""actions/contacts.py - voice access to the shared address book (core/contacts.py)."""
from __future__ import annotations

from core import contacts as book


# ── the tool ─────────────────────────────────────────────────────────────────
def contacts_tool(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "lookup")).lower().strip()
    name = str(parameters.get("name", "")).strip()
    if action in ("add", "save", "update"):
        return book.upsert(name, parameters.get("phone", ""), parameters.get("email", ""), parameters.get("instagram", ""))
    if action == "list":
        cs = book.all_contacts()
        return ("Contacts: " + ", ".join(c["name"] for c in cs)) if cs else "You have no saved contacts."
    if action in ("delete", "remove"):
        return book.remove(name)
    c = book.find(name)
    if not c:
        amb = book.ambiguous(name)
        return f"Which one: {', '.join(amb)}?" if amb else f"I have no contact called {name}."
    return f"{c['name']}: " + ", ".join(f"{k} {v}" for k, v in c.items() if k != "name")


TOOL = {
    "name": "contacts",
    "description": (
        "The user's address book used by gmail, messenger and calendar. Save or look up a person's phone number, email and "
        "Instagram handle: 'save Ali's number 0300 1234567', 'Ali's email is ...', 'what is Ali's number', 'list my contacts'. "
        "Call it whenever the user gives you a new phone/email/handle for someone."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "add | lookup (default) | list | delete"},
            "name": {"type": "STRING", "description": "Person's name"},
            "phone": {"type": "STRING", "description": "Phone number, any format"},
            "email": {"type": "STRING", "description": "Email address"},
            "instagram": {"type": "STRING", "description": "Instagram username"},
        },
        "required": ["action"],
    },
    "handler": contacts_tool,
}
