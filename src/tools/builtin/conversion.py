"""Production-grade deterministic unit conversion tool supporting 8 primary measurement categories."""

import logging
from typing import Any

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

# Unit conversion factors to base unit per category
# Value * factor = base_value; base_value / factor = target_value

_LENGTH_FACTORS: dict[str, float] = {
    "meter": 1.0,
    "kilometer": 1000.0,
    "centimeter": 0.01,
    "millimeter": 0.001,
    "micrometer": 1e-6,
    "nanometer": 1e-9,
    "mile": 1609.344,
    "yard": 0.9144,
    "foot": 0.3048,
    "inch": 0.0254,
    "nautical_mile": 1852.0,
}

_WEIGHT_FACTORS: dict[str, float] = {
    "kilogram": 1.0,
    "gram": 0.001,
    "milligram": 1e-6,
    "metric_ton": 1000.0,
    "pound": 0.45359237,
    "ounce": 0.028349523125,
    "stone": 6.35029318,
}

_VOLUME_FACTORS: dict[str, float] = {
    "liter": 1.0,
    "milliliter": 0.001,
    "cubic_meter": 1000.0,
    "gallon_us": 3.785411784,
    "gallon_uk": 4.54609,
    "quart": 0.946352946,
    "pint": 0.473176473,
    "cup": 0.24,
    "fluid_ounce": 0.0295735295625,
    "tablespoon": 0.01478676478125,
    "teaspoon": 0.00492892159375,
}

_AREA_FACTORS: dict[str, float] = {
    "square_meter": 1.0,
    "square_kilometer": 1e6,
    "square_centimeter": 0.0001,
    "square_foot": 0.09290304,
    "square_yard": 0.83612736,
    "acre": 4046.8564224,
    "hectare": 10000.0,
    "square_mile": 2589988.110336,
}

_SPEED_FACTORS: dict[str, float] = {
    "meter_per_second": 1.0,
    "kilometer_per_hour": 1.0 / 3.6,
    "mile_per_hour": 0.44704,
    "knot": 0.5144444444444445,
}

_TIME_FACTORS: dict[str, float] = {
    "second": 1.0,
    "millisecond": 0.001,
    "microsecond": 1e-6,
    "minute": 60.0,
    "hour": 3600.0,
    "day": 86400.0,
    "week": 604800.0,
    "month": 2629800.0,  # Average 30.4375 days
    "year": 31557600.0,  # Average 365.25 days
}

_DIGITAL_FACTORS: dict[str, float] = {
    "byte": 1.0,
    "kilobyte": 1e3,
    "megabyte": 1e6,
    "gigabyte": 1e9,
    "terabyte": 1e12,
    "petabyte": 1e15,
    "kibibyte": 1024.0,
    "mebibyte": 1048576.0,
    "gibibyte": 1073741824.0,
    "tebibyte": 1099511627776.0,
    "pebibyte": 1125899906842624.0,
    "bit": 0.125,
    "kilobit": 125.0,
    "megabit": 125000.0,
    "gigabit": 125000000.0,
}

