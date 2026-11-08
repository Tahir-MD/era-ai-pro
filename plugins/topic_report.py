"""
ERA AI plugin: topic_report

A short spoken news summary, or a full written report saved as a Word file, built
from live web search by Gemini. Needs internet. Word output needs:  pip install python-docx
For a spoken summary every morning, turn on the Morning Briefing that already comes with ERA AI.
"""

PLUGIN = {
    "name": "topic_report",
    "description": (
        "Writes a report or news summary on a topic using live web search. kind 'news' gives a short "
        "spoken summary of the latest headlines on a topic, kind 'report' with length 'short' gives a "
        "short spoken summary, and length 'detailed' saves a full report as a Word file on the Desktop. "
        "Use for 'give me today's news on cricket', 'report on solar energy', 'research report on AI in "
        "healthcare'. Needs internet. For a quick single fact use web_search instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {"type": "STRING", "description": "Topic of the news or report. For general news use top world news."},
            "kind": {"type": "STRING", "description": "news or report. Default report."},
            "length": {"type": "STRING", "description": "short (spoken) or detailed (saved as a Word file). Default short."},
        },
        "required": ["topic"],
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


def _gemini(prompt: str, json_mode: bool = False, search: bool = False) -> str:
    from google import genai

    key = json.loads((_BASE / "config" / "api_keys.json").read_text(encoding="utf-8"))["gemini_api_key"]
    client = genai.Client(api_key=key)
    config = {}
    if json_mode:
        config["response_mime_type"] = "application/json"
    if search:
        config["tools"] = [{"google_search": {}}]
    response = client.models.generate_content(
        model="gemini-flash-latest", contents=prompt, config=config or None
    )
    text = ""
    for part in response.candidates[0].content.parts:
        if getattr(part, "text", None):
            text += part.text
    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty answer.")
    return text


def _parse_json(text: str):
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)




def _plain(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.M)
    return re.sub(r"\n{2,}", "\n", text).strip()


def _add_runs(paragraph, text: str):
    for i, piece in enumerate(re.split(r"\*\*", text)):
        if piece:
            run_ = paragraph.add_run(piece)
            run_.bold = (i % 2 == 1)


def _save_report(title: str, text: str):
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
    except ImportError:
        path = _unique_path(_desktop_folder(), _safe_name(title, "Report"), ".txt")
        path.write_text(_plain(text), encoding="utf-8")
        return path, "I saved a text file because python docx is missing."
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    head = doc.add_paragraph()
    hr = head.add_run(title)
    hr.bold = True
    hr.font.size = Pt(24)
    hr.font.color.rgb = RGBColor(0x16, 0x1E, 0x33)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.match(r"^(#{1,3})\s*(.+)$", line)
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if heading:
            p = doc.add_paragraph()
            r = p.add_run(re.sub(r"\*\*", "", heading.group(2)))
            r.bold = True
            r.font.size = Pt(16 if len(heading.group(1)) == 1 else 13)
            r.font.color.rgb = RGBColor(0x00, 0x96, 0xC7)
            p.paragraph_format.space_before = Pt(12)
            p.paragraph_format.keep_with_next = True
        elif bullet:
            _add_runs(doc.add_paragraph(style="List Bullet"), bullet.group(1))
        else:
            p = doc.add_paragraph()
            _add_runs(p, line)
            p.paragraph_format.space_after = Pt(8)
    path = _unique_path(_desktop_folder(), _safe_name(title, "Report"), ".docx")
    doc.save(str(path))
    return path, ""


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        topic = str(parameters.get("topic", "") or "").strip() or "top world news"
        kind = str(parameters.get("kind", "report") or "report").strip().lower()
        length = str(parameters.get("length", "short") or "short").strip().lower()
        today = time.strftime("%d %B %Y")

        if kind == "news":
            prompt = (
                f"Today is {today}. Search the web and give the 5 most important latest news items about: {topic}. "
                "Write each as one plain sentence a person can listen to. No links, no markdown, no numbering symbols."
            )
            return _plain(_gemini(prompt, search=True))

        if length != "detailed":
            prompt = (
                f"Today is {today}. Search the web and explain this topic in about 120 words of plain spoken English, "
                f"easy for a student: {topic}. No markdown, no links, no bullet symbols."
            )
            return _plain(_gemini(prompt, search=True))

        prompt = (
            f"Today is {today}. Search the web and write a clear, well organised report for a university student on: {topic}. "
            "Use this layout with markdown: a short introduction paragraph, then sections each starting with '## ' and "
            "containing short paragraphs or '- ' bullet points, then a '## Conclusion' section, then a '## Key facts' bullet list. "
            "Be accurate and simple. Do not include links."
        )
        text = _gemini(prompt, search=True)
        path, note = _save_report(f"Report {topic}"[:60], text)
        _open_file(path)
        message = f"Your report on {topic} is saved as {path.name} on your Desktop. {note}".strip()
        _log(player, message)
        return message
    except Exception as error:
        return f"Sorry, I could not write the report: {error}. This feature needs an internet connection."
