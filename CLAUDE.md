@HARNESS.md
# CLAUDE.mdS

이 파일은 이 저장소에서 작업하는 Claude Code(및 다른 협업자)를 위한 가이드입니다.

## 프로젝트 개요

**insider-signal**은 SEC Form 4(내부자 거래 공시)를 실시간으로 수집해 특정 조건을 만족하는
"매수" 거래만 걸러내고, 조건을 만족하면 알림을 보내는 신호 생성 파이프라인입니다.
최종 목표는 이 신호를 자동매매 프로그램의 매수 트리거로 사용하는 것이지만,
**현재 범위는 "데이터 수집 → 파싱 → 필터링 → 알림"과, 과거 데이터에 대한 오프라인 백테스트까지이며
실제 주문 실행(브로커 연동)은 포함하지 않습니다.**
주문 실행 모듈을 추가할 때는 반드시 별도 논의 후 진행하세요 (실제 자금이 움직이는 민감한 영역).

## 매수 신호 필터링 규칙 (핵심 도메인 로직)

`insider_signal/filters.py`에 구현되어 있으며, 다음 4가지 조건을 **모두** 만족해야 신호로 인정합니다.
이 규칙은 사용자가 명시적으로 정의한 것이므로 임의로 바꾸지 말고, 바꿀 때는 반드시 확인 후 진행하세요.

1. **거래 금액 필터**: `shares * price` 기준 10만 달러(\$100,000) ~ 50만 달러(\$500,000) 사이의 거래만 사용.
   - 기본값은 `config.py`의 `MIN_TXN_VALUE_USD` / `MAX_TXN_VALUE_USD` (환경변수로 override 가능).
2. **개인 거래만 사용**: 법인/펀드/신탁 등 기업 명의의 신고는 제외.
   - Form 4에는 "개인/법인"을 직접 구분하는 필드가 없어 다음을 함께 사용하는 휴리스틱으로 판단합니다.
     - `isOfficer` 또는 `isDirector` 플래그가 있는 경우 사람일 확률이 높음 (법인은 임원/이사가 될 수 없음).
     - 신고인 이름(`rptOwnerName`)에 `LLC`, `L.P.`, `LP`, `Inc`, `Corp`, `Trust`, `Partners`,
       `Capital`, `Management`, `Fund`, `Holdings`, `Group` 등 법인 접미사가 포함되면 기업으로 간주해 제외.
   - `filters.py::is_individual()` 참고.
3. **직급 필터**: `10% Owner`만 있는 신고는 제외하고, `Officer`(CEO/CFO 포함) 또는 `Director`만 사용.
   - 즉 `isOfficer == True or isDirector == True` 이어야 하고, 이 둘이 모두 False면(10% Owner/Other만 해당) 제외.
   - CEO/CFO는 Form 4의 `officerTitle` 필드에 텍스트로 들어있는 `Officer`의 하위 개념이라 별도 필드가 없음 — 즉
     "Officer, Director만 사용"이라는 규칙이 CEO/CFO를 포함하는 상위 규칙입니다.
4. **반복 매수 배제**: 어떤 이유가 있어서 사는 게 아니라 정기적/자동으로 사는 거래(신호로서 가치가 낮음)는 제외.
   - **Rule 10b5-1 플랜 거래**: 각주(footnote)에 `10b5-1` 문구가 포함되면 무조건 제외 (가장 신뢰도 높은 신호).
   - **로컬 이력 기반 반복 매수 탐지**: `history.py`가 SQLite(`data/insider_signal.db`)에 모든 매수 이력을
     (issuer_cik, owner_cik) 단위로 누적 저장하고, 최근 `RECURRING_LOOKBACK_DAYS`(기본 90일) 동안
     `RECURRING_MIN_OCCURRENCES`(기본 3회) 이상 같은 조합의 매수가 있으면 "반복 매수자"로 간주해 제외.
   - 이 로컬 이력은 프로그램을 처음 돌리는 시점에는 비어 있으므로, 첫 실행 시 EDGAR
     submissions API(`edgar_client.get_owner_recent_form4_count`)로 해당 신고인의 최근 Form 4 제출 빈도를
     조회해 초기 이력을 보강합니다 (이슈어 단위까지는 구분하지 못하는 근사치이며, 알려진 한계입니다).

