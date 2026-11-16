import json
import os
import sys
import datetime

import requests
from playwright.sync_api import sync_playwright

# ───────── 설정 ─────────
URL = "https://mobile.a-bly.com/screens?screen_name=BEAUTY_RANKING&next_token=eyJiZWF1dHlfcmFua2luZ190eXBlIjogMX0%3D"
WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
BRAND_KEYWORDS = [k.strip().lower() for k in os.getenv("BRAND_KEYWORDS", "캔메이크,canmake").split(",") if k.strip()]
SCROLLS = int(os.getenv("SCROLLS", "10"))   # 아래로 내려서 더 많은 순위를 불러오는 횟수
DEBUG = os.getenv("DEBUG") == "1"
KST = datetime.timezone(datetime.timedelta(hours=9))


# ───────── 수집 ─────────
def capture_json():
    """페이지를 열고 스크롤하면서 받은 a-bly.com의 JSON 응답을 모두 모은다."""
    payloads = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)

        def on_response(resp):
            if "a-bly.com" in resp.url and "application/json" in resp.headers.get("content-type", ""):
                try:
                    payloads.append((resp.url, resp.json()))
                except Exception:
                    pass

        page.on("response", on_response)
        resp = page.goto(URL, wait_until="networkidle", timeout=60000)
        status = resp.status if resp else None

        for _ in range(SCROLLS):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)

        browser.close()
    return status, payloads


# ───────── 파싱 ─────────
def text_of(v):
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and isinstance(v.get("name"), str):
        return v["name"]
    return ""


def find_goods(obj, out):
    """'name'과 숫자형 가격 필드를 함께 가진 객체를 상품으로 간주한다."""
    if isinstance(obj, dict):
        price_key = next(
            (k for k in obj if "price" in k.lower() and isinstance(obj[k], (int, float))), None
        )
        if isinstance(obj.get("name"), str) and price_key:
            brand = next(
                (text_of(obj[k]) for k in obj
                 if any(w in k.lower() for w in ("market", "brand")) and text_of(obj[k])),
                "",
            )
            out.append({"name": obj["name"], "brand": brand, "price": obj[price_key]})
        for v in obj.values():
            find_goods(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_goods(v, out)


def collect_goods(payloads):
    goods, seen = [], set()
    for _, data in payloads:
        found = []
        find_goods(data, found)
        for g in found:
            if g["name"] not in seen:
                seen.add(g["name"])
                goods.append(g)
    return goods


def is_target(g):
    text = f"{g['brand']} {g['name']}".lower()
    return any(k in text for k in BRAND_KEYWORDS)


# ───────── 슬랙 ─────────
def post_slack(text):
    r = requests.post(WEBHOOK, json={"text": text}, timeout=15)
    r.raise_for_status()


# ───────── 실행 ─────────
def main():
    status, payloads = capture_json()

    if DEBUG:
        with open("debug_payloads.json", "w", encoding="utf-8") as f:
            json.dump([{"url": u, "data": d} for u, d in payloads], f, ensure_ascii=False, indent=2)
        print(f"status={status}, JSON 응답 {len(payloads)}개를 debug_payloads.json에 저장")

    goods = collect_goods(payloads)
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")

    if status != 200 or not goods:
        post_slack(f":warning: {today} 에이블리 뷰티랭킹 수집 실패 (status={status}, 상품 {len(goods)}개)")
        sys.exit(1)

    hits = [(rank, g) for rank, g in enumerate(goods, 1) if is_target(g)]

    if DEBUG:
        print(f"전체 {len(goods)}개 중 캔메이크 {len(hits)}개")
        for rank, g in hits:
            print(f"  {rank}위 [{g['brand']}] {g['name']} {g['price']}")

    if not hits:
        post_slack(
            f"*에이블리 뷰티랭킹 · 캔메이크* ({today})\n"
            f"오늘은 상위 {len(goods)}위 안에 캔메이크 제품이 없어요.\n<{URL}|랭킹 보기>"
        )
        return

    lines = [f"*에이블리 뷰티랭킹 · 캔메이크* ({today}) — 상위 {len(goods)}위 중 {len(hits)}개"]
    for rank, g in hits:
        lines.append(f"• *{rank}위* {g['name']} · {g['price']:,}원")
    lines.append(f"<{URL}|랭킹 전체 보기>")
    post_slack("\n".join(lines))


if __name__ == "__main__":
    main()
