# insider-signal

SEC Form 4(내부자 거래 공시) 데이터를 실시간으로 모니터링해 특정 조건을 만족하는
"내부자 매수" 신호를 감지하고 알림을 보내는 파이프라인입니다.

자동매매의 매수 트리거로 쓰기 위한 프로젝트지만, **현재 범위는 데이터 수집 → 필터링 → 알림까지**이며
실제 주문 실행(브로커 연동)은 포함하지 않습니다. 필터링 규칙과 아키텍처의 자세한 설명은
[CLAUDE.md](CLAUDE.md)를 참고하세요.

## 필터링 규칙 요약

1. 거래 금액이 **10만~50만 달러**인 매수 거래만 사용
2. **개인** 신고인 거래만 사용 (법인/펀드/신탁 등 제외)
3. **10% Owner 제외**, Officer(CEO/CFO 포함)/Director만 사용
4. **반복/자동 매수 제외**: Rule 10b5-1 사전 계획 거래, 로컬 이력상 최근 반복 매수자

## 설치

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements-dev.txt
```

## 설정

`.env.example`을 복사해 `.env`를 만들고 값을 채우세요.

```bash
cp .env.example .env
```

**반드시 `SEC_EDGAR_CONTACT`를 본인의 실제 연락처로 채우세요.** SEC EDGAR는 자동화된 요청에
실제 연락처가 담긴 User-Agent를 요구하며, 없으면 차단될 수 있습니다.

```
SEC_EDGAR_CONTACT="insider-signal/0.1 your-email@example.com"
```

알림 채널(`SLACK_WEBHOOK_URL`, `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID`)은
하나 이상 설정하면 됩니다. 아무것도 설정하지 않으면 콘솔 로그로만 출력됩니다.

## 실행

```bash
# 최신 Form 4를 한 번만 조회하고 종료 (동작 확인용)
python -m insider_signal.cli run --once -v

# 계속 polling (기본 5분 주기, .env의 POLL_INTERVAL_SECONDS로 조절)
python -m insider_signal.cli run

# 주기를 CLI에서 직접 지정
python -m insider_signal.cli run --interval 120
```

이력(dedup, 반복매수 탐지용 매수 기록)은 `data/insider_signal.db`(SQLite)에 저장됩니다.
처음 실행 시에는 이력이 비어 있어 "반복 매수" 판정이 정확하지 않을 수 있고, 신고인을 처음
관측할 때 EDGAR에서 최근 제출 이력을 조회해 자동으로 보강합니다 (알려진 한계는
[CLAUDE.md](CLAUDE.md)의 "요구사항 4" 섹션 참고).

## 테스트

```bash
pytest
```

네트워크 호출 없이 `tests/fixtures/*.xml` 샘플과, 실제 EDGAR 응답을 curl로 확인해 만든
`tests/test_edgar_client.py`의 고정 샘플로 파서/필터/EDGAR 클라이언트 파싱 로직을 검증합니다.

## 알려진 한계

- 반복 매수 탐지는 신고인 CIK 기준 로컬 이력 + EDGAR 제출 이력 근사치를 사용합니다.
  이슈어 단위까지 완벽히 구분하지 못하는 경우가 있습니다 (한 명의 신고인이 여러 회사의
  임원/이사인 경우).
- "개인 vs 법인" 판별은 Form 4 스키마에 직접적인 필드가 없어 이름 기반 휴리스틱을 사용합니다.
- 파생상품 거래(스톡옵션 등, `derivativeTable`)는 이번 범위에서 다루지 않습니다.
