"""SEC EDGAR HTTP 클라이언트.

SEC Fair Access 정책(https://www.sec.gov/os/webmaster-faq#developers)에 따라
모든 요청에 연락처가 담긴 User-Agent를 보내야 하고, 과도한 요청 빈도를 피해야 합니다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

LATEST_FORM4_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&company=&dateb=&owner=include&count={count}&output=atom"
)
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

MIN_REQUEST_INTERVAL_SEC = 0.2  # 초당 최대 5회 수준으로 보수적으로 제한 (SEC 권장치보다 낮음)


@dataclass(frozen=True)
class FeedEntry:
    cik: str
    accession_no: str  # dash 포함 정식 형식, 예: 0000320193-24-000106
    index_url: str
    title: str
    updated: str


class _RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_request_at = time.monotonic()


class SECEdgarClient:
    def __init__(self, contact: str, min_request_interval: float = MIN_REQUEST_INTERVAL_SEC) -> None:
        if not contact.strip():
            raise ValueError(
                "SEC_EDGAR_CONTACT가 비어 있습니다. .env에 실제 연락처를 설정하세요 "
                '(예: SEC_EDGAR_CONTACT="insider-signal/0.1 you@example.com").'
            )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": contact,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self._limiter = _RateLimiter(min_request_interval)

    def _get(self, url: str, *, retries: int = 3) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            self._limiter.wait()
            try:
                resp = self._session.get(url, timeout=15)
                if resp.status_code == 429:
                    wait_s = 1.0 * attempt
                    logger.warning("SEC EDGAR 429 rate limited, %.1fs 대기 후 재시도: %s", wait_s, url)
                    time.sleep(wait_s)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:  # pragma: no cover - 네트워크 예외 경로
                last_exc = exc
                logger.warning("SEC EDGAR 요청 실패(%d/%d): %s (%s)", attempt, retries, url, exc)
                time.sleep(0.5 * attempt)
        assert last_exc is not None
        raise last_exc

    def get_latest_form4_entries(self, count: int = 100) -> list[FeedEntry]:
        url = LATEST_FORM4_FEED_URL.format(count=count)
        resp = self._get(url)
        root = ElementTree.fromstring(resp.content)
        entries: list[FeedEntry] = []
        for entry_el in root.findall("atom:entry", ATOM_NS):
            link_el = entry_el.find("atom:link", ATOM_NS)
            title_el = entry_el.find("atom:title", ATOM_NS)
            updated_el = entry_el.find("atom:updated", ATOM_NS)
            if link_el is None:
                continue
            href = link_el.get("href", "")
            parsed = _parse_index_href(href)
            if parsed is None:
                continue
            cik, accession_no = parsed
            entries.append(
                FeedEntry(
                    cik=cik,
                    accession_no=accession_no,
                    index_url=href,
                    title=(title_el.text or "").strip() if title_el is not None else "",
                    updated=(updated_el.text or "").strip() if updated_el is not None else "",
                )
            )
        return entries

    def get_primary_xml_url(self, cik: str, accession_no: str) -> str | None:
        cik_nodash = cik.lstrip("0") or "0"
        accession_nodash = accession_no.replace("-", "")
        index_json_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_nodash}/{accession_nodash}/"
            f"{accession_no}-index.json"
        )
        resp = self._get(index_json_url)
        data = resp.json()
        items = data.get("directory", {}).get("item", [])

        def is_candidate(item: dict) -> bool:
            name = item.get("name", "")
            return name.lower().endswith(".xml") and "index" not in name.lower()

        candidates = [it for it in items if is_candidate(it)]
        if not candidates:
            return None
        # form type과 정확히 일치하는 문서를 우선 사용 (Form 4 -> type "4")
        exact = [it for it in candidates if it.get("type") == "4"]
        chosen = exact[0] if exact else candidates[0]
        name = chosen["name"]
        return f"https://www.sec.gov/Archives/edgar/data/{cik_nodash}/{accession_nodash}/{name}"

    def fetch_xml(self, url: str) -> bytes:
        resp = self._get(url)
        return resp.content

    def get_owner_recent_form4_count(self, owner_cik: str, lookback_days: int) -> int:
        """신고인(owner)의 최근 Form 4 제출 횟수를 조회합니다.

        이슈어(issuer) 단위까지는 구분하지 못하는 근사치입니다 — CLAUDE.md의
        "반복 매수 배제" 섹션에 명시된 알려진 한계입니다. 로컬 SQLite 이력이
        쌓이기 전, 최초 관측 시점에 이력을 보강하는 용도로만 사용하세요.
        """

        cik10 = owner_cik.zfill(10)
        url = SUBMISSIONS_URL.format(cik10=cik10)
        try:
            resp = self._get(url)
        except requests.HTTPError as exc:  # 신고인 CIK가 submissions API에 없는 경우 등
            logger.info("submissions API 조회 실패(owner_cik=%s): %s", owner_cik, exc)
            return 0
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        cutoff = date.today() - timedelta(days=lookback_days)
        count = 0
        for form, filing_date_str in zip(forms, filing_dates):
            if form != "4":
                continue
            try:
                filing_date = datetime.strptime(filing_date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if filing_date >= cutoff:
                count += 1
        return count


def _parse_index_href(href: str) -> tuple[str, str] | None:
    """예: https://www.sec.gov/Archives/edgar/data/320193/000032019324000106-index.htm
    -> ("320193", "0000320193-24-000106")
    """

    marker = "/Archives/edgar/data/"
    idx = href.find(marker)
    if idx == -1:
        return None
    tail = href[idx + len(marker) :]
    parts = tail.split("/")
    if len(parts) < 2:
        return None
    cik = parts[0]
    accession_part = parts[1]
    accession_nodash = accession_part.replace("-index.htm", "").replace("-index.html", "")
    accession_nodash = accession_nodash.split(".")[0]
    if len(accession_nodash) != 18 or not accession_nodash.isdigit():
        return None
    accession_no = f"{accession_nodash[0:10]}-{accession_nodash[10:12]}-{accession_nodash[12:18]}"
    return cik, accession_no
