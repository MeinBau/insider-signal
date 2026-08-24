from datetime import date
from pathlib import Path

from insider_signal.parser import parse_form4_xml

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str, accession_no: str = "0000000000-26-000001"):
    xml_bytes = (FIXTURES / name).read_bytes()
    return parse_form4_xml(xml_bytes, accession_no=accession_no, source_url="https://example.test/doc.xml")


def test_parses_cfo_purchase():
    filing = _load("sample_form4_cfo_purchase.xml")

    assert filing.issuer.name == "Example Robotics Inc"
    assert filing.issuer.ticker == "EXRB"
    assert filing.owner.name == "Smith Jane"
    assert filing.owner.is_officer is True
    assert filing.owner.is_director is False
    assert filing.owner.is_ten_percent_owner is False
    assert filing.owner.officer_title == "Chief Financial Officer"

    assert len(filing.transactions) == 1
    txn = filing.transactions[0]
    assert txn.transaction_code == "P"
    assert txn.acquired_disposed_code == "A"
    assert txn.shares == 3000
    assert txn.price_per_share == 80.0
    assert txn.value_usd == 240_000.0
    assert txn.transaction_date == date(2026, 8, 10)
    assert txn.is_open_market_purchase is True
    assert txn.is_10b5_1_plan is False


def test_parses_entity_ten_percent_owner():
    filing = _load("sample_form4_entity_ten_percent.xml")

    assert filing.owner.name == "Meridian Capital Partners LP"
    assert filing.owner.is_ten_percent_owner is True
    assert filing.owner.is_officer is False
    assert filing.owner.is_director is False


def test_parses_10b5_1_footnote():
    filing = _load("sample_form4_director_10b51.xml")

    txn = filing.transactions[0]
    assert txn.footnote_texts
    assert "10b5-1" in " ".join(txn.footnote_texts)
    assert txn.is_10b5_1_plan is True


def test_accession_no_is_passed_through_not_parsed_from_xml():
    filing = _load("sample_form4_cfo_purchase.xml", accession_no="0001234567-26-000042")
    assert filing.accession_no == "0001234567-26-000042"
