from datetime import date
from pathlib import Path

import pytest

from insider_signal import backtest
from insider_signal.config import Settings
from insider_signal.edgar_client import HistoricalFilingRef
from insider_signal.history import HistoryStore
from insider_signal.price_data import DailyBar

FIXTURES = Path(__file__).parent / "fixtures"


def _bar(day: int, *, open: float, high: float, low: float, close: float) -> DailyBar:
    return DailyBar(trade_date=date(2026, 1, day), open=open, high=high, low=low, close=close)


# --- simulate_trade ---


def test_simulate_trade_target_hit_first_day():
    bars = [_bar(1, open=100, high=105, low=99, close=103)]

    result = backtest.simulate_trade(100.0, bars, target_pct=3.0, stop_pct=4.0, max_hold_days=30)

    assert result.exit_reason == "target"
    assert result.exit_price == pytest.approx(103.0)
    assert result.hold_days == 1


def test_simulate_trade_stop_hit_first_day():
    bars = [_bar(1, open=100, high=101, low=90, close=95)]

    result = backtest.simulate_trade(100.0, bars, target_pct=3.0, stop_pct=4.0, max_hold_days=30)

    assert result.exit_reason == "stop"
    assert result.exit_price == pytest.approx(96.0)
    assert result.hold_days == 1


def test_simulate_trade_same_bar_touches_both_stop_wins():
    # 저가(90)는 손절가(96) 아래, 고가(110)는 목표가(103) 위 -- 같은 봉에서 둘 다 닿음.
    bars = [_bar(1, open=100, high=110, low=90, close=95)]

    result = backtest.simulate_trade(100.0, bars, target_pct=3.0, stop_pct=4.0, max_hold_days=30)

    assert result.exit_reason == "stop"


def test_simulate_trade_timeout_when_max_hold_days_reached():
    bars = [
        _bar(1, open=100, high=101, low=99, close=100.5),
        _bar(2, open=100.5, high=101.5, low=99.5, close=101),
        _bar(3, open=101, high=101.8, low=100, close=101.2),
    ]

    result = backtest.simulate_trade(100.0, bars, target_pct=3.0, stop_pct=4.0, max_hold_days=3)

    assert result.exit_reason == "timeout"
    assert result.hold_days == 3
    assert result.exit_price == pytest.approx(101.2)
    assert result.return_pct == pytest.approx(1.2)


def test_simulate_trade_data_ended_when_fewer_bars_than_max_hold_days():
    bars = [_bar(1, open=100, high=101, low=99, close=100.5)]

    result = backtest.simulate_trade(100.0, bars, target_pct=3.0, stop_pct=4.0, max_hold_days=30)

    assert result.exit_reason == "data_ended"
    assert result.hold_days == 1


def test_simulate_trade_raises_on_empty_bars():
    with pytest.raises(ValueError):
        backtest.simulate_trade(100.0, [], target_pct=3.0, stop_pct=4.0, max_hold_days=30)


# --- _process_one_filing / enumerate_and_filter ---


class _FakeClient:
    def __init__(self, xml_bytes: bytes, refs: list[HistoricalFilingRef]) -> None:
        self._xml_bytes = xml_bytes
        self._refs = refs
        self.get_primary_xml_url_calls = 0
        self.fetch_xml_calls = 0

    def get_primary_xml_url(self, cik: str, accession_no: str) -> str:
        self.get_primary_xml_url_calls += 1
        return "https://example.test/doc.xml"

    def fetch_xml(self, url: str) -> bytes:
        self.fetch_xml_calls += 1
        return self._xml_bytes

    def iter_historical_form4_filings(self, start, end, *, include_amendments: bool = False):
        return list(self._refs)


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
    store = HistoryStore(tmp_path / "history.db")
    yield store
    store.close()


@pytest.fixture()
def store(tmp_path) -> backtest.BacktestStore:
    s = backtest.BacktestStore(tmp_path / "results.db")
    yield s
    s.close()


@pytest.fixture()
def cfo_purchase_ref() -> HistoricalFilingRef:
    return HistoricalFilingRef(
        cik="0000000001",
        company_name="Example Corp",
        form_type="4",
        filed_at=date(2026, 1, 10),
        accession_no="0000000000-26-000001",
    )


def test_process_one_filing_writes_passing_signal_and_records_purchase(
    settings, history, store, cfo_purchase_ref
):
    xml_bytes = (FIXTURES / "sample_form4_cfo_purchase.xml").read_bytes()
    client = _FakeClient(xml_bytes, refs=[cfo_purchase_ref])

    backtest._process_one_filing(
        cfo_purchase_ref, client=client, history=history, store=store, settings=settings
    )

    assert history.is_seen(cfo_purchase_ref.accession_no) is True
    assert store.count_signals() == 1
    assert store.count_signals(passed_only=True) == 1


def test_enumerate_and_filter_skips_already_seen_filings_on_resume(
    settings, history, store, cfo_purchase_ref
):
    xml_bytes = (FIXTURES / "sample_form4_cfo_purchase.xml").read_bytes()
    client = _FakeClient(xml_bytes, refs=[cfo_purchase_ref])

    backtest.enumerate_and_filter(
        client=client,
        history=history,
        store=store,
        settings=settings,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )
    assert client.fetch_xml_calls == 1
    assert store.count_signals() == 1

    # 재개 상황을 흉내냄: 같은 filing이 다시 열거되어도 이미 is_seen이라 재조회하지 않아야 함.
    backtest.enumerate_and_filter(
        client=client,
        history=history,
        store=store,
        settings=settings,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )
    assert client.fetch_xml_calls == 1
    assert store.count_signals() == 1


