from datetime import date, timedelta

import pytest

from insider_signal.history import HistoryStore


@pytest.fixture()
def history(tmp_path) -> HistoryStore:
    store = HistoryStore(tmp_path / "test.db")
    yield store
    store.close()


def test_seen_filings_dedup(history):
    assert history.is_seen("0001-26-000001") is False
    history.mark_seen("0001-26-000001")
    assert history.is_seen("0001-26-000001") is True


def test_count_recent_purchases_respects_lookback_window(history):
    today = date.today()
    history.record_purchase(
        owner_cik="1",
        issuer_cik="2",
        transaction_date=today - timedelta(days=10),
        shares=100,
        price_per_share=10,
        accession_no="a1",
    )
    history.record_purchase(
        owner_cik="1",
        issuer_cik="2",
        transaction_date=today - timedelta(days=400),  # 범위 밖
        shares=100,
        price_per_share=10,
        accession_no="a2",
    )
    history.record_purchase(
        owner_cik="1",
        issuer_cik="OTHER_ISSUER",  # 다른 issuer는 카운트되지 않아야 함
        transaction_date=today - timedelta(days=5),
        shares=100,
        price_per_share=10,
        accession_no="a3",
    )

    count = history.count_recent_purchases(owner_cik="1", issuer_cik="2", lookback_days=180)
    assert count == 1


def test_seed_owner_history_marks_owner_as_seeded(history):
    assert history.is_owner_seeded("owner-1") is False

    history.seed_owner_history(owner_cik="owner-1", issuer_cik="issuer-1", synthetic_count=5)

    assert history.is_owner_seeded("owner-1") is True
    count = history.count_recent_purchases(owner_cik="owner-1", issuer_cik="issuer-1", lookback_days=30)
    assert count == 5


def test_seed_owner_history_with_zero_count_still_marks_seeded(history):
    history.seed_owner_history(owner_cik="owner-2", issuer_cik="issuer-1", synthetic_count=0)
    assert history.is_owner_seeded("owner-2") is True


def test_count_recent_purchases_as_of_uses_historical_reference_date(history):
    """백테스트가 과거 filing 날짜를 as_of로 넘기면, date.today() 대신 그 날짜 기준으로
    lookback 창이 계산되어야 합니다 (오늘 기준으로 계산하면 과거 데이터의 반복매수 판정이
    틀어지는 버그를 고정하는 회귀 테스트)."""

    reference = date(2020, 6, 1)
    history.record_purchase(
        owner_cik="1",
        issuer_cik="2",
        transaction_date=reference - timedelta(days=10),  # as_of 기준으로는 범위 내
        shares=100,
        price_per_share=10,
        accession_no="a1",
    )

    # as_of 없이(today 기준)는 2020년 거래가 lookback 밖이라 0건이어야 함.
    assert history.count_recent_purchases(owner_cik="1", issuer_cik="2", lookback_days=180) == 0
    # as_of를 그 시점으로 주면 180일 이내이므로 1건이어야 함.
    assert (
        history.count_recent_purchases(
            owner_cik="1", issuer_cik="2", lookback_days=180, as_of=reference
        )
        == 1
    )


def test_record_purchases_and_mark_seen_records_all_purchases_and_marks_seen(history):
    """단일 트랜잭션으로 매수 이력 전체 + seen 마킹이 함께 커밋되어야 합니다. 중복 재처리 방지는
    이 메서드 자체가 아니라 호출부가 ``is_seen()``을 먼저 확인하는 패턴으로 보장됩니다
    (백테스트의 재개 로직이 실제로 그렇게 사용합니다)."""

    purchases = [
        {
            "owner_cik": "1",
            "issuer_cik": "2",
            "transaction_date": date(2026, 1, 5),
            "shares": 100,
            "price_per_share": 10,
            "accession_no": "acc-1",
        },
        {
            "owner_cik": "1",
            "issuer_cik": "2",
            "transaction_date": date(2026, 1, 5),
            "shares": 50,
            "price_per_share": 12,
            "accession_no": "acc-1",
        },
    ]

    assert history.is_seen("acc-1") is False

    history.record_purchases_and_mark_seen(accession_no="acc-1", purchases=purchases)

    assert history.is_seen("acc-1") is True
    assert history.count_recent_purchases(owner_cik="1", issuer_cik="2", lookback_days=3650) == 2


def test_record_purchases_and_mark_seen_marks_seen_even_with_no_purchases(history):
    """XML 파싱 실패나 owner/issuer CIK 누락처럼 매수 이력이 없는 filing도 seen 처리는
    되어야, 재개 시 같은 filing을 무한히 재시도하지 않습니다."""

    history.record_purchases_and_mark_seen(accession_no="acc-empty", purchases=[])

    assert history.is_seen("acc-empty") is True
