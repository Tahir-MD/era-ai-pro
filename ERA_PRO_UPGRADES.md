# ERA AI Pro: advanced upgrades

Same ERA AI you already have (Gemini Live API, PyQt HUD, wake word, plugins, dashboard, memory, undo) with **six new
advanced capabilities** added as drop-in modules. Nothing was removed. `main.py` only gained a few small hooks.

| New | What it does | Example (say it) |
|---|---|---|
| **knowledge_base** | Private "ask my documents" search over your PDFs, Word, PowerPoint, notes and code. Hybrid BM25 + Gemini embeddings, incremental indexing, answers name the source file | "Index my Documents/University folder" · "What did my notes say about gradient descent?" |
| **deep_research** | Plans sub-questions, researches each in parallel with live Google Search grounding, writes a structured **Word report with sources**, speaks the key points. Runs in the background | "Do deep research on federated learning in healthcare" |
| **run_python** | Writes and runs Python for exact answers: CSV analysis, statistics, charts (PNG). **You approve every run on screen**; clean environment, time and memory limits, dangerous code refused | "Plot the numbers in my sales.csv" |
| **routines** | Voice-created automations that chain any tool, can be scheduled or run at startup | "Create a routine called study mode: open VS Code, start focus for 25 minutes, say good luck" · "Run study mode every weekday at 9" |
| **assistant_mode** | Switch personality: study (tutor), coding, focus (minimal speech), brainstorm, or your own in `config/modes.json` | "Study mode" · "Back to normal" |
| **Telegram remote** | Control your PC from **anywhere** (not just home WiFi): type or send **voice notes**, get replies back, `/screenshot` | Send "open chrome" or a voice note from your phone |

## Setup
Same as ERA (`python setup.py`, then `python main.py`). New optional packages are already in `requirements.txt`
(`pypdf`, `python-docx`, `numpy`). Nothing else to install.

### Telegram remote (2 minutes)
1. Telegram -> **@BotFather** -> `/newbot` -> copy the token.
2. Add to `config/api_keys.json`: `"telegram_bot_token": "123456:ABC..."`
3. Start the assistant. The HUD log prints a 6-digit **pairing code**.
4. In your bot send `/pair 123456`. Only that chat is ever allowed; strangers get no reply at all, and 5 wrong codes lock pairing for 10 minutes.

### Knowledge base tips
* First time: "index my Documents folder" (runs in the background; it tells you when done). Say "re-index" later to refresh, only changed files are re-read.
* Semantic search uses your Gemini key. To keep files fully offline set `"kb_embeddings": false` in `config/api_keys.json` (keyword search still works well).

## Files added / changed
```
actions/knowledge_base.py   actions/deep_research.py   actions/python_sandbox.py
actions/routines.py         actions/modes.py           core/telegram_bridge.py
core/services.py            <- the small bridge background features use to talk to the live session
main.py                     <- ~35 lines: services.bind / start_all, conversation events, mode prompt
core/prompt.txt             <- routing rules for the new tools
tests/test_pro.py           <- 31 automated tests
```
Run tests: `pip install pytest && pytest tests/`

## Honest status
* **Verified by automated tests (31):** all new logic, with Gemini, Telegram and the live session faked: file indexing and search across
  md/txt/docx/pptx/pdf, incremental updates, hybrid fusion, routine parsing/scheduling/execution, mode switching, the sandbox (limits,
  clean env, refusal of dangerous code, human confirmation gate), Telegram pairing/lockout/commands/voice/mirroring, discovery of the
  new tools by ERA's real action loader with Gemini-valid schemas, all 12 plugins still loading, `main.py` wiring.
* **Not testable here, so verify on your PC:** the real Gemini Live session, the PyQt window, real Gemini embedding/search calls, a real
  Telegram bot. Gemini preview-model names and API shapes change; if `gemini-embedding-001` is rejected the knowledge base automatically
  falls back to keyword search and keeps working.
* `run_python` is a guard against accidents, not a security sandbox. That is why every run needs your CONFIRM on screen.

Based on ERA AI (usancreation/era-ai), licensed CC BY-NC 4.0: personal / non-commercial use, keep the attribution.
