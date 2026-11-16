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

# 프록시 (GitHub Actions IP가 막힐 때 한국 IP 프록시 사용) 예: http://user:pass@host:port
PROXY_URL = os.getenv("PROXY_URL", "").strip()
# 접속 환경: mobile(아이폰 UA) / desktop(데스크톱 UA). 막히면 desktop 으로 바꿔 시도
CLIENT_MODE = os.getenv("CLIENT_MODE", "mobile").strip().lower()
# 페이지가 비어 있을 때 새로고침하며 기다리는 횟수/시간
BLANK_RELOADS = int(os.getenv("BLANK_RELOADS", "3"))
BLANK_WAIT_SEC = int(os.getenv("BLANK_WAIT_SEC", "20"))

# API 수집 설정
MARKET_ID = os.getenv("MARKET_ID", "").strip()
API_KEYWORD = os.getenv("API_KEYWORD", "").strip()           # 랭킹 API 주소의 일부 (찾은 뒤 입력)
USE_API = os.getenv("USE_API", "false").lower() == "true"    # true면 API 결과로 순위 판정

# 로딩 대기 설정 (사이트가 느릴 때를 대비해 넉넉하게)
STALE_SEC = int(os.getenv("STALE_SEC", "120"))   # 새 순위가 이 시간 동안 안 늘면 포기
MAX_SEC = int(os.getenv("MAX_SEC", "360"))       # 랭킹 1회 스캔 최대 시간
SCAN_TRIES = int(os.getenv("SCAN_TRIES", "1"))   # 덜 뜨면 새로고침 후 재시도 횟수
NUDGE_EVERY = 10                                 # 로딩 멈춤 시 위아래 흔들기 간격(초)

KST = timezone(timedelta(hours=9))
RANK_RE = re.compile(r"^\d{1,3}$")
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
UA = UA_DESKTOP if CLIENT_MODE == "desktop" else UA_MOBILE
SCROLL_JS = "() => { window.scrollBy(0, 1800); document.querySelectorAll('*').forEach(el => { const s = getComputedStyle(el).overflowY; if ((s === 'auto' || s === 'scroll') && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollTop + 1800; } }); }"
STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US']}); window.chrome = {runtime: {}};"
CHALLENGE_WORDS = ["잠시만 기다리", "just a moment", "attention required", "checking your browser", "access denied", "error 1020", "verify you are human"]
CHALLENGE_BODY_WORDS = ["cloudflare", "verify you are human", "checking your browser", "enable javascript and cookies", "access denied", "사람인지 확인", "잠시만 기다리"]
BLOCKED_MSG = "에이블리 접속 차단/빈 화면 (GitHub Actions IP 차단 가능성 - PROXY_URL 또는 self-hosted runner 필요)"
VERIFY_WORDS = {"뷰티": ["스킨케어", "포인트 메이크업", "베이스 메이크업"]}
MAIN_URL = "https://mobile.a-bly.com/"
OPEN_RETRY = 3
TAB_MAX_Y = 800


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# API 응답 수집 / 분석
# ---------------------------------------------------------------------------

class ApiCollector:
    """페이지가 받아오는 JSON(XHR/fetch) 응답을 모아둔다"""

    def __init__(self, page):
        self.items = []
        page.on("response", self._on_response)

    def _on_response(self, resp):
        try:
            rt = resp.request.resource_type
            if rt == "document" or resp.status >= 400:
                log("[resp] " + str(resp.status) + " " + rt + " " + resp.url[:110])
            if rt not in ("xhr", "fetch"):
                return
            if "json" not in (resp.headers.get("content-type") or ""):
                return
            if API_KEYWORD and API_KEYWORD not in resp.url:
                return
            self.items.append((resp.url, resp.json()))
        except Exception:
            pass

    def reset(self):
        self.items = []

    def dump(self, path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{"url": u, "data": d} for u, d in self.items], f, ensure_ascii=False, indent=1)
            log("[api] 응답 " + str(len(self.items)) + "개 저장 -> " + path)
        except Exception as e:
            log("[api] 저장 실패: " + str(e)[:80])


def find_lists(obj):
    # JSON 안에서 '상품 목록'처럼 보이는 리스트(dict 5개 이상)를 찾는다
    found = []
    if isinstance(obj, list):
        if len(obj) >= 5 and all(isinstance(x, dict) for x in obj):
            found.append(obj)
        for x in obj:
            found.extend(find_lists(x))
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(find_lists(v))
    return found


