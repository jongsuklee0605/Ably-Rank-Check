import os
import re
import sys
import time
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
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# 브라우저/기기 설정
BROWSER = os.getenv("BROWSER", "chromium").strip().lower()
CHANNEL = os.getenv("CHANNEL", "").strip()
DEVICE = os.getenv("DEVICE", "Pixel 7").strip()

# 요청 차단 설정
BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "false").lower() == "true"
BLOCK_HOSTS = ["api2.amplitude.com"]

# 로딩 대기 설정
STALE_SEC = int(os.getenv("STALE_SEC", "120"))
MAX_SEC = int(os.getenv("MAX_SEC", "360"))
SCAN_TRIES = int(os.getenv("SCAN_TRIES", "1"))
NUDGE_EVERY = 10

KST = timezone(timedelta(hours=9))
RANK_RE = re.compile(r"^\d{1,3}$")
SCROLL_JS = "() => { window.scrollBy(0, 1800); document.querySelectorAll('*').forEach(el => { const s = getComputedStyle(el).overflowY; if ((s === 'auto' || s === 'scroll') && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollTop + 1800; } }); }"
LAST_ITEM_JS = """() => {
  const els = [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 && /^\\d{1,3}$/.test((e.textContent || '').trim()));
  let best = null, bv = 0;
  for (const e of els) { const v = parseInt(e.textContent.trim()); if (v > bv) { bv = v; best = e; } }
  if (best) best.scrollIntoView({block: 'center'});
  return bv;
}"""
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US']});"
STEALTH_JS_CHROME = "window.chrome = {runtime: {}};"
CHALLENGE_WORDS = ["잠시만 기다리", "just a moment", "attention required", "checking your browser"]
AUTH_WORDS = ["인증되지 않은 유저"]
VERIFY_WORDS = {"뷰티": ["스킨케어", "포인트 메이크업", "베이스 메이크업"]}
POPUP_TEXTS = ["오늘 하루 보지 않기", "다음에 할게요", "괜찮아요", "웹으로 볼게요", "모바일 웹으로 볼게요", "웹에서 계속하기", "닫기"]
MAIN_URL = "https://mobile.a-bly.com/"
OPEN_RETRY = 3
TAB_MAX_Y = 800

NET_STATS = {"goods_ok": 0, "goods_fail": 0}


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


def fix_auth(page):
    for i in range(3):
        page.wait_for_timeout(2000)
        try:
            body = page.inner_text("body")
        except Exception:
            return
        if not any(w in body for w in AUTH_WORDS):
            if i > 0:
                log("[auth] 인증 복구 성공")
            return
        log("[auth] '인증되지 않은 유저' 화면 - 새로고침 " + str(i + 1) + "회차")
        try:
            btn = page.get_by_text("새로고침", exact=True)
            if btn.count():
                btn.first.click(timeout=3000)
            else:
                page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        page.wait_for_timeout(5000)
    raise RuntimeError("에이블리가 '인증되지 않은 유저'로 접속을
