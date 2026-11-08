"""
actions/knowledge_base.py - "ask my documents": a private search engine over your own files.

    "Index my Documents/University folder"        -> reads pdf / docx / pptx / txt / md / code / csv ...
    "What did my notes say about gradient descent?" -> finds the right passages and answers from them,
                                                       naming the file
    "Which of my files mention the PBM scholarship?"

HOW IT SEARCHES (hybrid, like modern RAG systems)
  * BM25 keyword ranking - fully offline, always available, great for names / numbers / exact terms.
  * Gemini embeddings (gemini-embedding-001) - understands meaning ("loss minimisation" finds
    "gradient descent"). Used automatically when your API key works; if embeddings fail the feature
    keeps working on BM25 alone.
  * Reciprocal-rank fusion merges the two lists.
Indexing is incremental (unchanged files are skipped, deleted files are dropped). Everything is stored
locally in memory/knowledge.db - the files themselves are never uploaded; only text chunks are sent to
Gemini to be embedded, and only if embeddings are enabled (set "kb_embeddings": false in api_keys.json to disable).
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import threading
from collections import Counter
from pathlib import Path

from core import services

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "memory" / "knowledge.db"             # tests monkeypatch this
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

EXTS = {".txt", ".md", ".rst", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".json", ".csv",
        ".html", ".htm", ".xml", ".yml", ".yaml", ".ini", ".tex", ".pdf", ".docx", ".pptx"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "site-packages", "AppData", "$RECYCLE.BIN"}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_CHUNKS_PER_DOC = 500
CHUNK_CHARS, OVERLAP = 900, 150
EMBED_MODEL = "gemini-embedding-001"

_STOP = set("the a an and or of to in on for is are was were be been it this that with as at by from i you he she we they "
            "what which who how do does did can could should would my me your our their about into than then so if not no yes".split())
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_lock = threading.RLock()
_cache: dict = {"stamp": None, "rows": None, "df": None, "avgdl": 0.0}
_EMBED_OVERRIDE = None                    # tests inject a fake embedder: fn(list[str], task) -> list[list[float]]
_embed_disabled = False


# ── database ─────────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS docs(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, chunks INTEGER);
        CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, idx INTEGER, text TEXT, emb BLOB);
        CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
        CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY);
    """)
    return con


def _invalidate() -> None:
    _cache["stamp"] = None


# ── reading files ────────────────────────────────────────────────────────────
def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        r = PdfReader(str(path))
        return "\n\n".join((p.extract_text() or "") for p in r.pages)
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)
    if ext == ".pptx":
        from pptx import Presentation
        out = []
        for i, s in enumerate(Presentation(str(path)).slides, 1):
            out.append(f"[slide {i}]")
            for sh in s.shapes:
                if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                    out.append(sh.text_frame.text)
            if s.has_notes_slide:
                out.append(s.notes_slide.notes_text_frame.text)
        return "\n".join(out)
    raw = path.read_text(encoding="utf-8", errors="replace")
    if ext in (".html", ".htm", ".xml"):
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"<[^>]+>", " ", raw)
    return raw


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text.replace("\r", ""))
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:                              # a huge paragraph: cut on a sentence/space boundary
            cut = max(p.rfind(". ", 0, size), p.rfind(" ", 0, size))
            cut = cut + 1 if cut > size // 2 else size
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:cut].strip())
            p = p[max(cut - overlap, 1):].strip() if len(p) > cut else ""
        if not p:
            continue
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            tail = cur[-overlap:] if cur and overlap else ""
            cur = f"{tail} {p}".strip() if tail and len(tail) + len(p) <= size else p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) > 20]


# ── embeddings (optional) ────────────────────────────────────────────────────
def _kb_embeddings_enabled() -> bool:
    try:
        return bool(json.loads(API_CONFIG_PATH.read_text(encoding="utf-8")).get("kb_embeddings", True))
    except Exception:
        return True


