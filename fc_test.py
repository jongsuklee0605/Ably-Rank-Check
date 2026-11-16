import os
import re
import requests

KEY = os.environ["FIRECRAWL_API_KEY"]
URL = "https://mobile.a-bly.com/screens?screen_name=BEAUTY_RANKING&next_token=eyJiZWF1dHlfcmFua2luZ190eXBlIjogMX0%3D"

actions = [{"type": "wait", "milliseconds": 3000}]
for _ in range(15):
    actions += [{"type": "scroll", "direction": "down"}, {"type": "wait", "milliseconds": 1500}]

body = {
    "url": URL,
    "formats": ["markdown"],
    "actions": actions,
    "mobile": True,
    "proxy": "auto",
    "location": {"country": "KR", "languages": ["ko-KR"]},
    "timeout": 120000,
}

r = requests.post(
    "https://api.firecrawl.dev/v2/scrape",
    headers={"Authorization": "Bearer " + KEY},
    json=body,
    timeout=180,
)
print("status:", r.status_code)
j = r.json()
md = (j.get("data") or j).get("markdown", "")
with open("firecrawl_out.md", "w", encoding="utf-8") as f:
    f.write(md)
print("길이:", len(md), "/ 캔메이크 포함:", ("캔메이크" in md) or ("canmake" in md.lower()))

# --- 순위 분석 ---
lines = [l.strip() for l in md.split("\n") if l.strip()]
ranks = [int(l) for l in lines if re.fullmatch(r"\d{1,3}", l)]
print("순위 숫자 개수:", len(ranks), "/ 최대:", max(ranks) if ranks else 0)

shown = 0
for i, l in enumerate(lines):
    if "캔메이크" in l or "canmake" in l.lower():
        print("--- 캔메이크 주변 ---")
        print("\n".join(x[:120] for x in lines[max(0, i - 6): i + 4]))
        shown += 1
        if shown >= 2:
            break
