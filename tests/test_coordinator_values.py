"""Tests for numeric sensor values used by the coordinator."""

from homeassistant.core import HomeAssistant

from custom_components.electricity_lens.coordinator import _number


async def test_live_sensor_rejects_non_finite_values(hass: HomeAssistant) -> None:
    """NaN and infinity are unavailable, not valid prices or measurements."""
    hass.states.async_set("sensor.valid", "3.7")
    hass.states.async_set("sensor.nan", "nan")
    hass.states.async_set("sensor.infinity", "inf")
    hass.states.async_set("sensor.unknown", "unavailable")

    assert _number(hass, "sensor.valid") == 3.7
    assert _number(hass, "sensor.nan") is None
    assert _number(hass, "sensor.infinity") is None
    assert _number(hass, "sensor.unknown") is None
    assert _number(hass, "sensor.missing") is None
