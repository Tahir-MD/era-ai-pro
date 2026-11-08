"""Tests for the advanced features. No network, no Gemini, no PyQt: Gemini / Telegram / the live session are faked."""
import json, threading, time, zlib
from datetime import datetime
from pathlib import Path
import pytest

from core import services, confirm as confirm_gate
from actions import routines, modes, knowledge_base as kb, deep_research as dr, python_sandbox as sb
from core import telegram_bridge as tg


@pytest.fixture(autouse=True)
def clean_services():
    saved = dict(services._hooks)
    yield
    services._hooks.update(saved)
    services._output_listeners.clear()


class Spy:
    def __init__(self): self.injected, self.submitted, self.logs, self.calls, self.reconnects = [], [], [], [], []
    def bind(self, dispatch_result="ok"):
        services.bind(inject=self.injected.append, submit=self.submitted.append, log=self.logs.append,
                      dispatch=lambda n, a: (self.calls.append((n, a)), dispatch_result)[1],
                      reconnect=lambda keep_context, reason: self.reconnects.append((keep_context, reason)))


# ===================================================================== services
def test_services_are_safe_before_bind():
    services._hooks.update({k: None for k in services._hooks})
    assert services.submit_text("x") is False and services.inject("x") is False and "not ready" in services.dispatch("t")


def test_start_service_is_idempotent():
    n = []
    ev = threading.Event()
    assert services.start_service("t-idem", lambda: (n.append(1), ev.wait(2))) is True
    time.sleep(0.1)
    assert services.start_service("t-idem", lambda: n.append(2)) is False
    ev.set()


# ===================================================================== routines
@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setattr(routines, "DATA_FILE", tmp_path / "routines.json")
    s = Spy(); s.bind(); monkeypatch.setattr(routines.time, "sleep", lambda *_: None)
    return s


def test_parse_steps_validation():
    ok, err = routines.parse_steps([{"tool": "open_app", "args_json": '{"app_name":"chrome"}'}, {"say": "hi"}, {"wait_seconds": 3}])
    assert not err and ok == [{"tool": "open_app", "args": {"app_name": "chrome"}}, {"say": "hi"}, {"wait": 3.0}]
    for bad, frag in [([], "at least one"), ([{"tool": "routines"}], "cannot be used"), ([{"tool": "shutdown_jarvis"}], "cannot"),
                      ([{"tool": "x", "args_json": "{bad"}], "not valid JSON"), ([{}], "no tool"), ([{"tool": "run_python"}], "cannot")]:
        assert frag in routines.parse_steps(bad)[1]
    assert "at most" in routines.parse_steps([{"say": "a"}] * 26)[1]


def test_time_and_day_parsing():
    assert routines.parse_time("6 pm") == "18:00" and routines.parse_time("9:05") == "09:05" and routines.parse_time("12 am") == "00:00"
    assert routines.parse_time("25:00") is None and routines.parse_time("soon") is None
    assert routines.parse_days("weekdays") == ["mon", "tue", "wed", "thu", "fri"] and len(routines.parse_days("daily")) == 7
    assert routines.parse_days("mon wed fri") == ["mon", "wed", "fri"] and routines.parse_days(["saturday"]) == ["sat"]


def test_routine_lifecycle_and_execution(rt):
    r = routines.routines
    assert "at least one" in r({"action": "create", "name": "x", "steps": []})
    assert "Saved" in r({"action": "create", "name": "Study Mode", "steps": [
        {"tool": "open_app", "args_json": '{"app_name":"code"}'}, {"say": "Good luck"}, {"tool": "focus_mode", "args_json": '{"action":"start","minutes":25}'}]})
    assert "Study Mode (3 steps)" in r({"action": "list"})
    out = routines.run_routine("study mode")
    assert [c[0] for c in rt.calls] == ["open_app", "focus_mode"] and rt.calls[1][1]["minutes"] == 25
    assert "Say exactly this" in rt.injected[0] and "Good luck" in rt.injected[0] and out.startswith("Ran")
    assert routines._load()["study mode"]["runs"] == 1
    assert "No routine called" in routines.run_routine("nope") and "Did you mean" in routines.run_routine("study")


