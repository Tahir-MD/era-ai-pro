"""
ERA AI plugin: make_word_document

Say "write my application letter" or "make a report on climate change" and Jarvis
builds a formatted Word file, saves it on the Desktop and opens it. Works offline.
Needs the python docx package:  pip install python-docx
"""

PLUGIN = {
    "name": "make_word_document",
    "description": (
        "Creates a real Word document (.docx) and opens it. Use when the user asks for "
        "a Word file, document, assignment, report, essay, resume, CV, application letter, "
        "cover letter or any written document. Write the complete text yourself before "
        "calling. Choose style 'letter' for letters and applications, 'resume' for a resume "
        "or CV, and 'report' for everything else. Do NOT use file_processor, that tool only "
        "reads or converts existing files."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING", "description": "Document title, or the person's full name for a resume. Leave empty for a letter."},
            "subtitle": {"type": "STRING", "description": "Line under the title, for example the course, date or the contact details for a resume"},
            "style": {"type": "STRING", "description": "One of: report, letter, resume"},
            "file_name": {"type": "STRING", "description": "Short file name without extension"},
            "sections": {
                "type": "ARRAY",
                "description": "Sections in order",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "heading": {"type": "STRING", "description": "Section heading, may be empty for letters"},
                        "paragraphs": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Paragraphs of text"},
                        "bullets": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Bullet points"},
                    },
                },
            },
        },
        "required": ["sections"],
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



_RTL_HINT = re.compile(r"[\u0600-\u06FF]")


def _bottom_border(paragraph, colour="0096C7"):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "8")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), colour)
    borders.append(bottom)
    p_pr.append(borders)


def _make_rtl(paragraph):
    """Right to left layout for Urdu or Arabic text."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    p_pr = paragraph._p.get_or_add_pPr()
    bidi = OxmlElement("w:bidi")
    p_pr.append(bidi)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in paragraph.runs:
        r_pr = run._r.get_or_add_rPr()
        r_pr.append(OxmlElement("w:rtl"))


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches, Pt, RGBColor
    except ImportError:
        return "I need the python docx package for this. Please run: pip install python-docx"

    try:
        sections = parameters.get("sections") or []
        if not sections:
            return "I need some content to write into the document."
        title = str(parameters.get("title", "")).strip()
        subtitle = str(parameters.get("subtitle", "")).strip()
        style = str(parameters.get("style", "report")).strip().lower()
        if style not in ("report", "letter", "resume"):
            style = "report"

        document = Document()
        normal = document.styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(11)
        margin = Inches(0.8 if style == "resume" else 1)
        for section in document.sections:
            section.left_margin = section.right_margin = margin
            section.top_margin = section.bottom_margin = margin

        dark = RGBColor(0x16, 0x1E, 0x33)
        accent = RGBColor(0x00, 0x96, 0xC7)

        if title and style != "letter":
            p = document.add_paragraph()
            run_ = p.add_run(title)
            run_.bold = True
            run_.font.size = Pt(24 if style == "resume" else 26)
            run_.font.color.rgb = dark
            if style == "resume":
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(2)
            if _RTL_HINT.search(title):
                _make_rtl(p)
            if subtitle:
                sp = document.add_paragraph()
                sr = sp.add_run(subtitle)
                sr.font.size = Pt(11)
                sr.font.color.rgb = RGBColor(0x5A, 0x64, 0x7A)
                if style == "resume":
                    sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                sp.paragraph_format.space_after = Pt(10)
                if _RTL_HINT.search(subtitle):
                    _make_rtl(sp)

        for item in sections:
            heading = str(item.get("heading", "") or "").strip()
            paragraphs = [str(t).strip() for t in (item.get("paragraphs") or []) if str(t).strip()]
            bullets = [str(t).strip() for t in (item.get("bullets") or []) if str(t).strip()]

            if heading:
                hp = document.add_paragraph()
                hr = hp.add_run(heading.upper() if style == "resume" else heading)
                hr.bold = True
                hr.font.size = Pt(13 if style == "resume" else 16)
                hr.font.color.rgb = accent if style != "letter" else dark
                hp.paragraph_format.space_before = Pt(12)
                hp.paragraph_format.space_after = Pt(4)
                hp.paragraph_format.keep_with_next = True
                if style == "resume":
                    _bottom_border(hp)
                if _RTL_HINT.search(heading):
                    _make_rtl(hp)

            for text in paragraphs:
                para = document.add_paragraph(text)
                para.paragraph_format.space_after = Pt(8 if style != "resume" else 4)
                para.paragraph_format.line_spacing = 1.15
                if _RTL_HINT.search(text):
                    _make_rtl(para)

            for text in bullets:
                bp = document.add_paragraph(text, style="List Bullet")
                bp.paragraph_format.space_after = Pt(3)
                if _RTL_HINT.search(text):
                    _make_rtl(bp)

        stem = _safe_name(parameters.get("file_name") or title or ("Letter" if style == "letter" else "Document"), "Document")
        path = _unique_path(_desktop_folder(), stem, ".docx")
        document.save(str(path))
        _open_file(path)

        message = f"Your Word document is ready. I saved it as {path.name} on the {path.parent.name} folder."
        _log(player, message)
        return message
    except Exception as error:
        return f"Sorry, I could not build the Word document: {error}"