거래 신호(Transaction Code)는 오직 **`transactionCode == "P"` (공개시장 매수) AND
`transactionAcquiredDisposedCode == "A"` (취득)** 만 사용합니다. 스톡옵션 행사(`M`), 상여/부여(`A` 코드가 아닌
grant), 증여(`G`) 등은 "본인 판단으로 시장에서 매수"한 것이 아니므로 신호에서 제외합니다. 파생상품
(`derivativeTable`)도 이번 범위에서는 다루지 않고 `nonDerivativeTable`만 사용합니다.

## 아키텍처

```
insider_signal/
  config.py        하이퍼파라미터/환경변수 로딩 (Settings 데이터클래스)
  models.py         Filing / Transaction / ReportingOwner / Issuer 데이터클래스
  edgar_client.py    SEC EDGAR HTTP 클라이언트 (rate limit, retry, User-Agent 필수)
  parser.py         Form 4 XML(ownershipDocument) -> models 파싱
  filters.py         4가지 필터 규칙 구현 + evaluate() 오케스트레이터 (as_of로 백테스트도 지원)
  history.py         SQLite 기반 dedup + 반복매수 탐지 저장소
  notifier.py         알림 채널 추상화 (Console/Slack/Discord/Telegram, 조합 가능)
  poller.py           전체 파이프라인 오케스트레이션 (polling loop)
  backtest.py          오프라인 백테스트 오케스트레이션 (과거 신호 재구성 + 목표가/손절 시뮬레이션)
  price_data.py         yfinance 기반 주가(OHLC) 조회 (백테스트 전용, yfinance 의존성이 갇혀있는 유일한 모듈)
  cli.py              진입점 (`run [--once] [--interval N]`, `backtest [--start] [--end] [--target-pct] [--stop-pct]`)
```

데이터 흐름(라이브): EDGAR "최신 Form 4" Atom 피드 polling → 신규 accession 발견 →
filing index JSON에서 실제 XML 문서 URL 탐색 → XML 파싱 → 트랜잭션 단위로 4가지 필터 적용 →
통과 시 알림 전송 + 이력 DB에 기록 (통과 여부와 무관하게 반복매수 탐지를 위해 모든 매수는 기록).

백테스트는 같은 파싱/필터 로직을 재사용하지만 별도 데이터 흐름을 씁니다 — 아래 "백테스트" 섹션 참고.

## 백테스트 (오프라인 시뮬레이션)

라이브 polling과 별개로, 과거 기간에 대해 신호를 재구성하고 목표수익률/손절 시뮬레이션을 도는
오프라인 백테스트가 있습니다 (`backtest.py`, `price_data.py`, `python -m insider_signal.cli backtest`).
나중에 로직을 수정할 가능성(예: 종목별로 다른 목표/손절 가중치)을 염두에 두고 아래처럼 분리해뒀습니다.

- **신호 발굴과 가격 시뮬레이션이 완전히 분리**되어 있습니다:
  1. `enumerate_and_filter()` — SEC 분기별 벌크 인덱스(`master.idx`)로 과거 Form 4를 열거하고,
     기존 `filters.evaluate()`를 그대로 재사용해 신호를 판정한 뒤 `BacktestStore.signals`에 저장.
     이 단계가 EDGAR 레이트리밋 때문에 느립니다 (filing당 요청 2회, 시장 전체 기준 하루 1,000~1,500건).
  2. `run_price_simulations()` — 저장된 신호에 대해 yfinance로 일봉을 가져와
     `simulate_trade()`(순수 함수)로 목표가/손절가 도달 여부를 계산해 `BacktestStore.simulations`에 저장.
  - **분리해둔 이유**: target/stop 값이나 보유기간 같은 시뮬레이션 파라미터를 바꿔서 다시 돌릴 때
    (그리고 여러 시간 걸리는) EDGAR 재조회가 필요 없게 하기 위함입니다. `simulations` 테이블은
    `(signal_id, target_pct, stop_pct, max_hold_days)` 단위로 저장되므로, `signals` 테이블은 그대로
    두고 시뮬레이션만 다른 파라미터로 몇 번이든 재실행할 수 있습니다.