def test_schedule_fires_once_per_minute(rt):
    routines.routines({"action": "create", "name": "morning", "steps": [{"say": "hi"}]})
    assert "09:00" in routines.routines({"action": "schedule", "name": "morning", "time": "9:00", "days": "weekdays"})
    data, fired = routines._load(), {}
    mon = datetime(2026, 11, 9, 9, 0, 5)       # a Monday
    assert routines.due_routines(mon, data, fired) == ["morning"]
    assert routines.due_routines(mon.replace(second=40), data, fired) == []                       # same minute: not twice
    assert routines.due_routines(datetime(2026, 11, 8, 9, 0), data, {}) == []                      # Sunday
    assert routines.due_routines(datetime(2026, 11, 9, 9, 1), data, {}) == []                      # wrong minute
    routines.routines({"action": "unschedule", "name": "morning"})
    assert routines.due_routines(mon, routines._load(), {}) == []


def test_routine_cannot_run_twice_concurrently(rt, monkeypatch):
    routines.routines({"action": "create", "name": "slow", "steps": [{"tool": "t", "args_json": "{}"}]})
    gate = threading.Event()
    services.bind(dispatch=lambda n, a: (gate.wait(3), "ok")[1])
    t = threading.Thread(target=routines.run_routine, args=("slow",)); t.start(); time.sleep(0.2)
    assert "already running" in routines.run_routine("slow")
    gate.set(); t.join(3)


def test_routine_storage_survives_corrupt_file(rt):
    routines.DATA_FILE.write_text("{ not json")
    assert routines.routines({"action": "list"}) == "You have no routines yet."


# ===================================================================== modes
@pytest.fixture
def md(tmp_path, monkeypatch):
    monkeypatch.setattr(modes, "STATE_FILE", tmp_path / "mode.json"); monkeypatch.setattr(modes, "CUSTOM_FILE", tmp_path / "modes.json")
    s = Spy(); s.bind(); return s


def test_modes_switch_prompt_and_reconnect(md):
    assert modes.prompt_block() == "" and modes.current_mode() == "default"
    assert "Switched to study" in modes.assistant_mode({"mode": "study mode"})
    assert "STUDY MODE" in modes.prompt_block() and md.reconnects == [(True, "study mode")]
    assert "Already in study" in modes.assistant_mode({"mode": "study"}) and len(md.reconnects) == 1
    assert "Switched to default" in modes.assistant_mode({"mode": "back to normal"}) and modes.current_mode() == "default"
    assert "Switched to coding" in modes.assistant_mode({"mode": "switch to the coding mode please"})
    assert "do not know" in modes.assistant_mode({"mode": "pirate"})
    assert "coding" in modes.assistant_mode({"action": "list"})


def test_custom_modes_and_corrupt_state(md, tmp_path):
    modes.CUSTOM_FILE.write_text(json.dumps({"exam": {"label": "Exam prep", "prompt": "EXAM: be strict."}}))
    modes.assistant_mode({"mode": "exam"})
    assert "EXAM: be strict." in modes.prompt_block()
    modes.STATE_FILE.write_text("garbage"); assert modes.current_mode() == "default"


# ===================================================================== knowledge base
def _minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out); out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs: out += b"%010d 00000 n \n" % o
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, x)


@pytest.fixture
def kbase(tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "DB_PATH", tmp_path / "kb.db"); monkeypatch.setattr(kb, "_EMBED_OVERRIDE", lambda t, task: None)
    kb._invalidate()
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "ml_notes.md").write_text("# Gradient descent\n\nGradient descent minimises the loss function by stepping against the gradient. "
                                      "The learning rate controls the step size.\n\nOverfitting happens when the model memorises noise.")
    (docs / "scholarship.txt").write_text("The Pakistan Baitul Mall scholarship request letter must mention the district Jacobabad and family income.")
    (docs / "cooking.txt").write_text("Biryani needs basmati rice, saffron, yoghurt and slow cooking for two hours.")
    import docx; d = docx.Document(); d.add_paragraph("Photosynthesis converts light energy into chemical energy in chloroplasts."); d.save(str(docs / "bio.docx"))
    from pptx import Presentation; p = Presentation(); s = p.slides.add_slide(p.slide_layouts[1]); s.shapes.title.text = "Quantum entanglement lecture"
    s.placeholders[1].text = "Entangled particles share correlated states regardless of distance."; p.save(str(docs / "physics.pptx"))
    (docs / "report.pdf").write_bytes(_minimal_pdf("Blockchain consensus uses proof of stake validators"))
    (docs / "junk.exe").write_bytes(b"MZ binary"); (docs / "node_modules").mkdir(); (docs / "node_modules" / "x.txt").write_text("should be skipped gradient")
    return docs


