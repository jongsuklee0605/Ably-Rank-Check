"""
에이블리 상품 랭킹에 캔메이크 상품이 있는지 확인하고 Slack으로 알림을 보냅니다.

1) 캔메이크 브랜드관(/markets/116160)에서 캔메이크 상품 ID 목록 수집
2) 랭킹 페이지에서 상품 ID를 순서대로 수집
3) 겹치는 상품의 순위를 Slack으로 전송
"""
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests
from playwright.sync_api import sync_playwright

MARKET_ID = os.getenv("MARKET_ID", "116160")       # 캔메이크
BRAND_NAME = os.getenv("BRAND_NAME", "캔메이크")
MARKET_URL = f"https://mobile.a-bly.com/markets/{MARKET_ID}"
RANKING_URL = os.environ["RANKING_URL"]
TOP_N = int(os.getenv("TOP_N", "100"))
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

KST = timezone(timedelta(hours=9))
GOODS_RE = re.compile(r"/goods/(\d+)")


def collect_goods_ids(page, url, limit, max_scroll=40, screenshot=None):
    """페이지를 스크롤하며 상품 ID를 화면 순서대로 수집"""
    page.goto(url, wait_until="networkidle", timeout=60000)
    ids, stale = [], 0

    for _ in range(max_scroll):
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
        current = []
        for h in hrefs:
            m = GOODS_RE.search(h)
            if m and m.group(1) not in current:
                current.append(m.group(1))

        stale = stale + 1 if len(current) == len(ids) else 0
        ids = current
        if len(ids) >= limit or stale >= 3:   # 목표 개수 도달 또는 더 이상 로드 안 됨
            break

        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1200)

    if screenshot:
        page.screenshot(path=screenshot)
    return ids[:limit]


def check_ranking():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 430, "height": 932},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            locale="ko-KR",
        )
        try:
            brand_goods = set(collect_goods_ids(
                page, MARKET_URL, limit=1000, max_scroll=60,
                screenshot="debug_market.png",
            ))
            ranking = collect_goods_ids(
                page, RANKING_URL, limit=TOP_N,
                screenshot="debug_ranking.png",
            )
        finally:
            browser.close()

    if not brand_goods:
        raise RuntimeError(f"{BRAND_NAME} 브랜드관에서 상품을 찾지 못했습니다.")
    if not ranking:
        raise RuntimeError("랭킹 페이지에서 상품을 찾지 못했습니다.")

    hits = [(i + 1, gid) for i, gid in enumerate(ranking) if gid in brand_goods]
    return ranking, hits, len(brand_goods)


def send_slack(text):
    res = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    res.raise_for_status()


def main():
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    try:
        ranking, hits, brand_cnt = check_ranking()
    except Exception as e:
        send_slack(f":warning: [{now}] 에이블리 랭킹 조회 실패: {e}\n"
                   f"(GitHub Actions 실행 결과의 debug 스크린샷을 확인하세요)")
        sys.exit(1)

    if hits:
        lines = [
            f"• *{rank}위* <https://mobile.a-bly.com/goods/{gid}|상품 보기>"
            for rank, gid in hits
        ]
        msg = (f":tada: [{now}] {BRAND_NAME} 에이블리 랭킹 진입! "
               f"(TOP {len(ranking)} 중 {len(hits)}개)\n" + "\n".join(lines))
    else:
        msg = (f":mag: [{now}] {BRAND_NAME} 상품은 현재 에이블리 랭킹 "
               f"TOP {len(ranking)}에 없습니다. (브랜드 상품 {brand_cnt}개 확인)")

    send_slack(msg)
    print(msg)


if __name__ == "__main__":
    main()