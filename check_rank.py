import os
import re
import sys
import json
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
BROWSER = os.getenv("BROWSER", "webkit").strip().lower()
CHANNEL = os.getenv("CHANNEL", "").strip()
DEVICE = os.getenv("DEVICE", "iPhone 14").strip()

# 랭킹 화면 주소 (카테고리 탭 확인용)
RANKING_URL = os.getenv("RANKING_URL", "https://mobile.a-bly.com/ranking").strip()

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

SCROLL_ALL_JS = """(dy) => {
  const cands = [document.scrollingElement || document.documentElement];
  document.querySelectorAll('*').forEach(el => {
    const s = getComputedStyle(el).overflowY;
    if ((s === 'auto' || s === 'scroll' || s === 'overlay') && el.scrollHeight > el.clientHeight + 50) cands.push(el);
  });
  let moved = 0;
  for (const el of cands) {
    const before = el.scrollTop;
    el.scrollTop = before + dy;
    el.dispatchEvent(new Event('scroll', {bubbles: true}));
    moved += Math.abs(el.scrollTop - before);
  }
  window.scrollBy(0, dy);
  window.dispatchEvent(new Event('scroll'));
  return Math.round(moved);
}"""

SCROLL_INFO_JS = """() => {
  const d = document.scrollingElement || document.documentElement;
  const out = [];
  document.querySelectorAll('*').forEach(el => {
    const s = getComputedStyle(el).overflowY;
    if ((s === 'auto' || s === 'scroll' || s === 'overlay') && el.scrollHeight > el.clientHeight + 50) {
      out.push(el.tagName + '.' + String(el.className || '').slice(0, 30) + ' top=' + Math.round(el.scrollTop) + ' h=' + el.scrollHeight + '/' + el.clientHeight);
    }
  });
  return {doc: 'top=' + Math.round(d.scrollTop) + ' h=' + d.scrollHeight + '/' + d.clientHeight, boxes: out.slice(0, 5), url: location.href};
}"""

LAST_ITEM_JS = """() => {
  const els = [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 && /^\\d{1,3}$/.test((e.textContent || '').trim()));
  let best = null, bv = 0;
  for (const e of els) { const v = parseInt(e.textContent.trim()); if (v > bv) { bv = v; best = e; } }
  if (best) best.scrollIntoView({block: 'end'});
  return bv;
}"""

STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US']});"
STEALTH_JS_CHROME = "window.chrome = {runtime: {}};"
CHALLENGE_WORDS = ["잠시만 기다리", "just a moment", "attention required", "checking your browser"]
AUTH_WORDS = ["인증되지 않은 유저"]
VERIFY_WORDS = {"뷰티": ["스킨케어", "포인트 메이크업", "베이스 메이크업"]}
POPUP_