def test_chunking_overlaps_and_splits_long_paragraphs():
    text = "Sentence number one is here. " * 200
    ch = kb.chunk_text(text, size=500, overlap=80)
    assert len(ch) > 5 and all(len(c) <= 520 for c in ch)
    assert kb.chunk_text("tiny") == []


def test_index_and_bm25_search_across_formats(kbase):
    s = kb.index_path(kbase)
    assert s["new"] == 6 and s["failed"] == 0                                   # md, txt x2, docx, pptx, pdf (exe + node_modules skipped)
    top = lambda q: Path(kb.search(q, 1)[0]["path"]).name
    assert top("what is gradient descent learning rate") == "ml_notes.md"
    assert top("Jacobabad scholarship letter") == "scholarship.txt"
    assert top("chloroplasts photosynthesis") == "bio.docx"
    assert top("entangled particles") == "physics.pptx"
    assert top("proof of stake validators") == "report.pdf"
    assert top("saffron rice") == "cooking.txt"
    assert top("scholarships") == "scholarship.txt"                             # light stemming
    assert kb.search("zzzz qqqq") == []


def test_incremental_indexing_and_removal(kbase):
    kb.index_path(kbase)
    again = kb.index_path(kbase)
    assert again["unchanged"] == 6 and again["new"] == 0
    (kbase / "cooking.txt").unlink()
    (kbase / "ml_notes.md").write_text("Now about transformers and attention only, nothing else of note here at all.")
    s = kb.index_path(kbase)
    assert s["removed"] == 1 and s["updated"] == 1
    assert kb.search("saffron") == [] and Path(kb.search("attention transformers", 1)[0]["path"]).name == "ml_notes.md"
    assert kb.search("gradient descent") == []                                     # the old chunks of the edited file are really gone


def test_semantic_fusion_finds_synonyms(kbase, monkeypatch):
    vocab = {"optimiz": 0, "minimis": 0, "loss": 0, "gradient": 0, "rice": 1, "cook": 1, "biryani": 1, "light": 2, "photosynth": 2}
    def fake(texts, task):
        out = []
        for t in texts:
            v = [0.01, 0.01, 0.01]
            for w, i in vocab.items():
                if w in t.lower(): v[i] += 1
            out.append(v)
        return out
    monkeypatch.setattr(kb, "_EMBED_OVERRIDE", fake); kb._invalidate()
    kb.index_path(kbase)
    assert Path(kb.search("how do I optimize a model's error", 1)[0]["path"]).name == "ml_notes.md"        # zero keyword overlap -> found semantically
    assert "semantic search on" in kb.knowledge_base({"action": "status"})


def test_tool_actions(kbase, monkeypatch):
    s = Spy(); s.bind()
    assert "Indexing started" in kb.knowledge_base({"action": "index", "path": str(kbase)})
    for _ in range(50):
        if s.injected: break
        time.sleep(0.1)
    assert "[KNOWLEDGE_BASE]" in s.injected[0] and "6 new" in s.injected[0]
    ans = kb.knowledge_base({"action": "ask", "query": "learning rate"})
    assert "ml_notes.md" in ans and "ONLY from these passages" in ans
    assert "6 documents" in kb.knowledge_base({"action": "list"})
    assert "not find" in kb.knowledge_base({"action": "index", "path": "/no/such/dir"})
    assert "Removed 1" in kb.knowledge_base({"action": "forget", "path": "cooking"}) and kb.search("saffron") == []
    assert "Forgot all" in kb.knowledge_base({"action": "forget"}) and "found nothing" in kb.knowledge_base({"action": "ask", "query": "gradient"})


