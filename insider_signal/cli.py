"""CLI 진입점.

사용법:
    python -m insider_signal.cli run --once
    python -m insider_signal.cli run --interval 300
    python -m insider_signal.cli backtest --target-pct 5 --stop-pct 10
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .config import REPO_ROOT, load_settings
from .edgar_client import SECEdgarClient
from .history import HistoryStore
from .notifier import build_notifier
from .poller import run_forever, run_once


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _parse_date_arg(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"날짜 형식은 YYYY-MM-DD여야 합니다: {s}") from exc


def _months_ago(d: date, months: int) -> date:
    """달력 월 단위로 뺄셈 (day-of-month 초과 시 말일로 클램프). timedelta(days=30*n)의
    누적 오차를 피하기 위해 dateutil 없이 stdlib만으로 계산합니다."""

    total_month = d.month - 1 - months
    year = d.year + total_month // 12
    month = total_month % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


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

    bt_p = sub.add_parser(
        "backtest", help="과거 기간에 대해 오프라인으로 신호를 재구성하고 가격 시뮬레이션을 수행합니다."
    )
    bt_p.add_argument("--start", type=_parse_date_arg, default=None, help="시작일 YYYY-MM-DD (기본: 종료일 1개월 전)")
    bt_p.add_argument("--end", type=_parse_date_arg, default=None, help="종료일 YYYY-MM-DD (기본: 오늘)")
    bt_p.add_argument("--target-pct", type=float, default=5.0, help="목표수익률 %% (기본 5.0)")
    bt_p.add_argument("--stop-pct", type=float, default=10.0, help="손절률 %% (기본 10.0)")
    bt_p.add_argument("--max-hold-days", type=int, default=30, help="최대 보유 거래일수 (기본 30)")
    bt_p.add_argument(
        "--include-amendments", action="store_true", help="Form 4/A(정정신고)도 포함"
    )
    bt_p.add_argument(
        "--checkpoint-dir", type=Path, default=None,
        help="재개용 체크포인트/결과 DB 및 CSV 리포트를 저장할 디렉터리 (기본: data/backtest_runs/)",
    )
    bt_p.add_argument(
        "--progress-every", type=int, default=200, help="진행 로그를 남길 filing 처리 간격 (기본 200)"
    )

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    settings = load_settings()
    if args.command == "run" and args.interval is not None:
        settings.poll_interval_seconds = args.interval

    try:
        client = SECEdgarClient(contact=settings.sec_edgar_contact)
    except ValueError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 1

    if args.command == "backtest":
        from .backtest import print_summary, run_backtest  # yfinance는 backtest에서만 필요하므로 지연 import

        end = args.end or date.today()
        start = args.start or _months_ago(end, 1)
        checkpoint_dir = args.checkpoint_dir or (REPO_ROOT / "data" / "backtest_runs")
        run_tag = f"{start.isoformat()}_{end.isoformat()}"

        report = run_backtest(
            client=client,
            settings=settings,
            start=start,
            end=end,
            target_pct=args.target_pct,
            stop_pct=args.stop_pct,
            max_hold_days=args.max_hold_days,
            history_db_path=checkpoint_dir / f"{run_tag}_history.sqlite3",
            results_db_path=checkpoint_dir / f"{run_tag}_results.sqlite3",
            output_csv_path=checkpoint_dir / f"{run_tag}_results.csv",
            include_amendments=args.include_amendments,
            progress_every=args.progress_every,
        )
        print_summary(report)
        return 0

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
