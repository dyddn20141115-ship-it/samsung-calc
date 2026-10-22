# -*- coding: utf-8 -*-
"""
issue_alert.py — 삼성전자 이슈 알림 1단계: 뉴스 RSS의 새 제목을 골라 텔레그램 채널에 「제목 + 링크」로 올린다.

실행:
    python scripts/issue_alert.py --dry            # 보내지 않고 무엇을 보낼지만 출력 (토큰 없이도 됨)
    python scripts/issue_alert.py --bootstrap      # 지금 보이는 글을 전부 「이미 본 글」로 표시만 (첫 가동 때 한 번)
    python scripts/issue_alert.py                  # 실제 전송 (환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 필요)

토큰·채널 ID는 환경변수로만 받는다. 파일·로그·화면에 찍지 않는다.
  TELEGRAM_BOT_TOKEN   봇 토큰 (BotFather)
  TELEGRAM_CHAT_ID     채널 (예: @채널이름 또는 -100…)
  TELEGRAM_ADMIN_CHAT_ID  (선택) 전부 실패가 이어질 때 사장님께만 가는 점검 알림

넘지 않는 선: 기사 본문은 가져오지도 옮기지도 않는다(제목·매체·시각·링크만). 매매 지시·목표가·전망 문구를 만들지 않는다.
키워드 필터는 「어떤 사건 기사인가」 표시용 분류이고, 그 분류 이름이 우리가 덧붙이는 유일한 말이다.
"""
import argparse
import datetime
import difflib
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "..", "state", "seen.json")
KST = datetime.timezone(datetime.timedelta(hours=9))

# ── 소스 (2026-10-22 실측: 구글뉴스·연합뉴스 경제·한경 증권 응답 OK. 매경 403, 머니투데이·네이버 404라 뺀다)
SOURCES = [
    ("구글뉴스", "https://news.google.com/rss/search?q=" + urllib.parse.quote("삼성전자 when:1d") + "&hl=ko&gl=KR&ceid=KR:ko"),
    ("연합뉴스", "https://www.yna.co.kr/rss/economy.xml"),
    ("한국경제", "https://www.hankyung.com/feed/finance"),
]

# ── 종목 이름: 제목에 이 중 하나가 있어야 한다
NAMES = ["삼성전자", "삼성 전자", "삼전"]

# ── 분류 키워드(순서대로 첫 일치가 분류 이름). 분류가 없으면 보내지 않는다 = 잡음 컷
CATEGORIES = [
    ("공시", ["공시", "자사주", "배당", "유상증자", "지분", "공급계약", "수주"]),
    ("실적", ["실적", "영업이익", "영업익", "잠정", "매출", "컨퍼런스콜", "어닝"]),
    ("감산·증산", ["감산", "증산", "가동률", "생산량", "라인 증설", "신규 팹"]),
    ("수급", ["외국인", "기관", "순매수", "순매도", "공매도", "개인 투자자", "수급"]),
    ("반도체", ["HBM", "파운드리", "D램", "DRAM", "낸드", "엔비디아", "TSMC", "수율", "퀄 테스트", "2나노", "3나노"]),
    ("시장", ["코스피", "급락", "급등", "신고가", "신저가", "마감", "장중"]),
]
EXCLUDE = ["[광고]", "[포토]", "[부고]", "[인사]", "[게시판]"]   # 이런 머리말은 제외

MAX_AGE_HOURS = 6          # 이보다 오래된 글은 보내지 않는다
MAX_PER_RUN = 5            # 한 번에 최대 전송 수 (넘치는 글은 로그에만 남기고 「본 글」 처리)
SEEN_KEEP = 600            # 중복 기억 개수
SIMILAR = 0.82             # 제목 유사도가 이 이상이면 같은 기사로 본다 (매체가 달라도)
SEND_GAP = 1.2             # 메시지 사이 간격(초) — 텔레그램 채널 분당 한도 보호