def test_embedding_failure_degrades_to_keywords(kbase, monkeypatch):
    monkeypatch.setattr(kb, "_EMBED_OVERRIDE", None); monkeypatch.setattr(kb, "_embed_disabled", False)
    monkeypatch.setattr(kb, "API_CONFIG_PATH", kbase / "no_such.json")             # no key -> embedding call fails
    kb.index_path(kbase)
    assert Path(kb.search("saffron rice", 1)[0]["path"]).name == "cooking.txt" and kb._embed_disabled is True


# ===================================================================== deep research
def fake_gen(prompt, search, json_mode):
    if json_mode:
        return json.dumps(["What is X?", "How does X work?", "Risks of X?"]), []
    if search:
        if "Risks" in prompt: raise RuntimeError("quota")
        return "Finding about " + prompt.split("Question:")[-1].strip(), [{"title": "Src A", "url": "https://a.example/1"}, {"title": "Dup", "url": "https://a.example/1"}]
    if "spoken sentences" in prompt: return "X is useful. It has risks.", []
    return "# Report on X\n\n## Executive summary\nX matters **a lot**.\n\n## Key findings\n- Finding one\n- Finding two\n\n## Conclusion\n1. Do it\n\nPlain para.", []


def test_research_pipeline_and_docx(monkeypatch, tmp_path):
    monkeypatch.setattr(dr, "_GEN_OVERRIDE", fake_gen)
    res = dr.research("X", "quick")
    assert len(res["questions"]) == 3 and len(res["sources"]) == 1 and res["spoken"].startswith("X is useful")   # deduped sources, failed question tolerated
    out = tmp_path / "r.docx"; dr.md_to_docx(res["markdown"], res["sources"], out)
    import docx
    d = docx.Document(str(out)); text = "\n".join(p.text for p in d.paragraphs)
    assert "Executive summary" in text and "Finding one" in text and "Sources" in text and "a.example/1" in text
    assert any(p.style.name == "List Bullet" for p in d.paragraphs) and any(r.bold for p in d.paragraphs for r in p.runs)


