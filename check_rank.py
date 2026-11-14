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

# 로딩 대기 설정 (사이트가 느릴 때를 대비해 넉넉하게)
STALE_SEC = int(os.getenv("STALE_SEC", "120"))   # 새 순위가 이 시간 동안 안 늘면 포기
MAX_SEC = int(os.getenv("MAX_SEC", "360"))       # 랭킹 1회 스캔 최대 시간
SCAN_TRIES = int(os.getenv("SCAN_TRIES", "1"))   # 덜 뜨면 새로고침 후 재시도 횟수
NUDGE_EVERY = 10                                 # 로딩 멈춤 시 위아래 흔들기 간격(초)

KST = timezone(timedelta(hours=9))
RANK_RE = re.compile(r"^\d{1,3}$")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
SCROLL_JS = "() => { window.scrollBy(0, 1800); document.querySelectorAll('*').forEach(el => { const s = getComputedStyle(el).overflowY; if ((s === 'auto' || s === 'scroll') && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollTop + 1800; } }); }"
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US']}); window.chrome = {runtime: {}};"
CHALLENGE_WORDS = ["잠시만 기다리", "just a moment", "attention required", "checking your browser"]
VERIFY_WORDS = {"뷰티": ["스킨케어", "포인트 메이크업", "베이스 메이크업"]}
MAIN_URL = "https://mobile.a-bly.com/"
OPEN_RETRY = 3
TAB_MAX_Y = 800


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


