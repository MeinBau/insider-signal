"""이슈어 SIC(Standard Industrial Classification) 코드 -> 섹터 매핑.

백테스트에서 바이오/금 관련 이슈어에 대해 실험적으로 다른 목표수익률/손절률을 적용하기 위한
용도입니다 (사용자 요청, --sector-experiment 플래그로만 켜짐). 여기 적힌 target/stop 값은
아직 실제로 백테스트 검증되지 않은 가설치이며, 검증 전까지는 기본값(+5%/-10%)을 대체하지
않습니다 -- CLAUDE.md의 "백테스트의 목표수익률/손절률 기본값... 확인 없이 바꾸지 말 것" 참고.

SIC 코드는 EDGAR submissions API(``edgar_client.get_issuer_sic``)에서 가져옵니다.

- 바이오: 2836(Biological Products), 8731(Commercial Physical & Biological Research).
  2834(Pharmaceutical Preparations, 일반 제약)는 제외 -- 임상 바이너리 이벤트 변동성이라는
  가설과 맞지 않는 대형 제네릭 제약사까지 섞여 들어와 신호를 희석시킬 수 있음.
- 금: 1040(Gold Mining)만 사용 ("금 관련"이라는 요청 범위에 맞춤, 은/귀금속 전반은 제외).
"""

from __future__ import annotations

BIO_SIC_CODES: frozenset[str] = frozenset({"2836", "8731"})
GOLD_SIC_CODES: frozenset[str] = frozenset({"1040"})

# (target_pct, stop_pct) -- 기준값 +5%/-10% 대비 변동성이 크다는 가설로 넓게 잡은 값.
SECTOR_TARGET_STOP: dict[str, tuple[float, float]] = {
    "bio": (9.0, 15.0),
    "gold": (6.5, 12.0),
}


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
