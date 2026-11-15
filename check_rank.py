import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs
@@ -15,6 +16,12 @@
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
@@ -186,40 +193,78 @@
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
        open_with_retry(page, rk["url"])
        page.wait_for_timeout(4000)
        log("[loaded] title=" + page.title())

        if rk["tab"]:
            click_categories(page, rk["tab"], rk["url"])
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(1500)
        prepare_page(page, rk)

        try:
            with open("debug_text_" + str(idx) + ".txt", "w", encoding="utf-8") as f:
                f.write(page.inner_text("body"))
        except Exception:
            pass

        stale = 0
        for _ in range(60):
            m = parse_text(page.inner_text("body"), hits)
        for attempt in range(1, SCAN_TRIES + 1):
            m = scan(page, rk, hits)
            if m > max_seen:
                max_seen = m
                stale = 0
            else:
                stale += 1
            if max_seen >= rk["top_n"] or stale >= 5:
            if max_seen >= rk["top_n"] or attempt == SCAN_TRIES:
                break
            page.evaluate(SCROLL_JS)
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1300)
            log("[retry-scan] TOP " + str(max_seen) + "까지만 떠서 새로고침 후 재시도")
            prepare_page(page, rk)

        log("[" + rk["name"] + "] 확인한 최대 순위=" + str(max_seen) + " / 캔메이크=" + str(sorted(hits)))
    finally:
@@ -242,60 +287,62 @@
        lines = ["*[" + name + "]* :tada: " + str(len(hits)) + "개 진입"]
        for r in sorted(hits):
            lines.append("• *" + str(r) + "위* " + str(hits[r])[:45])
        if max_seen < rk["top_n"]:
            lines.append("_(사이트 로딩 지연으로 TOP " + str(max_seen) + "까지만 확인)_")
        return "\n".join(lines)
    if max_seen == 0:
        return "*[" + name + "]* :warning: 순위를 읽지 못했습니다. (스크린샷 확인 필요)"
    if max_seen < rk["top_n"]:
        return "*[" + name + "]* 진입 없음 (TOP " + str(max_seen) + "까지만 확인됨)"
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
