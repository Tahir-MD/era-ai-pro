"""core/paths.py - the folder that holds config/, memory/, actions/ and plugins/.

Running `python main.py` that is the project folder. In the packaged build (PyInstaller) it is the folder
that contains the .exe, so settings, memory and drop-in plugins stay editable next to the program."""
import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
