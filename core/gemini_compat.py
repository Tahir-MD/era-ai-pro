"""
core/gemini_compat.py - keeps every Gemini text call working when a model name stops existing.

Symptom it fixes:  404 NOT_FOUND  "models/gemini-flash-latest is not found for API version v1 ..."
Google renames and retires model aliases from time to time. ERA hard-codes "gemini-flash-latest" in ~15 places
(news, research, code writing, translation, Telegram ...). Instead of editing each one, install() wraps the SDK's
generate_content once: when a call fails with 404/not found, it asks Google which models YOUR key can really use
(client.models.list), picks the best Flash model, retries, and remembers the choice for the rest of the session.

You can also pin one yourself: config/api_keys.json -> "text_model": "gemini-2.5-flash".
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from core.paths import app_dir

API_CONFIG = app_dir() / "config" / "api_keys.json"
STATIC_FALLBACKS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
_RESOLVED: dict[str, str] = {}              # requested name -> name that worked
_lock = threading.Lock()
_BAD = ("image", "tts", "live", "audio", "embedding", "robotics", "computer-use", "thinking", "exp", "learnlm", "gemma", "vision", "veo", "imagen")


def is_not_found(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return ("404" in s and "not_found" in s.replace(" ", "_")) or "is not found" in s or "not supported for generatecontent" in s


def _pinned() -> str:
    try:
        return str(json.loads(API_CONFIG.read_text(encoding="utf-8")).get("text_model", "")).strip()
    except Exception:
        return ""


def rank(names: list[str], want_lite: bool) -> list[str]:
    """Best usable text model first: stable over preview, newer over older, lite only when lite was asked for."""
    def score(n: str):
        ver = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        v = float(ver.group(1)) if ver else 0.0
        return (("lite" in n) == want_lite, "preview" not in n, "latest" not in n or v == 0, v, -len(n))
    ok = [n for n in names if "flash" in n and not any(b in n for b in _BAD)]
    return sorted(ok, key=score, reverse=True)


def discover(models_api, want_lite: bool) -> list[str]:
    names = []
    try:
        for m in models_api.list():
            acts = [str(a) for a in (getattr(m, "supported_actions", None) or [])]
            if acts and not any("generatecontent" in a.lower() for a in acts):
                continue
            names.append(str(getattr(m, "name", "")).replace("models/", ""))
    except Exception:
        pass
    return rank(names, want_lite)


def wrap(orig):
    def generate_content(self, *args, **kwargs):
        req = kwargs.get("model") or ""
        with _lock:
            use = _RESOLVED.get(req) or (_pinned() if req.startswith("gemini-flash") or req.startswith("gemini-2") else "")
        if use and use != req and not args:
            kwargs["model"] = use
        try:
            return orig(self, *args, **kwargs)
        except Exception as e:
            if args or not req or not is_not_found(e):
                raise
            last = e
            tried = {kwargs.get("model")}
            for alt in discover(self, "lite" in req) + STATIC_FALLBACKS:
                if alt in tried:
                    continue
                tried.add(alt)
                try:
                    kwargs["model"] = alt
                    out = orig(self, *args, **kwargs)
                    with _lock:
                        _RESOLVED[req] = alt
                    print(f"[Gemini] '{req}' was not found; using '{alt}' instead.")
                    return out
                except Exception as e2:
                    last = e2
                    if not is_not_found(e2):
                        raise
            raise last
    generate_content._era_compat = True
    generate_content.__wrapped__ = orig
    return generate_content


def install() -> bool:
    try:
        from google.genai import models as _m
    except Exception:
        return False
    if getattr(_m.Models.generate_content, "_era_compat", False):
        return True
    _m.Models.generate_content = wrap(_m.Models.generate_content)
    return True
