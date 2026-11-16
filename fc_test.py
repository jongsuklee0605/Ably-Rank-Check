import os, requests

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
r = requests.post("https://api.firecrawl.dev/v2/scrape",
                  headers={"Authorization": "Bearer " + KEY}, json=body, timeout=180)
print("status:", r.status_code)
j = r.json()
md = (j.get("data") or j).get("markdown", "")
open("firecrawl_out.md", "w", encoding="utf-8").write(md)
print("길이:", len(md), "/ 캔메이크 포함:", ("캔메이크" in md) or ("canmake" in md.lower()))
print(md[:500])
