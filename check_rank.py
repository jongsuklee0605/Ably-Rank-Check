import os
import re
import sys
import traceback
from datetime import datetime, timezone, timedelta

import requests
from playwright.sync_api import sync_playwright

MARKET_ID = os.getenv("MARKET_ID", "116160")
BRAND_NAME = os.getenv("BRAND_NAME", "Canmake")
MARKET_URL = "https://mobile.a-bly.com/markets/" + MARKET_ID
RANKING_URL = os.environ["RANKING_URL"]
TOP_N = int(os.getenv("TOP_N", "100"))
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

KST = timezone(timedelta(hours=9))
GOODS_RE = re.compile(r"/goods/(\d+)")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
JS_LINKS = "els => els.map(a => a.href)"


def log(msg):
    print(msg, flush=True)


def collect_goods_ids(page, url, limit, max_scroll, screenshot):
    log("[open] " + url)
    hrefs = []
    ids = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
        log("[loaded] title=" + page.title() + " / url=" + page.url)

        stale = 0
        for _ in range(max_scroll):
            hrefs = page.eval_on_selector_all("a[href]", JS_LINKS)
            current = []
            for h in hrefs:
                m = GOODS_RE.search(h)
                if m and m.group(1) not in current:
                    current.append(m.group(1))

            if len(current) == len(ids):
                stale += 1
            else:
                stale = 0
            ids = current

            if len(ids) >= limit or stale >= 3:
                break
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)

        log("[found] goods=" + str(len(ids)) + " / links=" + str(len(hrefs)))
        if not ids:
            log("[sample links] " + ", ".join(hrefs[:10]))
        return ids[:limit]
    finally:
        try:
            page.screenshot(path=screenshot)
        except Exception:
            pass


def check_ranking():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 430, "height": 932}, user_agent=UA, locale="ko-KR")
        try:
            brand_goods = set(collect_goods_ids(page, MARKET_URL, 1000, 60, "debug_market.png"))
            ranking = collect_goods_ids(page, RANKING_URL, TOP_N, 40, "debug_ranking.png")
        finally:
            browser.close()

    if not brand_goods:
        raise RuntimeError(BRAND_NAME + " 브랜드관에서 상품을 찾지 못했습니다.")
    if not ranking:
        raise RuntimeError("랭킹 페이지에서 상품을 찾지 못했습니다.")

    hits = []
    for i, gid in enumerate(ranking):
        if gid in brand_goods:
            hits.append((i + 1, gid))
    return ranking, hits, len(brand_goods)


def send_slack(text):
    res = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    log("[slack] " + str(res.status_code) + " " + res.text)
    res.raise_for_status()


def main():
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    try:
        ranking, hits, brand_cnt = check_ranking()
    except Exception as e:
        traceback.print_exc()
        send_slack(":warning: [" + now + "] 에이블리 랭킹 조회 실패: " + str(e) + "\n(GitHub Actions의 debug 스크린샷을 확인하세요)")
        sys.exit(1)

    if hits:
        lines = []
        for rank, gid in hits:
            lines.append("• *" + str(rank) + "위* <https://mobile.a-bly.com/goods/" + gid + "|상품 보기>")
        msg = ":tada: [" + now + "] " + BRAND_NAME + " 에이블리 랭킹 진입! (TOP " + str(len(ranking)) + " 중 " + str(len(hits)) + "개)\n" + "\n".join(lines)
    else:
        msg = ":mag: [" + now + "] " + BRAND_NAME + " 상품은 현재 에이블리 랭킹 TOP " + str(len(ranking)) + "에 없습니다. (브랜드 상품 " + str(brand_cnt) + "개 확인)"

    log(msg)
    send_slack(msg)


if __name__ == "__main__":
    main()