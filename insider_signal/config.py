"""환경변수 기반 설정 로딩."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y"}


def _int_env(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _float_env(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return float(val)


# 법인/펀드/신탁 등을 이름으로 걸러내기 위한 접미사 목록 (요구사항 2: 개인 거래만 사용).
ENTITY_NAME_HINTS: tuple[str, ...] = (
    "LLC",
    "L.L.C",
    "L.P.",
    " LP",
    "LP.",
    "INC",
    "INC.",
    "CORP",
    "CORPORATION",
    "TRUST",
    "PARTNERS",
    "PARTNERSHIP",
    "CAPITAL",
    "MANAGEMENT",
    "MANAGEMENT LLC",
    "FUND",
    "HOLDINGS",
    "HOLDING",
    "GROUP",
    "VENTURES",
    "ADVISORS",
    "ADVISERS",
    "ASSOCIATES",
    "COMPANY",
    " CO.",
    "PLC",
    "N.A.",
    "FOUNDATION",
)


@dataclass
class Settings:
    sec_edgar_contact: str = field(
        default_factory=lambda: os.getenv("SEC_EDGAR_CONTACT", "")
    )

    min_txn_value_usd: float = field(
        default_factory=lambda: _float_env("MIN_TXN_VALUE_USD", 100_000.0)
    )
    max_txn_value_usd: float = field(
        default_factory=lambda: _float_env("MAX_TXN_VALUE_USD", 500_000.0)
    )

    recurring_lookback_days: int = field(
        default_factory=lambda: _int_env("RECURRING_LOOKBACK_DAYS", 90)
    )
    recurring_min_occurrences: int = field(
        default_factory=lambda: _int_env("RECURRING_MIN_OCCURRENCES", 3)
    )

    poll_interval_seconds: int = field(
        default_factory=lambda: _int_env("POLL_INTERVAL_SECONDS", 300)
    )

    history_db_path: Path = field(
        default_factory=lambda: REPO_ROOT
        / os.getenv("HISTORY_DB_PATH", "data/insider_signal.db")
    )

    slack_webhook_url: str = field(
        default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL", "")
    )
    discord_webhook_url: str = field(
        default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", "")
    )
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "")
    )


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env", override=False)
    return Settings()
