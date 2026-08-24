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
