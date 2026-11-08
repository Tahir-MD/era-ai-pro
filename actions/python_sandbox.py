"""
actions/python_sandbox.py - the assistant can WRITE AND RUN Python to answer questions that need real computation:
data analysis on your CSV, charts, unit conversions, statistics, quick scripts, "how many days until...".

Safety (read this - it is deliberately conservative):
  * every run is shown on the HUD and waits for YOUR confirmation (core/confirm.py) - the model cannot approve itself
  * it runs in a separate interpreter (`python -I`), in a throw-away folder, with a clean environment
    (no API keys), a 25 s time limit and CPU/memory limits on Linux/macOS
  * a static check refuses obviously dangerous code (deleting trees, spawning processes, network sockets)
  * charts/files the script creates are collected into Documents/Jarvis/outputs and opened
This is a guard against accidents, NOT a security sandbox: a determined script could still do harm, which is why
you always see the code and must press CONFIRM first.
"""
from __future__ import annotations

import ast
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from core import confirm as confirm_gate
from core import services

TIMEOUT_S = 25
MAX_OUT = 2500
_FORBIDDEN_CALLS = {"system", "popen", "rmtree", "remove", "unlink", "rmdir", "removedirs", "kill", "killpg", "fork", "execv",
                    "execve", "spawnl", "spawnv", "startfile", "Popen", "run", "call", "check_output", "check_call"}
_FORBIDDEN_MODULES = {"subprocess", "socket", "ctypes", "shutil", "requests", "urllib", "http", "ftplib", "smtplib", "telnetlib",
                      "pty", "winreg", "multiprocessing", "webbrowser"}
_SAFE_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "PYTHONIOENCODING")
OUT_DIR = Path.home() / "Documents" / "Jarvis" / "outputs"       # tests monkeypatch


def static_check(code: str) -> str:
    """'' if acceptable, else the reason it is refused."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax error on line {e.lineno}: {e.msg}"
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in _FORBIDDEN_MODULES:
                    return f"importing '{a.name}' is not allowed"
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in _FORBIDDEN_MODULES:
            return f"importing from '{n.module}' is not allowed"
        elif isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if name in _FORBIDDEN_CALLS:
                return f"calling '{name}' is not allowed"
            if name in ("eval", "exec", "compile", "__import__"):
                return f"'{name}' is not allowed"
    return ""


def _limits():                                  # POSIX only; runs in the child before exec
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S, TIMEOUT_S + 2))
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 2**20, 1536 * 2**20))
    except Exception:
        pass


def execute(code: str, timeout: int = TIMEOUT_S, out_dir: Path | None = None) -> dict:
    """Run code. Returns {ok, output, files}. Pure function of the code (unit-tested)."""
    why = static_check(code)
    if why:
        return {"ok": False, "output": f"Refused: {why}.", "files": []}
    work = Path(tempfile.mkdtemp(prefix="era_py_"))
    try:
        script = work / "script.py"
        script.write_text(code, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
        env["MPLBACKEND"] = "Agg"
        env["PYTHONIOENCODING"] = "utf-8"
        kw = {"preexec_fn": _limits} if platform.system() != "Windows" else {}
        try:
            p = subprocess.run([sys.executable, "-I", str(script)], cwd=str(work), env=env, capture_output=True, text=True,
                               timeout=timeout, **kw)
            out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
            ok = p.returncode == 0
        except subprocess.TimeoutExpired:
            out, ok = f"Stopped: the script ran longer than {timeout} seconds.", False
        files = []
        dest = out_dir or OUT_DIR
        for f in sorted(work.iterdir()):
            if f.name != "script.py" and f.is_file() and f.stat().st_size < 50 * 2**20:
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / f.name
                n = 2
                while target.exists():
                    target = dest / f"{f.stem}_{n}{f.suffix}"
                    n += 1
                shutil.copy2(f, target)
                files.append(str(target))
        return {"ok": ok, "output": out.strip()[-MAX_OUT:], "files": files}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _open(path: str) -> None:
    try:
        s = platform.system()
        if s == "Windows":
            os.startfile(path)           # type: ignore[attr-defined]
        elif s == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def run_python(parameters: dict, player=None, speak=None) -> str:
    code = str(parameters.get("code", "")).strip("\n")
    purpose = str(parameters.get("purpose", "")).strip() or "run a Python script"
    if not code.strip():
        return "I need the Python code to run."
    why = static_check(code)
    if why:
        return f"I will not run that code: {why}. Rewrite it without that, or do the task another way."

    def go() -> str:
        res = execute(code)
        for f in res["files"]:
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".pdf", ".html", ".xlsx", ".csv")):
                _open(f)
        files = f" Files saved: {', '.join(Path(f).name for f in res['files'])}." if res["files"] else ""
        msg = (f"[CODE_RESULT] The script {'finished' if res['ok'] else 'FAILED'}. Output:\n{res['output'] or '(no output)'}{files}\n"
               f"Tell the user the result naturally in their language; if it failed, say what went wrong and offer to fix it.")
        services.inject(msg)
        return "ran"

    preview = code if len(code) < 700 else code[:700] + "\n..."
    return confirm_gate.request(
        key="run_python", title=f"Run Python: {purpose[:60]}", detail=preview, run=go)


TOOL = {
    "name": "run_python",
    "description": (
        "Write and run a Python script to get an exact answer: data analysis, statistics, charts (matplotlib, saved as PNG), "
        "maths, date arithmetic, parsing a CSV, simulations. Use when the result needs real computation instead of guessing. "
        "Print the final answer with print(). The user must approve every run on screen first, then the result is spoken. "
        "Allowed: standard library, numpy, pandas, matplotlib if installed. Not allowed: network, subprocesses, deleting files."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "code": {"type": "STRING", "description": "Complete Python 3 script. Use print() for output; save charts with plt.savefig('chart.png')."},
            "purpose": {"type": "STRING", "description": "One short line: what this script does"},
        },
        "required": ["code"],
    },
    "handler": run_python,
}