# Unit alias mappings for user convenience
_ALIASES: dict[str, str] = {
    # Length
    "m": "meter", "meters": "meter", "metre": "meter", "metres": "meter",
    "km": "kilometer", "kilometers": "kilometer", "kilometre": "kilometer",
    "cm": "centimeter", "centimeters": "centimeter",
    "mm": "millimeter", "millimeters": "millimeter",
    "um": "micrometer", "micrometers": "micrometer", "micron": "micrometer",
    "nm": "nanometer", "nanometers": "nanometer",
    "mi": "mile", "miles": "mile",
    "yd": "yard", "yards": "yard",
    "ft": "foot", "feet": "foot",
    "in": "inch", "inches": "inch",
    "nmi": "nautical_mile", "nautical_miles": "nautical_mile",
    # Weight
    "kg": "kilogram", "kilograms": "kilogram", "kilos": "kilogram",
    "g": "gram", "grams": "gram",
    "mg": "milligram", "milligrams": "milligram",
    "t": "metric_ton", "ton": "metric_ton", "tons": "metric_ton", "tonne": "metric_ton",
    "lb": "pound", "lbs": "pound", "pounds": "pound",
    "oz": "ounce", "ounces": "ounce",
    "st": "stone", "stones": "stone",
    # Temperature
    "c": "celsius", "degc": "celsius", "centigrade": "celsius",
    "f": "fahrenheit", "degf": "fahrenheit",
    "k": "kelvin", "degk": "kelvin",
    # Volume
    "l": "liter", "liters": "liter", "litre": "liter", "litres": "liter",
    "ml": "milliliter", "milliliters": "milliliter",
    "m3": "cubic_meter", "cubic_meters": "cubic_meter",
    "gal": "gallon_us", "gallon": "gallon_us", "gallons": "gallon_us", "gal_us": "gallon_us",
    "gal_uk": "gallon_uk", "uk_gallon": "gallon_uk",
    "qt": "quart", "quarts": "quart",
    "pt": "pint", "pints": "pint",
    "cups": "cup",
    "fl_oz": "fluid_ounce", "floz": "fluid_ounce",
    "tbsp": "tablespoon", "tablespoons": "tablespoon",
    "tsp": "teaspoon", "teaspoons": "teaspoon",
    # Area
    "sq_m": "square_meter", "sqm": "square_meter", "m2": "square_meter",
    "sq_km": "square_kilometer", "sqkm": "square_kilometer", "km2": "square_kilometer",
    "sq_ft": "square_foot", "sqft": "square_foot", "ft2": "square_foot",
    "sq_yd": "square_yard", "sqyd": "square_yard", "yd2": "square_yard",
    "ac": "acre", "acres": "acre",
    "ha": "hectare", "hectares": "hectare",
    "sq_mi": "square_mile", "sqmi": "square_mile", "mi2": "square_mile",
    # Speed
    "m/s": "meter_per_second", "mps": "meter_per_second",
    "km/h": "kilometer_per_hour", "kmh": "kilometer_per_hour", "kph": "kilometer_per_hour",
    "mph": "mile_per_hour",
    "kn": "knot", "knots": "knot",
    # Time
    "s": "second", "sec": "second", "seconds": "second",
    "ms": "millisecond", "milliseconds": "millisecond",
    "us": "microsecond", "microseconds": "microsecond",
    "min": "minute", "minutes": "minute",
    "h": "hour", "hr": "hour", "hrs": "hour", "hours": "hour",
    "d": "day", "days": "day",
    "wk": "week", "weeks": "week",
    "mo": "month", "months": "month",
    "yr": "year", "yrs": "year", "years": "year",
    # Digital Storage
    "b": "byte", "bytes": "byte",
    "kb": "kilobyte", "kbytes": "kilobyte",
    "mb": "megabyte", "mbytes": "megabyte",
    "gb": "gigabyte", "gbytes": "gigabyte",
    "tb": "terabyte", "tbytes": "terabyte",
    "pb": "petabyte", "pbytes": "petabyte",
    "kib": "kibibyte",
    "mib": "mebibyte",
    "gib": "gibibyte",
    "tib": "tebibyte",
    "pib": "pebibyte",
    "bit": "bit", "bits": "bit",
    "kbit": "kilobit", "kbps": "kilobit",
    "mbit": "megabit", "mbps": "megabit",
    "gbit": "gigabit", "gbps": "gigabit",
}

_CATEGORIES: dict[str, dict[str, float]] = {
    "length": _LENGTH_FACTORS,
    "weight": _WEIGHT_FACTORS,
    "volume": _VOLUME_FACTORS,
    "area": _AREA_FACTORS,
    "speed": _SPEED_FACTORS,
    "time": _TIME_FACTORS,
    "digital_storage": _DIGITAL_FACTORS,
}


def normalize_unit_name(raw: str) -> str:
    """Normalize user unit string to canonical name."""
    clean = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(clean, clean)


def find_category_for_unit(unit: str) -> str | None:
    """Identify which category contains the normalized unit."""
    if unit in ("celsius", "fahrenheit", "kelvin"):
        return "temperature"
    for cat_name, factors in _CATEGORIES.items():
        if unit in factors:
            return cat_name
    return None


