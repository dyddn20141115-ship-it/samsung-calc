# -*- coding: utf-8 -*-
"""issue_alert.py 검증 — 네트워크·텔레그램 없이 돈다.  python -I tests/test_issue_alert.py"""
import datetime, os, sys, tempfile, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import issue_alert as ia

KST = ia.KST
NOW = datetime.datetime(2026, 10, 22, 11, 0, tzinfo=KST)   # 목요일 장중


def rss(items):
    body = "".join("<item><title>%s</title><link>%s</link><pubDate>%s</pubDate>%s</item>" % (t, l, p, s) for t, l, p, s in items)
    return ('<?xml version="1.0"?><rss version="2.0"><channel>%s</channel></rss>' % body).encode("utf-8")


def pub(h, m):
    return datetime.datetime(2026, 10, 22, h, m, tzinfo=KST).strftime("%a, %d %b %Y %H:%M:%S +0900")


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.bad += 1
check.bad = 0

# 1) 구글 제목 꼬리표·본문 무시
g = ia.parse_rss(rss([("삼성전자 3분기 영업이익 발표 - 연합뉴스", "https://x/1", pub(10, 0), "<source>연합뉴스</source>")]), "구글뉴스")
check("구글 제목에서 매체 꼬리 제거", g[0]["title"] == "삼성전자 3분기 영업이익 발표" and g[0]["outlet"] == "연합뉴스")

# 2) 분류
check("실적 분류", ia.category("삼성전자 3분기 영업이익 발표") == "실적")
check("종목 이름 없으면 제외", ia.category("SK하이닉스 HBM 수율 개선") is None)
check("분류 키워드 없으면 제외", ia.category("삼성전자 신입사원 채용 시작") is None)
check("머리말 제외", ia.category("[광고] 삼성전자 실적 설명회") is None)

# 3) 중복(매체 달라도 비슷한 제목)
a = {"title": "삼성전자 외국인 순매수 3거래일 연속", "link": "x", "ts": None, "outlet": "a"}
b = {"title": "[종합] 삼성전자, 외국인 순매수 3거래일 연속", "link": "y", "ts": None, "outlet": "b"}
c = {"title": "삼성전자 자사주 매입 공시", "link": "z", "ts": None, "outlet": "c"}
seen = {ia.key(a["title"]): {"n": ia.norm(a["title"])}}
check("유사 제목은 중복", ia.is_dup(b, seen))
check("다른 기사는 중복 아님", not ia.is_dup(c, seen))

# 4) 한 바퀴: 오래됨·분류없음·중복 컷, 한도, 상태 저장
tmp = tempfile.mkdtemp()
ia.STATE_PATH = os.path.join(tmp, "seen.json")
sent = []
ia.tg_send = lambda chat, text: (sent.append(text) or True)
ia.SEND_GAP = 0
os.environ["TELEGRAM_BOT_TOKEN"] = "TEST"; os.environ["TELEGRAM_CHAT_ID"] = "@test"
items = ia.parse_rss(rss([
    ("삼성전자 영업이익 컨센서스 상회", "https://x/a", pub(10, 30), ""),
    ("삼성전자 영업이익 컨센서스 상회 전망", "https://x/b", pub(10, 40), ""),      # 위와 중복
    ("삼성전자 노조 임금협상 재개", "https://x/c", pub(10, 35), ""),               # 분류 없음
    ("삼성전자 공급계약 체결 공시", "https://x/d", pub(1, 0), ""),                 # 6시간 초과
    ("삼성전자 HBM4 퀄 테스트 통과", "https://x/e", pub(10, 50), ""),
]), "연합뉴스")
args = types.SimpleNamespace(dry=False, bootstrap=False, force=False)
rc = ia.run(args, items=items, ok=1, bad=0, now=NOW)
check("정상 종료", rc == 0)
check("보낼 것만 2건(분류·신선·중복 컷)", len(sent) == 2)
check("오래된 글이 먼저(시간순)", "컨센서스" in sent[0] and "HBM4" in sent[1])
check("링크·분류 머리말 포함", "[실적]" in sent[0] and 'href="https://x/a"' in sent[0])
sent.clear()
ia.run(args, items=items, ok=1, bad=0, now=NOW)
check("재실행하면 새 전송 0건(중복 기억)", len(sent) == 0)

# 5) 전송 실패는 본 글로 기록하지 않는다 → 다음 회차 재시도
os.remove(ia.STATE_PATH)
ia.tg_send = lambda chat, text: False
ia.run(args, items=items, ok=1, bad=0, now=NOW)
ia.tg_send = lambda chat, text: (sent.append(text) or True)
sent.clear()
ia.run(args, items=items, ok=1, bad=0, now=NOW)
check("전송 실패분은 다음 회차에 재시도", len(sent) == 2)

# 6) 한도: 7건 이상이면 최신 5건만
os.remove(ia.STATE_PATH); sent.clear()
many = ia.parse_rss(rss([("삼성전자 영업이익 기사 %d번 서로다른내용%s" % (i, "가나다라마바사아자차카타파하"[i % 14] * 6), "https://x/m%d" % i, pub(10, i), "") for i in range(8)]), "연합뉴스")
ia.run(args, items=many, ok=1, bad=0, now=NOW)
check("한 번에 최대 5건", len(sent) == ia.MAX_PER_RUN)

# 7) 시간대 / 전부 실패
os.remove(ia.STATE_PATH); sent.clear()
night = datetime.datetime(2026, 10, 22, 22, 0, tzinfo=KST)
ia.run(args, items=items, ok=1, bad=0, now=night)
check("장 시간대 밖이면 안 보냄", len(sent) == 0)
sat = datetime.datetime(2026, 10, 24, 11, 0, tzinfo=KST)
check("토요일은 시간대 밖", not ia.in_window(sat))
rc = ia.run(args, items=[], ok=0, bad=3, now=NOW)
check("전부 실패하면 종료코드 1 + 연속 실패 기록", rc == 1 and ia.load_state()["fail_streak"] == 1)

# 8) 첫 가동(bootstrap): 전송 없이 본 글만 기록
os.remove(ia.STATE_PATH); sent.clear()
ia.run(types.SimpleNamespace(dry=False, bootstrap=True, force=False), items=items, ok=1, bad=0, now=NOW)
ia.run(args, items=items, ok=1, bad=0, now=NOW)
check("bootstrap 뒤엔 기존 글을 안 보냄", len(sent) == 0)

print("\n실패 %d건" % check.bad)
sys.exit(1 if check.bad else 0)