- **확장 지점(종목별 가중치 등)**: `simulate_trade(entry_price, hold_bars, *, target_pct, stop_pct,
  max_hold_days)`는 순수 함수라 신호마다 다른 target/stop 값을 넣어 호출하는 확장이 쉽습니다.
  다만 지금은 `run_price_simulations()`가 런 전체에 대해 target_pct/stop_pct를 CLI에서 하나의
  값으로만 받으므로, 종목별로 다르게 주려면 이 함수에 티커→값 매핑(또는 신호별 가중치 함수)을
  넣어주는 국소적인 확장이 필요합니다 — `edgar_client.py`/`filters.py`/신호 수집 단계는 안 건드려도 됩니다.
  포트폴리오 비중(자금 배분) 시뮬레이션은 아직 없고, 필요해지면 `simulations` 테이블에 이미 저장된
  진입/청산가·수익률 위에 얹는 형태로 추가하면 됩니다 (신호 재발굴 불필요).
- **진입가/시점**: `master.idx`에서 얻은 `HistoricalFilingRef.filed_at`(EDGAR 실제 제출일) 다음
  거래일 시가. 파싱된 `Filing.filed_at`(XML `periodOfReport`, 실질적으로 거래일과 거의 같은 값)은
  진입가 계산에 쓰면 안 됩니다 — 그대로 쓰면 look-ahead bias가 재발합니다.
- **청산 규칙**: +target_pct%/-stop_pct% 중 일봉 고가/저가로 먼저 닿는 쪽 (기본 +3%/-4%). 같은 봉에서
  둘 다 닿으면 보수적으로 손절 우선. 최대 보유 기간(기본 30거래일) 안에 안 닿으면 그날 종가로
  "timeout" 종료.
- **반복매수 판정의 as_of**: 라이브 poller는 `date.today()` 기준으로 반복매수를 판정하지만, 백테스트는
  각 filing의 실제 제출일을 `as_of`로 넘겨 그 시점 기준으로 판정합니다 (`filters.evaluate(...,
  as_of=...)` → `history.count_recent_purchases(..., as_of=...)`). `as_of`를 생략하면 기존과 동일하게
  `date.today()`로 동작하므로 라이브 경로는 이 변경으로 영향받지 않습니다.
- **알려진 v1 한계**: 백테스트 구간 시작 시점엔 로컬 이력이 비어있어, 첫 `RECURRING_LOOKBACK_DAYS`
  (기본 90일) 동안은 반복매수 판정이 실제보다 느슨합니다.
- **DB 분리**: 라이브 이력 DB(`data/insider_signal.db`)는 백테스트가 절대 건드리지 않습니다. 대신
  `data/backtest_runs/{기간}_history.sqlite3`(재개용 dedup/반복매수 이력)와
  `data/backtest_runs/{기간}_results.sqlite3`(신호+시뮬레이션 결과, CSV로도 export)를 따로 씁니다.
- 실행: `python -m insider_signal.cli backtest --target-pct 3 --stop-pct 4` (기본 최근 1개월,
  `--start`/`--end`로 조정). 시장 전체 스캔은 느리므로(하루 1,000~1,500건, 레이트리밋 때문에 filing당
  약 0.4초) 기간을 넓게 잡을수록 오래 걸립니다 — 중단 후 같은 명령으로 재실행하면 이미 처리한
  filing은 건너뛰고 이어서 진행합니다.

