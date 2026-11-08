"""
ERA AI plugin: send_email_gmail

Send an email from your Gmail by voice. It always waits for your CONFIRM button on
the screen before anything is sent. Needs internet.

ONE TIME SETUP
  1. In your Google account turn on 2 step verification.
  2. In your Google account search for "App passwords" and make one for Jarvis.
  3. In Jarvis open the settings, then PLUGIN SETTINGS, and fill in your Gmail
     address and that app password. Optionally add contacts like  ali=ali@gmail.com
"""

PLUGIN = {
    "name": "send_email_gmail",
    "description": (
        "Sends an email from the user's Gmail. Use for 'send an email to Ali', 'mail my teacher'. "
        "Write a clear subject and a polite complete body yourself from what the user said. "
        "The user must press a confirm button on the screen before it is sent, so after calling "
        "this tool tell the user to confirm. 'to' can be an email address or a saved contact name. "
        "Use action check_setup to see whether Gmail is set up. Do NOT use send_message for email."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "send (default) or check_setup"},
            "to": {"type": "STRING", "description": "Email address or saved contact name. Several can be separated by commas."},
            "subject": {"type": "STRING", "description": "Email subject"},
            "body": {"type": "STRING", "description": "Full email text"},
            "attachment_name": {"type": "STRING", "description": "Optional file name to attach, found on Desktop, Downloads or Documents"},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "gmail",
    "title": "Gmail (send email by voice)",
    "fields": [
        {"key": "email", "label": "Your Gmail address", "type": "text", "placeholder": "name@gmail.com"},
        {"key": "app_password", "label": "Gmail app password", "type": "password", "placeholder": "16 letter app password"},
        {"key": "contacts", "label": "Contacts (name=email, separated by commas)", "type": "text", "placeholder": "ali=ali@gmail.com, sir=teacher@uni.edu"},
    ],
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


def _find_file(name) -> "Path | None":
    raw = Path(str(name).strip().strip('"'))
    if raw.is_file():
        return raw
    folders = (_desktop_folder(), Path.home() / "Downloads", Path.home() / "Documents", _BASE)
    for folder in folders:
        candidate = folder / raw.name
        if candidate.is_file():
            return candidate
    def squash(value: str) -> str:
        return re.sub(r"[\s_\-]+", "", value.lower())

    wanted = squash(raw.stem if raw.suffix else raw.name)
    if wanted:
        for folder in folders:
            try:
                for item in folder.iterdir():
                    if item.is_file() and wanted in squash(item.stem):
                        return item
            except Exception:
                continue
    return None


def _read_source(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".csv", ".json", ".html", ".py"):
        return path.read_text(encoding="utf-8", errors="ignore")
    if ext == ".docx":
        try:
            from docx import Document
        except ImportError:
            raise ValueError("I need the python docx package to read Word files. Please run: pip install python-docx")
        document = Document(str(path))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    if ext == ".pptx":
        from pptx import Presentation
        parts = []
        for slide in Presentation(str(path)).slides:
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                    parts.append(shape.text_frame.text)
        return "\n".join(parts)
    if ext == ".pdf":
        for module_name in ("pdfplumber", "pypdf", "PyPDF2"):
            try:
                if module_name == "pdfplumber":
                    import pdfplumber
                    with pdfplumber.open(str(path)) as pdf:
                        return "\n".join((page.extract_text() or "") for page in pdf.pages)
                if module_name == "pypdf":
                    import pypdf
                    return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(str(path)).pages)
                import PyPDF2
                return "\n".join((p.extract_text() or "") for p in PyPDF2.PdfReader(str(path)).pages)
            except ImportError:
                continue
        raise ValueError("I need a PDF reader package. Please run: pip install pypdf")
    raise ValueError(f"I cannot read {ext} files yet.")



import smtplib
from email.message import EmailMessage

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config("gmail")
    except Exception:
        try:
            cfg = json.loads((_BASE / "config" / "api_keys.json").read_text(encoding="utf-8"))
            return dict(cfg.get("plugin_config", {}).get("gmail", {}))
        except Exception:
            return {}


def _contacts(raw: str) -> dict:
    book = {}
    for part in re.split(r"[,;\n]", raw or ""):
        if "=" in part:
            name, address = part.split("=", 1)
            if name.strip() and address.strip():
                book[name.strip().lower()] = address.strip()
    return book


def _resolve(to: str, book: dict):
    addresses, unknown = [], []
    for item in re.split(r"[,;]", to):
        item = item.strip()
        if not item:
            continue
        if _EMAIL_RE.match(item):
            addresses.append(item)
            continue
        key = item.lower()
        match = book.get(key) or next((a for n, a in book.items() if key in n or n in key), None)
        (addresses if match else unknown).append(match or item)
    return addresses, unknown


def _send(sender: str, password: str, recipients: list, subject: str, body: str, attachment):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    if attachment:
        import mimetypes
        mime, _ = mimetypes.guess_type(str(attachment))
        main, sub = (mime or "application/octet-stream").split("/", 1)
        message.add_attachment(attachment.read_bytes(), maintype=main, subtype=sub, filename=attachment.name)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(sender, password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        return "Gmail refused the login. Check the address and the app password in Plugin Settings."
    except Exception as error:
        return f"The email could not be sent: {error}. Please check your internet."
    return f"Email sent to {', '.join(recipients)}."


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        action = str(parameters.get("action", "send") or "send").strip().lower()
        cfg = _settings()
        sender = str(cfg.get("email", "") or "").strip()
        password = str(cfg.get("app_password", "") or "").replace(" ", "").strip()
        configured = bool(_EMAIL_RE.match(sender) and password)

        if action == "check_setup":
            return ("Gmail is set up and ready." if configured else
                    "Gmail is not set up yet. Open the settings, then Plugin Settings, and enter your Gmail address and app password.")
        if not configured:
            return ("Gmail is not set up yet. Open the settings, then Plugin Settings, and enter your Gmail address "
                    "and a Gmail app password. You need 2 step verification turned on in your Google account for that.")

        to = str(parameters.get("to", "") or "").strip()
        subject = str(parameters.get("subject", "") or "").strip() or "(no subject)"
        body = str(parameters.get("body", "") or "").strip()
        if not to:
            return "Who should I send the email to?"
        if not body:
            return "What should the email say?"

        recipients, unknown = _resolve(to, _contacts(str(cfg.get("contacts", "") or "")))
        if unknown or not recipients:
            return (f"I do not have an email address for {', '.join(unknown) or to}. "
                    "Tell me the address, or add the contact in Plugin Settings.")

        attachment = None
        name = str(parameters.get("attachment_name", "") or "").strip()
        if name:
            attachment = _find_file(name)
            if not attachment:
                return f"I could not find a file called {name} to attach."
            if attachment.stat().st_size > 20 * 1024 * 1024:
                return "That file is bigger than 20 megabytes, which Gmail does not allow."

        try:
            from core import confirm
        except Exception:
            return "I cannot ask for your confirmation right now, so I did not send anything."

        preview = body if len(body) <= 160 else body[:160] + "..."
        detail = f"To: {', '.join(recipients)}  |  Subject: {subject}  |  {preview}"
        return confirm.request(
            key="send_email",
            title=f"Send email to {', '.join(recipients)}",
            detail=detail,
            run=lambda: _send(sender, password, recipients, subject, body, attachment),
        )
    except Exception as error:
        return f"Sorry, the email feature failed: {error}"
