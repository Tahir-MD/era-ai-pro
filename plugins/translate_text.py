"""
ERA AI plugin: translate_text

Translate a sentence or a whole file, for example Urdu to English or English to
Urdu. Needs internet because Gemini does the translation. Urdu and Arabic files
are saved right to left. Word output needs:  pip install python-docx
"""

PLUGIN = {
    "name": "translate_text",
    "description": (
        "Translates text or a whole file into another language, for example Urdu to English, "
        "English to Urdu, Hindi, Arabic or French. Use for 'translate this', 'iska tarjuma karo', "
        "'translate my file'. Short text is spoken back, long text or a file is saved as a Word file "
        "on the Desktop. Needs internet."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "text": {"type": "STRING", "description": "The text to translate"},
            "file_name": {"type": "STRING", "description": "Name or path of a file to translate instead of text (pdf, docx, pptx, txt)"},
            "target_language": {"type": "STRING", "description": "Language to translate into, for example Urdu or English"},
            "source_language": {"type": "STRING", "description": "Original language, optional, detected automatically if empty"},
        },
        "required": ["target_language"],
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



_RTL_LANGS = ("urdu", "arabic", "persian", "farsi", "pashto", "sindhi", "hebrew")


def _chunks(text: str, size: int = 5000) -> list:
    parts, current = [], ""
    for paragraph in text.splitlines():
        if len(current) + len(paragraph) + 1 > size and current:
            parts.append(current)
            current = ""
        current += paragraph + "\n"
    if current.strip():
        parts.append(current)
    return parts


def _translate(text: str, target: str, source: str) -> str:
    origin = f" from {source}" if source else ""
    prompt = (
        f"Translate the following text{origin} into {target}. Keep the meaning, tone and paragraph "
        f"breaks. Return ONLY the translation, with no notes and no quotation marks.\n\n{text}"
    )
    return _gemini(prompt)


def _save_doc(translated: str, target: str, stem: str):
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.shared import Pt
    except ImportError:
        path = _unique_path(_desktop_folder(), f"{stem}_{_safe_name(target, 'translated')}", ".txt")
        path.write_text(translated, encoding="utf-8")
        return path, "I saved a text file because python docx is missing."
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(12)
    rtl = any(name in target.lower() for name in _RTL_LANGS)
    for line in translated.splitlines():
        if not line.strip():
            continue
        p = doc.add_paragraph(line.strip())
        p.paragraph_format.space_after = Pt(8)
        if rtl:
            p._p.get_or_add_pPr().append(OxmlElement("w:bidi"))
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            for r in p.runs:
                r._r.get_or_add_rPr().append(OxmlElement("w:rtl"))
    path = _unique_path(_desktop_folder(), f"{stem}_{_safe_name(target, 'translated')}", ".docx")
    doc.save(str(path))
    return path, ""


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        target = str(parameters.get("target_language", "") or "").strip()
        source = str(parameters.get("source_language", "") or "").strip()
        text = str(parameters.get("text", "") or "").strip()
        file_name = str(parameters.get("file_name", "") or "").strip()
        if not target:
            return "Which language should I translate into?"

        stem = "Translation"
        if file_name:
            path = _find_file(file_name)
            if not path:
                return f"I could not find a file called {file_name} on your Desktop, Downloads or Documents."
            text = _read_source(path)
            stem = _safe_name(path.stem, "Translation")
            if not text.strip():
                return "I could not read any text from that file."
        if not text:
            return "Tell me the text to translate, or the name of a file."

        if not file_name and len(text) <= 1200:
            result = _translate(text, target, source)
            try:
                import pyperclip
                pyperclip.copy(result)
            except Exception:
                pass
            return f"The translation into {target} is: {result}"

        pieces = [_translate(chunk, target, source) for chunk in _chunks(text[:60000])]
        path, note = _save_doc("\n".join(pieces), target, stem)
        _open_file(path)
        message = f"The translation is saved as {path.name} on your Desktop. {note}".strip()
        _log(player, message)
        return message
    except ValueError as error:
        return str(error)
    except Exception as error:
        return f"Sorry, the translation failed: {error}. This feature needs an internet connection."
