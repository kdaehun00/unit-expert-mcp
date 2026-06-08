from __future__ import annotations

import math

import pytest

from unit_expert_mcp.converter import (
    convert_area,
    convert_length,
    convert_temperature,
    convert_volume,
    convert_weight,
    list_supported_units,
)


def assert_close(actual: float, expected: float) -> None:
    assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)


def test_convert_length_uses_aliases() -> None:
    result = convert_length(1, "meter", "cm")

    assert result["input_unit"] == "m"
    assert result["output_unit"] == "cm"
    assert_close(float(result["output_value"]), 100.0)


def test_convert_weight_pounds_to_kg() -> None:
    result = convert_weight(10, "lb", "kg")

    assert result["category"] == "weight"
    assert_close(float(result["output_value"]), 4.5359237)


def test_convert_temperature_f_to_c() -> None:
    result = convert_temperature(32, "fahrenheit", "celsius")

    assert result["input_unit"] == "f"
    assert result["output_unit"] == "c"
    assert_close(float(result["output_value"]), 0.0)


def test_convert_area_acre_to_m2() -> None:
    result = convert_area(1, "acre", "m2")

    assert_close(float(result["output_value"]), 4046.8564224)


def test_convert_volume_gallon_to_liter() -> None:
    result = convert_volume(1, "gallon", "l")

    assert_close(float(result["output_value"]), 3.785411784)


def test_list_supported_units() -> None:
    result = list_supported_units()

    assert "length" in result
    assert "temperature" in result
    assert "m" in result["length"]
    assert "k" in result["temperature"]


def test_rejects_unknown_unit() -> None:
    with pytest.raises(ValueError, match="unsupported length unit"):
        convert_length(1, "parsec", "m")


def test_rejects_non_finite_value() -> None:
    with pytest.raises(ValueError, match="finite number"):
        convert_weight(float("nan"), "kg", "g")
