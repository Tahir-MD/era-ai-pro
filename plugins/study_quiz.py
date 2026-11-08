"""
ERA AI plugin: study_quiz

Give Jarvis a lecture file, notes or a topic and it makes a quiz (Word file with
answer key) and or flashcards (Excel file). Needs internet because Gemini writes
the questions. Word output needs:  pip install python-docx
"""

PLUGIN = {
    "name": "study_quiz",
    "description": (
        "Makes exam practice material from the user's own notes or a topic: a multiple choice "
        "quiz saved as a Word file with an answer key, and or flashcards saved as an Excel file. "
        "Use for 'make a quiz from my lecture', 'flashcards from this PDF', 'test me on chapter 3'. "
        "Pass file_name when the user names a file (pdf, docx, pptx, txt) or topic_or_text for "
        "pasted notes or a topic. Needs internet."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_name": {"type": "STRING", "description": "Name or full path of the notes file, for example Lecture3.pdf"},
            "topic_or_text": {"type": "STRING", "description": "A topic, or notes text, when there is no file"},
            "kind": {"type": "STRING", "description": "One of: quiz, flashcards, both. Default both."},
            "count": {"type": "INTEGER", "description": "Number of questions or cards, default 10, maximum 30"},
            "language": {"type": "STRING", "description": "Language for the questions, default English"},
        },
        "required": [],
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




def _make_quiz_doc(title: str, questions: list, folder: Path, stem: str):
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
    except ImportError:
        return None
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    heading = doc.add_paragraph()
    run_ = heading.add_run(title)
    run_.bold = True
    run_.font.size = Pt(22)
    run_.font.color.rgb = RGBColor(0x16, 0x1E, 0x33)
    doc.add_paragraph("Choose the best answer. The answer key is on the last page.")
    for number, q in enumerate(questions, start=1):
        p = doc.add_paragraph()
        r = p.add_run(f"{number}. {q['question']}")
        r.bold = True
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.keep_with_next = True
        for letter, option in zip("ABCD", q["options"]):
            doc.add_paragraph(f"{letter}.  {option}").paragraph_format.left_indent = Pt(24)
    doc.add_page_break()
    key = doc.add_paragraph()
    kr = key.add_run("Answer key")
    kr.bold = True
    kr.font.size = Pt(18)
    for number, q in enumerate(questions, start=1):
        line = f"{number}. {q['answer']}"
        if q.get("explanation"):
            line += f"   {q['explanation']}"
        doc.add_paragraph(line)
    path = _unique_path(folder, f"{stem}_quiz", ".docx")
    doc.save(str(path))
    return path


def _make_cards_sheet(cards: list, folder: Path, stem: str):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Flashcards"
    for col, text in enumerate(("Question", "Answer"), start=1):
        cell = ws.cell(row=1, column=col, value=text)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="161E33")
    for row, card in enumerate(cards, start=2):
        ws.cell(row=row, column=1, value=card["front"]).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=row, column=2, value=card["back"]).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 55
    ws.column_dimensions["B"].width = 55
    ws.freeze_panes = "A2"
    path = _unique_path(folder, f"{stem}_flashcards", ".xlsx")
    wb.save(str(path))
    return path


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        file_name = str(parameters.get("file_name", "") or "").strip()
        topic = str(parameters.get("topic_or_text", "") or "").strip()
        kind = str(parameters.get("kind", "both") or "both").strip().lower()
        if kind not in ("quiz", "flashcards", "both"):
            kind = "both"
        count = max(3, min(int(parameters.get("count") or 10), 30))
        language = str(parameters.get("language", "") or "English").strip()

        material, stem, label = topic, "Study", topic[:40]
        if file_name:
            path = _find_file(file_name)
            if not path:
                return f"I could not find a file called {file_name} on your Desktop, Downloads or Documents."
            material = _read_source(path)
            stem, label = _safe_name(path.stem, "Study"), path.stem
            if not material.strip():
                return "I could not read any text from that file. It may be a scanned picture."
        if not material.strip():
            return "Tell me a topic, or the name of a notes file, and I will make the practice material."

        material = material[:30000]
        wanted = []
        if kind in ("quiz", "both"):
            wanted.append(f'"questions": exactly {count} multiple choice questions, each {{"question": str, "options": [4 strings without letters], "answer": one letter A to D, "explanation": one short sentence}}')
        if kind in ("flashcards", "both"):
            wanted.append(f'"flashcards": exactly {count} cards, each {{"front": question or term, "back": short answer}}')
        prompt = (
            f"You are a careful exam setter. Using ONLY the material below, write study material in {language}. "
            f"Return JSON with these keys: {'; '.join(wanted)}. "
            f"Questions must test understanding, have one clearly correct answer and plausible wrong options.\n\nMATERIAL:\n{material}"
        )
        data = _parse_json(_gemini(prompt, json_mode=True))
        if isinstance(data, list):
            data = {"questions": data}

        folder = _desktop_folder()
        made, notes = [], []
        questions = [q for q in data.get("questions", []) if isinstance(q, dict) and len(q.get("options", [])) >= 4 and q.get("question")]
        cards = [c for c in data.get("flashcards", []) if isinstance(c, dict) and c.get("front") and c.get("back")]

        if kind in ("quiz", "both") and questions:
            doc_path = _make_quiz_doc(f"Quiz: {label}", questions, folder, stem)
            if doc_path:
                made.append(doc_path)
            else:
                notes.append("I could not make the Word quiz because python docx is missing, run pip install python-docx.")
        if kind in ("flashcards", "both") and cards:
            made.append(_make_cards_sheet(cards, folder, stem))

        if not made:
            return " ".join(notes) or "I could not make good questions from that material. Try a longer text."
        for p in made:
            _open_file(p)
        names = " and ".join(p.name for p in made)
        result = f"Done. I made {names} on your Desktop. " + " ".join(notes)
        _log(player, result)
        return result.strip()
    except json.JSONDecodeError:
        return "Gemini gave an answer I could not read. Please try again."
    except ValueError as error:
        return str(error)
    except Exception as error:
        return f"Sorry, I could not make the study material: {error}. This feature needs an internet connection."
