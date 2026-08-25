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