# --- BacktestStore ---


def test_backtest_store_upsert_signal_is_idempotent_on_conflict(store):
    id1 = store.upsert_signal(
        accession_no="acc-1",
        txn_index=0,
        issuer_cik="1",
        issuer_ticker="ABC",
        issuer_name="ABC Inc",
        owner_cik="2",
        owner_name="Jane Doe",
        officer_title="CEO",
        transaction_date=date(2026, 1, 5),
        filed_at=date(2026, 1, 7),
        shares=1000,
        price_per_share=100,
        value_usd=100_000,
        passed_filters=True,
        reasons_failed=(),
    )
    id2 = store.upsert_signal(
        accession_no="acc-1",
        txn_index=0,
        issuer_cik="1",
        issuer_ticker="ABC",
        issuer_name="ABC Inc",
        owner_cik="2",
        owner_name="Jane Doe",
        officer_title="CEO",
        transaction_date=date(2026, 1, 5),
        filed_at=date(2026, 1, 7),
        shares=1000,
        price_per_share=100,
        value_usd=100_000,
        passed_filters=False,
        reasons_failed=("test",),
    )

    assert id1 == id2
    assert store.count_signals() == 1
    assert store.count_signals(passed_only=True) == 0


def test_backtest_store_iter_signals_needing_simulation_excludes_failed_and_already_simulated(store):
    passed_id = store.upsert_signal(
        accession_no="acc-pass",
        txn_index=0,
        issuer_cik="1",
        issuer_ticker="ABC",
        issuer_name="ABC Inc",
        owner_cik="2",
        owner_name="Jane Doe",
        officer_title="CEO",
        transaction_date=date(2026, 1, 5),
        filed_at=date(2026, 1, 7),
        shares=1000,
        price_per_share=100,
        value_usd=100_000,
        passed_filters=True,
        reasons_failed=(),
    )
    store.upsert_signal(
        accession_no="acc-fail",
        txn_index=0,
        issuer_cik="1",
        issuer_ticker="XYZ",
        issuer_name="XYZ Inc",
        owner_cik="3",
        owner_name="John Doe",
        officer_title="CFO",
        transaction_date=date(2026, 1, 5),
        filed_at=date(2026, 1, 7),
        shares=1000,
        price_per_share=100,
        value_usd=100_000,
        passed_filters=False,
        reasons_failed=("법인",),
    )

    pending = store.iter_signals_needing_simulation(3.0, 4.0, 30)
    assert [p.id for p in pending] == [passed_id]

    store.upsert_simulation(
        signal_id=passed_id,
        target_pct=3.0,
        stop_pct=4.0,
        max_hold_days=30,
        entry_date=date(2026, 1, 8),
        entry_price=101.0,
        exit_date=date(2026, 1, 9),
        exit_price=104.0,
        exit_reason="target",
        return_pct=3.0,
        hold_days=1,
    )

    assert store.iter_signals_needing_simulation(3.0, 4.0, 30) == []
    # 다른 파라미터 조합으로는 여전히 시뮬레이션이 필요함 (신호 재사용, EDGAR 재조회 없음).
    assert len(store.iter_signals_needing_simulation(5.0, 4.0, 30)) == 1


# --- 티커 플레이스홀더("N/A" 등) 처리 ---


@pytest.mark.parametrize(
    "ticker, expected",
    [
        ("", True),
        ("N/A", True),
        ("n/a", True),
        (" NA ", True),
        ("-", True),
        ("AAPL", False),
    ],
)
def test_is_missing_ticker(ticker, expected):
    assert backtest._is_missing_ticker(ticker) is expected


def test_run_price_simulations_treats_na_ticker_as_no_ticker_without_fetching(store):
    """issuerTradingSymbol이 "N/A"로 채워진 filing도 빈 문자열과 동일하게 no_ticker로
    처리되어야 하고, yfinance 조회 자체를 시도하면 안 됩니다 (실제로 이 값이 yfinance에
    그대로 넘어가서 예외로 전체 백테스트가 죽은 적이 있음)."""

    store.upsert_signal(
        accession_no="acc-na",
        txn_index=0,
        issuer_cik="1",
        issuer_ticker="N/A",
        issuer_name="Some Corp",
        owner_cik="2",
        owner_name="Jane Doe",
        officer_title="CEO",
        transaction_date=date(2026, 1, 5),
        filed_at=date(2026, 1, 7),
        shares=1000,
        price_per_share=100,
        value_usd=100_000,
        passed_filters=True,
        reasons_failed=(),
    )

    calls = []

    def fake_fetch(ticker, filed_at, max_hold_days):
        calls.append(ticker)
        raise AssertionError("N/A 티커는 fetch_bars를 호출하면 안 됨")

    backtest.run_price_simulations(
        store=store, target_pct=3.0, stop_pct=4.0, max_hold_days=30, fetch_bars=fake_fetch
    )

    results = store.iter_results(3.0, 4.0, 30)
    assert len(results) == 1
    assert results[0].exit_reason == "no_ticker"
    assert calls == []
