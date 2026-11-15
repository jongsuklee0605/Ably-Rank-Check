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

# 모바일 기기 에뮬레이션 설정
#   안드로이드: BROWSER=chromium, DEVICE="Pixel 7" (또는 "Galaxy S9+")
#   아이폰   : BROWSER=webkit,   DEVICE="iPhone 14"
BROWSER = os.getenv("BROWSER", "chromium").strip().lower()
DEVICE = os.getenv("DEVICE", "Pixel 7").strip()

# 불필요한 요청 차단 (로딩 속도 개선)
BLOCK_IMAGES = os.getenv("BLOCK_IMAGES", "true").lower() == "true"
BLOCK_HOSTS = ["amplitude.com"]

# 로딩 대기 설정 (사이트가 느릴 때를 대비해 넉넉하게)
STALE_SEC = int(os.getenv("STALE_SEC", "120"))   # 새 순위가 이 시간 동안 안 늘면 포기
MAX_SEC = int(os.getenv("MAX_SEC", "360"))       # 랭킹 1회 스캔 최대 시간
SCAN_TRIES = int(os.getenv("SCAN_TRIES", "1"))   # 덜 뜨면 새로고침 후 재시도 횟수
NUDGE_EVERY = 10                                 # 로딩 멈춤 시 위아래 흔들기 간격(초)

KST = timezone(timedelta(hours=9))
RANK_RE = re.compile(r"^\d{1,3}$")
SCROLL_JS = "() => { window.scrollBy(0, 1800); document.querySelectorAll('*').forEach(el => { const s = getComputedStyle(el).overflowY; if ((s === 'auto' || s === 'scroll') && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollTop + 1800; } }); }"
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US']});"
STEALTH_JS_CHROME = "window.chrome = {runtime: {}};"
CHALLENGE_WORDS = ["잠시만 기다리", "just a moment", "attention required", "checking your browser"]
VERIFY_WORDS = {"뷰티": ["스킨케어", "포인트 메이크업", "베이스 메이크업"]}
# 앱 설치 유도 팝업 등에서 누를 버튼 문구 (스크린샷 보고 필요하면 추가)
POPUP_TEXTS = ["오늘 하루 보지 않기", "다음에 할게요", "괜찮아요", "웹으로 볼게요", "모바일 웹으로 볼게요", "웹에서 계속하기", "닫기"]
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


def close_popups(page):
    for t in POPUP_TEXTS:
        try:
            loc = page.get_by_text(t, exact=True)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=2000)
                log("[popup] '" + t + "' 클릭")
                page.wait_for_timeout(800)
        except Exception:
            pass


