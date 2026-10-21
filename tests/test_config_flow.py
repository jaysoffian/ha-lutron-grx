# pyright: reportPrivateUsage=false
"""Tests for the Lutron GrafikEye GRX config-flow helpers."""

import pytest

from custom_components.lutron_grx.client import UnitConfig
from custom_components.lutron_grx.config_flow import (
    _parse_units,
    _unit_form_defaults,
)


def test_parse_units_aligned_values() -> None:
    assert _parse_units("1,2", "4,16", "GRX-3504,GRX-4516") == [
        UnitConfig(1, 4, "GRX-3504"),
        UnitConfig(2, 16, "GRX-4516"),
    ]


def test_parse_units_broadcasts_single_values() -> None:
    assert _parse_units("1,2", "4", "GRX-3504") == [
        UnitConfig(1, 4, "GRX-3504"),
        UnitConfig(2, 4, "GRX-3504"),
    ]


@pytest.mark.parametrize(
    ("units", "zones", "models"),
    [
        ("", "4", "GRX-3504"),
        ("1,2", "4,8,16", "GRX-3504"),
        ("1,2", "4,16", "GRX-3504,GRX-4516,GRX-3504"),
        ("1,1", "4,4", "GRX-3504"),
        ("1,2", "16,4", "GRX-4516,GRX-3504"),
        ("1", "25", "GRX-3504"),
    ],
)
def test_parse_units_rejects_invalid_input(units: str, zones: str, models: str) -> None:
    with pytest.raises(ValueError):
        _parse_units(units, zones, models)


def test_unit_form_defaults() -> None:
    assert _unit_form_defaults(
        [UnitConfig(1, 4, "GRX-3504"), UnitConfig(2, 16, "GRX-4516")]
    ) == ("1,2", "4,16", "GRX-3504,GRX-4516")
