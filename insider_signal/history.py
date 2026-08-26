"""SQLite 기반 이력 저장소: 필링 dedup + 반복 매수 탐지."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_filings (
    accession_no TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_cik TEXT NOT NULL,
    issuer_cik TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    shares REAL NOT NULL,
    price_per_share REAL NOT NULL,
    value_usd REAL NOT NULL,
    accession_no TEXT NOT NULL,
    is_synthetic_seed INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_purchases_owner_issuer
    ON purchases (owner_cik, issuer_cik, transaction_date);

CREATE TABLE IF NOT EXISTS seeded_owners (
    owner_cik TEXT PRIMARY KEY,
    seeded_at TEXT NOT NULL
);
"""


class HistoryStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- dedup ---

    def is_seen(self, accession_no: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seen_filings WHERE accession_no = ?", (accession_no,)
        )
        return cur.fetchone() is not None

    def mark_seen(self, accession_no: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_filings (accession_no, processed_at) VALUES (?, ?)",
                (accession_no, datetime.now(UTC).isoformat()),
            )

    # --- 반복 매수 탐지 ---

    def record_purchase(
        self,
        *,
        owner_cik: str,
        issuer_cik: str,
        transaction_date: date,
        shares: float,
        price_per_share: float,
        accession_no: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO purchases
                    (owner_cik, issuer_cik, transaction_date, shares, price_per_share,
                     value_usd, accession_no, is_synthetic_seed, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    owner_cik,
                    issuer_cik,
                    transaction_date.isoformat(),
                    shares,
                    price_per_share,
                    shares * price_per_share,
                    accession_no,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def record_purchases_and_mark_seen(
        self, *, accession_no: str, purchases: list[dict]
    ) -> None:
        """백테스트 전용: 한 filing에서 나온 매수 이력 전체 + seen 마킹을 단일 트랜잭션으로
        원자적으로 커밋합니다.

        백테스트는 중단 후 재개가 가능해야 하는데, filing 처리 도중(매수 여러 건 기록 중)
        크래시가 나면 ``is_seen``이 아직 False라 재개 시 해당 filing 전체가 재처리됩니다.
        purchases를 하나씩 개별 커밋했다면 이전 시도에서 이미 기록된 건이 중복 기록될 수 있으므로,
        전체를 한 트랜잭션으로 묶어 "seen 처리됨 = purchases도 전부 기록됨"을 보장합니다.
        라이브 poller가 쓰는 ``record_purchase``/``mark_seen`` 개별 호출 경로는 그대로 둡니다.

        ``purchases``의 각 dict는 owner_cik, issuer_cik, transaction_date(date), shares,
        price_per_share, accession_no 키를 가집니다.
        """

        with self._conn:
            for p in purchases:
                self._conn.execute(
                    """
                    INSERT INTO purchases
                        (owner_cik, issuer_cik, transaction_date, shares, price_per_share,
                         value_usd, accession_no, is_synthetic_seed, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        p["owner_cik"],
                        p["issuer_cik"],
                        p["transaction_date"].isoformat(),
                        p["shares"],
                        p["price_per_share"],
                        p["shares"] * p["price_per_share"],
                        p["accession_no"],
                        datetime.now(UTC).isoformat(),
                    ),
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_filings (accession_no, processed_at) VALUES (?, ?)",
                (accession_no, datetime.now(UTC).isoformat()),
            )

    def count_recent_purchases(
        self,
        *,
        owner_cik: str,
        issuer_cik: str,
        lookback_days: int,
        as_of: date | None = None,
    ) -> int:
        """``as_of`` 이전 ``lookback_days`` 일 이내의 매수 건수. 백테스트가 과거 시점 기준으로
        반복매수를 판정할 수 있도록 하는 용도이며, 생략 시(``None``) 기존과 동일하게
        ``date.today()`` 기준으로 동작합니다 (라이브 poller 경로는 그대로 유지)."""

        reference = as_of if as_of is not None else date.today()
        cutoff = (reference - timedelta(days=lookback_days)).isoformat()
        cur = self._conn.execute(
            """
            SELECT COUNT(*) FROM purchases
            WHERE owner_cik = ? AND issuer_cik = ? AND transaction_date >= ?
            """,
            (owner_cik, issuer_cik, cutoff),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    # --- 초기 이력 보강 (EDGAR submissions API 기반) ---

    def is_owner_seeded(self, owner_cik: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seeded_owners WHERE owner_cik = ?", (owner_cik,)
        )
        return cur.fetchone() is not None

    def seed_owner_history(
        self, *, owner_cik: str, issuer_cik: str, synthetic_count: int
    ) -> None:
        """신고인을 처음 관측했을 때, EDGAR에서 조회한 과거 Form 4 제출 횟수만큼
        오늘 날짜의 synthetic 레코드를 채워 넣어 반복매수 탐지가 즉시 동작하도록 합니다.
        """

        today = date.today().isoformat()
        now = datetime.now(UTC).isoformat()
        with self._conn:
            for _ in range(synthetic_count):
                self._conn.execute(
                    """
                    INSERT INTO purchases
                        (owner_cik, issuer_cik, transaction_date, shares, price_per_share,
                         value_usd, accession_no, is_synthetic_seed, recorded_at)
                    VALUES (?, ?, ?, 0, 0, 0, 'SEED', 1, ?)
                    """,
                    (owner_cik, issuer_cik, today, now),
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO seeded_owners (owner_cik, seeded_at) VALUES (?, ?)",
                (owner_cik, now),
            )