## SEC EDGAR 사용 시 반드시 지킬 것

- **User-Agent 헤더 필수**: SEC Fair Access 정책상 `"AppName contact@example.com"` 형식의 실제 연락처가
  포함된 User-Agent가 없으면 차단/제한될 수 있습니다. `.env`의 `SEC_EDGAR_CONTACT`에 실제 연락처를 넣어야 합니다.
  **Claude는 사용자의 이메일을 이 값에 자동으로 채워 넣지 않습니다** — 반드시 사용자가 직접 `.env`에 입력하도록 안내하세요.
- **Rate limit**: 초당 10회 이하 권장(SEC 정책), 이 프로젝트는 기본적으로 요청 간 최소 간격을 두어 더 보수적으로 동작합니다
  (`edgar_client.py`의 `MIN_REQUEST_INTERVAL_SEC`).
- 공개 데이터만 조회하는 GET 요청만 사용합니다. 인증/로그인/주문 관련 요청은 이 프로젝트 범위에 없습니다.

## 코딩 컨벤션

- Python 3.11+, 표준 라이브러리 우선 사용 (`xml.etree.ElementTree`, `sqlite3`, `dataclasses`).
  외부 의존성은 `requests`, `python-dotenv`, (백테스트 전용) `yfinance` 정도로 최소화합니다.
  `yfinance`/`pandas` import는 `price_data.py`에만 두고, `run` 커맨드 경로에서는 지연 import도
  거치지 않게 유지하세요 (라이브 polling만 쓰는 사용자가 불필요한 의존성을 설치하지 않도록).
- 타입 힌트를 항상 사용하고, 도메인 값(금액/개수)은 `Decimal` 대신 `float`로 다루되 금액 비교는
  정수 센트 단위 반올림 오차에 민감하지 않으므로 float로 충분합니다 (이미 큰 금액 단위 필터라 오차 무관).
- 로그는 `logging` 모듈 사용, 알림 실패는 예외를 삼키지 말고 로그만 남기고 다음 항목으로 진행
  (한 건의 알림 실패로 전체 polling이 죽으면 안 됨).
- 주석은 "왜"가 비직관적일 때만 작성 (예: SEC 스키마의 특이사항, 휴리스틱의 근거).

## 테스트

- `tests/`에 pytest 기반 단위 테스트. 실제 네트워크 호출 없이 `tests/fixtures/*.xml` 샘플로 파서/필터를 검증합니다.
  `tests/test_backtest.py`는 `simulate_trade`를 순수 함수로, filing 처리는 가짜 EDGAR 클라이언트로
  검증합니다(실제 EDGAR/yfinance 호출 없음).
- 실행: `pytest`
- EDGAR 실동작 확인은 `python -m insider_signal.cli run --once`로 수동 검증 (네트워크 필요, `.env` 설정 필요).
- 백테스트 실동작 확인은 좁은 날짜 구간(며칠)으로 `python -m insider_signal.cli backtest --start ... --end ...`
  실행해 EDGAR+yfinance 연동을 먼저 확인한 뒤 넓은 구간을 돌리세요 (시장 전체 스캔은 느립니다).

## 하지 말아야 할 것

- 실제 주문 실행/브로커 API 연동 코드를 이 저장소에 조용히 추가하지 말 것 (사용자가 명시적으로 요청할 때만).
- 필터 임계값(10만~50만 달러, Officer/Director 전용 등)을 사용자 확인 없이 임의로 변경하지 말 것.
- 백테스트의 목표수익률/손절률 기본값(+3%/-4%), 최대 보유 기간(30거래일), 진입가 규칙(공시일 다음
  거래일 시가)도 사용자가 명시적으로 확정한 값이므로 확인 없이 바꾸지 말 것.
- `.env` 파일이나 실제 알림 webhook URL, SEC 연락처 이메일을 커밋하지 말 것 (`.gitignore`에 이미 포함).
