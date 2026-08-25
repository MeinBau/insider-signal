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

    def count_recent_purchases(
        self, *, owner_cik: str, issuer_cik: str, lookback_days: int
    ) -> int:
        cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
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
