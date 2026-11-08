# ERA AI Pro v2 — Calendar, Gmail, WhatsApp, Instagram, any IDE

New tools (auto-discovered, no `main.py` change): `calendar_manager`, `gmail_manager`, `messenger`, `contacts`, `ide_control`.
Prompt routing is in `core/prompt.txt` ("PERSONAL TOOLS"). Install once: `pip install -r requirements.txt` then `python -m playwright install chromium`.

## 1. Google Calendar + Gmail (one login for both)
1. console.cloud.google.com → new project → **APIs & Services → Library** → enable **Google Calendar API** and **Gmail API**.
2. **OAuth consent screen**: External, add yourself as a *Test user* (your Gmail).
3. **Credentials → Create credentials → OAuth client ID → Desktop app** → download JSON → save as `config/google_credentials.json`.
4. Say: **"connect my Google account"** → browser opens → approve. Token is saved in `config/google_token.json` (keep private; say "disconnect Google" to delete it).

Examples: "What's on my calendar today?" · "Schedule study session tomorrow at 3 pm for 2 hours with a Meet link" · "Am I free Friday for an hour?" · "Move the standup to 5" · "Cancel the dentist" (asks to confirm) · "Any new email?" · "Read my latest email" · "Email Ali the report" (asks to confirm) · "Reply: sounds good" (asks to confirm).
Safety: sending/replying/deleting always waits for your button press. Email text is marked *untrusted*, so instructions hidden inside an email are not obeyed.

## 2. WhatsApp and Instagram
Say **"log in to WhatsApp"** → a browser window opens → scan the QR (Phone → Linked devices). Login is kept in `memory/browser_profiles/`.
Save numbers once: "Save Ali's WhatsApp number 0300 1234567" (default country code 92, change with `default_country_code` in `config/api_keys.json`).
Examples: "WhatsApp Ali: main late hoon" · "Any unread WhatsApp?" · "What did Sara say on WhatsApp?" · "DM zain.khan on Instagram: nice work" (Instagram: "log in to Instagram" once).
Optional `config/api_keys.json` keys: `messenger_confirm` (false = send without the confirm button), `messenger_headless`, `messenger_keep_open`, `default_ide`.

## 3. Editors / IDEs
"Open VS Code" · "Open my project folder in PyCharm" · "Open Cursor and write a script that renames photos by date" · "Write a snake game in Java and open it in IntelliJ".
`write_and_open` generates real code with Gemini, saves it to `Documents/Jarvis/code/`, and opens that file in the IDE. `type_live` pastes code into whatever editor is focused. VS Code, Cursor, Windsurf, PyCharm, IntelliJ, WebStorm, Android Studio, Sublime, Notepad++, Vim. For full multi-file projects the existing `dev_agent` is still the right tool.

## Honest status — what was and was not tested
Tested here (56 tests, `python -m pytest tests`): date parsing, free-slot maths, event/email request bodies against a faked Google API, email body/MIME building (Urdu text, attachments), confirm-before-send flow, contact matching, WhatsApp and Instagram send/read logic in a **real Chromium against local mock pages**, IDE name resolution, launch commands and file saving, tool schemas load in ERA's real loader.

**Not testable in this sandbox — please try these yourself:**
- Real Google sign-in and live Calendar/Gmail calls (logic is tested against fakes, the OAuth screen is not).
- The real WhatsApp Web and Instagram pages. They have no official API for personal accounts, and their HTML changes; if a send stops working, fix the selectors in `SELECTORS` at the top of `actions/messenger.py` (one place). Sends are verified by whether the message box empties; if unsure, ERA says "not confirmed" instead of claiming success.
- Instagram's terms discourage automation and may flag unusual activity. Use it for your own normal messages only, never bulk.
- Launching real IDEs on your PC (command building is tested; I have no IDEs here) and `type_live` (needs a focused editor).
- The full Gemini Live voice loop and the PyQt window, as in v1.
