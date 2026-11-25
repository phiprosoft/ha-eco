"""Tests for persisted configuration models."""

from math import inf, nan

from custom_components.electricity_lens.models import _numbers


def test_config_numbers_ignore_non_finite_values() -> None:
    """Malformed numeric values must not enter calculation timelines."""
    assert _numbers(
        {
            "valid": 3.7,
            "nan": nan,
            "positive_infinity": inf,
            "negative_infinity": -inf,
            "boolean": True,
            "text": "3.7",
        }
    ) == {"valid": 3.7}
