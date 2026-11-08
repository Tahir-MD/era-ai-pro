"""
actions/deep_research.py - a research analyst that works in the background.

    "Do deep research on federated learning in healthcare"
        1. Gemini plans 3-8 sub-questions that together cover the topic
        2. each one is researched IN PARALLEL with live Google Search grounding (real, current sources)
        3. the findings are synthesised into one structured report
        4. saved as a Word file with a sources list, opened for you, and summarised out loud

It runs on a background thread: the voice conversation stays fully usable while it works, and the
assistant tells you when the report is ready.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core import services

BASE_DIR = Path(__file__).resolve().parent.parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
MODEL = "gemini-flash-latest"
DEPTH = {"quick": 3, "standard": 5, "deep": 8}
_GEN_OVERRIDE = None          # tests: fn(prompt, search, json_mode) -> (text, [ {"title","url"} ])
_active: set[str] = set()


# ── Gemini ───────────────────────────────────────────────────────────────────
def _gen(prompt: str, search: bool = False, json_mode: bool = False) -> tuple[str, list[dict]]:
    if _GEN_OVERRIDE is not None:
        return _GEN_OVERRIDE(prompt, search, json_mode)
    from google import genai
    key = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))["gemini_api_key"]
    client = genai.Client(api_key=key)
    cfg: dict = {}
    if search:
        cfg["tools"] = [{"google_search": {}}]
    if json_mode:
        cfg["response_mime_type"] = "application/json"
    resp = client.models.generate_content(model=MODEL, contents=prompt, config=cfg or None)
    cand = resp.candidates[0]
    text = "".join(getattr(p, "text", "") or "" for p in cand.content.parts).strip()
    sources = []
    gm = getattr(cand, "grounding_metadata", None)
    for ch in (getattr(gm, "grounding_chunks", None) or []):
        web = getattr(ch, "web", None)
        if web and getattr(web, "uri", None):
            sources.append({"title": getattr(web, "title", "") or web.uri, "url": web.uri})
    if not text:
        raise ValueError("Gemini returned an empty answer.")
    return text, sources


def plan_questions(topic: str, n: int, language: str) -> list[str]:
    prompt = (f"You are a research planner. Topic: \"{topic}\".\n"
              f"Write {n} specific, non-overlapping research questions that together give a complete, balanced understanding "
              f"(definitions/background, how it works, current state, evidence or numbers, benefits, risks/criticism, "
              f"future outlook - adapt to the topic). Questions must be in English for search quality.\n"
              f"Reply with ONLY a JSON array of {n} strings.")
    try:
        text, _ = _gen(prompt, json_mode=True)
        qs = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
        qs = [str(q).strip() for q in qs if str(q).strip()][:n]
        if qs:
            return qs
    except Exception as e:
        print(f"[DeepResearch] planning fallback: {e}")
    return [f"What is {topic} and why does it matter?", f"How does {topic} work?", f"What is the current state and evidence for {topic}?",
            f"What are the benefits and risks of {topic}?", f"What is the future outlook for {topic}?"][:n]


def research(topic: str, depth: str = "standard", language: str = "English", progress=None) -> dict:
    n = DEPTH.get(depth, 5)
    questions = plan_questions(topic, n, language)
    if progress:
        progress(f"Planned {len(questions)} questions")

    def one(q: str):
        try:
            text, srcs = _gen(f"Research this question thoroughly using current web sources. Give concrete facts, figures, "
                              f"dates and names; note disagreements between sources. Be dense, no fluff, max 250 words.\n\nQuestion: {q}",
                              search=True)
            return q, text, srcs
        except Exception as e:
            return q, f"(could not be researched: {e})", []

    with ThreadPoolExecutor(max_workers=4) as pool:
        findings = list(pool.map(one, questions))
    ok = [f for f in findings if not f[1].startswith("(could not")]
    if not ok:
        raise RuntimeError("None of the research questions could be answered (check your internet / API key).")
    if progress:
        progress("Writing the report")

    notes = "\n\n".join(f"### {q}\n{t}" for q, t, _ in findings)
    report_md, _ = _gen(
        f"Write a professional research report on \"{topic}\" in {language}, using ONLY the research notes below.\n"
        "Structure in Markdown: '# <Title>', '## Executive summary' (4 sentences), '## Key findings' (6-8 bullets starting with '- '), "
        "then 3-5 themed '## ' sections with short paragraphs, '## Risks and open questions', '## Conclusion'. "
        "Do not invent facts or sources that are not in the notes. If notes conflict, say so.\n\nRESEARCH NOTES:\n" + notes)
    seen, sources = set(), []
    for _, _, srcs in findings:
        for s in srcs:
            if s["url"] not in seen:
                seen.add(s["url"])
                sources.append(s)
    try:
        spoken, _ = _gen(f"In {language}, in 2-3 short natural spoken sentences (no markdown, no lists), give the most important "
                         f"takeaways of this report:\n\n{report_md[:3500]}")
    except Exception:
        spoken = _first_paragraph(report_md)
    return {"topic": topic, "markdown": report_md, "sources": sources, "spoken": spoken, "questions": questions}


def _first_paragraph(md: str) -> str:
    for blk in re.split(r"\n\s*\n", md):
        blk = blk.strip()
        if blk and not blk.startswith("#"):
            return re.sub(r"[*_`#-]", "", blk)[:400]
    return "The report is ready."


# ── Word output ──────────────────────────────────────────────────────────────
def _runs(par, text: str) -> None:
    for i, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if chunk:
            r = par.add_run(chunk)
            r.bold = bool(i % 2)


def md_to_docx(md: str, sources: list[dict], path: Path, topic: str = "") -> None:
    import docx
    d = docx.Document()
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            d.add_heading(m.group(2).strip(), level=len(m.group(1)) - 1 if len(m.group(1)) > 1 else 0)
        elif re.match(r"^\s*[-*•]\s+", line):
            _runs(d.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*•]\s+", "", line))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            _runs(d.add_paragraph(style="List Number"), re.sub(r"^\s*\d+[.)]\s+", "", line))
        else:
            _runs(d.add_paragraph(), line)
    if sources:
        d.add_heading("Sources", level=1)
        for i, s in enumerate(sources, 1):
            d.add_paragraph(f"{i}. {s['title']} - {s['url']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))


def _out_folder() -> Path:
    f = Path.home() / "Documents" / "Jarvis"
    try:
        f.mkdir(parents=True, exist_ok=True)
        return f
    except Exception:
        return Path.home()


def _open(path: Path) -> None:
    try:
        s = platform.system()
        if s == "Windows":
            os.startfile(str(path))      # type: ignore[attr-defined]
        elif s == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def _job(topic: str, depth: str, language: str, player, out_dir: Path | None = None, open_file: bool = True) -> str:
    def log(m):
        if player:
            try:
                player.write_log(f"SYS: Research - {m}")
            except Exception:
                pass
    try:
        res = research(topic, depth, language, progress=log)
        name = re.sub(r"[^A-Za-z0-9 ]+", "", topic).strip().replace(" ", "_")[:40] or "Research"
        folder = out_dir or _out_folder()
        path = folder / f"Research_{name}.docx"
        n = 2
        while path.exists():
            path = folder / f"Research_{name}_{n}.docx"
            n += 1
        md_to_docx(res["markdown"], res["sources"], path, topic)
        if open_file:
            _open(path)
        msg = (f"[DEEP_RESEARCH] The report on '{topic}' is finished and saved as {path.name} in {folder.name} "
               f"({len(res['sources'])} sources). Tell the user it is ready and give these key points in {language}, "
               f"naturally and briefly: {res['spoken']}")
        services.inject(msg)
        return str(path)
    except Exception as e:
        services.inject(f"[DEEP_RESEARCH] The research on '{topic}' failed: {str(e)[:150]}. Tell the user briefly.")
        return f"error: {e}"
    finally:
        _active.discard(topic.lower())


def deep_research(parameters: dict, player=None, speak=None) -> str:
    topic = str(parameters.get("topic", "")).strip()
    if not topic:
        return "What topic should I research?"
    depth = str(parameters.get("depth", "standard")).lower().strip()
    depth = depth if depth in DEPTH else "standard"
    language = str(parameters.get("language", "") or "English").strip()
    if topic.lower() in _active:
        return f"I am already researching '{topic}'."
    _active.add(topic.lower())
    threading.Thread(target=_job, args=(topic, depth, language, player), daemon=True, name="deep-research").start()
    eta = {"quick": "about a minute", "standard": "one to two minutes", "deep": "two to three minutes"}[depth]
    return (f"Deep research on '{topic}' started in the background ({depth}, {eta}). "
            f"Keep talking to the user normally; I will announce when the report is ready.")


TOOL = {
    "name": "deep_research",
    "description": (
        "In-depth multi-source research that produces a written Word report with sources. Use ONLY when the user asks for "
        "'deep research', 'a full report', 'research this thoroughly', 'investigate', 'detailed analysis' on a topic. "
        "Takes 1-3 minutes and runs in the background. For a quick factual question use web_search instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {"type": "STRING", "description": "The topic or question to research"},
            "depth": {"type": "STRING", "description": "quick | standard (default) | deep"},
            "language": {"type": "STRING", "description": "Language for the report, default English (e.g. Urdu)"},
        },
        "required": ["topic"],
    },
    "handler": deep_research,
}
