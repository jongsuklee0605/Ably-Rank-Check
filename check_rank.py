import os
import re
import sys
import traceback
from urllib.parse import urlsplit
from datetime import datetime, timezone, timedelta

import requests
from playwright.sync_api import sync_playwright

MARKET_ID = os.getenv("MARKET_ID", "116160")
BRAND_NAME = os.getenv("BRAND_NAME", "Canmake")
BRAND_KEYWORDS = ["캔메이크", "canmake"]
MARKET_URL = "https://mobile.a-bly.com/markets/" + MARKET_ID
TOP_N = int(os.getenv("TOP_N", "100"))
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"].strip()

KST = timezone(timedelta(hours=9))
GOODS_RE = re.compile(r"/goods/(\d+)")
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
JS_LINKS = "els => els.map(a => a.href)"
SCROLL_JS = "() => { window.scrollBy(0, 2500); document.querySelectorAll('*').forEach(el => { const s = getComputedStyle(el).overflowY; if ((s === 'auto' || s === 'scroll') && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollTop + 2500; } }); }"


def log(msg):
    print(msg, flush=True)


def has_keyword(text):
    t = str(text or "").lower()
    for k in BRAND_KEYWORDS:
        if k.lower() in t:
            return True
    return False


def is_brand_line(line):
    t = line.strip().lower()
    for k in BRAND_KEYWORDS:
        if t == k.lower():
            return True
    return False


def load_rankings():
    """RANKINGS 환경변수: 한 줄에 하나씩 '이름|URL|클릭할탭' 형식"""
    rankings = []
    raw = os.getenv("RANKINGS", "")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split("|")]
        while len(parts) < 3:
            parts.append("")
        rankings.append({"name": parts[0], "url": parts[1], "tab": parts[2]})
    if not rankings and os.getenv("RANKING_URL"):
        rankings.append({"name": "랭킹", "url": os.environ["RANKING_URL"].strip(), "tab": ""})
    return rankings


