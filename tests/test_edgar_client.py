"""edgar_client의 파싱 로직 테스트 (네트워크 호출 없음).

아래 SAMPLE_ATOM_FEED는 실제 SEC EDGAR getcurrent atom 피드(2026-08-24)를 그대로 curl로
받아 확인한 두 가지 실제 동작을 재현한 축약 샘플입니다:
1. `type=4` 쿼리 파라미터가 접두어 매칭이라 424B3 등도 섞여 들어옴.
2. 신고인이 여러 명인 Form 4는 issuer/owner 각각 별도 entry로, 같은 accession 번호가 중복됨.
"""

from insider_signal.edgar_client import SECEdgarClient, _parse_index_href

SAMPLE_ATOM_FEED = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
<title>424B3 - Game Your Game Inc. (0002111846) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/2111846/000121390026092783/0001213900-26-092783-index.htm"/>
<updated>2026-08-24T06:07:32-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="424B3"/>
<id>urn:tag:sec.gov,2008:accession-number=0001213900-26-092783</id>
</entry>
<entry>
<title>4 - Wu Yongming (0002121606) (Reporting)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/2121606/000119312526361711/0001193125-26-361711-index.htm"/>
<updated>2026-08-24T06:04:00-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001193125-26-361711</id>
</entry>
<entry>
<title>4 - Alibaba Group Holding Ltd (0001577552) (Issuer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1577552/000119312526361711/0001193125-26-361711-index.htm"/>
<updated>2026-08-24T06:04:00-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
<id>urn:tag:sec.gov,2008:accession-number=0001193125-26-361711</id>
</entry>
</feed>
"""


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass


def test_parse_index_href_extracts_cik_and_accession():
    href = (
        "https://www.sec.gov/Archives/edgar/data/1577552/000119312526361711/"
        "0001193125-26-361711-index.htm"
    )
    result = _parse_index_href(href)
    assert result == ("1577552", "0001193125-26-361711")


def test_parse_index_href_returns_none_for_unrelated_url():
    assert _parse_index_href("https://www.sec.gov/robots.txt") is None


def test_get_latest_form4_entries_filters_non_form4_and_dedups(monkeypatch):
    client = SECEdgarClient(contact="test test@example.com")
    monkeypatch.setattr(client, "_get", lambda url, **kw: _FakeResponse(SAMPLE_ATOM_FEED))

    entries = client.get_latest_form4_entries(count=10)

    # 424B3는 제외되고, 같은 accession의 (Reporting)/(Issuer) 두 entry는 하나로 합쳐져야 함.
    assert len(entries) == 1
    assert entries[0].accession_no == "0001193125-26-361711"
    # 피드에 먼저 등장한 entry(신고인 Wu Yongming, cik=2121606)가 채택됨.
    assert entries[0].cik == "2121606"
