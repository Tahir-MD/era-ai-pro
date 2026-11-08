"""
ERA AI plugin: recorder_and_screenshot

Screenshots, voice recording and screen recording. Files go to a folder called
Jarvis Captures on your Desktop. Works offline.
Screen recording has no sound. Voice recording uses your normal microphone.
"""

PLUGIN = {
    "name": "recorder_and_screenshot",
    "description": (
        "Takes screenshots and records audio or the screen. Actions: screenshot "
        "('take a screenshot'), start_audio and stop_audio ('record my voice', 'record this "
        "lecture'), start_screen and stop_screen ('record my screen'), status. Files are "
        "saved on the Desktop in Jarvis Captures. Do NOT use screen_processor for this, "
        "that tool is only for looking at the screen to answer questions."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: screenshot, start_audio, stop_audio, start_screen, stop_screen, status",
            },
            "delay_seconds": {"type": "INTEGER", "description": "Wait before a screenshot so the user can switch windows, default 2"},
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



_STATE = {"audio": None, "screen": None}
_LOCK = threading.Lock()
_AUDIO_RATE = 22050


def _capture_folder() -> Path:
    folder = _desktop_folder() / "Jarvis Captures"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _screenshot(delay: int) -> str:
    import mss
    import mss.tools
    if delay > 0:
        time.sleep(min(delay, 10))
    path = _unique_path(_capture_folder(), f"Screenshot_{_stamp()}", ".png")
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
    return f"Screenshot saved as {path.name} in Jarvis Captures."


def _start_audio() -> str:
    import numpy as np
    import sounddevice as sd
    if _STATE["audio"]:
        return "Voice recording is already running."
    chunks = []

    def callback(indata, _frames, _time, _status):
        chunks.append(indata.copy())

    stream = sd.InputStream(samplerate=_AUDIO_RATE, channels=1, dtype="int16", callback=callback)
    stream.start()
    _STATE["audio"] = {"stream": stream, "chunks": chunks, "started": time.time()}
    return "Voice recording started. Tell me when to stop."


def _stop_audio() -> str:
    import wave
    import numpy as np
    info = _STATE["audio"]
    if not info:
        return "No voice recording is running."
    _STATE["audio"] = None
    info["stream"].stop()
    info["stream"].close()
    if not info["chunks"]:
        return "The recording was empty, nothing was saved."
    data = np.concatenate(info["chunks"], axis=0)
    path = _unique_path(_capture_folder(), f"Recording_{_stamp()}", ".wav")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_AUDIO_RATE)
        wav.writeframes(data.tobytes())
    seconds = int(time.time() - info["started"])
    return f"Voice recording saved as {path.name}, about {seconds} seconds long."


def _screen_worker(path: Path, stop_event, ready) -> None:
    import cv2
    import mss
    import numpy as np
    fps = 10
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            width, height = monitor["width"], monitor["height"]
            scale = min(1.0, 1280 / width)
            size = (int(width * scale) // 2 * 2, int(height * scale) // 2 * 2)
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
            ready.set()
            while not stop_event.is_set():
                started = time.time()
                frame = np.array(sct.grab(monitor))[:, :, :3]
                if scale < 1.0:
                    frame = cv2.resize(frame, size)
                writer.write(frame)
                wait = 1.0 / fps - (time.time() - started)
                if wait > 0:
                    time.sleep(wait)
            writer.release()
    except Exception as error:
        print(f"[Recorder] screen recording failed: {error}")
        ready.set()


def _start_screen() -> str:
    if _STATE["screen"]:
        return "Screen recording is already running."
    path = _unique_path(_capture_folder(), f"ScreenRecording_{_stamp()}", ".mp4")
    stop_event, ready = threading.Event(), threading.Event()
    thread = threading.Thread(target=_screen_worker, args=(path, stop_event, ready), daemon=True, name="screen-rec")
    thread.start()
    ready.wait(5)
    _STATE["screen"] = {"stop": stop_event, "thread": thread, "path": path, "started": time.time()}
    return "Screen recording started. It has no sound. Tell me when to stop."


def _stop_screen() -> str:
    info = _STATE["screen"]
    if not info:
        return "No screen recording is running."
    _STATE["screen"] = None
    info["stop"].set()
    info["thread"].join(timeout=10)
    seconds = int(time.time() - info["started"])
    if not info["path"].exists() or info["path"].stat().st_size == 0:
        return "The screen recording did not produce a file."
    return f"Screen recording saved as {info['path'].name}, about {seconds} seconds long."


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "")).strip().lower()
    try:
        with _LOCK:
            if action == "screenshot":
                delay = parameters.get("delay_seconds")
                result = _screenshot(2 if delay is None else int(delay))
            elif action == "start_audio":
                result = _start_audio()
            elif action == "stop_audio":
                result = _stop_audio()
            elif action == "start_screen":
                result = _start_screen()
            elif action == "stop_screen":
                result = _stop_screen()
            elif action == "status":
                running = [n for n, k in (("voice", "audio"), ("screen", "screen")) if _STATE[k]]
                result = ("Running now: " + " and ".join(running) + " recording.") if running else "Nothing is being recorded."
            else:
                return "I can take a screenshot, record your voice, or record your screen."
        _log(player, result)
        return result
    except ImportError as error:
        return f"A needed package is missing: {error}. Please run setup again."
    except Exception as error:
        return f"Sorry, the recorder failed: {error}"
