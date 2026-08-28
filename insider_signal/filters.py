"""매수 신호 필터링 규칙 (CLAUDE.md의 4가지 규칙 구현).

각 함수는 하나의 규칙만 담당하고, ``evaluate()``가 이들을 조합해 최종 판정을 내립니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import ENTITY_NAME_HINTS, Settings
from .history import HistoryStore
from .models import ReportingOwner, Transaction


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reasons_failed: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.passed


def is_open_market_purchase(txn: Transaction) -> bool:
    """매수(취득) 거래인지 여부. Transaction Code 'P' + Acquired 'A'만 신호로 인정."""

    return txn.is_open_market_purchase


def is_within_value_range(
    txn: Transaction,
    settings: Settings,
    *,
    min_override: float | None = None,
    max_override: float | None = None,
) -> bool:
    """요구사항 1: 10만 달러 ~ 50만 달러 거래만 사용.

    ``min_override``: 백테스트 섹터 실험(--sector-experiment) 전용. 바이오 이슈어에 한해
    완화된 하한을 실험적으로 적용할 때만 쓰이며, 라이브 poller와 기본 백테스트 경로는
    이 값을 넘기지 않으므로 CLAUDE.md에 명시된 기본 하한($100,000)이 그대로 유지된다.
    ``max_override``: 백테스트 상한 실험(--max-value-override) 전용. 상한 $500,000이
    신호 품질에 정말 필요한지 검증하기 위한 것으로, 마찬가지로 라이브 poller와 기본
    백테스트 경로에는 영향을 주지 않는다.
    """

    min_value = min_override if min_override is not None else settings.min_txn_value_usd
    max_value = max_override if max_override is not None else settings.max_txn_value_usd
    return min_value <= txn.value_usd <= max_value


def is_individual(owner: ReportingOwner) -> bool:
    """요구사항 2: 개인 거래만 사용 (기업/펀드/신탁 등 제외).

    Form 4에는 개인/법인을 직접 구분하는 필드가 없어 휴리스틱으로 판단합니다:
    이름에 법인 접미사가 포함되면 기업으로 간주해 제외합니다.
    """

    name_upper = owner.name.upper()
    return not any(hint in name_upper for hint in ENTITY_NAME_HINTS)


def is_officer_or_director(owner: ReportingOwner) -> bool:
    """요구사항 3: 10% Owner 제외, Officer(CEO/CFO 포함)/Director만 사용."""

    return owner.is_officer or owner.is_director


def is_10b5_1_plan_trade(txn: Transaction) -> bool:
    """요구사항 4-a: Rule 10b5-1 사전 계획 거래는 반복/자동 매수로 간주해 배제."""

    return txn.is_10b5_1_plan


def is_recurring_buyer(
    owner: ReportingOwner,
    issuer_cik: str,
    settings: Settings,
    history: HistoryStore,
    *,
    as_of: date | None = None,
) -> bool:
    """요구사항 4-b: 로컬 이력상 같은 (issuer, owner) 조합의 반복 매수인지 판단.

    ``as_of``는 백테스트에서 과거 filing의 날짜 기준으로 lookback 창을 계산하기 위한 것으로,
    생략하면 기존과 동일하게 ``date.today()`` 기준으로 동작합니다.
    """

    occurrences = history.count_recent_purchases(
        owner_cik=owner.cik,
        issuer_cik=issuer_cik,
        lookback_days=settings.recurring_lookback_days,
        as_of=as_of,
    )
    return occurrences >= settings.recurring_min_occurrences


def evaluate(
    txn: Transaction,
    owner: ReportingOwner,
    issuer_cik: str,
    settings: Settings,
    history: HistoryStore,
    *,
    as_of: date | None = None,
    min_value_override: float | None = None,
    max_value_override: float | None = None,
) -> FilterResult:
    failed: list[str] = []

    if not is_open_market_purchase(txn):
        failed.append("공개시장 매수(P/A)가 아님")
    if not is_within_value_range(
        txn, settings, min_override=min_value_override, max_override=max_value_override
    ):
        applied_min = min_value_override if min_value_override is not None else settings.min_txn_value_usd
        applied_max = max_value_override if max_value_override is not None else settings.max_txn_value_usd
        failed.append(
            f"거래금액 ${txn.value_usd:,.0f}가 허용범위 "
            f"(${applied_min:,.0f}~${applied_max:,.0f}) 밖"
        )
    if not is_individual(owner):
        failed.append("법인/펀드 등으로 추정되는 신고인 이름")
    if not is_officer_or_director(owner):
        failed.append("Officer/Director가 아님 (10% Owner 또는 Other만 해당)")
    if is_10b5_1_plan_trade(txn):
        failed.append("Rule 10b5-1 사전 계획 거래")
    if is_recurring_buyer(owner, issuer_cik, settings, history, as_of=as_of):
        failed.append("최근 반복 매수 이력 존재")

    return FilterResult(passed=not failed, reasons_failed=tuple(failed))
