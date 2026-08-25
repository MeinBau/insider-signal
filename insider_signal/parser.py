"""Form 4 ownershipDocument XML 파서.

SEC의 ownership XML 스키마는 네임스페이스를 사용하지 않으므로 ElementTree를 그대로 사용합니다.
"""

from __future__ import annotations

from datetime import date, datetime
from xml.etree import ElementTree

from .models import Filing, Issuer, ReportingOwner, Transaction


class Form4ParseError(ValueError):
    pass


def _text(el: ElementTree.Element | None, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _value_text(el: ElementTree.Element | None, tag: str, default: str = "") -> str:
    """SEC 스키마는 대부분의 필드를 <tag><value>실제값</value></tag> 형태로 감쌉니다."""

    if el is None:
        return default
    child = el.find(tag)
    if child is None:
        return default
    value_el = child.find("value")
    if value_el is not None and value_el.text is not None:
        return value_el.text.strip()
    if child.text is not None:
        return child.text.strip()
    return default


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_bool_flag(s: str) -> bool:
    return s.strip() in {"1", "true", "True"}


def _collect_footnotes(root: ElementTree.Element) -> dict[str, str]:
    footnotes: dict[str, str] = {}
    footnotes_el = root.find("footnotes")
    if footnotes_el is None:
        return footnotes
    for fn in footnotes_el.findall("footnote"):
        fn_id = fn.get("id", "")
        footnotes[fn_id] = (fn.text or "").strip()
    return footnotes


def _footnote_ids_for(txn_el: ElementTree.Element) -> list[str]:
    return [fid_el.get("id", "") for fid_el in txn_el.findall(".//footnoteId")]


def parse_form4_xml(xml_bytes: bytes, *, accession_no: str, source_url: str) -> Filing:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise Form4ParseError(f"XML 파싱 실패: {exc}") from exc

    if root.tag != "ownershipDocument":
        raise Form4ParseError(f"ownershipDocument가 아닌 문서입니다: root tag={root.tag}")

    issuer_el = root.find("issuer")
    issuer = Issuer(
        cik=_text(issuer_el, "issuerCik"),
        name=_text(issuer_el, "issuerName"),
        ticker=_text(issuer_el, "issuerTradingSymbol"),
    )

    owner_el = root.find("reportingOwner")
    owner_id_el = owner_el.find("reportingOwnerId") if owner_el is not None else None
    rel_el = owner_el.find("reportingOwnerRelationship") if owner_el is not None else None

    owner = ReportingOwner(
        cik=_text(owner_id_el, "rptOwnerCik"),
        name=_text(owner_id_el, "rptOwnerName"),
        is_director=_parse_bool_flag(_text(rel_el, "isDirector", "0")),
        is_officer=_parse_bool_flag(_text(rel_el, "isOfficer", "0")),
        is_ten_percent_owner=_parse_bool_flag(_text(rel_el, "isTenPercentOwner", "0")),
        is_other=_parse_bool_flag(_text(rel_el, "isOther", "0")),
        officer_title=_text(rel_el, "officerTitle"),
        other_text=_text(rel_el, "otherText"),
    )

    footnotes = _collect_footnotes(root)

    period_of_report = _text(root, "periodOfReport")
    filed_at = _parse_date(period_of_report) if period_of_report else date.today()

    transactions: list[Transaction] = []
    non_deriv_table = root.find("nonDerivativeTable")
    if non_deriv_table is not None:
        for txn_el in non_deriv_table.findall("nonDerivativeTransaction"):
            coding_el = txn_el.find("transactionCoding")
            amounts_el = txn_el.find("transactionAmounts")
            post_el = txn_el.find("postTransactionAmounts")

            txn_date_s = _value_text(txn_el, "transactionDate")
            shares_s = _value_text(amounts_el, "transactionShares", "0")
            price_s = _value_text(amounts_el, "transactionPricePerShare", "0")
            shares_after_s = _value_text(post_el, "sharesOwnedFollowingTransaction", "")

            fn_texts = tuple(
                footnotes[fid] for fid in _footnote_ids_for(txn_el) if fid in footnotes
            )

            try:
                shares = float(shares_s) if shares_s else 0.0
                price = float(price_s) if price_s else 0.0
            except ValueError:
                shares = 0.0
                price = 0.0

            transactions.append(
                Transaction(
                    security_title=_value_text(txn_el, "securityTitle"),
                    transaction_date=_parse_date(txn_date_s) if txn_date_s else filed_at,
                    transaction_code=_text(coding_el, "transactionCode"),
                    acquired_disposed_code=_value_text(amounts_el, "transactionAcquiredDisposedCode"),
                    shares=shares,
                    price_per_share=price,
                    shares_owned_after=float(shares_after_s) if shares_after_s else None,
                    footnote_texts=fn_texts,
                )
            )

    return Filing(
        accession_no=accession_no,
        issuer=issuer,
        owner=owner,
        filed_at=filed_at,
        source_url=source_url,
        transactions=tuple(transactions),
    )