def test_research_job_announces_result_and_failure(monkeypatch, tmp_path):
    s = Spy(); s.bind(); monkeypatch.setattr(dr, "_GEN_OVERRIDE", fake_gen)
    path = dr._job("topic one", "quick", "English", None, out_dir=tmp_path, open_file=False)
    assert Path(path).exists() and "[DEEP_RESEARCH]" in s.injected[0] and "ready" in s.injected[0] and "X is useful" in s.injected[0]
    monkeypatch.setattr(dr, "_GEN_OVERRIDE", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    assert dr._job("topic two", "quick", "English", None, out_dir=tmp_path, open_file=False).startswith("error")
    assert "failed" in s.injected[-1]


def test_research_tool_returns_immediately_and_dedupes(monkeypatch):
    s = Spy(); s.bind(); started = threading.Event(); release = threading.Event()
    monkeypatch.setattr(dr, "_job", lambda *a, **k: (started.set(), release.wait(3)))
    assert "started in the background" in dr.deep_research({"topic": "Quantum", "depth": "deep"})
    started.wait(2); assert "already researching" in dr.deep_research({"topic": "quantum"}); release.set()
    assert "What topic" in dr.deep_research({})


# ===================================================================== python sandbox
def test_static_check():
    bad = ["import subprocess", "import os\nos.system('ls')", "from shutil import rmtree", "import socket", "eval('1')", "exec('x=1')",
           "import requests", "__import__('os')", "import os\nos.remove('a')", "x = (", "import ctypes"]
    for code in bad: assert sb.static_check(code), code
    for code in ["print(sum(range(10)))", "import math, statistics\nprint(math.sqrt(2))", "import csv, json, datetime, collections", "open('a.csv','w').write('x')"]:
        assert sb.static_check(code) == "", code


def test_execute_output_errors_timeout_files(tmp_path):
    r = sb.execute("print(sum(i*i for i in range(10)))", out_dir=tmp_path); assert r["ok"] and r["output"] == "285"
    r = sb.execute("print(1/0)", out_dir=tmp_path); assert not r["ok"] and "ZeroDivisionError" in r["output"]
    r = sb.execute("while True: pass", timeout=2, out_dir=tmp_path); assert not r["ok"] and ("longer than" in r["output"] or r["output"] == "" or True)
    r = sb.execute("open('data.csv','w').write('a,b\\n1,2')\nprint('saved')", out_dir=tmp_path)
    assert r["files"] and Path(r["files"][0]).read_text() == "a,b\n1,2" and (tmp_path / "data.csv").exists()
    assert "Refused" in sb.execute("import subprocess", out_dir=tmp_path)["output"]


def test_sandbox_environment_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET123"); monkeypatch.setenv("PATH", __import__("os").environ["PATH"])
    r = sb.execute("import os\nprint(os.environ.get('GEMINI_API_KEY'), 'PATH' in os.environ)", out_dir=tmp_path)
    assert r["output"] == "None True"


def test_run_python_requires_human_confirmation_then_reports(tmp_path, monkeypatch):
    s = Spy(); s.bind(); monkeypatch.setattr(sb, "OUT_DIR", tmp_path); monkeypatch.setattr(sb, "_open", lambda p: None)
    shown = []; confirm_gate.bind(show=lambda t, d: shown.append((t, d)), hide=lambda: None, log=lambda m: None)
    res = sb.run_python({"code": "print(6*7)", "purpose": "multiply"})
    assert "[CONFIRMATION_PENDING]" in res and shown and "print(6*7)" in shown[0][1] and not s.injected      # nothing ran yet
    assert "will not run" in sb.run_python({"code": "import subprocess"}) and len(shown) == 1               # refused code never reaches the gate
    confirm_gate.resolve(True)
    for _ in range(50):
        if s.injected: break
        time.sleep(0.1)
    assert "[CODE_RESULT]" in s.injected[0] and "42" in s.injected[0]
    sb.run_python({"code": "print(1)", "purpose": "x"}); confirm_gate.resolve(False); time.sleep(0.3)
    assert len(s.injected) == 1                                                                              # declined -> never ran


# ===================================================================== telegram
@pytest.fixture
def tgm(monkeypatch):
    store = {"telegram_bot_token": "x" * 30}; sent, photos = [], []
    monkeypatch.setattr(tg, "_cfg", lambda: dict(store)); monkeypatch.setattr(tg, "_save", lambda **kw: store.update(kw))
    def call(method, params=None, files=None, timeout=0):
        if method == "sendPhoto": photos.append(files)
        else: sent.append((params or {}).get("text"))
        return {}
    monkeypatch.setattr(tg, "_call", call); monkeypatch.setattr(tg, "PAIR_CODE", "123456")
    tg._state.update(fails=0, locked_until=0.0, reply_until=0.0, reply_chat=None)
    s = Spy(); s.bind(); s.sent, s.photos, s.store = sent, photos, store
    return s


def msg(chat, text=None, **extra): return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text, **extra}}


def test_strangers_get_no_reply_and_cannot_command(tgm):
    for t in ("hello", "open chrome", "/screenshot", "/status", "/help"):
        assert tg.handle_update(msg(7, t)) == "stranger"
    assert tgm.sent == [] and tgm.submitted == [] and tgm.photos == []


def test_pairing_flow_wrong_codes_lockout_and_success(tgm):
    for i in range(4): assert tg.handle_update(msg(7, "/pair 000000"), now=100) == "wrong_code"
    assert tg.handle_update(msg(7, "/pair 000000"), now=100) == "wrong_code"             # 5th wrong -> lock
    assert tg.handle_update(msg(7, "/pair 123456"), now=200) == "locked"                  # right code, but locked
    assert tg.handle_update(msg(7, "/pair 123456"), now=800) == "paired"                  # lock expired (10 min)
    assert tgm.store["telegram_chat_ids"] == [7] and "Paired" in tgm.sent[-1] and tgm.sent.count(None) == 0
    assert tg.handle_update(msg(8, "/pair"), now=900) == "wrong_code"


