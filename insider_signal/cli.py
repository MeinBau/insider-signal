"""CLI 진입점.

사용법:
    python -m insider_signal.cli run --once
    python -m insider_signal.cli run --interval 300
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_settings
from .edgar_client import SECEdgarClient
from .history import HistoryStore
from .notifier import build_notifier
from .poller import run_forever, run_once


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="insider-signal")
    parser.add_argument("-v", "--verbose", action="store_true", help="디버그 로그 출력")

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="EDGAR polling을 시작합니다.")
    run_p.add_argument("--once", action="store_true", help="한 번만 polling하고 종료")
    run_p.add_argument(
        "--interval", type=int, default=None, help="polling 주기(초). 미지정 시 .env 값 사용"
    )
    run_p.add_argument(
        "--feed-count", type=int, default=100, help="한 번에 조회할 최신 Form 4 개수 (--once 전용)"
    )

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    settings = load_settings()
    if args.interval is not None:
        settings.poll_interval_seconds = args.interval

    try:
        client = SECEdgarClient(contact=settings.sec_edgar_contact)
    except ValueError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 1

    notifier = build_notifier(settings)

    with HistoryStore(settings.history_db_path) as history:
        if args.command == "run":
            if args.once:
                total = run_once(
                    client=client,
                    history=history,
                    notifier=notifier,
                    settings=settings,
                    feed_count=args.feed_count,
                )
                logging.getLogger(__name__).info("완료: 신호 %d건 발생", total)
            else:
                run_forever(client=client, history=history, notifier=notifier, settings=settings)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
