"""Pure unit conversion logic for the Unit Expert MCP server."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from math import isfinite


@dataclass(frozen=True)
class ConversionResult:
    """Structured result returned by converter functions and MCP tools."""

    input_value: float
    input_unit: str
    output_value: float
    output_unit: str
    category: str

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


UnitTable = Mapping[str, float]
AliasTable = Mapping[str, str]

LENGTH_FACTORS: UnitTable = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "ft": 0.3048,
    "yd": 0.9144,
    "mi": 1609.344,
}

WEIGHT_FACTORS: UnitTable = {
    "mg": 0.000001,
    "g": 0.001,
    "kg": 1.0,
    "t": 1000.0,
    "oz": 0.028349523125,
    "lb": 0.45359237,
}

AREA_FACTORS: UnitTable = {
    "mm2": 0.000001,
    "cm2": 0.0001,
    "m2": 1.0,
    "km2": 1_000_000.0,
    "in2": 0.00064516,
    "ft2": 0.09290304,
    "yd2": 0.83612736,
    "acre": 4046.8564224,
}

VOLUME_FACTORS: UnitTable = {
    "ml": 0.001,
    "l": 1.0,
    "m3": 1000.0,
    "in3": 0.016387064,
    "ft3": 28.316846592,
    "cup": 0.2365882365,
    "pt": 0.473176473,
    "qt": 0.946352946,
    "gal": 3.785411784,
    "floz": 0.0295735295625,
}

TEMPERATURE_UNITS = ("c", "f", "k")

ALIASES: AliasTable = {
    # Length
    "millimeter": "mm",
    "millimeters": "mm",
    "centimeter": "cm",
    "centimeters": "cm",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "inch": "in",
    "inches": "in",
    "foot": "ft",
    "feet": "ft",
    "yard": "yd",
    "yards": "yd",
    "mile": "mi",
    "miles": "mi",
    # Weight
    "milligram": "mg",
    "milligrams": "mg",
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "ton": "t",
    "tons": "t",
    "tonne": "t",
    "tonnes": "t",
    "ounce": "oz",
    "ounces": "oz",
    "pound": "lb",
    "pounds": "lb",
    # Temperature
    "celsius": "c",
    "centigrade": "c",
    "fahrenheit": "f",
    "kelvin": "k",
    # Area
    "sqmm": "mm2",
    "squaremillimeter": "mm2",
    "squaremillimeters": "mm2",
    "sqcm": "cm2",
    "squarecentimeter": "cm2",
    "squarecentimeters": "cm2",
    "sqm": "m2",
    "squaremeter": "m2",
    "squaremeters": "m2",
    "squaremetre": "m2",
    "squaremetres": "m2",
    "sqkm": "km2",
    "squarekilometer": "km2",
    "squarekilometers": "km2",
    "sqin": "in2",
    "squareinch": "in2",
    "squareinches": "in2",
    "sqft": "ft2",
    "squarefoot": "ft2",
    "squarefeet": "ft2",
    "sqyd": "yd2",
    "squareyard": "yd2",
    "squareyards": "yd2",
    "acres": "acre",
    # Volume
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "cubicmeter": "m3",
    "cubicmeters": "m3",
    "cubicmetre": "m3",
    "cubicmetres": "m3",
    "cubicinch": "in3",
    "cubicinches": "in3",
    "cubicfoot": "ft3",
    "cubicfeet": "ft3",
    "cups": "cup",
    "pint": "pt",
    "pints": "pt",
    "quart": "qt",
    "quarts": "qt",
    "gallon": "gal",
    "gallons": "gal",
    "fluidounce": "floz",
    "fluidounces": "floz",
}

SUPPORTED_UNITS: Mapping[str, tuple[str, ...]] = {
    "length": tuple(LENGTH_FACTORS),
    "weight": tuple(WEIGHT_FACTORS),
    "temperature": TEMPERATURE_UNITS,
    "area": tuple(AREA_FACTORS),
    "volume": tuple(VOLUME_FACTORS),
}


def convert_length(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert a length value."""
    return _convert_with_factor(value, from_unit, to_unit, "length", LENGTH_FACTORS).to_dict()


def convert_weight(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert a weight value."""
    return _convert_with_factor(value, from_unit, to_unit, "weight", WEIGHT_FACTORS).to_dict()


def convert_area(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert an area value."""
    return _convert_with_factor(value, from_unit, to_unit, "area", AREA_FACTORS).to_dict()


def convert_volume(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert a volume value."""
    return _convert_with_factor(value, from_unit, to_unit, "volume", VOLUME_FACTORS).to_dict()


def convert_temperature(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert a temperature value."""
    numeric_value = _validate_value(value)
    normalized_from = _normalize_unit(from_unit)
    normalized_to = _normalize_unit(to_unit)

    _ensure_supported(normalized_from, TEMPERATURE_UNITS, "temperature")
    _ensure_supported(normalized_to, TEMPERATURE_UNITS, "temperature")

    celsius = _temperature_to_celsius(numeric_value, normalized_from)
    converted = _celsius_to_temperature(celsius, normalized_to)

    return ConversionResult(
        input_value=numeric_value,
        input_unit=normalized_from,
        output_value=converted,
        output_unit=normalized_to,
        category="temperature",
    ).to_dict()


def list_supported_units() -> dict[str, tuple[str, ...]]:
    """Return supported canonical units by category."""
    return dict(SUPPORTED_UNITS)


def _convert_with_factor(
    value: float,
    from_unit: str,
    to_unit: str,
    category: str,
    factors: UnitTable,
) -> ConversionResult:
    numeric_value = _validate_value(value)
    normalized_from = _normalize_unit(from_unit)
    normalized_to = _normalize_unit(to_unit)

    _ensure_supported(normalized_from, factors, category)
    _ensure_supported(normalized_to, factors, category)

    base_value = numeric_value * factors[normalized_from]
    converted = base_value / factors[normalized_to]

    return ConversionResult(
        input_value=numeric_value,
        input_unit=normalized_from,
        output_value=converted,
        output_unit=normalized_to,
        category=category,
    )


def _normalize_unit(unit: str) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("unit must be a non-empty string")

    normalized = unit.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    normalized = normalized.replace("^2", "2").replace("^3", "3")
    return ALIASES.get(normalized, normalized)


def _validate_value(value: float) -> float:
    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be a finite number") from error

    if not isfinite(numeric_value):
        raise ValueError("value must be a finite number")

    return numeric_value


def _ensure_supported(unit: str, supported_units: UnitTable | tuple[str, ...], category: str) -> None:
    if unit not in supported_units:
        choices = ", ".join(supported_units)
        raise ValueError(f"unsupported {category} unit '{unit}'. Supported units: {choices}")


def _temperature_to_celsius(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return (value - 32.0) * 5.0 / 9.0
    if unit == "k":
        return value - 273.15
    raise ValueError(f"unsupported temperature unit '{unit}'")


def _celsius_to_temperature(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return (value * 9.0 / 5.0) + 32.0
    if unit == "k":
        return value + 273.15
    raise ValueError(f"unsupported temperature unit '{unit}'")