def embed_texts(texts: list[str], task: str = "RETRIEVAL_DOCUMENT"):
    """list of vectors, or None when embeddings are unavailable (then BM25 alone is used)."""
    global _embed_disabled
    if _EMBED_OVERRIDE is not None:
        return _EMBED_OVERRIDE(texts, task)
    if _embed_disabled or not _kb_embeddings_enabled():
        return None
    try:
        from google import genai
        key = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))["gemini_api_key"]
        client = genai.Client(api_key=key)
        out = []
        for i in range(0, len(texts), 50):
            resp = client.models.embed_content(model=EMBED_MODEL, contents=texts[i:i + 50], config={"task_type": task})
            out.extend(list(e.values) for e in resp.embeddings)
        return out
    except Exception as e:
        _embed_disabled = True                              # one failure -> stay on BM25 for this run
        print(f"[KnowledgeBase] embeddings unavailable, using keyword search only: {e}")
        return None


def _pack(vec) -> bytes:
    import numpy as np
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v)) or 1.0
    return (v / n).tobytes()


def _unpack(blob: bytes):
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32)


# ── indexing ─────────────────────────────────────────────────────────────────
def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")]
        for f in fn:
            p = Path(dp) / f
            if p.suffix.lower() in EXTS and not f.startswith(("~$", ".")):
                yield p


def index_path(target: Path, progress=None) -> dict:
    """Index a file or folder (incremental). Returns counts."""
    stats = {"new": 0, "updated": 0, "unchanged": 0, "removed": 0, "failed": 0, "chunks": 0}
    with _lock:
        con = _db()
        try:
            con.execute("INSERT OR IGNORE INTO sources(path) VALUES(?)", (str(target),))
            seen = set()
            for f in _iter_files(target):
                try:
                    st = f.stat()
                    if st.st_size > MAX_FILE_BYTES or st.st_size == 0:
                        continue
                    key = str(f)
                    seen.add(key)
                    row = con.execute("SELECT mtime,size FROM docs WHERE path=?", (key,)).fetchone()
                    if row and abs(row[0] - st.st_mtime) < 1 and row[1] == st.st_size:
                        stats["unchanged"] += 1
                        continue
                    chunks = chunk_text(read_text(f))[:MAX_CHUNKS_PER_DOC]
                    if not chunks:
                        continue
                    vecs = embed_texts(chunks, "RETRIEVAL_DOCUMENT")
                    con.execute("DELETE FROM chunks WHERE path=?", (key,))
                    for i, c in enumerate(chunks):
                        emb = _pack(vecs[i]) if vecs and i < len(vecs) else None
                        con.execute("INSERT INTO chunks(path,idx,text,emb) VALUES(?,?,?,?)", (key, i, c, emb))
                    con.execute("INSERT OR REPLACE INTO docs(path,mtime,size,chunks) VALUES(?,?,?,?)", (key, st.st_mtime, st.st_size, len(chunks)))
                    stats["updated" if row else "new"] += 1
                    stats["chunks"] += len(chunks)
                    con.commit()
                    if progress:
                        progress(f"Indexed {f.name}")
                except Exception as e:
                    stats["failed"] += 1
                    print(f"[KnowledgeBase] skipped {f.name}: {e}")
            # drop documents that vanished from this source
            prefix = str(target)
            for (p,) in con.execute("SELECT path FROM docs").fetchall():
                if (p == prefix or p.startswith(prefix.rstrip(os.sep) + os.sep)) and p not in seen:
                    con.execute("DELETE FROM chunks WHERE path=?", (p,))
                    con.execute("DELETE FROM docs WHERE path=?", (p,))
                    stats["removed"] += 1
            con.commit()
        finally:
            con.close()
        _invalidate()
    return stats


# ── searching ────────────────────────────────────────────────────────────────
def tokenize(text: str) -> list[str]:
    return [w for w in (t.lower() for t in _WORD.findall(text)) if len(w) > 1 and w not in _STOP]


