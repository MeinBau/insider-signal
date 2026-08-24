from datetime import date, timedelta
from pathlib import Path

import pytest

from insider_signal import filters
from insider_signal.config import Settings
from insider_signal.history import HistoryStore
from insider_signal.parser import parse_form4_xml

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    xml_bytes = (FIXTURES / name).read_bytes()
    return parse_form4_xml(
        xml_bytes, accession_no="0000000000-26-000001", source_url="https://example.test/doc.xml"
    )


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        sec_edgar_contact="test test@example.com",
        min_txn_value_usd=100_000,
        max_txn_value_usd=500_000,
        recurring_lookback_days=180,
        recurring_min_occurrences=3,
        history_db_path=Path("unused"),
    )


@pytest.fixture()
def history(tmp_path) -> HistoryStore:
    store = HistoryStore(tmp_path / "test.db")
    yield store
    store.close()


def test_valid_officer_purchase_passes(settings, history):
    filing = _load("sample_form4_cfo_purchase.xml")
    txn = filing.transactions[0]

    result = filters.evaluate(txn, filing.owner, filing.issuer.cik, settings, history)

    assert result.passed is True
    assert result.reasons_failed == ()


def test_entity_ten_percent_owner_is_rejected(settings, history):
    filing = _load("sample_form4_entity_ten_percent.xml")
    txn = filing.transactions[0]

    result = filters.evaluate(txn, filing.owner, filing.issuer.cik, settings, history)

    assert result.passed is False
    assert any("법인" in r for r in result.reasons_failed)
    assert any("Officer/Director" in r for r in result.reasons_failed)


def test_10b5_1_plan_trade_is_rejected(settings, history):
    filing = _load("sample_form4_director_10b51.xml")
    txn = filing.transactions[0]

    result = filters.evaluate(txn, filing.owner, filing.issuer.cik, settings, history)

    assert result.passed is False
    assert any("10b5-1" in r for r in result.reasons_failed)


def test_out_of_range_value_is_rejected(settings, history):
    filing = _load("sample_form4_ceo_small_value.xml")
    txn = filing.transactions[0]

    result = filters.evaluate(txn, filing.owner, filing.issuer.cik, settings, history)

    assert result.passed is False
    assert any("거래금액" in r for r in result.reasons_failed)


def test_recurring_buyer_is_rejected(settings, history):
    filing = _load("sample_form4_cfo_purchase.xml")
    txn = filing.transactions[0]

    today = date.today()
    for i in range(settings.recurring_min_occurrences):
        history.record_purchase(
            owner_cik=filing.owner.cik,
            issuer_cik=filing.issuer.cik,
            transaction_date=today - timedelta(days=30 * (i + 1)),
            shares=1000,
            price_per_share=80,
            accession_no=f"seed-{i}",
        )

    result = filters.evaluate(txn, filing.owner, filing.issuer.cik, settings, history)

    assert result.passed is False
    assert any("반복" in r for r in result.reasons_failed)


def test_first_time_buyer_is_not_flagged_as_recurring(settings, history):
    filing = _load("sample_form4_cfo_purchase.xml")
    txn = filing.transactions[0]

    result = filters.evaluate(txn, filing.owner, filing.issuer.cik, settings, history)

    assert result.passed is True
