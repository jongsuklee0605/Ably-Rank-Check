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
    raise RuntimeError("에이블리가 '인증되지 않은 유저'로 접속을 막았습니다.")


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
            fix_auth(page)
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
    if BROWSER != "chromium":
        log("[drag] 방식: 마우스 드래그 (" + BROWSER + ")")
        return None
    try:
        cdp = context.new_cdp_session(page)
        log("[drag] 방식: 터치 드래그")
        return cdp
    except Exception as e:
        log("[drag] CDP 세션 실패, 마우스 드래그 사용: " + str(e)[:80])
        return None


def setup_net_log(page):
    seen = set()

    def on_resp(r):
        try:
            if r.request.resource_type not in ("xhr", "fetch") or "amplitude" in r.url:
                return
            if "/api/v2/goods" in r.url:
                if r.status < 400:
                    NET_STATS["goods_ok"] += 1
                else:
                    NET_STATS["goods_fail"] += 1
            path = urlparse(r.url).netloc + urlparse(r.url).path
            if r.status >= 400:
                log("[net] 실패 " + str(r.status) + " " + r.url[:140])
            elif path not in seen:
                seen.add(path)
                log("[net] " + str(r.status) + " " + path[:120])
        except Exception:
            pass

    page.on("response", on_resp)


def touch_drag(cdp, x, y_from, y_to, steps=15):
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y_from}]})
    for i in range(1, steps + 1):
        y = y_from + (y_to - y_from) * i / steps
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
        time.sleep(0.02)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def do_scroll(page, cdp, dy):
    vp = page.viewport_size or {"width": 400, "height": 800}
    x = vp["width"] // 2
    top = int(vp["height"] * 0.25)
    bottom = int(vp["height"] * 0.80)
    dist = bottom - top
    count = max(1, round(abs(dy) / dist))

    if dy > 0:
        try:
            page.evaluate(LAST_ITEM_JS)
            page.wait_for_timeout(300)
        except Exception:
            pass

    dragged = False
    if cdp:
        try:
            for _ in range(count):
                if dy > 0:
                    touch_drag(cdp, x, bottom, top)
                else:
                    touch_drag(cdp, x, top, bottom)
                page.wait_for_timeout(400)
            dragged = True
        except Exception as e:
            log("[drag] 터치 드래그 실패, 마우스 드래그로 대체: " + str(e)[:80])

    if not dragged:
        try:
            for _ in range(count):
                if dy > 0:
                    start_y, end_y = bottom, top
                else:
                    start_y, end_y = top, bottom
                page.mouse.move(x, start_y)
                page.mouse.down()
                page.mouse.move(x, end_y, steps=15)
                page.mouse.up()
                page.wait_for_timeout(400)
        except Exception:
            pass

    try:
        if dy > 0:
            page.evaluate(SCROLL_JS)
        else:
            page.evaluate("(d) => window.scrollBy(0, d)", dy)
    except Exception:
        pass


def load_rankings():
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
    rk.pop("clicked_at", None)
    open_with_retry(page, rk["url"])
    page.wait_for_timeout(4000)
    close_popups(page)
    log("[loaded] title=" + page.title())
    try:
        body = page.inner_text("body")
        log("[body] 글자수=" + str(len(body)) + " / 앞부분: " + body[:150].replace("\n", " | "))
    except Exception as e:
        log("[body] 읽기 실패: " + str(e)[:80])
    if rk["tab"]:
        click_categories(page, rk["tab"], rk["url"])
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(1500)
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

        if idle >= 6 and now - last_nudge >= NUDGE_EVERY:
            try:
                y = page.evaluate("() => Math.round(window.scrollY)")
            except Exception:
                y = -1
            log("[scan] 대기 중... " + str(int(idle)) + "초째 TOP " + str(max_seen) + " / 스크롤 위치=" + str(y))
            close_popups(page)
            do_scroll(page, cdp, -1500)
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
    if DRY_RUN:
        log("[slack] DRY_RUN - 전송 생략")
        return
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
    log("[block] 차단 대상=" + str(BLOCK_HOSTS) + " / 이미지 차단=" + str(BLOCK_IMAGES))