def open_with_retry(page, url):
    for attempt in range(1, OPEN_RETRY + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            pass_challenge(page)
            if attempt > 1:
                log("[retry] " + str(attempt) + "회차에 접속 성공")
            return
        except Exception as e:
            log("[retry] " + str(attempt) + "/" + str(OPEN_RETRY) + " 접속 실패: " + str(e).split("\n")[0][:80])
            if attempt == OPEN_RETRY:
                raise
            page.wait_for_timeout(5000)


def warm_up(context):
    page = context.new_page()
    try:
        log("[warmup] 에이블리 메인 페이지 접속")
        open_with_retry(page, MAIN_URL)
        page.wait_for_timeout(3000)
        log("[warmup] 완료 title=" + page.title())
    except Exception as e:
        log("[warmup] 실패(무시하고 진행): " + str(e).split("\n")[0][:80])
    finally:
        page.close()


def load_rankings():
    # RANKINGS: 한 줄에 '이름|URL|카테고리|TOP_N' (카테고리는 '상위>하위' 형식 가능)
    rankings = []
    for line in os.getenv("RANKINGS", "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split("|")]
        while len(parts) < 4:
            parts.append("")
        top_n = int(parts[3]) if parts[3].isdigit() else DEFAULT_TOP_N
        rankings.append({"name": parts[0], "url": parts[1], "tab": parts[2], "top_n": top_n})
    return rankings


def parse_text(text, hits):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    last_rank = None
    max_rank = 0
    for i, line in enumerate(lines):
        if RANK_RE.match(line):
            last_rank = int(line)
            if last_rank > max_rank:
                max_rank = last_rank
        elif last_rank is not None and is_brand_line(line):
            name = lines[i + 1] if i + 1 < len(lines) else ""
            if last_rank not in hits:
                hits[last_rank] = name
    return max_rank


def page_key(url):
    u = urlparse(url)
    screen = parse_qs(u.query).get("screen_name", [""])[0]
    return u.path.rstrip("/") + "?" + screen


def same_page(page, url):
    return page_key(page.url) == page_key(url)


def click_tab(page, tab, url, prev_tabs):
    loc = page.get_by_text(tab, exact=True)
    cands = []
    for i in range(loc.count()):
        try:
            el = loc.nth(i)
            if not el.is_visible():
                continue
            box = el.bounding_box()
            if box and box["y"] < TAB_MAX_Y:
                cands.append((box["y"], i))
        except Exception:
            continue
    log("[tab] '" + tab + "' 후보 " + str(len(cands)) + "개: " + str([round(c[0]) for c in cands]))
    cands.sort(reverse=True)

    verify = VERIFY_WORDS.get(tab, [])
    for y, i in cands:
        try:
            page.get_by_text(tab, exact=True).nth(i).click(timeout=10000)
            page.wait_for_timeout(4000)
        except Exception as e:
            log("[tab] 클릭 실패 y=" + str(round(y)) + " : " + str(e).split("\n")[0])
            continue

        ok_url = same_page(page, url)
        body = page.inner_text("body")
        ok_words = (not verify) or any(w in body for w in verify)
        log("[tab] '" + tab + "' y=" + str(round(y)) + " 클릭 -> url=" + page.url + " / 검증=" + str(ok_url and ok_words))
        if ok_url and ok_words:
            return True

        if not ok_url:
            try:
                page.go_back(wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(3000)
            except Exception:
                pass
            if not same_page(page, url):
                open_with_retry(page, url)
                page.wait_for_timeout(3000)
                for j, t in enumerate(prev_tabs):
                    if not click_tab(page, t, url, prev_tabs[:j]):
                        return False
    return False


def click_categories(page, spec, url):
    tabs = [t.strip() for t in spec.split(">") if t.strip()]
    for level, tab in enumerate(tabs):
        if not click_tab(page, tab, url, tabs[:level]):
            raise RuntimeError("'" + tab + "' 카테고리를 선택하지 못했습니다.")


def prepare_page(page, rk):
    open_with_retry(page, rk["url"])
    page.wait_for_timeout(4000)
    log("[loaded] title=" + page.title())
    if rk["tab"]:
        click_categories(page, rk["tab"], rk["url"])
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(1500)


def scan(page, rk, hits):
    max_seen = 0
    start = time.time()
    last_progress = start
    last_nudge = start
    while True:
        m = parse_text(page.inner_text("body"), hits)
        now = time.time()
        if m > max_seen:
            max_seen = m
            last_progress = now
            log("[scan] " + rk["name"] + " TOP " + str(max_seen) + " 로딩됨 (" + str(int(now - start)) + "초)")
        if max_seen >= rk["top_n"]:
            break

        idle = now - last_progress
        if idle >= STALE_SEC:
            log("[scan] 로딩 멈춤 - " + str(int(idle)) + "초간 변화 없음, 중단")
            break
        if now - start >= MAX_SEC:
            log("[scan] 최대 대기 시간(" + str(MAX_SEC) + "초) 초과, 중단")
            break

        # 로딩이 멈춘 것 같으면 주기적으로 위로 올렸다 내려서 lazy load 재트리거
        if idle >= 6 and now - last_nudge >= NUDGE_EVERY:
            log("[scan] 대기 중... " + str(int(idle)) + "초째 TOP " + str(max_seen))
            page.mouse.wheel(0, -2500)
            page.wait_for_timeout(800)
            last_nudge = now

        page.evaluate(SCROLL_JS)
        page.mouse.wheel(0, 1800)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
    return max_seen


def check_one(context, rk, idx):
    page = context.new_page()
    hits = {}
    max_seen = 0
    log("[open] " + rk["name"] + " : " + rk["url"] + (" (카테고리: " + rk["tab"] + ")" if rk["tab"] else ""))
    try:
        prepare_page(page, rk)

        try:
            with open("debug_text_" + str(idx) + ".txt", "w", encoding="utf-8") as f:
                f.write(page.inner_text("body"))
        except Exception:
            pass

        for attempt in range(1, SCAN_TRIES + 1):
            m = scan(page, rk, hits)
            if m > max_seen:
                max_seen = m
            if max_seen >= rk["top_n"] or attempt == SCAN_TRIES:
                break
            log("[retry-scan] TOP " + str(max_seen) + "까지만 떠서 새로고침 후 재시도")
            prepare_page(page, rk)

        log("[" + rk["name"] + "] 확인한 최대 순위=" + str(max_seen) + " / 캔메이크=" + str(sorted(hits)))
    finally:
        try:
            page.screenshot(path="debug_ranking_" + str(idx) + ".png")
        except Exception:
            pass
        page.close()

    in_range = {}
    for r, n in hits.items():
        if r <= rk["top_n"]:
            in_range[r] = n
    return in_range, max_seen


def format_section(rk, hits, max_seen):
    name = rk["name"]
    if hits:
        lines = ["*[" + name + "]* :tada: " + str(len(hits)) + "개 진입"]
        for r in sorted(hits):
            lines.append("• *" + str(r) + "위* " + str(hits[r])[:45])
        if max_seen < rk["top_n"]:
            lines.append("_(사이트 로딩 지연으로 TOP " + str(max_seen) + "까지만 확인)_")
        return "\n".join(lines)
    if max_seen == 0:
        return "*[" + name + "]* :warning: 순위를 읽지 못했습니다. (스크린샷 확인 필요)"
    if max_seen < rk["top_n"]:
        return "*[" + name + "]* :hourglass: 사이트 로딩 지연으로 TOP " + str(max_seen) + "까지만 확인 (그 안에는 진입 없음)"
    return "*[" + name + "]* 진입 없음 (TOP " + str(rk["top_n"]) + " 기준)"


def send_slack(text):
    res = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    log("[slack] " + str(res.status_code) + " " + res.text)
    res.raise_for_status()


def main():
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    rankings = load_rankings()
    if not rankings:
        send_slack(":warning: [" + now + "] RANKINGS 설정이 비어 있습니다.")
        sys.exit(1)

    sections = []
    any_hit = False
    fail_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(viewport={"width": 430, "height": 932}, user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul", is_mobile=True, has_touch=True)
        context.add_init_script(STEALTH_JS)
        try:
            warm_up(context)
            for idx, rk in enumerate(rankings, start=1):
                try:
                    hits, max_seen = check_one(context, rk, idx)
                    if hits:
                        any_hit = True
                    if max_seen == 0 and not hits:
                        fail_count += 1
                    sections.append(format_section(rk, hits, max_seen))
                except Exception as e:
                    traceback.print_exc()
                    fail_count += 1
                    sections.append("*[" + rk["name"] + "]* :warning: 조회 실패: " + str(e).split("\n")[0][:150])
        finally:
            browser.close()

    head = ":tada:" if any_hit else ":mag:"
    msg = head + " [" + now + "] " + BRAND_NAME + " " + REPORT_TITLE + "\n" + "\n".join(sections)
    log(msg)
    send_slack(msg)

    if fail_count == len(rankings):
        sys.exit(1)


if __name__ == "__main__":
    main()
