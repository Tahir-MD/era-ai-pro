"""
actions/ide_control.py - open ANY code editor/IDE and have the assistant write code in it.

    "Open VS Code"                              "Open my project folder in PyCharm"
    "Open Cursor and write a Python script that renames all photos by date"
    "Write a snake game in Java and open it in IntelliJ"
    "Type a bubble sort in the editor I have open"   (live typing into the focused editor)

  open            launch an IDE (optionally on a folder or file)
  write_and_open  Gemini writes the code, it is saved to a real file, and that file opens in the IDE
  type_live       paste fresh code into the editor that is currently focused (new tab/file) - good for a demo
For whole multi-file projects that are planned, installed, run and auto-fixed, use dev_agent instead.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG = BASE_DIR / "config" / "api_keys.json"
CODE_DIR = Path.home() / "Documents" / "Jarvis" / "code"          # tests monkeypatch
_CODEGEN_OVERRIDE = None                                           # tests: fn(description, language) -> code
OS = platform.system()

# name -> (aliases, cli commands, Windows exe globs, macOS app name)
IDES = {
    "vscode":       (("vs code", "visual studio code", "code", "vscode"), ["code"], [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe", r"%ProgramFiles%\Microsoft VS Code\Code.exe"], "Visual Studio Code"),
    "cursor":       (("cursor",), ["cursor"], [r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe"], "Cursor"),
    "windsurf":     (("windsurf",), ["windsurf"], [r"%LOCALAPPDATA%\Programs\Windsurf\Windsurf.exe"], "Windsurf"),
    "pycharm":      (("pycharm", "py charm"), ["pycharm", "pycharm64.exe", "charm"], [r"%ProgramFiles%\JetBrains\PyCharm*\bin\pycharm64.exe", r"%LOCALAPPDATA%\Programs\PyCharm*\bin\pycharm64.exe"], "PyCharm"),
    "intellij":     (("intellij", "intellij idea", "idea"), ["idea", "idea64.exe"], [r"%ProgramFiles%\JetBrains\IntelliJ IDEA*\bin\idea64.exe"], "IntelliJ IDEA"),
    "webstorm":     (("webstorm",), ["webstorm", "webstorm64.exe"], [r"%ProgramFiles%\JetBrains\WebStorm*\bin\webstorm64.exe"], "WebStorm"),
    "androidstudio": (("android studio",), ["studio", "studio64.exe"], [r"%ProgramFiles%\Android\Android Studio\bin\studio64.exe"], "Android Studio"),
    "sublime":      (("sublime", "sublime text"), ["subl"], [r"%ProgramFiles%\Sublime Text\sublime_text.exe"], "Sublime Text"),
    "notepad++":    (("notepad++", "notepad plus plus"), ["notepad++"], [r"%ProgramFiles%\Notepad++\notepad++.exe"], "Notepad++"),
    "atom":         (("atom",), ["atom"], [], "Atom"),
    "vim":          (("vim", "neovim", "nvim"), ["nvim", "vim"], [], ""),
    "notepad":      (("notepad",), ["notepad"], [], "TextEdit"),
}
LANG_EXT = {"python": ".py", "javascript": ".js", "typescript": ".ts", "java": ".java", "c": ".c", "c++": ".cpp", "cpp": ".cpp", "c#": ".cs",
            "go": ".go", "rust": ".rs", "html": ".html", "css": ".css", "php": ".php", "ruby": ".rb", "kotlin": ".kt", "swift": ".swift",
            "dart": ".dart", "sql": ".sql", "bash": ".sh", "shell": ".sh", "powershell": ".ps1", "r": ".r", "lua": ".lua", "matlab": ".m"}


def resolve_ide(name: str) -> str | None:
    n = re.sub(r"\s+", " ", (name or "").lower().strip())
    if not n:
        return None
    for key, (aliases, *_rest) in IDES.items():
        if n == key or n in aliases:
            return key
    for key, (aliases, *_rest) in IDES.items():
        if any(a in n for a in aliases if len(a) > 3):
            return key
    return None


def find_launcher(key: str) -> list[str] | None:
    """Command prefix that launches this IDE on this OS, or None if it is not installed."""
    aliases, clis, win_globs, mac_app = IDES[key]
    for c in clis:
        w = shutil.which(c)
        if w:
            return [w]
    if OS == "Windows":
        import glob
        for g in win_globs:
            hits = sorted(glob.glob(os.path.expandvars(g)))
            if hits:
                return [hits[-1]]
    if OS == "Darwin" and mac_app:
        for base in ("/Applications", str(Path.home() / "Applications")):
            if (Path(base) / f"{mac_app}.app").exists():
                return ["open", "-a", mac_app]
    return None


def launch(key: str, target: str = "") -> str:
    cmd = find_launcher(key)
    if cmd is None:
        if key == "vim" and OS != "Windows":
            return "Vim runs inside a terminal; open a terminal and type vim."
        return f"{key} does not seem to be installed (I could not find it)."
    args = list(cmd)
    if target:
        if cmd[0] == "open":                          # macOS: open -a App path
            args += [target]
        elif key == "vscode" or key in ("cursor", "windsurf"):
            args += ["-r", target] if Path(target).is_file() else [target]
        else:
            args += [target]
    kw = {"creationflags": 0x00000008} if OS == "Windows" else {"start_new_session": True}      # detach: IDE must outlive us
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except Exception as e:
        return f"Could not start {key}: {e}"
    return f"Opened {key}" + (f" with {Path(target).name}" if target else "") + "."


def _path(p: str) -> str:
    p = os.path.expanduser(p or "")
    if p and not Path(p).exists():
        for base in (Path.home() / "Documents", Path.home() / "Desktop", Path.home() / "Downloads", Path.home() / "Projects"):
            if (base / p).exists():
                return str(base / p)
    return p


# ── code generation ──────────────────────────────────────────────────────────
def clean_code(text: str) -> str:
    m = re.search(r"```[\w+#-]*\n(.*?)```", text, re.S)
    return (m.group(1) if m else re.sub(r"^```[\w+#-]*\n?|```$", "", text.strip())).strip("\n") + "\n"


def generate_code(description: str, language: str) -> str:
    if _CODEGEN_OVERRIDE is not None:
        return _CODEGEN_OVERRIDE(description, language)
    from google import genai
    key = json.loads(API_CONFIG.read_text(encoding="utf-8"))["gemini_api_key"]
    resp = genai.Client(api_key=key).models.generate_content(
        model="gemini-flash-latest",
        contents=(f"You are an expert {language} developer. Write complete, correct, runnable {language} code for this request. "
                  f"Include brief helpful comments and sensible error handling. Output ONLY the code, no explanation, no markdown fences.\n\n"
                  f"Request: {description}"))
    return clean_code(resp.text or "")


def _file_name(description: str, language: str, given: str) -> str:
    ext = LANG_EXT.get(language.lower(), ".txt")
    if given:
        return given if "." in given else given + ext
    words = re.sub(r"[^A-Za-z0-9 ]", " ", description).split()[:4]
    base = "_".join(w.lower() for w in words) or "script"
    if language.lower() == "java":                    # Java: public class name must match the file
        base = "".join(w.capitalize() for w in words) or "Main"
    return base + ext


def save_code(code: str, name: str, folder: Path | None = None) -> Path:
    folder = folder or CODE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path, n = folder / name, 2
    while path.exists():
        path = folder / f"{path.stem.rsplit('_v', 1)[0]}_v{n}{path.suffix}"
        n += 1
    path.write_text(code, encoding="utf-8")
    return path


def type_live(code: str, ide_key: str) -> str:
    """Open a fresh untitled file in the focused editor and paste the code (clipboard, so indentation survives)."""
    try:
        import pyautogui
        import pyperclip
    except ImportError:
        return "Live typing needs: pip install pyautogui pyperclip"
    mod = "command" if OS == "Darwin" else "ctrl"
    pyperclip.copy(code)
    pyautogui.hotkey(mod, "n")
    time.sleep(0.8)
    pyautogui.hotkey(mod, "v")
    return "Typed the code into a new file in your editor."


# ── the tool ─────────────────────────────────────────────────────────────────
def ide_control(parameters: dict, player=None, speak=None) -> str:
    action = str(parameters.get("action", "open")).lower().strip()
    named = str(parameters.get("ide", "")).strip()
    key = resolve_ide(named) if named else resolve_ide(_cfg_default())
    desc = str(parameters.get("description", "")).strip()
    lang = str(parameters.get("language", "") or "python").strip().lower()

    if action == "list":
        return "Installed: " + (", ".join(k for k in IDES if find_launcher(k)) or "none found") + "."

    if key is None:
        return (f"I do not know an editor called {named}. " if named else "") + "Which editor? For example VS Code, Cursor, PyCharm, IntelliJ, Sublime or Notepad++."

    if action == "open":
        return launch(key, _path(str(parameters.get("path", ""))))

    if action in ("write_and_open", "write", "create"):
        if not desc:
            return "What should the code do?"
        try:
            code = generate_code(desc, lang)
        except Exception as e:
            return f"I could not generate the code: {str(e)[:120]}"
        folder = Path(_path(str(parameters.get("folder", "")))) if parameters.get("folder") else None
        path = save_code(code, _file_name(desc, lang, str(parameters.get("filename", ""))), folder)
        res = launch(key, str(path))
        return f"Wrote {len(code.splitlines())} lines of {lang} to {path}. {res}"

    if action == "type_live":
        if not desc and not parameters.get("code"):
            return "What should I type?"
        try:
            code = str(parameters.get("code") or generate_code(desc, lang))
        except Exception as e:
            return f"I could not generate the code: {str(e)[:120]}"
        return type_live(code, key)

    return "Use action: open, write_and_open, type_live or list."


def _cfg_default() -> str:
    try:
        return str(json.loads(API_CONFIG.read_text(encoding="utf-8")).get("default_ide", "vscode"))
    except Exception:
        return "vscode"


TOOL = {
    "name": "ide_control",
    "description": (
        "Open any code editor / IDE (VS Code, Cursor, PyCharm, IntelliJ, WebStorm, Android Studio, Sublime, Notepad++, Vim) and write code in it. "
        "Use for 'open VS Code', 'open my project in PyCharm', 'open Cursor and write a script that ...', 'write a <program> in <language> and open it', "
        "'type this code in my editor'. action=write_and_open generates real code, saves it as a file and opens it in the chosen IDE. "
        "For a complete multi-file project (planning, installing, running, auto-fixing) use dev_agent; to edit/run an existing file use code_helper."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "open (default) | write_and_open | type_live | list"},
            "ide": {"type": "STRING", "description": "vscode | cursor | pycharm | intellij | webstorm | androidstudio | sublime | notepad++ | vim (default vscode)"},
            "path": {"type": "STRING", "description": "open: folder or file to open"},
            "description": {"type": "STRING", "description": "write_and_open/type_live: what the program should do"},
            "language": {"type": "STRING", "description": "Programming language (default python)"},
            "filename": {"type": "STRING", "description": "Optional file name"},
            "folder": {"type": "STRING", "description": "Optional folder to save into"},
            "code": {"type": "STRING", "description": "type_live: exact code to type (if you already have it)"},
        },
        "required": ["action"],
    },
    "handler": ide_control,
}
