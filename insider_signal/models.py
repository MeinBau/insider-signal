"""Form 4 도메인 모델."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Issuer:
    cik: str
    name: str
    ticker: str


@dataclass(frozen=True)
class ReportingOwner:
    cik: str
    name: str
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    is_other: bool
    officer_title: str = ""
    other_text: str = ""


@dataclass(frozen=True)
class Transaction:
    """nonDerivativeTable의 트랜잭션 한 건."""

    security_title: str
    transaction_date: date
    transaction_code: str  # P, S, A, M, G, F ...
    acquired_disposed_code: str  # "A" (취득) / "D" (처분)
    shares: float
    price_per_share: float
    shares_owned_after: float | None
    footnote_texts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def value_usd(self) -> float:
        return self.shares * self.price_per_share

    @property
    def is_open_market_purchase(self) -> bool:
        return self.transaction_code == "P" and self.acquired_disposed_code == "A"

    @property
    def is_10b5_1_plan(self) -> bool:
        combined = " ".join(self.footnote_texts).lower()
        return "10b5-1" in combined or "10b5‑1" in combined  # 두 번째는 유니코드 하이픈


@dataclass(frozen=True)
class Filing:
    accession_no: str
    issuer: Issuer
    owner: ReportingOwner
    filed_at: date
    source_url: str
    transactions: tuple[Transaction, ...] = field(default_factory=tuple)