def _load_rows():
    con = _db()
    try:
        stamp = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(MAX(mtime),0) FROM docs").fetchone()
        if _cache["stamp"] == stamp and _cache["rows"] is not None:
            return _cache
        rows = con.execute("SELECT id,path,idx,text,emb FROM chunks").fetchall()
    finally:
        con.close()
    toks = [Counter(tokenize(r[3])) for r in rows]
    df = Counter()
    for t in toks:
        df.update(t.keys())
    _cache.update(stamp=stamp, rows=[(r, t) for r, t in zip(rows, toks)], df=df,
                  avgdl=(sum(sum(t.values()) for t in toks) / len(toks)) if toks else 1.0)
    return _cache


def bm25_rank(query: str, cache, k1: float = 1.4, b: float = 0.75) -> dict[int, float]:
    q = tokenize(query)
    n = len(cache["rows"])
    scores: dict[int, float] = {}
    if not q or not n:
        return scores
    for i, (row, tf) in enumerate(cache["rows"]):
        dl = sum(tf.values()) or 1
        s = 0.0
        for term in q:
            f = tf.get(term, 0)
            if not f:
                # light prefix tolerance: "scholarships" ~ "scholarship"
                f = next((c for w, c in tf.items() if len(term) > 4 and (w.startswith(term[:-1]) or term.startswith(w[:-1])) and len(w) > 4), 0) * 0.6
                if not f:
                    continue
            d = cache["df"].get(term, 0) or 1
            idf = math.log(1 + (n - d + 0.5) / (d + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / cache["avgdl"]))
        if s > 0:
            scores[i] = s
    return scores


def search(query: str, k: int = 5) -> list[dict]:
    with _lock:
        cache = _load_rows()
        if not cache["rows"]:
            return []
        bm = bm25_rank(query, cache)
        rankings = [sorted(bm, key=bm.get, reverse=True)[:50]]
        if any(r[0][4] for r in cache["rows"]):
            qv = embed_texts([query], "RETRIEVAL_QUERY")
            if qv:
                import numpy as np
                q = _unpack(_pack(qv[0]))
                sims = {i: float(np.dot(q, _unpack(r[0][4]))) for i, r in enumerate(cache["rows"]) if r[0][4]}
                rankings.append(sorted(sims, key=sims.get, reverse=True)[:50])
        fused: dict[int, float] = {}
        for rk in rankings:                                     # reciprocal rank fusion
            for pos, i in enumerate(rk):
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + pos)
        top = sorted(fused, key=fused.get, reverse=True)[:k]
        out = []
        for i in top:
            (_, path, idx, text, _), _tf = cache["rows"][i]
            out.append({"path": path, "idx": idx, "text": text, "score": round(fused[i], 4)})
        return out


# ── tool ─────────────────────────────────────────────────────────────────────
def _resolve(p: str) -> Path | None:
    if not p:
        return None
    cand = Path(os.path.expanduser(p))
    if cand.exists():
        return cand
    for base in (Path.home() / "Documents", Path.home() / "Desktop", Path.home() / "Downloads", Path.home() / "OneDrive" / "Documents"):
        if (base / p).exists():
            return base / p
    return None


