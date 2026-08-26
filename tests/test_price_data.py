import math
from datetime import date

import pytest

from insider_signal import price_data


class _RaisingTicker:
    """yfinance가 잘못된 티커/Yahoo 쪽 오류(502, 404 등)에서 실제로 던지는 다양한 예외를
    흉내냅니다 — 어떤 예외든 PriceDataUnavailable로 통일되어야 합니다."""

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    def history(self, **kwargs):
        raise RuntimeError("Failed to parse json response from Yahoo Finance")


class _EmptyDF:
    def iterrows(self):
        return iter([])


class _EmptyTicker:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    def history(self, **kwargs):
        return _EmptyDF()


def test_fetch_entry_and_hold_bars_wraps_yfinance_exceptions(monkeypatch):
    """yfinance가 예외를 던지면(잘못된 티커, Yahoo 쪽 502/404 등) 그대로 전파되지 않고
    PriceDataUnavailable로 변환되어야 합니다 — 신호 하나 때문에 전체 백테스트가 죽으면 안 됨."""

    monkeypatch.setattr(price_data.yf, "Ticker", _RaisingTicker)

    with pytest.raises(price_data.PriceDataUnavailable):
        price_data.fetch_entry_and_hold_bars("N/A", date(2026, 1, 1), 30)


def test_fetch_entry_and_hold_bars_raises_when_no_bars_after_filed_at(monkeypatch):
    monkeypatch.setattr(price_data.yf, "Ticker", _EmptyTicker)

    with pytest.raises(price_data.PriceDataUnavailable):
        price_data.fetch_entry_and_hold_bars("ABC", date(2026, 1, 1), 30)


class _FakeIndex:
    def __init__(self, d: date) -> None:
        self._d = d

    def date(self) -> date:
        return self._d


class _FakeDF:
    def __init__(self, rows) -> None:
        self._rows = rows

    def iterrows(self):
        return iter(self._rows)


def test_rows_to_bars_skips_days_with_nan_ohlc():
    """yfinance가 특정 거래일의 OHLC 일부를 NaN으로 돌려주는 경우가 있는데, 그대로 담으면
    NaN이 simulate_trade의 비교(항상 False)를 조용히 통과해 return_pct가 NaN이 되고 리포트
    집계 전체가 오염됩니다 — 그런 날은 아예 건너뛰어야 합니다."""

    rows = [
        (_FakeIndex(date(2026, 1, 5)), {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5}),
        (_FakeIndex(date(2026, 1, 6)), {"Open": 100.5, "High": math.nan, "Low": 99.5, "Close": 100.0}),
        (_FakeIndex(date(2026, 1, 7)), {"Open": 100.0, "High": 102.0, "Low": 99.0, "Close": 101.0}),
    ]

    bars = price_data._rows_to_bars(_FakeDF(rows))

    assert [b.trade_date for b in bars] == [date(2026, 1, 5), date(2026, 1, 7)]
