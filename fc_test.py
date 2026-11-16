import os
import re
import json
import requests

KEY = os.environ["FIRECRAWL_API_KEY"]
URL = "https://mobile.a-bly.com/screens?screen_name=BEAUTY_RANKING&next_token=eyJiZWF1dHlfcmFua2luZ190eXBlIjogMX0%3D"

SCROLL_JS = (
    "(() => {"
    "window.scrollTo(0, document.body.scrollHeight);"
    "document.querySelectorAll('*').forEach(el => {"
    "const s = getComputedStyle(el).overflowY;"
    "if ((s === 'auto' || s === 'scroll') && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollHeight; }"
    "});"
    "return document.body.scrollHeight;"
    "})()"
)

actions = [{"type": "wait", "milliseconds": 3000}]
for _ in range(20):
    actions += [
        {"type": "executeJavascript", "script": SCROLL_JS},
        {"type": "wait", "milliseconds": 2000},
    ]

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
data = j.get("data") or j
md = data.get("markdown", "")
with open("firecrawl_out.md", "w", encoding="utf-8") as f:
    f.write(md)
print("길이:", len(md), "/ 캔메이크 포함:", ("캔메이크" in md) or ("canmake" in md.lower()))

# 스크롤 높이 변화 (커지면 로딩이 되고 있다는 뜻)
print("javascriptReturns:", json.dumps(data.get("actions", {}), ensure_ascii=False)[:600])


def parse_md(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    hits, max_rank, last_rank = {}, 0, None
    for i, l in enumerate(lines):
        if re.fullmatch(r"\d{1,3}", l):
            last_rank = int(l)
            max_rank = max(max_rank, last_rank)
            continue
        m = re.match(r"^\[([^\\\]\n]+)", l)
        if m and last_rank is not None:
            brand = m.group(1).strip().lower()
            if brand in ("캔메이크", "canmake") and last_rank not in hits:
                name = ""
                for k in range(i + 1, min(i + 4, len(lines))):
                    t = lines[k]
                    if re.fullmatch(r"\\+", t):
                        continue
                    name = re.sub(r"\]\(.*$", "", t)
                    break
                hits[last_rank] = name
    return hits, max_rank


hits, max_rank = parse_md(md)
print("파싱 최대 순위:", max_rank)
print("캔메이크 순위:", {k: hits[k][:30] for k in sorted(hits)})
print("--- 끝부분 ---")
print(md[-300:])
