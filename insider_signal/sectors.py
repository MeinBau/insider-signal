"""이슈어 SIC(Standard Industrial Classification) 코드 -> 섹터 매핑.

백테스트에서 바이오/금 관련 이슈어에 대해 실험적으로 다른 목표수익률/손절률을 적용하기 위한
용도입니다 (사용자 요청, --sector-experiment 플래그로만 켜짐). 여기 적힌 target/stop 값은
아직 실제로 백테스트 검증되지 않은 가설치이며, 검증 전까지는 기본값(+5%/-10%)을 대체하지
않습니다 -- CLAUDE.md의 "백테스트의 목표수익률/손절률 기본값... 확인 없이 바꾸지 말 것" 참고.

SIC 코드는 EDGAR submissions API(``edgar_client.get_issuer_sic``)에서 가져옵니다.

- 바이오: 2836(Biological Products), 8731(Commercial Physical & Biological Research),
  2834(Pharmaceutical Preparations, 일반 제약 포함). 1차 실험(n=13)이 표본 부족으로 판단이
  안 서서, 사용자 확인 후 2834까지 포함해 표본을 넓힌 버전 -- 순수 임상 바이오텍만 걸러내는
  가설과는 멀어지지만(일반 제약사까지 섞임), 표본을 확보하는 게 우선이라는 트레이드오프를
  사용자가 명시적으로 선택함.
- 금: 1040(Gold Mining)만 사용 ("금 관련"이라는 요청 범위에 맞춤, 은/귀금속 전반은 제외).
"""

from __future__ import annotations

BIO_SIC_CODES: frozenset[str] = frozenset({"2836", "8731", "2834"})
GOLD_SIC_CODES: frozenset[str] = frozenset({"1040"})

# (target_pct, stop_pct) -- 기준값 +5%/-10% 대비 변동성이 크다는 가설로 넓게 잡은 값.
SECTOR_TARGET_STOP: dict[str, tuple[float, float]] = {
    "bio": (9.0, 15.0),
    "gold": (6.5, 12.0),
}

# 바이오 한정, 완화된 거래금액 하한 (실험, --sector-experiment 전용). 바이오텍 임원은 보상이
# 스톡옵션 위주라 현금 매수 여력이 적어 기본 $100,000 하한 밑으로 빠지는 진짜 확신 매수가
# 많을 수 있다는 가설로, 사용자가 명시적으로 확정한 값. 상한($500,000)과 다른 필터 규칙은
# 그대로 유지 -- CLAUDE.md의 기본 필터 임계값 자체를 바꾸는 게 아니라, 백테스트 실험
# 경로에서만 바이오 이슈어에 한해 하한을 낮게 적용하는 국소적 확장.
BIO_MIN_TXN_VALUE_USD = 50_000.0


def classify_sic(sic: str | None) -> str | None:
    """SIC 코드를 "bio"/"gold" 섹터로 매핑합니다. 둘 다 아니면 None."""

    if sic is None:
        return None
    if sic in BIO_SIC_CODES:
        return "bio"
    if sic in GOLD_SIC_CODES:
        return "gold"
    return None


def resolve_target_stop(
    sector: str | None, default_target_pct: float, default_stop_pct: float
) -> tuple[float, float]:
    """섹터별 오버라이드가 있으면 그 값을, 없으면 기본값을 반환합니다."""

    if sector and sector in SECTOR_TARGET_STOP:
        return SECTOR_TARGET_STOP[sector]
    return default_target_pct, default_stop_pct
