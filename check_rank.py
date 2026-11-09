"""
에이블리 상품 랭킹에 캔메이크 상품이 있는지 확인하고 Slack으로 알림을 보냅니다.
"""
import os
import re
import sys
import traceback
from datetime import datetime, timezone, timedelta

import requests
from playwright.sync_api import sync_playwright

MARKET_ID = os.getenv("MARKET_ID", "116160")
BRAND_NAME = os.getenv("BRAND_NAME", "Canmake")
MARKET_URL = f"https://mobile.a-bly.com/markets/{MARKET_ID}"
RANKING_URL = os.environ["RANKING_URL"]
TOP_N = int(os.getenv("TOP_N", "100"))
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

KST = timezone(timedelta(hours=9))
GOODS_RE = re.compile(r"/goods/(\d+)")


def log(msg):
    print(msg, flush=True)


def collect_goods_ids(page, url, limit, max_scroll=40, screenshot=None):
    """페이지를 스크롤하며 상품 ID를 화면 순서대로 수집"""
    log(f"[열기] {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)   # 화면이 그려질 시간
        log(f"[로드됨] 제목: {page.title()} / 현재 주소: {page.url}")

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
            if len(ids) >= limit or stale >= 3:
                break
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)

        log(f"[수집] 상품 {len(ids)}개 / 전체 링크 {len(hrefs)}개")
        if not ids:
            log("[참고] 링크 예시: " + ", ".join(hrefs[:10]))
        return ids[:limit]
    finally:
        if screenshot:
            try:
                page.screenshot(path=screenshot)
            except Exception:
                pass


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
