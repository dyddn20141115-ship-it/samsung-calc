# -*- coding: utf-8 -*-
"""
make_price_data.py — 미니앱이 읽는 일봉 데이터 파일(data/<코드>.js)을 만든다.

사용법:
    python scripts/make_price_data.py 005930.KS 삼성전자 -o data/005930.js

왜 JSON이 아니라 .js인가: 화면이 <script src="data/005930.js">로 읽으면 CORS 설정 없이,
파일을 그냥 열어 본 시안(file://)에서도 호스팅에서도 같은 코드로 돌아간다.

원본 로직: 대본작업 쪽 일봉 도구 `슬라이드 관련\\02_영상제작_파이프라인\\motion-slides-kit\\tools\\fetch_chart_data.py --ohlc`
(야후 차트 API 일봉 시·고·저·종, 무료·키 없음). 이 파일은 저장소 밖(깃허브 서버)에서도 혼자 돌아야 해서 표준 라이브러리만 쓰는 사본이다.
장 마감 전에 돌면 오늘의 미완성 봉이 끼므로 장 마감(15:40 한국시간) 전이면 오늘 봉은 뺀다.
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request

API = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d"
UTC = datetime.timezone.utc
KST = datetime.timezone(datetime.timedelta(hours=9))


def fetch_bars(symbol, rng, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(API % (symbol, rng), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:   # 429·일시 장애
            last = e
            time.sleep(3 * (i + 1))
    else:
        raise RuntimeError("야후 응답 실패: %s" % last)
    res = d["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    off = res["meta"].get("gmtoffset", 0)
    bars = []
    for t, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]):
        if None in (o, h, l, c):
            continue   # 휴장·결측일
        day = datetime.datetime.fromtimestamp(t + off, UTC).strftime("%Y-%m-%d")
        row = [day, o, h, l, c, int(v or 0)]
        if bars and bars[-1][0] == day:
            bars[-1] = row
        else:
            bars.append(row)
    return bars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("name")
    ap.add_argument("--range", default="1y")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()

    bars = fetch_bars(a.symbol, a.range)
    now = datetime.datetime.now(KST)
    closed = (now.hour, now.minute) >= (15, 40)
    if bars and bars[-1][0] == now.strftime("%Y-%m-%d") and not closed:
        bars.pop()   # 장중 미완성 봉
    if len(bars) < 60:
        print("[make_price_data] 데이터가 너무 적다: %d개 — 기존 파일을 건드리지 않는다" % len(bars), file=sys.stderr)
        sys.exit(1)

    payload = {"symbol": a.symbol, "name": a.name, "asOf": bars[-1][0],
               "fetched": now.strftime("%Y-%m-%d %H:%M"), "source": "Yahoo Finance 일봉", "bars": bars}
    body = "window.PRICE_DATA=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, a.out)   # 쓰다 끊겨도 기존 파일은 멀쩡하게
    print("[make_price_data] %s -> %s (%d일, %s ~ %s, %d KB)" % (a.symbol, a.out, len(bars), bars[0][0], bars[-1][0], len(body) // 1024))


if __name__ == "__main__":
    main()
