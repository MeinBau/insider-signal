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
class ResultRow:
    issuer_ticker: str
    issuer_name: str
    owner_name: str
    officer_title: str
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
    ) -> int:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO signals
                    (accession_no, txn_index, issuer_cik, issuer_ticker, issuer_name,
                     owner_cik, owner_name, officer_title, transaction_date, filed_at,
                     shares, price_per_share, value_usd, passed_filters, reasons_failed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(accession_no, txn_index) DO UPDATE SET
                    issuer_cik=excluded.issuer_cik, issuer_ticker=excluded.issuer_ticker,
                    issuer_name=excluded.issuer_name, owner_cik=excluded.owner_cik,
                    owner_name=excluded.owner_name, officer_title=excluded.officer_title,
                    transaction_date=excluded.transaction_date, filed_at=excluded.filed_at,
                    shares=excluded.shares, price_per_share=excluded.price_per_share,
                    value_usd=excluded.value_usd, passed_filters=excluded.passed_filters,
                    reasons_failed=excluded.reasons_failed
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

    def iter_results(self, target_pct: float, stop_pct: float, max_hold_days: int) -> list[ResultRow]:
        cur = self._conn.execute(
            """
            SELECT s.issuer_ticker, s.issuer_name, s.owner_name, s.officer_title,
                   s.transaction_date, s.filed_at, s.value_usd,
                   sim.entry_date, sim.entry_price, sim.exit_date, sim.exit_price,
                   sim.exit_reason, sim.return_pct, sim.hold_days
            FROM signals s
            JOIN simulations sim ON sim.signal_id = s.id
            WHERE s.passed_filters = 1
              AND sim.target_pct = ? AND sim.stop_pct = ? AND sim.max_hold_days = ?
            ORDER BY s.filed_at
            """,
            (target_pct, stop_pct, max_hold_days),
        )
        rows = []
        for r in cur.fetchall():
            rows.append(
                ResultRow(
                    issuer_ticker=r[0],
                    issuer_name=r[1],
                    owner_name=r[2],
                    officer_title=r[3],
                    transaction_date=_parse_date(r[4]),
                    filed_at=_parse_date(r[5]),
                    value_usd=r[6],
                    entry_date=_parse_date(r[7]) if r[7] else None,
                    entry_price=r[8],
                    exit_date=_parse_date(r[9]) if r[9] else None,
                    exit_price=r[10],
                    exit_reason=r[11],
                    return_pct=r[12],
                    hold_days=r[13],
                )
            )
        return rows


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


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
) -> None:
    """분기 인덱스에서 열거한 filing을 filed_at 오름차순으로 순차 처리합니다.

    반드시 순차 처리해야 합니다 — ``count_recent_purchases``에는 미래 상한이 없어서, 순서를
    뒤섞으면 과거 거래를 평가할 때 미래의 매수 이력을 미리 알아버리는 look-ahead가 생깁니다
    (SEC 레이트리밋 자체가 전역 락으로 직렬화되어 있어 병렬화해도 처리량 이득도 없습니다).
    """

    refs = sorted(
        client.iter_historical_form4_filings(start, end, include_amendments=include_amendments),
        key=lambda r: (r.filed_at, r.accession_no),
    )
    logger.info("과거 Form 4 %d건 발견 (%s ~ %s)", len(refs), start.isoformat(), end.isoformat())

    for i, ref in enumerate(refs, start=1):
        if not history.is_seen(ref.accession_no):
            _process_one_filing(ref, client=client, history=history, store=store, settings=settings)
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


def run_price_simulations(
    *,
    store: BacktestStore,
    target_pct: float,
    stop_pct: float,
    max_hold_days: int,
    fetch_bars=price_data_module.fetch_entry_and_hold_bars,
) -> None:
    pending = store.iter_signals_needing_simulation(target_pct, stop_pct, max_hold_days)
    logger.info("가격 시뮬레이션 대상 %d건", len(pending))

    for signal in pending:
        if not signal.issuer_ticker:
            store.upsert_simulation(
                signal_id=signal.id,
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
            continue

        try:
            entry_bar, hold_bars = fetch_bars(signal.issuer_ticker, signal.filed_at, max_hold_days)
        except PriceDataUnavailable as exc:
            logger.info("가격 데이터 없음, 건너뜀: %s (%s)", signal.issuer_ticker, exc)
            store.upsert_simulation(
                signal_id=signal.id,
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
            continue

        result = simulate_trade(
            entry_bar.open, hold_bars, target_pct=target_pct, stop_pct=stop_pct, max_hold_days=max_hold_days
        )
        store.upsert_simulation(
            signal_id=signal.id,
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


# --- 3단계: 리포트 ---


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


def build_report(
    store: BacktestStore, *, start: date, end: date, target_pct: float, stop_pct: float, max_hold_days: int
) -> BacktestReport:
    total_signals = store.count_signals()
    total_passed = store.count_signals(passed_only=True)
    rows = store.iter_results(target_pct, stop_pct, max_hold_days)

    no_ticker = sum(1 for r in rows if r.exit_reason == "no_ticker")
    no_price_data = sum(1 for r in rows if r.exit_reason == "no_price_data")
    simulated_rows = [r for r in rows if r.exit_reason not in ("no_ticker", "no_price_data")]
    returns = [r.return_pct for r in simulated_rows if r.return_pct is not None]

    win_rate = None
    avg_return = None
    median_return = None
    expectancy = None
    if returns:
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        win_rate = len(wins) / len(returns)
        avg_return = statistics.mean(returns)
        median_return = statistics.median(returns)
        avg_win = statistics.mean(wins) if wins else 0.0
        avg_loss = statistics.mean(losses) if losses else 0.0
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

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
    )


def write_csv_report(report: BacktestReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "ticker", "issuer_name", "owner_name", "officer_title",
                "transaction_date", "filed_at", "value_usd",
                "entry_date", "entry_price", "exit_date", "exit_price",
                "exit_reason", "return_pct", "hold_days",
            ]
        )
        for r in report.rows:
            writer.writerow(
                [
                    r.issuer_ticker, r.issuer_name, r.owner_name, r.officer_title,
                    r.transaction_date.isoformat(), r.filed_at.isoformat(), f"{r.value_usd:.2f}",
                    r.entry_date.isoformat() if r.entry_date else "",
                    r.entry_price if r.entry_price is not None else "",
                    r.exit_date.isoformat() if r.exit_date else "",
                    r.exit_price if r.exit_price is not None else "",
                    r.exit_reason,
                    f"{r.return_pct:.2f}" if r.return_pct is not None else "",
                    r.hold_days if r.hold_days is not None else "",
                ]
            )


def print_summary(report: BacktestReport) -> None:
    print(f"\n=== 백테스트 결과 ({report.start.isoformat()} ~ {report.end.isoformat()}) ===")
    print(f"목표수익률 +{report.target_pct:g}% / 손절 -{report.stop_pct:g}% / 최대보유 {report.max_hold_days}거래일")
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
) -> BacktestReport:
    """cli.py의 ``backtest`` 서브커맨드가 호출하는 단일 진입점.

    라이브 ``settings.history_db_path``는 절대 사용하지 않고, ``history_db_path``/
    ``results_db_path``에 지정된 백테스트 전용 DB만 사용합니다.
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
        )
        run_price_simulations(
            store=store, target_pct=target_pct, stop_pct=stop_pct, max_hold_days=max_hold_days
        )
        report = build_report(
            store, start=start, end=end, target_pct=target_pct, stop_pct=stop_pct, max_hold_days=max_hold_days
        )

    write_csv_report(report, output_csv_path)
    return report
