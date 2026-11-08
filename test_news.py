# Run from the ERA folder:  python test_news.py   -> shows exactly which news source works or fails and why
import json, time
from actions import web_search as ws
for name, fn in [("Gemini", lambda: ws._gemini_search("top world news today")), ("DuckDuckGo", lambda: ws._ddg_news("world news today")),
                 ("Google News RSS", lambda: ws._rss_news(""))]:
    t = time.time()
    try:
        r = fn(); print(f"[OK]   {name}: {len(r)} chars/items in {time.time()-t:.1f}s")
    except Exception as e:
        print(f"[FAIL] {name} after {time.time()-t:.1f}s: {type(e).__name__}: {str(e)[:200]}")