def make_context(p):
    if DEVICE not in p.devices:
        raise RuntimeError("알 수 없는 DEVICE 이름: " + DEVICE)
    dev = dict(p.devices[DEVICE])
    dev.pop("default_browser_type", None)

    engine = getattr(p, BROWSER)
    launch_kwargs = {"headless": HEADLESS}
    if BROWSER == "chromium":
        launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
        if CHANNEL:
            launch_kwargs["channel"] = CHANNEL
    browser = engine.launch(**launch_kwargs)
    context = browser.new_context(**dev, locale="ko-KR", timezone_id="Asia/Seoul")
    setup_blocking(context)
    context.add_init_script(STEALTH_JS)
    if BROWSER == "chromium":
        context.add_init_script(STEALTH_JS_CHROME)
    combo = BROWSER + ("/" + CHANNEL if CHANNEL else "")
    log("[device] " + combo + " / " + DEVICE + " / headless=" + str(HEADLESS) + " / UA=" + dev.get("user_agent", "")[:70])
    return browser, context


def write_probe_summary(best):
    combo = BROWSER + ("/" + CHANNEL if CHANNEL else "") + " / " + DEVICE + " / headless=" + str(HEADLESS)
    ok = NET_STATS["goods_ok"]
    fail = NET_STATS["goods_fail"]
    if ok > 0 and best > 8:
        verdict = "✅ 통과"
    elif ok > 0:
        verdict = "⚠️ 일부"
    else:
        verdict = "❌ 차단"
    line = verdict + " | " + combo + " | goods 성공 " + str(ok) + " / 실패 " + str(fail) + " | 최대 순위 " + str(best)
    log("[probe] " + line)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("- " + line + "\n")
        except Exception:
            pass


def main():
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    rankings = load_rankings()
    if not rankings:
        send_slack(":warning: [" + now + "] RANKINGS 설정이 비어 있습니다.")
        sys.exit(1)

    plan = []
    for rk in rankings:
        plan.append(rk["name"] + ("(+" + str(rk["wait"]) + "초)" if rk["wait"] else ""))
    log("[plan] 순서: " + " -> ".join(plan))

    results = []
    any_hit = False
    fail_count = 0
    best = 0

    with sync_playwright() as p:
        browser, context = make_context(p)
        try:
            warm_up(context)
            page = context.new_page()
            page.on("pageerror", lambda e: log("[js-error] " + str(e)[:150]))
            setup_net_log(page)
            cdp = get_cdp(context, page)

            for idx, rk in enumerate(rankings, start=1):
                try:
                    hits, max_seen = check_one(page, cdp, rk, idx)
                    best = max(best, max_seen)
                    if hits:
                        any_hit = True
                    if max_seen == 0 and not hits:
                        fail_count += 1
                    results.append(format_section(rk, hits, max_seen))
                except Exception as e:
                    traceback.print_exc()
                    fail_count += 1
                    results.append("*[" + rk["name"] + "]* :warning: 조회 실패: " + str(e).split("\n")[0][:150])

                if rk["wait"] > 0 and idx < len(rankings):
                    if "clicked_at" in rk:
                        wait_until(page, rk["clicked_at"] + rk["wait"], rk["name"] + " 시작 " + str(rk["wait"]) + "초")
                    else:
                        log("[wait] " + rk["name"] + " 탭 선택 실패 - 대기 건너뜀")
        finally:
            browser.close()

    write_probe_summary(best)

    head = ":tada:" if any_hit else ":mag:"
    msg = head + " [" + now + "] " + BRAND_NAME + " " + REPORT_TITLE + "\n" + "\n".join(results)
    log(msg)
    send_slack(msg)

    if fail_count == len(rankings) and not DRY_RUN:
        sys.exit(1)


if __name__ == "__main__":
    main()
