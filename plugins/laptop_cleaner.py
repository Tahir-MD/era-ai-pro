"""
ERA AI plugin: laptop_cleaner

Disk space, temporary file cleaning and finding large files.
Works offline. Cleaning always waits for your CONFIRM button on the screen.
Nothing outside the temporary folder is ever deleted, and large files are only listed.
"""

PLUGIN = {
    "name": "laptop_cleaner",
    "description": (
        "Helps keep the laptop tidy. Actions: disk_space ('how much space is left'), "
        "scan_temp ('how much junk can be cleaned'), clean_temp ('clean temporary files', "
        "asks the user to press a confirm button first), find_large_files ('which files are "
        "taking space', only lists them and never deletes). Do NOT use this for the "
        "desktop organizer or normal file moving, that belongs to file_controller."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "One of: disk_space, scan_temp, clean_temp, find_large_files"},
            "folder": {"type": "STRING", "description": "Optional folder to search for large files. Default is Downloads, Documents, Desktop, Videos, Pictures and Music."},
            "count": {"type": "INTEGER", "description": "How many large files to list, default 8, maximum 20"},
            "min_megabytes": {"type": "INTEGER", "description": "Smallest file size to list in megabytes, default 100"},
        },
        "required": ["action"],
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



import tempfile

_OLD_AFTER_SECONDS = 2 * 24 * 3600
_SKIP_DIRS = {"appdata", "node_modules", ".git", "$recycle.bin", "system volume information", "windows", "program files"}


def _size_text(num_bytes: float) -> str:
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} gigabytes"
    return f"{num_bytes / (1024 ** 2):.0f} megabytes"


def _old_temp_files(limit_seconds: float = 40.0):
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - _OLD_AFTER_SECONDS
    started = time.time()
    files = []
    for dirpath, _dirs, names in os.walk(root, topdown=True):
        if time.time() - started > limit_seconds:
            break
        for name in names:
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full)
                if stat.st_mtime < cutoff:
                    files.append((full, stat.st_size))
            except OSError:
                continue
    return root, files


def _disk_space() -> str:
    lines = []
    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts.lower() or not part.fstype:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            lines.append(f"Drive {part.mountpoint.rstrip(chr(92))}: {_size_text(usage.free)} free of {_size_text(usage.total)}.")
    except ImportError:
        import shutil
        roots = [f"{c}:\\" for c in "CDEFGH"] if platform.system() == "Windows" else ["/"]
        for r in roots:
            if os.path.exists(r):
                usage = shutil.disk_usage(r)
                lines.append(f"Drive {r.rstrip(chr(92))}: {_size_text(usage.free)} free of {_size_text(usage.total)}.")
    return " ".join(lines) or "I could not read the disk information."


def _clean_now() -> str:
    _root, files = _old_temp_files()
    freed, removed = 0, 0
    for full, size in files:
        try:
            os.remove(full)
            freed += size
            removed += 1
        except OSError:
            continue
    return f"Cleaned {removed} old temporary files and freed {_size_text(freed)}."


def _find_large(folder: str, count: int, min_mb: int, player):
    if folder:
        roots = [Path(folder)]
        if not roots[0].exists():
            return None, f"I could not find the folder {folder}."
    else:
        home = Path.home()
        roots = [home / n for n in ("Downloads", "Documents", "Desktop", "Videos", "Pictures", "Music")]
    threshold = max(1, min_mb) * 1024 * 1024
    started, seen, found = time.time(), 0, []
    stack = [r for r in roots if r.exists()]
    while stack and time.time() - started < 45 and seen < 400000:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in _SKIP_DIRS:
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            size = entry.stat().st_size
                            if size >= threshold:
                                found.append((size, entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    found.sort(reverse=True)
    return found[:count], ""


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "")).strip().lower()
    try:
        if action == "disk_space":
            result = _disk_space()

        elif action == "scan_temp":
            _root, files = _old_temp_files()
            total = sum(size for _f, size in files)
            result = (f"I found {len(files)} old temporary files that can be cleaned, about {_size_text(total)}."
                      if files else "There are no old temporary files to clean.")

        elif action == "clean_temp":
            _root, files = _old_temp_files()
            if not files:
                return "There are no old temporary files to clean."
            total = sum(size for _f, size in files)
            try:
                from core import confirm
            except Exception:
                return "I cannot ask for your confirmation right now, so I did not delete anything."
            result = confirm.request(
                key="clean_temp",
                title="Clean old temporary files",
                detail=f"{len(files)} files older than 2 days in the temp folder, about {_size_text(total)}",
                run=_clean_now,
            )

        elif action == "find_large_files":
            count = max(1, min(int(parameters.get("count") or 8), 20))
            min_mb = int(parameters.get("min_megabytes") or 100)
            found, error = _find_large(str(parameters.get("folder") or "").strip(), count, min_mb, player)
            if error:
                return error
            if not found:
                return f"I did not find any file bigger than {min_mb} megabytes."
            for size, path in found:
                _log(player, f"{_size_text(size)}  {path}")
            spoken = [f"{Path(p).name}, {_size_text(s)}, in the folder {Path(p).parent.name}" for s, p in found[:5]]
            result = (f"I found {len(found)} large files. The biggest are: " + "; ".join(spoken) +
                      ". The full list with locations is in the activity log. I did not delete anything.")

        else:
            return "I can check disk space, scan or clean temporary files, or find large files."

        return result
    except Exception as error:
        return f"Sorry, the laptop cleaner failed: {error}"
