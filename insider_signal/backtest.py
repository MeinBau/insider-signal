"""오프라인 백테스트: 과거 Form 4 filing에서 신호를 재구성하고, 목표수익률/손절 시뮬레이션을 돌립니다.

라이브 poller(``poller.py``)와 신호 판정 로직(``filters.evaluate``)을 그대로 재사용하되,
과거 데이터를 다루는 세 가지 지점이 다릅니다:

1. filing 목록은 ``edgar_client.iter_historical_form4_filings``(분기별 master.idx)로 얻습니다.
2. 반복매수 판정은 "오늘"이 아니라 해당 filing의 실제 EDGAR 제출일(``as_of``) 기준으로 계산합니다.
3. 필터를 통과한 신호는 알림을 보내는 대신 가격 시뮬레이션(목표가/손절가)을 거쳐 리포트로 남습니다.

라이브 이력 DB(``settings.history_db_path``)는 절대 건드리지 않고, 백테스트 전용 DB 두 개
(재개용 ``HistoryStore`` + 결과용 ``BacktestStore``)를 별도로 사용합니다.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, UTC
from pathlib import Path

from . import filters
from . import price_data as price_data_module
from . import sectors
from .config import Settings
from .edgar_client import HistoricalFilingRef, SECEdgarClient
from .history import HistoryStore
from .parser import Form4ParseError, parse_form4_xml
from .price_data import DailyBar, PriceDataUnavailable

logger = logging.getLogger(__name__)


# --- 순수 시뮬레이션 로직 (네트워크/DB 없음, 단위 테스트 용이) ---


@dataclass(frozen=True)
class SimulationResult:
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    exit_reason: str  # "target" | "stop" | "timeout" | "data_ended"
    return_pct: float
    hold_days: int


def simulate_trade(
    entry_price: float,
    hold_bars: Sequence[DailyBar],
    *,
    target_pct: float,
    stop_pct: float,
    max_hold_days: int,
) -> SimulationResult:
    """진입가 기준 +target_pct%/-stop_pct% 중 일봉 고가/저가로 먼저 닿는 쪽을 찾습니다.

    같은 봉에서 목표가/손절가가 둘 다 닿으면 보수적으로 손절이 먼저 발생했다고 간주합니다
    (사용자가 확정한 tie-break 규칙). ``hold_bars[0]``은 진입 당일 봉이어야 합니다.
    """

    if not hold_bars:
        raise ValueError("hold_bars가 비어 있습니다")

    target_price = entry_price * (1 + target_pct / 100)
    stop_price = entry_price * (1 - stop_pct / 100)
    bars = hold_bars[:max_hold_days]
    entry_date = bars[0].trade_date

    for i, bar in enumerate(bars, start=1):
        if bar.low <= stop_price:
            return SimulationResult(entry_date, entry_price, bar.trade_date, stop_price, "stop", -stop_pct, i)
        if bar.high >= target_price:
            return SimulationResult(entry_date, entry_price, bar.trade_date, target_price, "target", target_pct, i)

    last = bars[-1]
    reason = "timeout" if len(bars) >= max_hold_days else "data_ended"
    realized_pct = (last.close - entry_price) / entry_price * 100
    return SimulationResult(entry_date, entry_price, last.trade_date, last.close, reason, realized_pct, len(bars))


# --- 결과 저장소 ---


@dataclass(frozen=True)
class PendingSignal:
    id: int
    issuer_ticker: str
    filed_at: date


@dataclass(frozen=True)
class PassedSignal:
    id: int
    sector: str
    issuer_ticker: str
    filed_at: date


@dataclass(frozen=True)
class ResultRow:
    issuer_ticker: str
    issuer_name: str
    owner_name: str
    officer_title: str
    sector: str
    transaction_date: date
    filed_at: date
    value_usd: float
    entry_date: date | None
    entry_price: float | None
    exit_date: date | None
    exit_price: float | None
    exit_reason: str
    return_pct: float | None
    hold_days: int | None
    target_pct: float
    stop_pct: float


_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_no TEXT NOT NULL,
    txn_index INTEGER NOT NULL,
    issuer_cik TEXT NOT NULL,
    issuer_ticker TEXT NOT NULL,
    issuer_name TEXT NOT NULL,
    owner_cik TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    officer_title TEXT NOT NULL DEFAULT '',
    transaction_date TEXT NOT NULL,
    filed_at TEXT NOT NULL,
    shares REAL NOT NULL,
    price_per_share REAL NOT NULL,
    value_usd REAL NOT NULL,
    passed_filters INTEGER NOT NULL,
    reasons_failed TEXT NOT NULL DEFAULT '',
    sector TEXT NOT NULL DEFAULT '',
    UNIQUE(accession_no, txn_index)
);

CREATE TABLE IF NOT EXISTS simulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    target_pct REAL NOT NULL,
    stop_pct REAL NOT NULL,
    max_hold_days INTEGER NOT NULL,
    entry_date TEXT,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT NOT NULL,
    return_pct REAL,
    hold_days INTEGER,
    computed_at TEXT NOT NULL,
    UNIQUE(signal_id, target_pct, stop_pct, max_hold_days)
);
"""


