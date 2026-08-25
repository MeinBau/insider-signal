"""yfinance 기반 주가(OHLC) 조회. 백테스트 전용.

``yfinance``/``pandas`` 의존성은 이 모듈에만 존재합니다 — backtest.py의 시뮬레이션 로직은
이 모듈이 반환하는 순수 ``DailyBar`` 값만 다뤄서, 네트워크 없이 단위 테스트할 수 있습니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float


class PriceDataUnavailable(Exception):
    """티커/기간에 대한 가격 데이터를 가져오지 못한 경우 (상장폐지, 오타 티커, 데이터 공백 등)."""


def fetch_entry_and_hold_bars(
    ticker: str, filed_at: date, max_hold_days: int, *, calendar_buffer_days: int = 25
) -> tuple[DailyBar, list[DailyBar]]:
    """``filed_at`` 다음 거래일의 시가를 진입가로 삼아, 그날부터 최대 ``max_hold_days``
    거래일치 일봉을 가져옵니다.

    반환값: (entry_bar, hold_bars) — ``hold_bars[0]``이 ``entry_bar``와 동일합니다.
    주말/공휴일을 감안해 넉넉한 달력일(calendar_buffer_days)만큼 여유를 두고 조회한 뒤,
    실제 거래일수 기준으로 앞에서 ``max_hold_days``개만 사용합니다.
    """

    window_end = filed_at + timedelta(days=int(max_hold_days * 1.6) + calendar_buffer_days)
    df = yf.Ticker(ticker).history(
        start=filed_at.isoformat(), end=window_end.isoformat(), interval="1d"
    )
    bars = [bar for bar in _rows_to_bars(df) if bar.trade_date > filed_at]
    if not bars:
        raise PriceDataUnavailable(f"{ticker}: {filed_at.isoformat()} 이후 거래일 데이터 없음")

    hold_bars = bars[:max_hold_days]
    return hold_bars[0], hold_bars


def _rows_to_bars(df) -> list[DailyBar]:
    bars: list[DailyBar] = []
    for idx, row in df.iterrows():
        trade_date = idx.date() if hasattr(idx, "date") else idx
        bars.append(
            DailyBar(
                trade_date=trade_date,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
            )
        )
    return bars
