import json
import os
import sys
import datetime

import requests
from playwright.sync_api import sync_playwright

URL = "https://mobile.a-bly.com/screens?screen_name=BEAUTY_RANKING&next_token=eyJiZWF1dHlfcmFua2luZ190eXBlIjogMX0%3D"
WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
TOP_N = int(os.getenv("TOP_N", "20"))
DEBUG = os.getenv("DEBUG") == "1"


def capture_json():
    """페이지를 열고, 그동안 받은 a-bly.com의 JSON 응답을 모두 모은다."""
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
        page.wait_for_timeout(3000)
        status = resp.status if resp else None
        browser.close()
    return status, payloads


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


def post_slack(text):
    r = requests.post(WEBHOOK, json={"text": text}, timeout=15)
    r.raise_for_status()


def main():
    status, payloads = capture_json()

    if DEBUG:
        with open("debug_payloads.json", "w", encoding="utf-8") as f:
            json.dump([{"url": u, "data": d} for u, d in payloads], f, ensure_ascii=False, indent=2)
        print(f"status={status}, JSON 응답 {len(payloads)}개를 debug_payloads.json에 저장")

    goods, seen = [], set()
    for _, data in payloads:
        found = []
        find_goods(data, found)
        for g in found:
            if g["name"] not in seen:
                seen.add(g["name"])
                goods.append(g)

    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d")

    if status != 200 or not goods:
        post_slack(f":warning: {today} 에이블리 뷰티랭킹 수집 실패 (status={status}, 상품 {len(goods)}개)")
        sys.exit(1)

    lines = [f"*에이블리 뷰티 랭킹 TOP {min(TOP_N, len(goods))}* ({today})"]
    for i, g in enumerate(goods[:TOP_N], 1):
        brand = f"[{g['brand']}] " if g["brand"] else ""
        lines.append(f"{i}. {brand}{g['name']} · {g['price']:,}원")
    lines.append(f"<{URL}|랭킹 전체 보기>")
    post_slack("\n".join(lines))


if __name__ == "__main__":
    main()