def open_with_retry(page, url):
    for attempt in range(1, OPEN_RETRY + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            pass_challenge(page)
            close_popups(page)
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
        close_popups(page)
        log("[warmup] 완료 title=" + page.title())
    except Exception as e:
        log("[warmup] 실패(무시하고 진행): " + str(e).split("\n")[0][:80])
    finally:
        page.close()


def get_cdp(context, page):
    # Chromium이면 실제 터치 스크롤(손가락 쓸기)을 쓰기 위한 세션
    if BROWSER != "chromium":
        return None
    try:
        return context.new_cdp_session(page)
    except Exception as e:
        log("[touch] CDP 세션 실패(일반 스크롤 사용): " + str(e)[:80])
        return None


def do_scroll(page, cdp, dy):
    # dy > 0 : 아래로, dy < 0 : 위로
    if cdp:
        try:
            vp = page.viewport_size or {"width": 400, "height": 800}
            step = int(vp["height"] * 0.6)
            moved = 0
            while moved < abs(dy):
                cdp.send("Input.synthesizeScrollGesture", {
                    "x": vp["width"] // 2,
                    "y": vp["height"] // 2,
                    "yDistance": -step if dy > 0 else step,
                    "speed": 3000,
                    "gestureSourceType": "touch",
                })
                moved += step
        except Exception:
            pass
    try:
        if dy > 0:
            page.evaluate(SCROLL_JS)
        else:
            page.evaluate("(d) => window.scrollBy(0, d)", dy)
    except Exception:
        pass
    if not cdp:
        try:
            page.mouse.wheel(0, dy)
        except Exception:
            pass


def load_rankings():
    # RANKINGS: 한 줄에 '이름|URL|카테고리|TOP_N|대기초'
    #   카테고리: '상위>하위' 형식 가능, 비우면 탭 안 누름
    #   대기초  : 이 랭킹의 탭을 누른 시점부터 몇 초가 지나야 다음 랭킹으로 넘어갈지 (비우면 0)
    rankings = []
    for line in os.getenv("RANKINGS", "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split("|")]
        while len(parts) < 5:
            parts.append("")
        top_n = int(parts[3]) if parts[3].isdigit() else DEFAULT_TOP_N
        wait = int(parts[4]) if parts[4].isdigit() else 0
        rankings.append({"name": parts[0], "url": parts[1], "tab": parts[2], "top_n": top_n, "wait": wait})
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
            page.get_by_text(tab, exact=True).nth(i).tap(timeout=10000)
            page.wait_for_timeout(4000)
        except Exception as e:
            log("[tab] 탭 실패 y=" + str(round(y)) + " : " + str(e).split("\n")[0])
            continue

        ok_url = same_page(page, url)
        body = page.inner_text("body")
        ok_words = (not verify) or any(w in body for w in verify)
        log("[tab] '" + tab + "' y=" + str(round(y)) + " 탭 -> url=" + page.url + " / 검증=" + str(ok_url and ok_words))
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
    close_popups(page)
    log("[loaded] title=" + page.title())
    if rk["tab"]:
        click_categories(page, rk["tab"], rk["url"])
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(1500)
    # 대기 타이머 기준 시점 (탭을 누른 직후)
    rk["clicked_at"] = time.time()


def scan(page, cdp, rk, hits):
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
            close_popups(page)
            do_scroll(page, cdp, -2500)
            page.wait_for_timeout(800)
            last_nudge = now

        do_scroll(page, cdp, 1800)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
    return max_seen


def check_one(page, cdp, rk, idx):
    # 같은 브라우저 탭(page)을 계속 재사용한다
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
            m = scan(page, cdp, rk, hits)
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

    in_range = {}
    for r, n in hits.items():
        if r <= rk["top_n"]:
            in_range[r] = n
    return in_range, max_seen


def wait_until(page, target, label):
    # target 시각까지 같은 화면을 유지한 채 대기 (1분마다 진행 로그)
    remain = target - time.time()
    if remain <= 0:
        log("[wait] " + label + " 이미 대기 시간 경과 - 바로 다음 랭킹 진행")
        return
    log("[wait] " + label + " 후 " + str(int(remain)) + "초 더 대기")
    while True:
        remain = target - time.time()
        if remain <= 0:
            break
        page.wait_for_timeout(int(min(60, remain) * 1000))
        left = int(target - time.time())
        if left > 0:
            log("[wait] 남은 시간 " + str(left) + "초")
    log("[wait] 대기 완료")


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


def setup_blocking(context):
    def handler(route):
        req = route.request
        try:
            if any(h in req.url for h in BLOCK_HOSTS):
                return route.abort()
            if BLOCK_IMAGES and req.resource_type in ("image", "media", "font"):
                return route.abort()
            return route.continue_()
        except Exception:
            pass
    context.route("**/*", handler)
    log("[block] 통계 요청 차단 / 이미지 차단=" + str(BLOCK_IMAGES))


def make_context(p):
    if DEVICE not in p.devices:
        raise RuntimeError("알 수 없는 DEVICE 이름: " + DEVICE)
    dev = dict(p.devices[DEVICE])
    dev.pop("default_browser_type", None)   # new_context에는 넘기면 안 되는 값

    engine = getattr(p, BROWSER)
    args = ["--disable-blink-features=AutomationControlled"] if BROWSER == "chromium" else []
    browser = engine.launch(headless=HEADLESS, args=args)
    context = browser.new_context(**dev, locale="ko-KR", timezone_id="Asia/Seoul")
    setup_blocking(context)
    context.add_init_script(STEALTH_JS)
    if BROWSER == "chromium":
        context.add_init_script(STEALTH_JS_CHROME)
    log("[device] " + BROWSER + " / " + DEVICE + " / UA=" + dev.get("user_agent", "")[:80])
    return browser, context


def main():
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    rankings = load_rankings()
    if not rankings:
        send_slack(":warning: [" + now + "] RANKINGS 설정이 비어 있습니다.")
        sys.exit(1)

    log("[plan] 순서: " + " -> ".join(rk["name"] + ("(+" + str(rk["wait"]) + "초)" if rk["wait"] else "") for rk in rankings))

    results = []   # 슬랙에는 RANKINGS 순서 그대로 표시
    any_hit = False
    fail_count = 0

    with sync_playwright() as p:
        browser, context = make_context(p)
        try:
            warm_up(context)
            page = context.new_page()
            cdp = get_cdp(context, page)

            for idx, rk in enumerate(rankings, start=1):
                t0 = time.time()
                try:
                    hits, max_seen = check_one(page, cdp, rk, idx)
                    if hits:
                        any_hit = True
                    if max_seen == 0 and not hits:
                        fail_count += 1
                    results.append(format_section(rk, hits, max_seen))
                except Exception as e:
                    traceback.print_exc()
                    fail_count += 1
                    results.append("*[" + rk["name"] + "]* :warning: 조회 실패: " + str(e).split("\n")[0][:150])

                # 다음 랭킹이 있으면, 이 랭킹 탭을 누른 시점부터 wait초가 지날 때까지 대기
                if rk["wait"] > 0 and idx < len(rankings):
                    base = rk.get("clicked_at", t0)
                    wait_until(page, base + rk["wait"], rk["name"] + " 시작 " + str(rk["wait"]) + "초")
        finally:
            browser.close()

    head = ":tada:" if any_hit else ":mag:"
    msg = head + " [" + now + "] " + BRAND_NAME + " " + REPORT_TITLE + "\n" + "\n".join(results)
    log(msg)
    send_slack(msg)

    if fail_count == len(rankings):
        sys.exit(1)


if __name__ == "__main__":
    main()
