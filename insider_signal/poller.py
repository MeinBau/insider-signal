"""전체 파이프라인 오케스트레이션: EDGAR polling -> 파싱 -> 필터링 -> 알림."""

from __future__ import annotations

import logging
import time

from . import filters
from .config import Settings
from .edgar_client import SECEdgarClient
from .history import HistoryStore
from .models import Filing, Transaction
from .notifier import CompositeNotifier
from .parser import Form4ParseError, parse_form4_xml

logger = logging.getLogger(__name__)


def format_alert(filing: Filing, txn: Transaction) -> tuple[str, str, str]:
    owner = filing.owner
    issuer = filing.issuer

    role_bits = []
    if owner.is_officer:
        role_bits.append(owner.officer_title or "Officer")
    if owner.is_director:
        role_bits.append("Director")
    role = " / ".join(role_bits) if role_bits else "직급 미상"

    title = f"내부자 매수 신호: {issuer.name} ({issuer.ticker or issuer.cik})"

    lines = [
        f"신고인: {owner.name} ({role})",
        f"거래일: {txn.transaction_date.isoformat()}",
        f"수량: {txn.shares:,.0f}주 @ ${txn.price_per_share:,.2f}",
        f"거래금액: ${txn.value_usd:,.0f}",
    ]
    if txn.shares_owned_after is not None:
        lines.append(f"거래 후 보유 주식: {txn.shares_owned_after:,.0f}주")
    body = "\n".join(lines)

    return title, body, filing.source_url


def process_filing(
    *,
    cik: str,
    accession_no: str,
    client: SECEdgarClient,
    history: HistoryStore,
    notifier: CompositeNotifier,
    settings: Settings,
) -> int:
    """하나의 filing을 처리하고, 알림을 보낸 신호 개수를 반환합니다."""

    signal_count = 0
    try:
        xml_url = client.get_primary_xml_url(cik, accession_no)
        if xml_url is None:
            logger.info("XML 문서를 찾지 못함, 건너뜀: %s", accession_no)
            history.mark_seen(accession_no)
            return 0

        xml_bytes = client.fetch_xml(xml_url)
        filing = parse_form4_xml(xml_bytes, accession_no=accession_no, source_url=xml_url)
    except Form4ParseError as exc:
        logger.warning("Form4 파싱 실패, 건너뜀: %s (%s)", accession_no, exc)
        history.mark_seen(accession_no)
        return 0
    except Exception:
        # 네트워크 등 일시적 오류일 수 있으나, 재시도 폭주를 막기 위해 일단 seen 처리합니다.
        logger.exception("filing 처리 중 예외 발생, 건너뜀: %s", accession_no)
        history.mark_seen(accession_no)
        return 0

    if not filing.owner.cik or not filing.issuer.cik:
        history.mark_seen(accession_no)
        return 0

    if not history.is_owner_seeded(filing.owner.cik):
        try:
            synthetic_count = client.get_owner_recent_form4_count(
                filing.owner.cik, settings.recurring_lookback_days
            )
        except Exception:
            logger.exception("신고인 이력 보강(seed) 실패: owner_cik=%s", filing.owner.cik)
            synthetic_count = 0
        # count가 0이어도 seed_owner_history가 seeded_owners에 기록해 재조회를 막습니다.
        history.seed_owner_history(
            owner_cik=filing.owner.cik,
            issuer_cik=filing.issuer.cik,
            synthetic_count=synthetic_count,
        )

    for txn in filing.transactions:
        if not txn.is_open_market_purchase:
            continue

        result = filters.evaluate(txn, filing.owner, filing.issuer.cik, settings, history)
        if result.passed:
            title, body, url = format_alert(filing, txn)
            notifier.notify(title=title, body=body, url=url)
            signal_count += 1
            logger.info("신호 발생: %s / %s ($%.0f)", filing.issuer.ticker, filing.owner.name, txn.value_usd)
        else:
            logger.debug(
                "필터 탈락: %s / %s - %s",
                filing.issuer.ticker,
                filing.owner.name,
                ", ".join(result.reasons_failed),
            )

        # 통과 여부와 무관하게 반복매수 탐지를 위해 모든 공개시장 매수를 기록합니다.
        history.record_purchase(
            owner_cik=filing.owner.cik,
            issuer_cik=filing.issuer.cik,
            transaction_date=txn.transaction_date,
            shares=txn.shares,
            price_per_share=txn.price_per_share,
            accession_no=accession_no,
        )

    history.mark_seen(accession_no)
    return signal_count


def run_once(
    *,
    client: SECEdgarClient,
    history: HistoryStore,
    notifier: CompositeNotifier,
    settings: Settings,
    feed_count: int = 100,
) -> int:
    entries = client.get_latest_form4_entries(count=feed_count)
    new_entries = [e for e in entries if not history.is_seen(e.accession_no)]
    logger.info("최신 Form 4 %d건 중 신규 %d건", len(entries), len(new_entries))

    total_signals = 0
    for entry in new_entries:
        total_signals += process_filing(
            cik=entry.cik,
            accession_no=entry.accession_no,
            client=client,
            history=history,
            notifier=notifier,
            settings=settings,
        )
    return total_signals


def run_forever(
    *,
    client: SECEdgarClient,
    history: HistoryStore,
    notifier: CompositeNotifier,
    settings: Settings,
) -> None:
    logger.info("polling 시작 (주기 %d초)", settings.poll_interval_seconds)
    while True:
        try:
            run_once(client=client, history=history, notifier=notifier, settings=settings)
        except Exception:
            logger.exception("polling 사이클 중 예외 발생, 다음 주기에 재시도합니다.")
        time.sleep(settings.poll_interval_seconds)