def convert_temperature(value: float, from_u: str, to_u: str) -> tuple[float, str]:
    """Perform deterministic temperature conversion."""
    # Convert from_u to Celsius first
    if from_u == "celsius":
        celsius = value
    elif from_u == "fahrenheit":
        celsius = (value - 32.0) * (5.0 / 9.0)
    elif from_u == "kelvin":
        celsius = value - 273.15
    else:
        raise ValueError(f"Unsupported source temperature unit '{from_u}'.")

    # Convert Celsius to to_u
    if to_u == "celsius":
        res = celsius
        formula = "C = C" if from_u == "celsius" else "(F - 32) * 5/9" if from_u == "fahrenheit" else "K - 273.15"
    elif to_u == "fahrenheit":
        res = (celsius * (9.0 / 5.0)) + 32.0
        formula = "(C * 9/5) + 32"
    elif to_u == "kelvin":
        res = celsius + 273.15
        formula = "C + 273.15"
    else:
        raise ValueError(f"Unsupported destination temperature unit '{to_u}'.")

    return res, formula


class ConversionTool(BaseTool):
    """Production-grade deterministic unit conversion tool supporting 8 primary measurement categories."""

    @property
    def name(self) -> str:
        return "conversion_tool"

    @property
    def description(self) -> str:
        return (
            "Deterministically converts quantities between compatible measurement units: "
            "length, weight/mass, temperature, volume, area, speed, time, and digital storage. "
            "Requires explicit source and destination units. Prevents invalid cross-category conversions."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "value": {
                    "type": "number",
                    "description": "The numeric magnitude or quantity to convert (e.g. 100, 3.14).",
                },
                "from_unit": {
                    "type": "string",
                    "description": "Source unit (e.g. 'meters', 'lbs', 'celsius', 'GB', 'mph', 'hours').",
                },
                "to_unit": {
                    "type": "string",
                    "description": "Target destination unit (e.g. 'feet', 'kg', 'fahrenheit', 'MB', 'km/h', 'minutes').",
                },
                "precision": {
                    "type": "integer",
                    "description": "Number of decimal places in the returned result (default 4, max 10).",
                },
            },
            "required": ["value", "from_unit", "to_unit"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        val = kwargs.get("value")
        if val is None:
            raise ValueError("The 'value' parameter is required.")
        try:
            numeric_val = float(val)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Parameter 'value' must be a numeric number (got {val}).") from exc

        raw_from = kwargs.get("from_unit")
        raw_to = kwargs.get("to_unit")
        if not raw_from or not raw_to:
            raise ValueError("Both 'from_unit' and 'to_unit' must be provided.")

        from_norm = normalize_unit_name(raw_from)
        to_norm = normalize_unit_name(raw_to)

        from_cat = find_category_for_unit(from_norm)
        to_cat = find_category_for_unit(to_norm)

        if not from_cat:
            raise ValueError(f"Unknown or unsupported source unit '{raw_from}'.")
        if not to_cat:
            raise ValueError(f"Unknown or unsupported destination unit '{raw_to}'.")

        if from_cat != to_cat:
            raise ValueError(
                f"Incompatible unit conversion: Cannot convert '{raw_from}' ({from_cat}) "
                f"to '{raw_to}' ({to_cat}). Both units must belong to the same physical category."
            )

        precision = min(max(int(kwargs.get("precision", 4)), 0), 10)

        # Handle Temperature
        if from_cat == "temperature":
            result, formula = convert_temperature(numeric_val, from_norm, to_norm)
            rounded = round(result, precision)
            return {
                "action": "convert",
                "category": "temperature",
                "original_value": numeric_val,
                "from_unit": from_norm,
                "converted_value": rounded,
                "to_unit": to_norm,
                "formula": formula,
                "formatted": f"{numeric_val} {from_norm} = {rounded} {to_norm}",
            }

        # Handle standard factor-based conversion
        factors = _CATEGORIES[from_cat]
        from_factor = factors[from_norm]
        to_factor = factors[to_norm]

        # Convert to base unit, then to target unit
        base_value = numeric_val * from_factor
        converted = base_value / to_factor
        rounded = round(converted, precision)
        ratio = from_factor / to_factor

        return {
            "action": "convert",
            "category": from_cat,
            "original_value": numeric_val,
            "from_unit": from_norm,
            "converted_value": rounded,
            "to_unit": to_norm,
            "conversion_factor": round(ratio, 8),
            "formatted": f"{numeric_val} {from_norm} = {rounded} {to_norm}",
        }

