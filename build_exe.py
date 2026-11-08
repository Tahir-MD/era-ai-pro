"""
build_exe.py - turn ERA into a normal Windows/macOS program (no `python main.py` needed).

    pip install -r requirements.txt pyinstaller
    python build_exe.py            ->  dist/ERA/ERA.exe  (zip the whole dist/ERA folder to share it)
    python build_exe.py --dry-run  ->  just print the PyInstaller command

Why a folder and not one single .exe: ERA loads actions/ and plugins/ from real files at start-up (that is how
drop-in plugins work), and keeps config/ and memory/ beside the program. A one-file exe unpacks to a temp folder
on every launch, which would lose all of that. The folder starts fast and plugins stay editable.
Build on the OS you are targeting (Windows build -> .exe, macOS build -> .app). PyInstaller cannot cross-compile.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "ERA"
LOCAL = {"actions", "core", "memory", "plugins", "dashboard", "config", "ui", "main", "tests"}
# import name -> pip name differences do not matter here: PyInstaller wants IMPORT names.
ALWAYS_COLLECT = ["playwright", "google.genai", "googleapiclient", "google_auth_oauthlib", "sounddevice"]
OPTIONAL_COLLECT = ["openwakeword", "pptx", "docx", "cv2"]
DATA_FOLDERS = ["actions", "plugins"]                      # shipped as real .py files beside the exe
DATA_FILES = [("core/prompt.txt", "core"), ("config/jarvis.ico", "config"), ("config/__init__.py", "config"), ("readme.md", "."),
              ("ERA_PRO_UPGRADES.md", "."), ("ERA_PRO_V2.md", "."), ("LICENSE", ".")]


def third_party_imports() -> set[str]:
    """Every top-level module imported anywhere in actions/, plugins/, core/, memory/, dashboard/ or the entry files."""
    found: set[str] = set()
    files = [ROOT / "main.py", ROOT / "ui.py"] + [p for d in ("actions", "plugins", "core", "memory", "dashboard") for p in (ROOT / d).rglob("*.py")]
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    return {m for m in found if m not in stdlib and m not in LOCAL}


def local_modules() -> list[str]:
    out = []
    for d in ("actions", "core", "memory", "dashboard"):
        out += [f"{d}.{p.stem}" for p in (ROOT / d).glob("*.py") if p.stem != "__init__"]
    return sorted(out)


def available(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def command() -> list[str]:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", NAME, "--windowed", "--onedir",
           "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build")]
    ico = ROOT / "config" / "jarvis.ico"
    if ico.exists() and sys.platform == "win32":
        cmd += ["--icon", str(ico)]
    for m in local_modules():
        cmd += ["--hidden-import", m]
    for m in sorted(third_party_imports()):
        if available(m):
            cmd += ["--hidden-import", m]
    for m in ALWAYS_COLLECT + OPTIONAL_COLLECT:
        if available(m):
            cmd += ["--collect-all", m]
    cmd += ["--paths", str(ROOT), str(ROOT / "main.py")]
    return cmd


def copy_beside_exe() -> Path:
    out = ROOT / "dist" / NAME
    if sys.platform == "darwin" and (ROOT / "dist" / f"{NAME}.app").exists():
        out = ROOT / "dist" / f"{NAME}.app" / "Contents" / "MacOS"
    for d in DATA_FOLDERS:
        dst = out / d
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(ROOT / d, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for src, sub in DATA_FILES:
        s = ROOT / src
        if s.exists():
            (out / sub).mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, out / sub / s.name)
    if (ROOT / "dashboard" / "static").exists():
        shutil.copytree(ROOT / "dashboard" / "static", out / "dashboard" / "static", dirs_exist_ok=True)
    for empty in ("config", "memory"):
        (out / empty).mkdir(exist_ok=True)                 # NEVER copy the developer's keys or memories
    (out / "memory" / "__init__.py").touch()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true"); a = ap.parse_args()
    cmd = command()
    if a.dry_run:
        print(" ".join(f'"{c}"' if " " in c else c for c in cmd)); return 0
    if not available("PyInstaller"):
        print("PyInstaller is missing. Run: pip install pyinstaller"); return 1
    print(f"Building {NAME} ... (a few minutes)")
    if subprocess.call(cmd, cwd=ROOT) != 0:
        print("Build failed - read the error above."); return 1
    out = copy_beside_exe()
    print(f"\nDone: {out}\nRun {NAME}{'.exe' if sys.platform == 'win32' else ''} from there. To share it, zip the whole '{out.name}' folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
