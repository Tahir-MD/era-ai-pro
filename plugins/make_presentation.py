"""
ERA AI plugin: make_presentation

Copy this file into the plugins folder of the project, then restart the assistant.
When you say "make a presentation about ...", Jarvis writes the slides and this
plugin builds a real PowerPoint file, saves it on your Desktop and opens it.
"""
import os
import platform
import re
import subprocess
from pathlib import Path

PLUGIN = {
    "name": "make_presentation",
    "description": (
        "Creates a real PowerPoint presentation file (.pptx) and opens it. "
        "Use this when the user asks to make, create or prepare a presentation, "
        "slides, a slide deck or a PPT on any topic. Write the full content of "
        "every slide yourself (clear slide titles and 3 to 5 short bullet points "
        "each) before calling this tool. Do NOT use file_processor for this, "
        "that tool only reads existing files."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING", "description": "Main title of the presentation"},
            "subtitle": {"type": "STRING", "description": "Short line under the title, for example the presenter name or course"},
            "slides": {
                "type": "ARRAY",
                "description": "Content slides in order, not counting the title slide",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING", "description": "Slide heading"},
                        "points": {
                            "type": "ARRAY",
                            "description": "Three to five short bullet points",
                            "items": {"type": "STRING"},
                        },
                        "notes": {"type": "STRING", "description": "Optional speaker notes for this slide"},
                    },
                    "required": ["title", "points"],
                },
            },
        },
        "required": ["title", "slides"],
    },
}

# Colours (red, green, blue)
DARK = (22, 30, 51)
ACCENT = (0, 150, 199)
LIGHT = (245, 247, 250)
TEXT = (40, 48, 66)


def _output_folder() -> Path:
    for candidate in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop", Path.home() / "Documents"):
        if candidate.exists():
            return candidate
    return Path.home()


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", "", text).strip().replace(" ", "_")
    return cleaned[:50] or "Presentation"


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


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
        from pptx.util import Inches, Pt
    except ImportError:
        return "The python pptx package is missing. Please install it and try again."

    try:
        title = str(parameters.get("title", "")).strip() or "Presentation"
        subtitle = str(parameters.get("subtitle", "")).strip()
        slides = parameters.get("slides") or []
        if not slides:
            return "I need at least one content slide to build the presentation."

        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        blank = prs.slide_layouts[6]
        width, height = prs.slide_width, prs.slide_height

        def rgb(values):
            return RGBColor(*values)

        def add_rect(slide, left, top, w, h, colour):
            shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, w, h)
            shape.fill.solid()
            shape.fill.fore_color.rgb = rgb(colour)
            shape.line.fill.background()
            return shape

        def add_text(slide, left, top, w, h, text, size, colour, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
            box = slide.shapes.add_textbox(left, top, w, h)
            frame = box.text_frame
            frame.word_wrap = True
            frame.vertical_anchor = anchor
            paragraph = frame.paragraphs[0]
            paragraph.alignment = align
            run_ = paragraph.add_run()
            run_.text = text
            run_.font.size = Pt(size)
            run_.font.bold = bold
            run_.font.name = "Calibri"
            run_.font.color.rgb = rgb(colour)
            return box

        # Title slide
        slide = prs.slides.add_slide(blank)
        add_rect(slide, 0, 0, width, height, DARK)
        add_rect(slide, Inches(0.9), Inches(3.55), Inches(1.6), Inches(0.08), ACCENT)
        add_text(slide, Inches(0.9), Inches(1.7), Inches(11.5), Inches(1.8), title, 44, (255, 255, 255), bold=True, anchor=MSO_ANCHOR.BOTTOM)
        if subtitle:
            add_text(slide, Inches(0.9), Inches(3.8), Inches(11.5), Inches(1.0), subtitle, 22, (190, 205, 225))

        # Content slides
        for index, item in enumerate(slides, start=1):
            slide = prs.slides.add_slide(blank)
            add_rect(slide, 0, 0, width, height, LIGHT)
            add_rect(slide, 0, 0, width, Inches(1.35), DARK)
            add_rect(slide, 0, Inches(1.35), width, Inches(0.07), ACCENT)
            add_text(slide, Inches(0.7), Inches(0.15), Inches(11.9), Inches(1.1),
                     str(item.get("title", "")), 32, (255, 255, 255), bold=True, anchor=MSO_ANCHOR.MIDDLE)

            points = [str(p).strip() for p in (item.get("points") or []) if str(p).strip()]
            size = 26 if len(points) <= 4 else 22 if len(points) <= 6 else 18
            box = slide.shapes.add_textbox(Inches(0.9), Inches(1.8), Inches(11.5), Inches(5.0))
            frame = box.text_frame
            frame.word_wrap = True
            for number, point in enumerate(points):
                paragraph = frame.paragraphs[0] if number == 0 else frame.add_paragraph()
                paragraph.space_after = Pt(14)
                bullet = paragraph.add_run()
                bullet.text = "\u25A0  "
                bullet.font.size = Pt(size - 8)
                bullet.font.color.rgb = rgb(ACCENT)
                body = paragraph.add_run()
                body.text = point
                body.font.size = Pt(size)
                body.font.name = "Calibri"
                body.font.color.rgb = rgb(TEXT)

            add_text(slide, Inches(11.9), Inches(6.9), Inches(1.2), Inches(0.4), str(index), 14, (130, 140, 160), align=PP_ALIGN.RIGHT)

            notes = str(item.get("notes", "")).strip()
            if notes:
                slide.notes_slide.notes_text_frame.text = notes

        folder = _output_folder()
        path = folder / f"{_safe_name(title)}.pptx"
        counter = 2
        while path.exists():
            path = folder / f"{_safe_name(title)}_{counter}.pptx"
            counter += 1
        prs.save(str(path))
        _open_file(path)

        message = f"Your presentation is ready with {len(slides) + 1} slides. I saved it as {path.name} in {folder.name}."
        if player:
            try:
                player.write_log(f"JARVIS: {message}")
            except Exception:
                pass
        return message
    except Exception as error:
        return f"Sorry, I could not build the presentation: {error}"
