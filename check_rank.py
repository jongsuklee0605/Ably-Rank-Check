import os
import re
import sys
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright

BRAND_NAME = os.getenv("BRAND_NAME", "Canmake")
REPORT_TITLE = os.getenv("REPORT_TITLE", "에이블리 랭킹 체크")
BRAND_KEYWORDS = ["캔메이크", "canmake"]
DEFAULT_TOP_N = int(os.getenv("TOP_N", "100"))
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"].strip()
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

KST = timezone(timedelta(hours=9))
RANK_RE = re.compile(r"^\d{1,3}$")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
SCROLL_JS = "() => { window.scrollBy(0, 1800); document.querySelectorAll('*').forEach(el => { const s = getComputedStyle(el).overflowY; if ((s === 'auto' || s === 'scroll') && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollTop + 1800; } }); }"
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US']}); window.chrome = {runtime: {}};"
CHALLENGE_WORDS = ["잠시만 기다리", "just a moment", "attention required", "checking your browser"]
VERIFY_WORDS = {"뷰티": ["스킨케어", "포인트 메이크업", "베이스 메이크업"]}
MAIN_URL = "https://mobile.a-bly.com/"
OPEN_RETRY = 3
TAB_MAX_Y = 800  # 이 높이(px)보다 위에 있는 텍스트만 카테고리 후보로 봄


def log(msg):
    print(msg, flush=True)


def is_brand_line(line):
    t = line.strip().lower()
    for k in BRAND_KEYWORDS:
        if t == k.lower():
            return True
    return False


def is_challenge(page):
    try:
        t = page.title().lower()
    except Exception:
        return False
    for w in CHALLENGE_WORDS:
        if w in t:
            return True
    return False


def pass_challenge(page):
    page.wait_for_timeout(3000)
    if not is_challenge(page):
        return
    log("[challenge] 봇 확인 화면 감지 - 통과 대기")
    for _ in range(30):
        page.wait_for_timeout(1000)
        if not is_challenge(page):
            log("[challenge] 통과")
            return
    raise RuntimeError("에이블리 봇 확인 화면(Cloudflare)을 통과하지 못했습니다.")


def
