"""SEC EDGAR HTTP 클라이언트.

SEC Fair Access 정책(https://www.sec.gov/os/webmaster-faq#developers)에 따라
모든 요청에 연락처가 담긴 User-Agent를 보내야 하고, 과도한 요청 빈도를 피해야 합니다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

LATEST_FORM4_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=4&company=&dateb=&owner=include&count={count}&output=atom"
)
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
FULL_INDEX_MASTER_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/master.idx"

MIN_REQUEST_INTERVAL_SEC = 0.2  # 초당 최대 5회 수준으로 보수적으로 제한 (SEC 권장치보다 낮음)


@dataclass(frozen=True)
class FeedEntry:
    cik: str
    accession_no: str  # dash 포함 정식 형식, 예: 0000320193-24-000106
    index_url: str
    title: str
    updated: str


@dataclass(frozen=True)
class HistoricalFilingRef:
    """분기별 벌크 인덱스(master.idx)에서 찾은 과거 Form 4 filing 한 건.

    백테스트 전용. ``cik``는 master.idx 기준 발행사(issuer) CIK이며, ``filed_at``은
    EDGAR가 실제로 접수한 날짜(``Date Filed``)입니다 — 파싱된 Filing.filed_at
    (XML의 periodOfReport, 실질적으로 거래일과 거의 같음)과는 다른 값이므로 혼동하면 안 됩니다.
    """

    cik: str
    company_name: str
    form_type: str
    filed_at: date
    accession_no: str


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
        self._master_idx_cache: dict[tuple[int, int], str] = {}

    def _get(self, url: str, *, retries: int = 3) -> requests.Response:
        """네트워크 오류/429/5xx는 재시도하고, 그 외 4xx(404 등)는 영구적 오류로 보고 즉시 raise합니다."""

        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            self._limiter.wait()
            try:
                resp = self._session.get(url, timeout=15)
            except requests.RequestException as exc:  # pragma: no cover - 네트워크 예외 경로
                last_exc = exc
                logger.warning("SEC EDGAR 요청 실패(%d/%d): %s (%s)", attempt, retries, url, exc)
                time.sleep(0.5 * attempt)
                continue

            if resp.status_code == 429:
                wait_s = 1.0 * attempt
                logger.warning("SEC EDGAR 429 rate limited, %.1fs 대기 후 재시도: %s", wait_s, url)
                time.sleep(wait_s)
                continue
            if 400 <= resp.status_code < 500:
                resp.raise_for_status()  # 영구적 오류로 간주, 재시도하지 않고 즉시 전파
            if resp.status_code >= 500:
                last_exc = requests.HTTPError(f"{resp.status_code} server error", response=resp)
                logger.warning("SEC EDGAR 서버 오류(%d/%d): %s", attempt, retries, url)
                time.sleep(0.5 * attempt)
                continue

            resp.raise_for_status()
            return resp
        assert last_exc is not None
        raise last_exc

    def get_latest_form4_entries(self, count: int = 100) -> list[FeedEntry]:
        """최신 Form 4 목록을 조회합니다.

        SEC EDGAR의 ``getcurrent`` atom 피드는 두 가지 특이한 동작을 보입니다 (실제 응답으로 확인함):

        1. ``type=4`` 쿼리 파라미터가 접두어 매칭이라 ``424B3``, ``424B5`` 같은 다른 서식도
           함께 반환됩니다. 그래서 각 entry의 ``<category label="form type" term="...">``
           값이 정확히 ``"4"``인 것만 클라이언트에서 다시 걸러냅니다.
        2. 신고인이 여러 명인 Form 4는 (신고인마다 + 발행사) 여러 entry로 중복 등장하며,
           같은 accession 번호가 실측 기준 최대 10회 이상 반복될 수 있습니다. 그래서
           accession_no 기준으로 첫 번째 entry만 남기고 중복 제거합니다.
        """

        url = LATEST_FORM4_FEED_URL.format(count=count)
        resp = self._get(url)
        root = ElementTree.fromstring(resp.content)
        entries: list[FeedEntry] = []
        seen_accession_nos: set[str] = set()
        for entry_el in root.findall("atom:entry", ATOM_NS):
            category_el = entry_el.find("atom:category", ATOM_NS)
            form_type = category_el.get("term", "") if category_el is not None else ""
            if form_type != "4":
                continue

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
            if accession_no in seen_accession_nos:
                continue
            seen_accession_nos.add(accession_no)

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
        """accession 폴더 안에서 실제 ownership XML 문서(보통 ``ownership.xml``)를 찾습니다.

        디렉터리 목록은 ``{accession-no-dash}-index.json`` 이 아니라 그냥 ``index.json``
        (실제 응답으로 확인함)이며, 각 item의 ``type`` 필드는 "4" 같은 서식 코드가 아니라
        ``"text.gif"`` 같은 아이콘 타입이라 폼타입 매칭에 쓸 수 없습니다. 그래서
        확장자(.xml)와 이름 패턴만으로 후보를 고릅니다.
        """

        cik_nodash = cik.lstrip("0") or "0"
        accession_nodash = accession_no.replace("-", "")
        index_json_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_nodash}/{accession_nodash}/index.json"
        )
        resp = self._get(index_json_url)
        data = resp.json()
        items = data.get("directory", {}).get("item", [])

        xbrl_linkbase_suffixes = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "_htm.xml")

        def is_candidate(item: dict) -> bool:
            name = item.get("name", "").lower()
            if not name.endswith(".xml"):
                return False
            if "index" in name:
                return False
            if name.endswith(xbrl_linkbase_suffixes):
                return False
            return True

        candidates = [it for it in items if is_candidate(it)]
        if not candidates:
            return None
        # ownership.xml(또는 유사 명명)을 우선 사용하고, 없으면 첫 후보로 대체.
        preferred = [it for it in candidates if "ownership" in it["name"].lower()]
        chosen = preferred[0] if preferred else candidates[0]
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

    def iter_historical_form4_filings(
        self, start: date, end: date, *, include_amendments: bool = False
    ) -> Iterator[HistoricalFilingRef]:
        """백테스트 전용: [start, end] 구간에 걸린 분기들의 master.idx를 받아 Form 4 filing을
        열거합니다. ``get_latest_form4_entries``(최신 N건만 지원)로는 할 수 없는, 특정 과거
        기간에 대한 조회입니다.
        """

        for year, quarter in _quarters_between(start, end):
            text = self._fetch_master_index(year, quarter)
            for ref in _parse_master_index(text, include_amendments=include_amendments):
                if start <= ref.filed_at <= end:
                    yield ref

    def _fetch_master_index(self, year: int, quarter: int) -> str:
        key = (year, quarter)
        if key not in self._master_idx_cache:
            url = FULL_INDEX_MASTER_URL.format(year=year, quarter=quarter)
            resp = self._get(url)
            # SEC의 idx 파일은 UTF-8이 아니라 latin-1로 인코딩되어 있음 (실제 응답으로 확인함).
            self._master_idx_cache[key] = resp.content.decode("latin-1")
        return self._master_idx_cache[key]


def _quarters_between(start: date, end: date) -> list[tuple[int, int]]:
    """start~end 구간이 걸치는 모든 (year, quarter)를 순서대로 반환합니다.

    예: 2026-05-25 ~ 2026-08-25 -> [(2026, 2), (2026, 3)]
    """

    quarters: list[tuple[int, int]] = []
    year, quarter = start.year, (start.month - 1) // 3 + 1
    end_year, end_quarter = end.year, (end.month - 1) // 3 + 1
    while (year, quarter) <= (end_year, end_quarter):
        quarters.append((year, quarter))
        quarter += 1
        if quarter > 4:
            quarter = 1
            year += 1
    return quarters


def _parse_master_index(
    text: str, *, include_amendments: bool = False
) -> Iterator[HistoricalFilingRef]:
    """master.idx는 ``CIK|Company Name|Form Type|Date Filed|Filename`` 파이프 구분 텍스트이며,
    본문 앞에 안내용 헤더/구분선이 몇 줄 붙어있습니다 (파이프가 4개가 아닌 줄은 그냥 건너뜁니다).
    ``Filename`` 컬럼은 이미 대시 포함 accession number를 담고 있어(``.../{accession}.txt``)
    별도 변환 없이 ``Path(filename).stem``으로 바로 추출됩니다.
    """

    wanted = {"4", "4/A"} if include_amendments else {"4"}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, company_name, form_type, date_filed_s, filename = (p.strip() for p in parts)
        if form_type not in wanted:
            continue
        try:
            filed_at = datetime.strptime(date_filed_s, "%Y-%m-%d").date()
        except ValueError:
            continue
        accession_no = PurePosixPath(filename).stem
        yield HistoricalFilingRef(
            cik=cik,
            company_name=company_name,
            form_type=form_type,
            filed_at=filed_at,
            accession_no=accession_no,
        )


def _parse_index_href(href: str) -> tuple[str, str] | None:
    """실제 관측된 형식: .../Archives/edgar/data/{cik}/{accession_nodash}/{accession-index.htm}

    예: https://www.sec.gov/Archives/edgar/data/1577552/000119312526361711/0001193125-26-361711-index.htm
    -> ("1577552", "0001193125-26-361711")

    accession_nodash는 두 번째 경로 세그먼트(폴더명) 자체이므로, 세 번째 세그먼트인
    "...-index.htm" 파일명은 파싱에 사용하지 않습니다.
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
    accession_nodash = parts[1]
    if len(accession_nodash) != 18 or not accession_nodash.isdigit():
        return None
    accession_no = f"{accession_nodash[0:10]}-{accession_nodash[10:12]}-{accession_nodash[12:18]}"
    return cik, accession_no