class Collector:
    """페이지가 서버에서 받아오는 JSON에서 상품 정보를 수집"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.items = []
        self.seen = set()

    def on_response(self, resp):
        try:
            if "json" not in resp.headers.get("content-type", ""):
                return
            data = resp.json()
        except Exception:
            return
        src = urlsplit(resp.url).netloc + urlsplit(resp.url).path
        self.walk(data, src)

    def walk(self, node, src):
        if isinstance(node, dict):
            gid = node.get("sno")
            if gid is None:
                gid = node.get("goods_sno")
            keys = " ".join(str(k) for k in node.keys())
            if gid is not None and ("price" in keys or "market_sno" in node):
                mk = node.get("market_sno")
                mname = node.get("market_name")
                m = node.get("market")
                if isinstance(m, dict):
                    if mk is None:
                        mk = m.get("sno")
                    if mname is None:
                        mname = m.get("name")
                self.add(str(gid), mk, mname, node.get("name"), src)
            for v in node.values():
                self.walk(v, src)
        elif isinstance(node, list):
            for v in node:
                self.walk(v, src)

    def add(self, gid, mk, mname, name, src):
        if gid in self.seen:
            return
        self.seen.add(gid)
        self.items.append({"id": gid, "market": None if mk is None else str(mk), "mname": mname, "name": name, "src": src})


def parse_text_ranks(text, found):
    """화면 글자에서 '순위숫자 ... 캔메이크 / 상품명' 패턴을 찾아 found[순위] = 상품명 으로 저장"""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    last_rank = None
    for i, line in enumerate(lines):
        if line.isdigit() and 1 <= int(line) <= 500:
            last_rank = int(line)
        elif last_rank is not None and is_brand_line(line):
            name = lines[i + 1] if i + 1 < len(lines) else ""
            if last_rank not in found:
                found[last_rank] = name


def open_and_collect(browser, url, limit, max_scroll, screenshot, tab):
    page = browser.new_page(viewport={"width": 430, "height": 932}, user_agent=UA, locale="ko-KR")
    col = Collector()
    page.on("response", col.on_response)
    dom_ids = []
    text_ranks = {}
    log("[open] " + url + (" (탭: " + tab + ")" if tab else ""))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
        log("[loaded] title=" + page.title())

        if tab:
            col.reset()
            page.get_by_text(tab, exact=True).first.click()
            page.wait_for_timeout(5000)
            log("[tab] '" + tab + "' 클릭 완료")

        stale = 0
        last = -1
        for _ in range(max_scroll):
            hrefs = page.eval_on_selector_all("a[href]", JS_LINKS)
            for h in hrefs:
                m = GOODS_RE.search(h)
                if m and m.group(1) not in dom_ids:
                    dom_ids.append(m.group(1))
            parse_text_ranks(page.inner_text("body"), text_ranks)
            total = len(dom_ids) + len(col.items) + len(text_ranks)
            if total == last:
                stale += 1
            else:
                stale = 0
            last = total
            if len(dom_ids) >= limit or len(col.items) >= limit * 2 or stale >= 4:
                break
            page.evaluate(SCROLL_JS)
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(1500)

        log("[found] dom_goods=" + str(len(dom_ids)) + " / api_goods=" + str(len(col.items)) + " / text_hits=" + str(len(text_ranks)))
    finally:
        try:
            page.screenshot(path=screenshot)
        except Exception:
            pass
        page.close()
    return dom_ids, col, text_ranks


def group_by_src(col):
    groups = {}
    for it in col.items:
        groups.setdefault(it["src"], []).append(it)
    return groups


def log_groups(label, col):
    groups = group_by_src(col)
    ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    for src, items in ordered[:5]:
        s = items[0]
        log("[" + label + " api] " + str(len(items)) + "개 <- " + src + " | 예: id=" + s["id"] + " market=" + str(s["market"]) + " mname=" + str(s["mname"]))


def get_brand_ids(dom_ids, col):
    ids = set(dom_ids)
    for it in col.items:
        if it["market"] == MARKET_ID or has_keyword(it["mname"]):
            ids.add(it["id"])
    return ids


def get_ranking_ids(dom_ids, col):
    if dom_ids:
        return dom_ids[:TOP_N], "화면 링크"
    groups = group_by_src(col)
    if not groups:
        return [], "없음"
    best = max(groups, key=lambda k: len(groups[k]))
    return [it["id"] for it in groups[best]][:TOP_N], "API " + best


def check_one(browser, rk, brand_ids, idx):
    shot = "debug_ranking_" + str(idx) + ".png"
    dom_ids, col, text_ranks = open_and_collect(browser, rk["url"], TOP_N, 30, shot, rk["tab"])
    log_groups(rk["name"], col)

    ranking, source = get_ranking_ids(dom_ids, col)
    log("[" + rk["name"] + "] 순위 목록 " + str(len(ranking)) + "개 (" + source + ")")

    info = {}
    for it in col.items:
        info[it["id"]] = it

    hits = {}
    for i, gid in enumerate(ranking):
        it = info.get(gid, {})
        if gid in brand_ids or it.get("market") == MARKET_ID or has_keyword(it.get("mname")):
            hits[i + 1] = {"id": gid, "name": it.get("name")}

    for rank, name in text_ranks.items():
        if rank <= TOP_N and rank not in hits:
            hits[rank] = {"id": None, "name": name}

    checked = len(ranking) if ranking else (max(text_ranks) if text_ranks else 0)
    return hits, checked


def format_section(name, hits, checked):
    if not hits:
        if checked == 0:
            return "*[" + name + "]* :warning: 순위를 읽지 못했습니다. (스크린샷 확인 필요)"
        return "*[" + name + "]* 진입 없음 (TOP " + str(TOP_N) + " 기준)"
    lines = ["*[" + name + "]* :tada: " + str(len(hits)) + "개 진입"]
    for rank in sorted(hits):
        h = hits[rank]
        label = str(h["name"] or "상품")[:40]
        if h["id"]:
            lines.append("• *" + str(rank) + "위* <https://mobile.a-bly.com/goods/" + h["id"] + "|" + label + ">")
        else:
            lines.append("• *" + str(rank) + "위* " + label)
    return "\n".join(lines)


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
        browser = p.chromium.launch(headless=True)
        try:
            try:
                b_dom, b_col, _ = open_and_collect(browser, MARKET_URL, 1000, 40, "debug_market.png", "")
                log_groups("brand", b_col)
                brand_ids = get_brand_ids(b_dom, b_col)
            except Exception:
                traceback.print_exc()
                brand_ids = set()
            log("[brand] 캔메이크 상품 " + str(len(brand_ids)) + "개 확보")

            for idx, rk in enumerate(rankings, start=1):
                try:
                    hits, checked = check_one(browser, rk, brand_ids, idx)
                    if hits:
                        any_hit = True
                    if checked == 0 and not hits:
                        fail_count += 1
                    sections.append(format_section(rk["name"], hits, checked))
                except Exception as e:
                    traceback.print_exc()
                    fail_count += 1
                    sections.append("*[" + rk["name"] + "]* :warning: 조회 실패: " + str(e)[:150])
        finally:
            browser.close()

    head = ":tada:" if any_hit else ":mag:"
    msg = head + " [" + now + "] " + BRAND_NAME + " 에이블리 랭킹 체크 (브랜드 상품 " + str(len(brand_ids)) + "개 기준)\n" + "\n".join(sections)
    log(msg)
    send_slack(msg)

    if fail_count == len(rankings):
        sys.exit(1)


if __name__ == "__main__":
    main()