# 전송 허용 시간대(한국시간). 크론이 느슨해도 여기 밖이면 아무것도 안 보낸다.
WINDOWS = [((8, 30), (15, 50)), ((16, 0), (16, 20))]   # 장중 30분 간격 + 장후 1회


try:                                                    # 윈도우 기본 콘솔(cp949)에서도 글자 때문에 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(*a):
    print("[issue_alert]", *a, flush=True)


# ── 가져오기 ──────────────────────────────────────────────
def fetch(url, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (issue-alert)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(str(last))


def parse_rss(raw, source):
    """RSS → [{title, link, ts, outlet}] — 본문(description)은 읽지 않는다."""
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        outlet = source
        if source == "구글뉴스":                       # 구글 제목 끝에 「 - 매체명」이 붙어 있다
            src = it.find("source")
            if src is not None and (src.text or "").strip():
                outlet = src.text.strip()
            title = re.sub(r"\s+-\s+" + re.escape(outlet) + r"\s*$", "", title) if outlet != source else title.rsplit(" - ", 1)[0]
        try:
            ts = email.utils.parsedate_to_datetime(pub).astimezone(KST) if pub else None
        except Exception:
            ts = None
        if title and link:
            out.append({"title": title, "link": link, "ts": ts, "outlet": outlet})
    return out


# ── 거르기 ────────────────────────────────────────────────
def category(title):
    if any(x in title for x in EXCLUDE):
        return None
    if not any(n in title for n in NAMES):
        return None
    for name, kws in CATEGORIES:
        if any(k.lower() in title.lower() for k in kws):
            return name
    return None


def norm(title):
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title)      # [단독]·(종합) 같은 머리말 제거
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
    return t


def key(title):
    return hashlib.sha1(norm(title).encode("utf-8")).hexdigest()[:16]


def is_dup(item, seen):
    k = key(item["title"])
    if k in seen:
        return True
    n = norm(item["title"])
    for s in seen.values():
        if difflib.SequenceMatcher(None, n, s.get("n", "")).ratio() >= SIMILAR:
            return True
    return False


# ── 상태 ──────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("seen", {})
    st.setdefault("fail_streak", 0)
    st.setdefault("bootstrapped", False)
    return st


def save_state(st):
    seen = st["seen"]
    if len(seen) > SEEN_KEEP:                           # 오래된 것부터 버린다
        keep = sorted(seen.items(), key=lambda kv: kv[1].get("t", ""))[-SEEN_KEEP:]
        st["seen"] = dict(keep)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def mark(st, item):
    st["seen"][key(item["title"])] = {"n": norm(item["title"]), "t": datetime.datetime.now(KST).isoformat(timespec="seconds")}


# ── 보내기 ────────────────────────────────────────────────
def message(item, cat):
    when = item["ts"].strftime("%H:%M") if item["ts"] else ""
    head = "<b>[%s] %s</b>" % (html.escape(cat, quote=False), html.escape(item["title"], quote=False))
    meta = html.escape(item["outlet"] + ((" · " + when) if when else ""), quote=False)
    return "%s\n%s\n<a href=\"%s\">기사 보기</a>" % (head, meta, html.escape(item["link"], quote=True))


def tg_send(chat_id, text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=20) as r:
                return json.loads(r.read().decode("utf-8")).get("ok", False)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = json.loads(e.read().decode()).get("parameters", {}).get("retry_after", 5)
                except Exception:
                    wait = 5
                time.sleep(min(wait + 1, 60))
                continue
            log("전송 실패: HTTP %s" % e.code)            # 토큰이 들어간 URL은 찍지 않는다
            return False
        except Exception as e:
            log("전송 실패:", type(e).__name__)
            time.sleep(2)
    return False


# ── 시간대 ────────────────────────────────────────────────
def in_window(now):
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return any(a <= hm <= b for a, b in WINDOWS)


