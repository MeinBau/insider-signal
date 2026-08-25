"""yfinance 기반 주가(OHLC) 조회. 백테스트 전용.

``yfinance``/``pandas`` 의존성은 이 모듈에만 존재합니다 — backtest.py의 시뮬레이션 로직은
이 모듈이 반환하는 순수 ``DailyBar`` 값만 다뤄서, 네트워크 없이 단위 테스트할 수 있습니다.
"""

from __future__ import annotations

import logging
import math
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
    try:
        df = yf.Ticker(ticker).history(
            start=filed_at.isoformat(), end=window_end.isoformat(), interval="1d"
        )
    except Exception as exc:
        # yfinance는 잘못된 티커/상장폐지/Yahoo 쪽 일시적 오류(429, 502 등) 시 다양한 예외
        # (YFException, JSON 파싱 오류, 네트워크 예외...)를 던집니다. 신호 하나의 가격 조회
        # 실패로 몇 시간 걸리는 백테스트 전체가 죽으면 안 되므로, 전부 PriceDataUnavailable로
        # 통일해서 상위(run_price_simulations)가 exit_reason="no_price_data"로 기록하고
        # 넘어갈 수 있게 합니다.
        raise PriceDataUnavailable(f"{ticker}: 가격 조회 실패 ({exc})") from exc

    bars = [bar for bar in _rows_to_bars(df) if bar.trade_date > filed_at]
    if not bars:
        raise PriceDataUnavailable(f"{ticker}: {filed_at.isoformat()} 이후 거래일 데이터 없음")

    hold_bars = bars[:max_hold_days]
    return hold_bars[0], hold_bars


def _rows_to_bars(df) -> list[DailyBar]:
    """yfinance가 가끔 특정 거래일의 OHLC 중 일부를 NaN으로 돌려줄 때가 있습니다 (일시적인
    Yahoo 쪽 데이터 공백). 그런 날을 그대로 DailyBar에 담으면 NaN이 simulate_trade의 비교
    연산(항상 False)을 조용히 통과해버려 그 신호의 return_pct가 NaN이 되고, 이게 리포트
    집계(평균/기대값)를 통째로 오염시킬 수 있습니다. 그래서 NaN이 하나라도 섞인 날은 그냥
    건너뜁니다 (주말/공휴일처럼 "그 날은 없는 셈" 취급 — 이미 그렇게 다루는 방식과 동일).
    """

    bars: list[DailyBar] = []
    for idx, row in df.iterrows():
        trade_date = idx.date() if hasattr(idx, "date") else idx
        values = (
            float(row["Open"]),
            float(row["High"]),
            float(row["Low"]),
            float(row["Close"]),
        )
        if any(math.isnan(v) for v in values):
            logger.debug("NaN OHLC 값이 있는 거래일 건너뜀: %s (%s)", trade_date, values)
            continue
        bars.append(
            DailyBar(
                trade_date=trade_date,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
            )
        )
    return bars