def pick(it, keys):
    src = it.get("goods") if isinstance(it.get("goods"), dict) else it
    for k in keys:
        v = src.get(k)
        if v not in (None, ""):
            return v
    return None


def item_is_brand(it):
    s = json.dumps(it, ensure_ascii=False).lower()
    if MARKET_ID and re.search(r'"[a-z_]*market[a-z_]*"\s*:\s*"?' + re.escape(MARKET_ID) + r'\b', s):
        return True
    return any('"' + k.lower() + '"' in s for k in BRAND_KEYWORDS)


def api_ranks(items):
    hits, seen, order, max_rank = {}, set(), 0, 0
    for url, data in items:
        lists = find_lists(data)
        if not lists:
            continue
        goods = max(lists, key=len)
        for it in goods:
            key = pick(it, ["sno", "goods_sno", "goods_id", "id"])
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            order += 1
            r = it.get("rank") if isinstance(it.get("rank"), int) else order
            max_rank = max(max_rank, r)
            if item_is_brand(it) and r not in hits:
                hits[r] = str(pick(it, ["name", "goods_name", "title"]) or "")
    return hits, max_rank


# ---------------------------------------------------------------------------
# 페이지 접속 / 화면 파싱
# ---------------------------------------------------------------------------

def is_brand_line(line):
    t = line.strip().lower()
    for k in BRAND_KEYWORDS:
        if t == k.lower():
            return True
    return False


class BlockedError(RuntimeError):
    """봇 차단 / 빈 화면으로 페이지를 읽을 수 없는 상태"""


def page_state(page):
    """'ok' / 'challenge' / 'blank' 중 하나와 진단 문자열을 돌려준다"""
    try:
        title = page.title().strip()
    except Exception:
        title = ""
    try:
        body = page.inner_text("body").strip()
    except Exception:
        body = ""
    tl, bl = title.lower(), body.lower()
    if any(w in tl for w in CHALLENGE_WORDS):
        return "challenge", title, body
    if len(body) < 300 and any(w in bl for w in CHALLENGE_BODY_WORDS):
        return "challenge", title, body
    # title 도 없고 본문도 거의 없으면 차단/미로딩 화면
    if not title and len(body) < 50:
        return "blank", title, body
    return "ok", title, body


def diag(tag, page):
    state, title, body = page_state(page)
    log("[" + tag + "] state=" + state + " url=" + page.url[:100] + " title=" + title[:40] + " body길이=" + str(len(body)))
    if body:
        log("[" + tag + "] body: " + body[:200].replace("\n", " | "))
    return state


def is_challenge(page):
    return page_state(page)[0] != "ok"


def pass_challenge(page):
    """챌린지/빈 화면이면 일정 시간 기다리고, 그래도 안 되면 BlockedError"""
    page.wait_for_timeout(3000)
    state = page_state(page)[0]
    if state == "ok":
        return
    log("[challenge] 비정상 화면 감지(" + state + ") - 통과 대기")
    waited = 0
    limit = 30 if state == "challenge" else BLANK_WAIT_SEC
    while waited < limit:
        page.wait_for_timeout(1000)
        waited += 1
        if page_state(page)[0] == "ok":
            log("[challenge] 통과 (" + str(waited) + "초)")
            return
    diag("blocked", page)
    raise BlockedError(BLOCKED_MSG)


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
            page.wait_for_timeout(5000 * attempt)


def warm_up(context):
    page = context.new_page()
    ApiCollector(page)   # 응답 상태코드를 로그로 남기기 위해 연결
    try:
        log("[warmup] 에이블리 메인 페이지 접속")
        open_with_retry(page, MAIN_URL)
        page.wait_for_timeout(3000)
        diag("warmup", page)
    except Exception as e:
        log("[warmup] 실패(무시하고 진행): " + str(e).split("\n")[0][:120])
        try:
            page.screenshot(path="debug_warmup.png")
        except Exception:
            pass
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


def click_categories(page, spec, url, api=None):
    tabs = [t.strip() for t in spec.split(">") if t.strip()]
    for level, tab in enumerate(tabs):
        if api:
            api.reset()   # 탭 누르기 전 응답(전체 랭킹 등)은 버림
        if not click_tab(page, tab, url, tabs[:level]):
            raise RuntimeError("'" + tab + "' 카테고리를 선택하지 못했습니다.")