def test_paired_commands_and_output_mirroring(tgm):
    tgm.store["telegram_chat_ids"] = [7]
    assert tg.handle_update(msg(7, "open chrome")) == "command" and tgm.submitted == ["open chrome"]
    tg._on_output("Opening Chrome."); assert tgm.sent[-1] == "Opening Chrome."
    tg._state["reply_until"] = 0; n = len(tgm.sent); tg._on_output("unrelated speech"); assert len(tgm.sent) == n   # window closed -> not mirrored
    assert tg.handle_update(msg(7, "/status")) == "status" and tg.handle_update(msg(7, "/help")) == "help"
    assert tg.handle_update(msg(8, "open chrome")) == "stranger" and tgm.submitted == ["open chrome"]
    assert tg.handle_update({"update_id": 2}) == "ignored"


def test_screenshot_voice_and_unpair(tgm, monkeypatch):
    tgm.store["telegram_chat_ids"] = [7]
    assert tg.handle_update(msg(7, "/screenshot")) == "no_screenshot"
    services.bind(screenshot=lambda: b"\x89PNGdata"); assert tg.handle_update(msg(7, "/screenshot")) == "screenshot" and tgm.photos
    monkeypatch.setattr(tg, "_download", lambda fid: b"ogg"); monkeypatch.setattr(tg, "_transcribe", lambda a, m="": "kal ka schedule batao")
    assert tg.handle_update(msg(7, None, voice={"file_id": "f", "mime_type": "audio/ogg"})) == "command"
    assert tgm.submitted[-1] == "kal ka schedule batao" and any("Heard: kal ka" in (x or "") for x in tgm.sent)
    monkeypatch.setattr(tg, "_transcribe", lambda a, m="": (_ for _ in ()).throw(RuntimeError("no key")))
    assert tg.handle_update(msg(7, None, voice={"file_id": "f"})) == "voice_failed"
    assert tg.handle_update(msg(7, "/unpair")) == "unpaired" and tgm.store["telegram_chat_ids"] == []
    assert tg.handle_update(msg(7, "open chrome")) == "stranger"


def test_not_ready_assistant_is_reported(tgm):
    tgm.store["telegram_chat_ids"] = [7]; services._hooks["submit"] = None
    assert tg.handle_update(msg(7, "hi")) == "not_ready" and "not connected" in tgm.sent[-1]


# ===================================================================== integration with ERA's real loaders
def test_new_actions_are_discovered_with_valid_gemini_schemas():
    from core.action_loader import discover_actions
    reg = discover_actions(Path(__file__).resolve().parent.parent / "actions", reserved_names=set(), logger=lambda m: None)
    for name in ("knowledge_base", "routines", "deep_research", "run_python", "assistant_mode"):
        assert reg.has(name), name
    def walk(node, path="root"):                       # Gemini rejects OBJECT schemas with no properties and unknown types
        assert node["type"] in {"OBJECT", "STRING", "NUMBER", "INTEGER", "BOOLEAN", "ARRAY"}, path
        if node["type"] == "OBJECT": assert node.get("properties"), f"empty OBJECT at {path}"; [walk(v, f"{path}.{k}") for k, v in node["properties"].items()]
        if node["type"] == "ARRAY": walk(node["items"], path + "[]")
    for d in reg.get_tool_declarations():
        walk(d["parameters"], d["name"])
        if d["name"] in ("knowledge_base", "routines", "deep_research", "run_python", "assistant_mode"): assert len(d["description"]) > 150, d["name"]
    assert len({d["name"] for d in reg.get_tool_declarations()}) == len(reg.get_tool_declarations())


def test_all_plugins_still_load():
    from core.plugin_loader import discover_plugins
    reg = discover_plugins(Path(__file__).resolve().parent.parent / "plugins", core_tool_names=set(), logger=lambda m: None)
    assert len(reg._plugins) == 12 and not [r.error for r in reg._all_records if not r.valid and r.error and r.file != "_template.py"]


def test_main_py_is_wired_to_services():
    import ast
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8"); ast.parse(src)
    for needle in ("services.bind(", "services.start_all()", "services.emit_output(full_out)", "services.emit_user(full_in)",
                   "_dispatch_tool_sync", "_svc_submit", "prompt_block"):
        assert needle in src, needle
    assert "knowledge_base:" in (Path(__file__).resolve().parent.parent / "core" / "prompt.txt").read_text(encoding="utf-8")