# ── 한 번 돌기 ────────────────────────────────────────────
def collect():
    """모든 소스를 독립적으로 가져온다. 하나가 죽어도 나머지는 계속. (항목들, 성공 수, 실패 수)"""
    items, ok, bad = [], 0, 0
    for name, url in SOURCES:
        try:
            got = parse_rss(fetch(url), name)
            items += got
            ok += 1
            log("%s: %d건" % (name, len(got)))
        except Exception as e:
            bad += 1
            log("%s 실패: %s" % (name, str(e)[:80]))
    return items, ok, bad


def run(args, items=None, ok=1, bad=0, now=None):
    now = now or datetime.datetime.now(KST)
    if not args.force and not args.dry and not args.bootstrap and not in_window(now):
        log("전송 시간대 밖(%s): 아무것도 하지 않음" % now.strftime("%a %H:%M"))
        return 0
    st = load_state()
    if items is None:
        items, ok, bad = collect()

    if ok == 0:                                           # 전부 실패
        st["fail_streak"] += 1
        log("모든 소스 실패 (연속 %d회)" % st["fail_streak"])
        if st["fail_streak"] == 3 and os.environ.get("TELEGRAM_ADMIN_CHAT_ID") and os.environ.get("TELEGRAM_BOT_TOKEN") and not args.dry:
            tg_send(os.environ["TELEGRAM_ADMIN_CHAT_ID"], "이슈 알림: 뉴스 소스 3회 연속 실패. 점검이 필요합니다.")
        save_state(st)
        return 1
    st["fail_streak"] = 0

    # 오래된 글 컷 → 분류 → 중복
    fresh, cut_old, cut_cat, cut_dup = [], 0, 0, 0
    pending_seen = {}
    for it in sorted(items, key=lambda x: x["ts"] or now):   # 오래된 것부터 — 같은 사건이면 먼저 나온 기사가 남는다
        if it["ts"] and (now - it["ts"]).total_seconds() > MAX_AGE_HOURS * 3600:
            cut_old += 1
            continue
        cat = category(it["title"])
        if not cat:
            cut_cat += 1
            continue
        if is_dup(it, st["seen"]) or is_dup(it, pending_seen):
            cut_dup += 1
            continue
        pending_seen[key(it["title"])] = {"n": norm(it["title"])}
        fresh.append((it, cat))
    log("후보 %d건 (오래됨 %d · 분류 없음 %d · 중복 %d 제외)" % (len(fresh), cut_old, cut_cat, cut_dup))

    if args.bootstrap:
        for it, _ in fresh:
            mark(st, it)
        st["bootstrapped"] = True
        save_state(st)
        log("첫 가동 표시: %d건을 본 글로 기록, 전송 없음" % len(fresh))
        return 0

    send, skipped = fresh[-MAX_PER_RUN:], fresh[:-MAX_PER_RUN]
    for it, _ in skipped:                                 # 넘친 글은 쌓이지 않게 본 글 처리
        mark(st, it)
    if skipped:
        log("한도 초과로 %d건은 보내지 않고 건너뜀" % len(skipped))

    sent = 0
    for it, cat in send:
        text = message(it, cat)
        if args.dry:
            print("-" * 40 + "\n" + text)
            continue
        if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
            log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없어 보내지 않음 (--dry로 확인만 가능)")
            break
        if tg_send(os.environ["TELEGRAM_CHAT_ID"], text):
            mark(st, it)                                  # 성공한 것만 본 글 처리 → 실패분은 다음 회차에 재시도
            sent += 1
            time.sleep(SEND_GAP)
    if not args.dry:
        save_state(st)
    log("전송 %d건%s" % (sent, " (드라이런: 상태 저장 안 함)" if args.dry else ""))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="보내지 않고 출력만 (상태도 저장 안 함)")
    ap.add_argument("--bootstrap", action="store_true", help="현재 글을 본 글로만 기록")
    ap.add_argument("--force", action="store_true", help="전송 시간대 검사 건너뜀")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