def knowledge_base(parameters: dict, player=None, speak=None) -> str:
    action = str(parameters.get("action", "ask")).lower().strip()
    query = str(parameters.get("query", "")).strip()
    path = str(parameters.get("path", "")).strip()

    def log(m):
        if player:
            try:
                player.write_log(f"SYS: {m}")
            except Exception:
                pass

    if action == "index":
        if path:
            target = _resolve(path)
            if target is None:
                return f"I could not find '{path}'."
            targets = [target]
        else:
            con = _db()
            targets = [Path(p) for (p,) in con.execute("SELECT path FROM sources").fetchall() if Path(p).exists()]
            con.close()
            if not targets:
                return "Tell me which folder to index, for example your Documents folder."

        def work():
            total = {"new": 0, "updated": 0, "removed": 0, "failed": 0}
            for t in targets:
                s = index_path(t, progress=None)
                for k in total:
                    total[k] += s[k]
            msg = (f"[KNOWLEDGE_BASE] Indexing finished: {total['new']} new, {total['updated']} updated, "
                   f"{total['removed']} removed, {total['failed']} unreadable. Tell the user in one short sentence.")
            log(msg)
            services.inject(msg)

        threading.Thread(target=work, daemon=True, name="kb-index").start()
        return "Indexing started in the background. I will tell you when it is done."

    if action in ("ask", "search"):
        if not query:
            return "What should I look for in your documents?"
        hits = search(query, k=5 if action == "ask" else 8)
        if not hits:
            return "I found nothing in your indexed documents. If you have not indexed a folder yet, ask me to index it."
        lines = []
        for h in hits:
            name = Path(h["path"]).name
            snippet = re.sub(r"\s+", " ", h["text"]).strip()
            lines.append(f"[{name} #{h['idx']}] {snippet[:700]}")
        head = ("Answer ONLY from these passages and say which file the answer came from. If they do not contain the answer, "
                "say you could not find it in the user's documents.\n\n") if action == "ask" else "Matching passages:\n\n"
        return head + "\n\n".join(lines)

    if action == "list":
        con = _db()
        rows = con.execute("SELECT path, chunks FROM docs ORDER BY path").fetchall()
        srcs = [p for (p,) in con.execute("SELECT path FROM sources").fetchall()]
        con.close()
        if not rows:
            return "Nothing is indexed yet."
        sample = ", ".join(Path(p).name for p, _ in rows[:12])
        return f"{len(rows)} documents indexed from {len(srcs)} folder(s). Examples: {sample}."

    if action == "status":
        con = _db()
        d = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        c, e = con.execute("SELECT COUNT(*), SUM(emb IS NOT NULL) FROM chunks").fetchone()
        con.close()
        return f"{d} documents, {c} passages, semantic search on {int(e or 0)} of them."

    if action == "forget":
        con = _db()
        if path in ("", "all", "everything"):
            con.execute("DELETE FROM chunks"); con.execute("DELETE FROM docs"); con.execute("DELETE FROM sources")
            msg = "Forgot all indexed documents."
        else:
            like = f"%{path}%"
            n = con.execute("SELECT COUNT(*) FROM docs WHERE path LIKE ?", (like,)).fetchone()[0]
            con.execute("DELETE FROM chunks WHERE path LIKE ?", (like,))
            con.execute("DELETE FROM docs WHERE path LIKE ?", (like,))
            con.execute("DELETE FROM sources WHERE path LIKE ?", (like,))
            msg = f"Removed {n} document(s) matching '{path}' from the index."
        con.commit(); con.close(); _invalidate()
        return msg

    return "Use action: index, ask, search, list, status or forget."


TOOL = {
    "name": "knowledge_base",
    "description": (
        "Private search engine over the user's OWN files (pdf, docx, pptx, txt, notes, code). "
        "Use 'ask' for any question about the user's documents, notes, lecture slides, reports or projects: "
        "'what did my notes say about X', 'find in my files', 'which document mentions Y', 'meri files me se batao'. "
        "Use 'index' once with a folder path to teach it a folder (or re-index to refresh). "
        "It returns the matching passages with file names - answer only from them. "
        "NOT for the internet (use web_search) and NOT for reading one specific file (use file_processor)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "ask (default) | search | index | list | status | forget"},
            "query": {"type": "STRING", "description": "The question or keywords (ask/search)"},
            "path": {"type": "STRING", "description": "Folder or file to index / forget, e.g. Documents/University"},
        },
        "required": ["action"],
    },
    "handler": knowledge_base,
}