def prepare_page(page, rk, api=None):
    if api:
        api.reset()
    open_with_retry(page, rk["url"])
    page.wait_for_timeout(4000)
    if diag("loaded", page) != "ok":
        raise BlockedError(BLOCKED_MSG)
    if rk["tab"]:
        click_categories(page, rk["tab"], rk["url"], api)
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
        # 아무것도 못 읽었는데 화면이 차단/빈 화면이면 120초 기다리지 않고 바로 중단
        if max_seen == 0 and now - start >= 30 and page_state(page)[0] != "ok":
            diag("scan-blocked", page)
            raise BlockedError(BLOCKED_MSG)
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
    api = ApiCollector(page)
    hits = {}
    max_seen = 0
    log("[open] " + rk["name"] + " : " + rk["url"] + (" (카테고리: " + rk["tab"] + ")" if rk["tab"] else ""))
    try:
        prepare_page(page, rk, api)

        for attempt in range(1, SCAN_TRIES + 1):
            m = scan(page, rk, hits)
            if m > max_seen:
                max_seen = m
            if max_seen >= rk["top_n"] or attempt == SCAN_TRIES:
                break
            log("[retry-scan] TOP " + str(max_seen) + "까지만 떠서 새로고침 후 재시도")
            prepare_page(page, rk, api)

        log("[" + rk["name"] + "] (화면) 최대 순위=" + str(max_seen) + " / 캔메이크=" + str(sorted(hits)))

        if API_KEYWORD:
            api_hits, api_max = api_ranks(api.items)
            log("[" + rk["name"] + "] (API) 최대 순위=" + str(api_max) + " / 캔메이크=" + str(sorted(api_hits)))
            if USE_API and api_max > 0:
                hits, max_seen = api_hits, api_max
    finally:
        # 성공/실패와 상관없이 마지막 화면 텍스트·스크린샷·API 응답을 남긴다
        try:
            with open("debug_text_" + str(idx) + ".txt", "w", encoding="utf-8") as f:
                f.write("url=" + page.url + "\ntitle=" + page.title() + "\n\n" + page.inner_text("body"))
        except Exception:
            pass
        try:
            page.screenshot(path="debug_ranking_" + str(idx) + ".png")
        except Exception:
            pass
        if api.items:
            api.dump("debug_api_" + str(idx) + ".json")
        page.close()

    in_range = {}
    for r, n in hits.items():
        if r <= rk["top_n"]:
            in_range[r] = n
    return in_range, max_seen


# ---------------------------------------------------------------------------
# 슬랙 리포트
# ---------------------------------------------------------------------------

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

    log("[config] API_KEYWORD=" + (API_KEYWORD or "(없음)") + " / USE_API=" + str(USE_API) + " / MARKET_ID=" + (MARKET_ID or "(없음)")
        + " / CLIENT_MODE=" + CLIENT_MODE + " / PROXY=" + ("사용" if PROXY_URL else "(없음)") + " / HEADLESS=" + str(HEADLESS))

    sections = []
    any_hit = False
    fail_count = 0
    blocked = False

    with sync_playwright() as p:
        launch_kwargs = {"headless": HEADLESS, "args": ["--disable-blink-features=AutomationControlled", "--lang=ko-KR"]}
        if PROXY_URL:
            pu = urlparse(PROXY_URL)
            proxy = {"server": pu.scheme + "://" + pu.hostname + (":" + str(pu.port) if pu.port else "")}
            if pu.username:
                proxy["username"] = pu.username
                proxy["password"] = pu.password or ""
            launch_kwargs["proxy"] = proxy
        browser = p.chromium.launch(**launch_kwargs)

        if CLIENT_MODE == "desktop":
            ctx_kwargs = {"viewport": {"width": 1280, "height": 900}}
        else:
            ctx_kwargs = {"viewport": {"width": 430, "height": 932}, "is_mobile": True, "has_touch": True}
        context = browser.new_context(user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul",
                                      extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"}, **ctx_kwargs)
        context.add_init_script(STEALTH_JS)
        try:
            warm_up(context)
            for idx, rk in enumerate(rankings, start=1):
                if blocked:
                    # 같은 IP로는 나머지도 막히므로 시간 낭비 없이 건너뜀
                    fail_count += 1
                    sections.append("*[" + rk["name"] + "]* :no_entry: " + BLOCKED_MSG)
                    continue
                try:
                    hits, max_seen = check_one(context, rk, idx)
                    if hits:
                        any_hit = True
                    if max_seen == 0 and not hits:
                        fail_count += 1
                    sections.append(format_section(rk, hits, max_seen))
                except BlockedError as e:
                    log("[blocked] " + rk["name"] + " : " + str(e))
                    blocked = True
                    fail_count += 1
                    sections.append("*[" + rk["name"] + "]* :no_entry: " + str(e))
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