class BacktestStore:
    """백테스트 결과 전용 SQLite 저장소. 라이브 이력 DB와는 별개의 파일입니다."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_SIGNALS_SCHEMA)
        try:
            # 섹터 실험 이전에 생성된 기존 결과 DB를 위한 마이그레이션. 새 DB는 이미
            # _SIGNALS_SCHEMA에 sector 컬럼이 있으므로 "duplicate column"으로 실패하고 넘어감.
            self._conn.execute("ALTER TABLE signals ADD COLUMN sector TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BacktestStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def upsert_signal(
        self,
        *,
        accession_no: str,
        txn_index: int,
        issuer_cik: str,
        issuer_ticker: str,
        issuer_name: str,
        owner_cik: str,
        owner_name: str,
        officer_title: str,
        transaction_date: date,
        filed_at: date,
        shares: float,
        price_per_share: float,
        value_usd: float,
        passed_filters: bool,
        reasons_failed: tuple[str, ...],
        sector: str = "",
    ) -> int:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO signals
                    (accession_no, txn_index, issuer_cik, issuer_ticker, issuer_name,
                     owner_cik, owner_name, officer_title, transaction_date, filed_at,
                     shares, price_per_share, value_usd, passed_filters, reasons_failed, sector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(accession_no, txn_index) DO UPDATE SET
                    issuer_cik=excluded.issuer_cik, issuer_ticker=excluded.issuer_ticker,
                    issuer_name=excluded.issuer_name, owner_cik=excluded.owner_cik,
                    owner_name=excluded.owner_name, officer_title=excluded.officer_title,
                    transaction_date=excluded.transaction_date, filed_at=excluded.filed_at,
                    shares=excluded.shares, price_per_share=excluded.price_per_share,
                    value_usd=excluded.value_usd, passed_filters=excluded.passed_filters,
                    reasons_failed=excluded.reasons_failed, sector=excluded.sector
                """,
                (
                    accession_no,
                    txn_index,
                    issuer_cik,
                    issuer_ticker,
                    issuer_name,
                    owner_cik,
                    owner_name,
                    officer_title,
                    transaction_date.isoformat(),
                    filed_at.isoformat(),
                    shares,
                    price_per_share,
                    value_usd,
                    1 if passed_filters else 0,
                    "; ".join(reasons_failed),
                    sector,
                ),
            )
            cur = self._conn.execute(
                "SELECT id FROM signals WHERE accession_no = ? AND txn_index = ?",
                (accession_no, txn_index),
            )
            row = cur.fetchone()
            assert row is not None
            return int(row[0])

    def count_signals(self, *, passed_only: bool = False) -> int:
        if passed_only:
            cur = self._conn.execute("SELECT COUNT(*) FROM signals WHERE passed_filters = 1")
        else:
            cur = self._conn.execute("SELECT COUNT(*) FROM signals")
        return int(cur.fetchone()[0])

    def iter_signals_needing_simulation(
        self, target_pct: float, stop_pct: float, max_hold_days: int
    ) -> list[PendingSignal]:
        cur = self._conn.execute(
            """
            SELECT s.id, s.issuer_ticker, s.filed_at
            FROM signals s
            WHERE s.passed_filters = 1
              AND NOT EXISTS (
                  SELECT 1 FROM simulations sim
                  WHERE sim.signal_id = s.id
                    AND sim.target_pct = ? AND sim.stop_pct = ? AND sim.max_hold_days = ?
              )
            ORDER BY s.filed_at
            """,
            (target_pct, stop_pct, max_hold_days),
        )
        return [
            PendingSignal(id=row[0], issuer_ticker=row[1], filed_at=_parse_date(row[2]))
            for row in cur.fetchall()
        ]

    def iter_passed_signals(self) -> list[PassedSignal]:
        """섹터별 target/stop 실험 경로 전용: 필터 통과 신호 전체를 섹터와 함께 반환합니다."""

        cur = self._conn.execute(
            "SELECT id, sector, issuer_ticker, filed_at FROM signals WHERE passed_filters = 1 ORDER BY filed_at"
        )
        return [
            PassedSignal(id=row[0], sector=row[1], issuer_ticker=row[2], filed_at=_parse_date(row[3]))
            for row in cur.fetchall()
        ]

    def has_simulation(self, signal_id: int, target_pct: float, stop_pct: float, max_hold_days: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM simulations WHERE signal_id = ? AND target_pct = ? AND stop_pct = ? AND max_hold_days = ?",
            (signal_id, target_pct, stop_pct, max_hold_days),
        )
        return cur.fetchone() is not None

    def upsert_simulation(
        self,
        *,
        signal_id: int,
        target_pct: float,
        stop_pct: float,
        max_hold_days: int,
        entry_date: date | None,
        entry_price: float | None,
        exit_date: date | None,
        exit_price: float | None,
        exit_reason: str,
        return_pct: float | None,
        hold_days: int | None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO simulations
                    (signal_id, target_pct, stop_pct, max_hold_days, entry_date, entry_price,
                     exit_date, exit_price, exit_reason, return_pct, hold_days, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id, target_pct, stop_pct, max_hold_days) DO UPDATE SET
                    entry_date=excluded.entry_date, entry_price=excluded.entry_price,
                    exit_date=excluded.exit_date, exit_price=excluded.exit_price,
                    exit_reason=excluded.exit_reason, return_pct=excluded.return_pct,
                    hold_days=excluded.hold_days, computed_at=excluded.computed_at
                """,
                (
                    signal_id,
                    target_pct,
                    stop_pct,
                    max_hold_days,
                    entry_date.isoformat() if entry_date else None,
                    entry_price,
                    exit_date.isoformat() if exit_date else None,
                    exit_price,
                    exit_reason,
                    return_pct,
                    hold_days,
                    datetime.now(UTC).isoformat(),
                ),
            )

    _RESULT_COLUMNS = """
        s.issuer_ticker, s.issuer_name, s.owner_name, s.officer_title, s.sector,
        s.transaction_date, s.filed_at, s.value_usd,
        sim.entry_date, sim.entry_price, sim.exit_date, sim.exit_price,
        sim.exit_reason, sim.return_pct, sim.hold_days, sim.target_pct, sim.stop_pct
    """

    def iter_results(self, target_pct: float, stop_pct: float, max_hold_days: int) -> list[ResultRow]:
        cur = self._conn.execute(
            f"""
            SELECT {self._RESULT_COLUMNS}
            FROM signals s
            JOIN simulations sim ON sim.signal_id = s.id
            WHERE s.passed_filters = 1
              AND sim.target_pct = ? AND sim.stop_pct = ? AND sim.max_hold_days = ?
            ORDER BY s.filed_at
            """,
            (target_pct, stop_pct, max_hold_days),
        )
        return [_row_to_result(r) for r in cur.fetchall()]

    def get_result_row(
        self, signal_id: int, target_pct: float, stop_pct: float, max_hold_days: int
    ) -> ResultRow | None:
        """섹터별 target/stop 실험 경로 전용: 신호 하나에 대해, 실제로 그 신호에 적용된
        (섹터로 resolve된) target/stop 조합으로 계산된 결과 행 하나를 가져옵니다."""

        cur = self._conn.execute(
            f"""
            SELECT {self._RESULT_COLUMNS}
            FROM signals s
            JOIN simulations sim ON sim.signal_id = s.id
            WHERE s.id = ? AND sim.target_pct = ? AND sim.stop_pct = ? AND sim.max_hold_days = ?
            """,
            (signal_id, target_pct, stop_pct, max_hold_days),
        )
        row = cur.fetchone()
        return _row_to_result(row) if row is not None else None


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _row_to_result(r: tuple) -> ResultRow:
    return ResultRow(
        issuer_ticker=r[0],
        issuer_name=r[1],
        owner_name=r[2],
        officer_title=r[3],
        sector=r[4],
        transaction_date=_parse_date(r[5]),
        filed_at=_parse_date(r[6]),
        value_usd=r[7],
        entry_date=_parse_date(r[8]) if r[8] else None,
        entry_price=r[9],
        exit_date=_parse_date(r[10]) if r[10] else None,
        exit_price=r[11],
        exit_reason=r[12],
        return_pct=r[13],
        hold_days=r[14],
        target_pct=r[15],
        stop_pct=r[16],
    )


# --- 1단계: 과거 filing 열거 -> 파싱 -> 필터 ---


def enumerate_and_filter(
    *,
    client: SECEdgarClient,
    history: HistoryStore,
    store: BacktestStore,
    settings: Settings,
    start: date,
    end: date,
    include_amendments: bool = False,
    progress_every: int = 200,
    classify_sector: bool = False,
) -> None:
    """분기 인덱스에서 열거한 filing을 filed_at 오름차순으로 순차 처리합니다.

    반드시 순차 처리해야 합니다 — ``count_recent_purchases``에는 미래 상한이 없어서, 순서를
    뒤섞으면 과거 거래를 평가할 때 미래의 매수 이력을 미리 알아버리는 look-ahead가 생깁니다
    (SEC 레이트리밋 자체가 전역 락으로 직렬화되어 있어 병렬화해도 처리량 이득도 없습니다).

    ``classify_sector``: True면 필터 통과 신호에 한해 이슈어 SIC 코드를 조회해 섹터를 저장합니다
    (--sector-experiment 전용, sectors.py 참고). 이미 ``is_seen``인 filing은 재분류되지 않으므로,
    분류 없이 먼저 돌린 기간을 나중에 이 옵션으로 재실행해도 소급 적용되지 않는 게 알려진 한계입니다.
    """

    refs = sorted(
        client.iter_historical_form4_filings(start, end, include_amendments=include_amendments),
        key=lambda r: (r.filed_at, r.accession_no),
    )
    logger.info("과거 Form 4 %d건 발견 (%s ~ %s)", len(refs), start.isoformat(), end.isoformat())

    issuer_sector_cache: dict[str, str | None] = {}
    for i, ref in enumerate(refs, start=1):
        if not history.is_seen(ref.accession_no):
            _process_one_filing(
                ref,
                client=client,
                history=history,
                store=store,
                settings=settings,
                classify_sector=classify_sector,
                issuer_sector_cache=issuer_sector_cache,
            )
        if i % progress_every == 0:
            logger.info(
                "진행 %d/%d, 누적 필터 통과 신호 %d건", i, len(refs), store.count_signals(passed_only=True)
            )


def _process_one_filing(
    ref: HistoricalFilingRef,
    *,
    client: SECEdgarClient,
    history: HistoryStore,
    store: BacktestStore,
    settings: Settings,
    classify_sector: bool = False,
    issuer_sector_cache: dict[str, str | None] | None = None,
) -> None:
    try:
        xml_url = client.get_primary_xml_url(ref.cik, ref.accession_no)
        if xml_url is None:
            history.record_purchases_and_mark_seen(accession_no=ref.accession_no, purchases=[])
            return
        xml_bytes = client.fetch_xml(xml_url)
        filing = parse_form4_xml(xml_bytes, accession_no=ref.accession_no, source_url=xml_url)
    except Form4ParseError as exc:
        logger.warning("Form4 파싱 실패, 건너뜀: %s (%s)", ref.accession_no, exc)
        history.record_purchases_and_mark_seen(accession_no=ref.accession_no, purchases=[])
        return
    except Exception:
        # poller.py와 의도적으로 다른 정책: 라이브 경로는 재시도 폭주를 막기 위해 예외도 seen
        # 처리하지만, 백테스트는 시간 압박이 없으므로 seen 처리하지 않고 다음 재개 때 재시도합니다.
        logger.exception("filing 처리 중 예외, 재개 시 재시도됨: %s", ref.accession_no)
        return

    if not filing.owner.cik or not filing.issuer.cik:
        history.record_purchases_and_mark_seen(accession_no=ref.accession_no, purchases=[])
        return

    purchases: list[dict] = []
    for txn_index, txn in enumerate(filing.transactions):
        if not txn.is_open_market_purchase:
            continue

        # ref.filed_at(EDGAR 실제 제출일)을 as_of로 사용합니다 — filing.filed_at(XML의
        # periodOfReport)은 거래일과 거의 같은 값이라 그대로 쓰면 look-ahead bias가 재발합니다.
        result = filters.evaluate(
            txn, filing.owner, filing.issuer.cik, settings, history, as_of=ref.filed_at
        )

        sector = ""
        if classify_sector and result.passed:
            cache = issuer_sector_cache if issuer_sector_cache is not None else {}
            if filing.issuer.cik not in cache:
                cache[filing.issuer.cik] = client.get_issuer_sic(filing.issuer.cik)
            sector = sectors.classify_sic(cache[filing.issuer.cik]) or ""

        store.upsert_signal(
            accession_no=ref.accession_no,
            txn_index=txn_index,
            issuer_cik=filing.issuer.cik,
            issuer_ticker=filing.issuer.ticker,
            issuer_name=filing.issuer.name,
            owner_cik=filing.owner.cik,
            owner_name=filing.owner.name,
            officer_title=filing.owner.officer_title,
            transaction_date=txn.transaction_date,
            filed_at=ref.filed_at,
            shares=txn.shares,
            price_per_share=txn.price_per_share,
            value_usd=txn.value_usd,
            passed_filters=result.passed,
            reasons_failed=result.reasons_failed,
            sector=sector,
        )
        purchases.append(
            {
                "owner_cik": filing.owner.cik,
                "issuer_cik": filing.issuer.cik,
                "transaction_date": txn.transaction_date,
                "shares": txn.shares,
                "price_per_share": txn.price_per_share,
                "accession_no": ref.accession_no,
            }
        )

    history.record_purchases_and_mark_seen(accession_no=ref.accession_no, purchases=purchases)


# --- 2단계: 필터 통과 신호에 대한 가격 시뮬레이션 ---

_MISSING_TICKER_PLACEHOLDERS = {"N/A", "NA", "NONE", "-", "N.A."}


def _is_missing_ticker(ticker: str) -> bool:
    """빈 문자열뿐 아니라, 일부 Form 4가 issuerTradingSymbol에 실제로 채워 넣는
    "N/A" 같은 플레이스홀더도 티커 없음으로 취급합니다 (yfinance에 그대로 넘기면 예외가 남).
    """

    return not ticker or ticker.strip().upper() in _MISSING_TICKER_PLACEHOLDERS


def _simulate_and_store(
    store: BacktestStore,
    *,
    signal_id: int,
    issuer_ticker: str,
    filed_at: date,
    target_pct: float,
    stop_pct: float,
    max_hold_days: int,
    fetch_bars,
) -> None:
    if _is_missing_ticker(issuer_ticker):
        store.upsert_simulation(
            signal_id=signal_id,
            target_pct=target_pct,
            stop_pct=stop_pct,
            max_hold_days=max_hold_days,
            entry_date=None,
            entry_price=None,
            exit_date=None,
            exit_price=None,
            exit_reason="no_ticker",
            return_pct=None,
            hold_days=None,
        )
        return

    try:
        entry_bar, hold_bars = fetch_bars(issuer_ticker, filed_at, max_hold_days)
    except PriceDataUnavailable as exc:
        logger.info("가격 데이터 없음, 건너뜀: %s (%s)", issuer_ticker, exc)
        store.upsert_simulation(
            signal_id=signal_id,
            target_pct=target_pct,
            stop_pct=stop_pct,
            max_hold_days=max_hold_days,
            entry_date=None,
            entry_price=None,
            exit_date=None,
            exit_price=None,
            exit_reason="no_price_data",
            return_pct=None,
            hold_days=None,
        )
        return

    result = simulate_trade(
        entry_bar.open, hold_bars, target_pct=target_pct, stop_pct=stop_pct, max_hold_days=max_hold_days
    )
    store.upsert_simulation(
        signal_id=signal_id,
        target_pct=target_pct,
        stop_pct=stop_pct,
        max_hold_days=max_hold_days,
        entry_date=result.entry_date,
        entry_price=result.entry_price,
        exit_date=result.exit_date,
        exit_price=result.exit_price,
        exit_reason=result.exit_reason,
        return_pct=result.return_pct,
        hold_days=result.hold_days,
    )


def run_price_simulations(
    *,
    store: BacktestStore,
    target_pct: float,
    stop_pct: float,
    max_hold_days: int,
    sector_overrides: dict[str, tuple[float, float]] | None = None,
    fetch_bars=price_data_module.fetch_entry_and_hold_bars,
    progress_every: int = 50,
) -> None:
    """``sector_overrides``가 없으면(기본) 모든 신호에 동일한 target_pct/stop_pct를 적용합니다.

    ``sector_overrides``가 있으면(``--sector-experiment``) 각 신호의 저장된 섹터
    (``_process_one_filing``에서 SIC 코드로 분류됨)에 따라 다른 target/stop을 적용합니다
    (``sectors.resolve_target_stop``). 매핑에 없는 섹터는 기본 target_pct/stop_pct를 씁니다.
    """

    if sector_overrides is None:
        pending = [
            (signal.id, signal.issuer_ticker, signal.filed_at, target_pct, stop_pct)
            for signal in store.iter_signals_needing_simulation(target_pct, stop_pct, max_hold_days)
        ]
    else:
        resolved = (
            (signal, *sectors.resolve_target_stop(signal.sector, target_pct, stop_pct))
            for signal in store.iter_passed_signals()
        )
        pending = [
            (signal.id, signal.issuer_ticker, signal.filed_at, t, s)
            for signal, t, s in resolved
            if not store.has_simulation(signal.id, t, s, max_hold_days)
        ]

    logger.info("가격 시뮬레이션 대상 %d건", len(pending))
    for i, (signal_id, issuer_ticker, filed_at, t, s) in enumerate(pending, start=1):
        if i % progress_every == 0:
            logger.info("가격 시뮬레이션 진행 %d/%d", i, len(pending))
        _simulate_and_store(
            store,
            signal_id=signal_id,
            issuer_ticker=issuer_ticker,
            filed_at=filed_at,
            target_pct=t,
            stop_pct=s,
            max_hold_days=max_hold_days,
            fetch_bars=fetch_bars,
        )


# --- 3단계: 리포트 ---


@dataclass(frozen=True)
class SectorStats:
    """섹터별(bio/gold/기본) 실험 결과 하나. ``build_report``가 sector_overrides와 함께
    호출됐을 때만 채워집니다."""

    label: str
    target_pct: float
    stop_pct: float
    count: int
    win_rate: float | None
    avg_return_pct: float | None
    median_return_pct: float | None
    expectancy_pct: float | None


@dataclass(frozen=True)
class BacktestReport:
    start: date
    end: date
    target_pct: float
    stop_pct: float
    max_hold_days: int
    total_signals: int
    total_passed: int
    total_no_ticker: int
    total_no_price_data: int
    total_simulated: int
    win_rate: float | None
    avg_return_pct: float | None
    median_return_pct: float | None
    expectancy_pct: float | None
    exit_reason_counts: dict[str, int]
    rows: list[ResultRow]
    by_sector: dict[str, SectorStats] | None = None


def _compute_stats(
    returns: list[float],
) -> tuple[float | None, float | None, float | None, float | None]:
    """returns 목록에서 (승률, 평균, 중앙값, 기대값)을 계산합니다. 비어있으면 전부 None."""

    if not returns:
        return None, None, None, None
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate = len(wins) / len(returns)
    avg_return = statistics.mean(returns)
    median_return = statistics.median(returns)
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    return win_rate, avg_return, median_return, expectancy


def _sector_breakdown(rows: list[ResultRow]) -> dict[str, SectorStats]:
    simulated = [r for r in rows if r.exit_reason not in ("no_ticker", "no_price_data")]
    groups: dict[str, list[ResultRow]] = {}
    for r in simulated:
        groups.setdefault(r.sector or "default", []).append(r)

    breakdown: dict[str, SectorStats] = {}
    for label, group_rows in groups.items():
        returns = [r.return_pct for r in group_rows if r.return_pct is not None]
        win_rate, avg_return, median_return, expectancy = _compute_stats(returns)
        breakdown[label] = SectorStats(
            label=label,
            target_pct=group_rows[0].target_pct,
            stop_pct=group_rows[0].stop_pct,
            count=len(group_rows),
            win_rate=win_rate,
            avg_return_pct=avg_return,
            median_return_pct=median_return,
            expectancy_pct=expectancy,
        )
    return breakdown


def build_report(
    store: BacktestStore,
    *,
    start: date,
    end: date,
    target_pct: float,
    stop_pct: float,
    max_hold_days: int,
    sector_overrides: dict[str, tuple[float, float]] | None = None,
) -> BacktestReport:
    total_signals = store.count_signals()
    total_passed = store.count_signals(passed_only=True)

    if sector_overrides is None:
        rows = store.iter_results(target_pct, stop_pct, max_hold_days)
    else:
        rows = []
        for signal in store.iter_passed_signals():
            t, s = sectors.resolve_target_stop(signal.sector, target_pct, stop_pct)
            row = store.get_result_row(signal.id, t, s, max_hold_days)
            if row is not None:
                rows.append(row)

    no_ticker = sum(1 for r in rows if r.exit_reason == "no_ticker")
    no_price_data = sum(1 for r in rows if r.exit_reason == "no_price_data")
    simulated_rows = [r for r in rows if r.exit_reason not in ("no_ticker", "no_price_data")]
    returns = [r.return_pct for r in simulated_rows if r.return_pct is not None]
    win_rate, avg_return, median_return, expectancy = _compute_stats(returns)

    exit_reason_counts: dict[str, int] = {}
    for r in rows:
        exit_reason_counts[r.exit_reason] = exit_reason_counts.get(r.exit_reason, 0) + 1

    return BacktestReport(
        start=start,
        end=end,
        target_pct=target_pct,
        stop_pct=stop_pct,
        max_hold_days=max_hold_days,
        total_signals=total_signals,
        total_passed=total_passed,
        total_no_ticker=no_ticker,
        total_no_price_data=no_price_data,
        total_simulated=len(simulated_rows),
        win_rate=win_rate,
        avg_return_pct=avg_return,
        median_return_pct=median_return,
        expectancy_pct=expectancy,
        exit_reason_counts=exit_reason_counts,
        rows=rows,
        by_sector=_sector_breakdown(rows) if sector_overrides is not None else None,
    )


def write_csv_report(report: BacktestReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "ticker", "issuer_name", "owner_name", "officer_title", "sector",
                "transaction_date", "filed_at", "value_usd",
                "entry_date", "entry_price", "exit_date", "exit_price",
                "exit_reason", "return_pct", "hold_days", "target_pct", "stop_pct",
            ]
        )
        for r in report.rows:
            writer.writerow(
                [
                    r.issuer_ticker, r.issuer_name, r.owner_name, r.officer_title, r.sector,
                    r.transaction_date.isoformat(), r.filed_at.isoformat(), f"{r.value_usd:.2f}",
                    r.entry_date.isoformat() if r.entry_date else "",
                    r.entry_price if r.entry_price is not None else "",
                    r.exit_date.isoformat() if r.exit_date else "",
                    r.exit_price if r.exit_price is not None else "",
                    r.exit_reason,
                    f"{r.return_pct:.2f}" if r.return_pct is not None else "",
                    r.hold_days if r.hold_days is not None else "",
                    f"{r.target_pct:g}", f"{r.stop_pct:g}",
                ]
            )


def print_summary(report: BacktestReport) -> None:
    print(f"\n=== 백테스트 결과 ({report.start.isoformat()} ~ {report.end.isoformat()}) ===")
    print(f"기본 목표수익률 +{report.target_pct:g}% / 손절 -{report.stop_pct:g}% / 최대보유 {report.max_hold_days}거래일")
    print(f"평가한 공개시장 매수 건수: {report.total_signals}")
    print(f"필터 통과 신호: {report.total_passed}")
    print(f"  - 티커 없어 제외: {report.total_no_ticker}")
    print(f"  - 가격 데이터 없어 제외: {report.total_no_price_data}")
    print(f"  - 시뮬레이션 완료: {report.total_simulated}")
    if report.win_rate is not None:
        print(f"승률: {report.win_rate * 100:.1f}%")
        print(f"평균 수익률: {report.avg_return_pct:.2f}%  중앙값: {report.median_return_pct:.2f}%")
        print(f"기대값(expectancy): {report.expectancy_pct:.2f}%")
    else:
        print("시뮬레이션된 신호가 없어 승률/수익률을 계산할 수 없습니다.")
    if report.by_sector:
        print("섹터별 breakdown (--sector-experiment, 미검증 가설치):")
        for label in sorted(report.by_sector):
            stats = report.by_sector[label]
            if stats.win_rate is None:
                print(f"  - {label} (+{stats.target_pct:g}%/-{stats.stop_pct:g}%): 표본 없음")
                continue
            print(
                f"  - {label} (+{stats.target_pct:g}%/-{stats.stop_pct:g}%, n={stats.count}): "
                f"승률 {stats.win_rate * 100:.1f}%, 평균 {stats.avg_return_pct:.2f}%, "
                f"기대값 {stats.expectancy_pct:.2f}%"
            )
    if report.exit_reason_counts:
        print("청산 사유 분포:")
        for reason, count in sorted(report.exit_reason_counts.items()):
            print(f"  - {reason}: {count}")
    print(
        "참고: 반복매수 판정은 백테스트 구간 시작 시점 이력이 비어있어 초반 "
        f"{report.start.isoformat()} 이후 recurring_lookback_days 동안 과소 판정될 수 있습니다 (알려진 v1 한계)."
    )


# --- 진입점 ---


def run_backtest(
    *,
    client: SECEdgarClient,
    settings: Settings,
    start: date,
    end: date,
    target_pct: float,
    stop_pct: float,
    max_hold_days: int,
    history_db_path: Path,
    results_db_path: Path,
    output_csv_path: Path,
    include_amendments: bool = False,
    progress_every: int = 200,
    sector_overrides: dict[str, tuple[float, float]] | None = None,
) -> BacktestReport:
    """cli.py의 ``backtest`` 서브커맨드가 호출하는 단일 진입점.

    라이브 ``settings.history_db_path``는 절대 사용하지 않고, ``history_db_path``/
    ``results_db_path``에 지정된 백테스트 전용 DB만 사용합니다.

    ``sector_overrides``는 ``--sector-experiment`` 전용 (``sectors.SECTOR_TARGET_STOP``):
    바이오/금으로 분류된 신호는 target_pct/stop_pct 대신 이 값을 씁니다.
    """

    with HistoryStore(history_db_path) as history, BacktestStore(results_db_path) as store:
        enumerate_and_filter(
            client=client,
            history=history,
            store=store,
            settings=settings,
            start=start,
            end=end,
            include_amendments=include_amendments,
            progress_every=progress_every,
            classify_sector=sector_overrides is not None,
        )
        run_price_simulations(
            store=store,
            target_pct=target_pct,
            stop_pct=stop_pct,
            max_hold_days=max_hold_days,
            sector_overrides=sector_overrides,
            progress_every=progress_every,
        )
        report = build_report(
            store,
            start=start,
            end=end,
            target_pct=target_pct,
            stop_pct=stop_pct,
            max_hold_days=max_hold_days,
            sector_overrides=sector_overrides,
        )

    write_csv_report(report, output_csv_path)
    return report
