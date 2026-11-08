"""
ERA AI plugin: make_excel_file

Say "make an Excel sheet of my marks" and Jarvis builds a neat spreadsheet,
saves it on the Desktop and opens it. Works fully offline.
"""

PLUGIN = {
    "name": "make_excel_file",
    "description": (
        "Creates a real Excel spreadsheet (.xlsx) with a styled table and opens it. "
        "Use when the user asks for an Excel file, sheet, table, marks sheet, budget, "
        "attendance sheet, expense list or any data in rows and columns. Decide the "
        "column headers and fill in every row yourself from what the user said. "
        "Do NOT use file_processor, that tool only reads existing files."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING", "description": "Name of the sheet and the file, for example Class Marks"},
            "headers": {
                "type": "ARRAY",
                "description": "Column headings in order",
                "items": {"type": "STRING"},
            },
            "rows": {
                "type": "ARRAY",
                "description": "Table rows in order, each row has its cells in the same order as the headers",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "cells": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Cell values of this row"},
                    },
                    "required": ["cells"],
                },
            },
            "add_totals": {"type": "BOOLEAN", "description": "Add a Total row that sums every number column"},
        },
        "required": ["title", "headers", "rows"],
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




def _to_value(text):
    s = str(text).strip()
    if s == "":
        return None
    plain = s.replace(",", "")
    if s.isdigit() and s.startswith("0") and len(s) > 1:
        return s
    try:
        if re.fullmatch(r"-?\d+", plain):
            return int(plain)
        if re.fullmatch(r"-?\d*\.\d+", plain):
            return float(plain)
    except ValueError:
        pass
    return s


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return "The openpyxl package is missing. Please run: pip install openpyxl"

    try:
        title = str(parameters.get("title", "")).strip() or "Sheet"
        headers = [str(h).strip() for h in (parameters.get("headers") or [])]
        raw_rows = parameters.get("rows") or []
        if not headers:
            return "I need at least one column heading to build the sheet."

        rows = []
        for item in raw_rows:
            cells = item.get("cells") if isinstance(item, dict) else item
            if isinstance(cells, (list, tuple)):
                rows.append([_to_value(c) for c in cells])

        wb = Workbook()
        ws = wb.active
        ws.title = re.sub(r"[\[\]\*\?/\\:]", "", title)[:31] or "Sheet"

        thin = Side(style="thin", color="C8CED8")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        head_fill = PatternFill("solid", fgColor="161E33")

        for col, text in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=text)
            cell.font = Font(bold=True, color="FFFFFF", size=12)
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border

        for r, row in enumerate(rows, start=2):
            for c in range(1, len(headers) + 1):
                value = row[c - 1] if c - 1 < len(row) else None
                cell = ws.cell(row=r, column=c, value=value)
                cell.border = border
                cell.alignment = Alignment(
                    horizontal="right" if isinstance(value, (int, float)) else "left",
                    vertical="center",
                )

        last_data_row = len(rows) + 1
        if parameters.get("add_totals") and rows:
            total_row = last_data_row + 1
            ws.cell(row=total_row, column=1, value="Total")
            for c in range(1, len(headers) + 1):
                values = [row[c - 1] for row in rows if c - 1 < len(row) and row[c - 1] is not None]
                if values and all(isinstance(v, (int, float)) for v in values):
                    letter = get_column_letter(c)
                    ws.cell(row=total_row, column=c, value=f"=SUM({letter}2:{letter}{last_data_row})")
            for c in range(1, len(headers) + 1):
                cell = ws.cell(row=total_row, column=c)
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="E8EEF5")
                cell.border = border

        for c, text in enumerate(headers, start=1):
            longest = len(text)
            for row in rows:
                if c - 1 < len(row) and row[c - 1] is not None:
                    longest = max(longest, len(str(row[c - 1])))
            ws.column_dimensions[get_column_letter(c)].width = min(max(12, longest + 4), 50)
        ws.row_dimensions[1].height = 24
        ws.freeze_panes = "A2"

        path = _unique_path(_desktop_folder(), _safe_name(title, "Sheet"), ".xlsx")
        wb.save(str(path))
        _open_file(path)

        message = f"Your Excel sheet is ready with {len(rows)} rows. I saved it as {path.name} on the {path.parent.name} folder."
        _log(player, message)
        return message
    except Exception as error:
        return f"Sorry, I could not build the Excel sheet: {error}"
