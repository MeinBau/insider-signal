"""연구용 일회성 스크립트: scripts/tickers_for_market_cap.json의 티커들의 현재 시가총액을
yfinance로 조회해 CSV로 저장한다.

CEO/CFO vs Director만 매수 신호의 성과 차이가 시가총액(회사 규모) 쏠림 때문인지 확인하는
교란요인 체크용 (insider_signal 핵심 파이프라인과는 무관, GH Actions에서만 실행 — yfinance가
로컬 클라우드 세션 프록시와 충돌해서 여기서는 못 돌림).
"""

import csv
import json
import time
from pathlib import Path

import yfinance as yf

HERE = Path(__file__).parent
tickers = json.loads((HERE / "tickers_for_market_cap.json").read_text())

rows = []
for i, ticker in enumerate(tickers, start=1):
    market_cap = None
    try:
        market_cap = yf.Ticker(ticker).fast_info.get("market_cap")
    except Exception as exc:
        print(f"[{i}/{len(tickers)}] {ticker}: 조회 실패 ({exc})")
    rows.append((ticker, market_cap))
    if i % 25 == 0:
        print(f"[{i}/{len(tickers)}] 진행 중...")
    time.sleep(0.2)

out_path = HERE / "market_caps.csv"
with out_path.open("w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["ticker", "market_cap"])
    writer.writerows(rows)

found = sum(1 for _, mc in rows if mc)
print(f"완료: {len(rows)}개 중 {found}개 시가총액 확보, {out_path}에 저장")
