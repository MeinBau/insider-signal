import pytest

from insider_signal import sectors


@pytest.mark.parametrize(
    "sic, expected",
    [
        ("2836", "bio"),
        ("8731", "bio"),
        ("1040", "gold"),
        ("2834", "bio"),  # 일반 제약 포함 (표본 확보를 위해 사용자가 명시적으로 확장)
        ("7372", None),  # 소프트웨어 등 무관 SIC
        (None, None),
    ],
)
def test_classify_sic(sic, expected):
    assert sectors.classify_sic(sic) == expected


def test_resolve_target_stop_uses_sector_override():
    assert sectors.resolve_target_stop("bio", 5.0, 10.0) == sectors.SECTOR_TARGET_STOP["bio"]
    assert sectors.resolve_target_stop("gold", 5.0, 10.0) == sectors.SECTOR_TARGET_STOP["gold"]


def test_resolve_target_stop_falls_back_to_default_for_unknown_or_empty_sector():
    assert sectors.resolve_target_stop("", 5.0, 10.0) == (5.0, 10.0)
    assert sectors.resolve_target_stop(None, 5.0, 10.0) == (5.0, 10.0)
    assert sectors.resolve_target_stop("tech", 5.0, 10.0) == (5.0, 10.0)
