import os
import re
import json
import requests

KEY = os.environ["FIRECRAWL_API_KEY"]
URL = "https://mobile.a-bly.com/screens?screen_name=BEAUTY_RANKING&next_token=eyJiZWF1dHlfcmFua2luZ190eXBlIjogMX0%3D"

# 마지막 상품을 화면에 끌어와서 무한 스크롤을 트리거하고, 현재 상품 링크 개수를 돌려준다
SCROLL_JS = (
    "(() => {"
    "const a = [...document.querySelectorAll('a[href*=\"/goods/\"]')];"
    "if (a.length) { a[a.length - 1].scrollIntoView({block: 'end'}); }"
    "window.scrollTo(0, document.documentElement.scrollHeight);"
    "document.dispatchEvent(new Event('scroll'));"
    "window.dispatchEvent(new Event('scroll'));"
    "return a.length;"
    "})()"
)

# 어떤 요소가 스크롤되는지 진단
DIAG_JS = (
    "(() => {"
    "const out = [];"
    "document.querySelectorAll('*').forEach(el => {"
    "const s = getComputedStyle(el).overflowY;"
    "if ((s === 'auto' || s === 'scroll') && el.scrollHeight > el.clientHeight + 5) {"
    "out.push(el.tagName + '.' + String(el.className).slice(0, 40) + ' sh=' + el.scrollHeight + ' ch=' + el.clientHeight);"
    "}});"
    "return JSON.stringify({docH: document.documentElement.scrollHeight, winH: window.innerHeight, goods: document.querySelectorAll('a[href*=\"/goods/\"]').length, scrollers: out.slice(0, 8)});"
    "})()"
)


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
            if m.group(1).strip().lower() in ("캔메이크", "canmake") and last_rank not in hits:
                name = ""
                for k in range(i + 1, min(i + 4, len(lines))):
                    if re.fullmatch(r"\\+", lines[k]):
                        continue
                    name = re.sub(r"\]\(.*$", "", lines[k])
                    break
                hits[last_rank] = name
    return hits, max_rank


def run(label, mobile, loops, with_diag=False):
    actions = [{"type": "wait", "milliseconds": 3000}]
    if with_diag:
        actions.append({"type": "executeJavascript", "script": DIAG_JS})
    for _ in range(loops):
        actions += [
            {"type": "executeJavascript", "script": SCROLL_JS},
            {"type": "wait", "milliseconds": 2000},
        ]
    if with_diag:
        actions.append({"type": "executeJavascript", "script": DIAG_JS})
    body = {
        "url": URL,
        "formats": ["markdown"],
        "actions": actions,
        "mobile": mobile,
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
    print("\n=====", label, "status:", r.status_code, "=====")
    try:
        j = r.json()
    except Exception:
        print(r.text[:300])
        return
    data = j.get("data") or j
    md = data.get("markdown", "") or ""
    rets = [x.get("value") for x in (data.get("actions") or {}).get("javascriptReturns", [])]
    print("javascriptReturns:", [str(x)[:260] for x in rets])
    hits, max_rank = parse_md(md)
    print("길이:", len(md), "/ 파싱 최대 순위:", max_rank)
    print("캔메이크 순위:", {k: hits[k][:25] for k in sorted(hits)})
    with open("firecrawl_out_" + label + ".md", "w", encoding="utf-8") as f:
        f.write(md)
    if not md:
        print(json.dumps(j, ensure_ascii=False)[:400])


run("mobile", True, 12, with_diag=True)
run("desktop", False, 12)
