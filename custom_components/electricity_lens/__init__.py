"""The Electricity Lens integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import (
    StaticPathConfig,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from . import websocket_api
from .const import (
    CARD_FILENAME,
    CONF_HISTORICAL_DATA,
    DOMAIN,
    FRONTEND_URL,
    HISTORICAL_BATTERY_CHARGE,
    HISTORICAL_BATTERY_DISCHARGE,
    HISTORICAL_COMBUSTION_CONSUMPTION,
    HISTORICAL_ELECTRICITY,
    HISTORICAL_EV_CONSUMPTION,
    HISTORICAL_EXPORT,
    HISTORICAL_HEAT,
    HISTORICAL_SCOP,
    HISTORICAL_SINCE,
    HISTORICAL_SOLAR,
    HISTORICAL_UNTIL,
    RETIRED_OPTIONS,
    SIGNAL_ENERGY_PREFS_UPDATED,
)
from .coordinator import (
    ElectricityLensConfigEntry,
    ElectricityLensCoordinator,
    async_remove_issues,
    coordinator_store,
)
from .sources import (
    EnergyInterfaceError,
    async_get_energy_setup,
    async_listen_energy_updates,
    async_remove_statistics,
    async_statistic_ids,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the card and follow changes of the energy dashboard."""
    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(Path(__file__).parent / "frontend"))]
    )
    # The version in the URL makes browsers fetch the card again after an update.
    frontend.add_extra_js_url(
        hass, f"{FRONTEND_URL}/{CARD_FILENAME}?v={integration.version}"
    )

    websocket_api.async_register(hass)

    async def _energy_prefs_updated() -> None:
        async_dispatcher_send(hass, SIGNAL_ENERGY_PREFS_UPDATED)

    try:
        await async_listen_energy_updates(hass, _energy_prefs_updated)
    except EnergyInterfaceError:
        # The coordinator raises a repair issue for this on its first update.
        _LOGGER.debug("Cannot follow energy dashboard changes", exc_info=True)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ElectricityLensConfigEntry
) -> bool:
    """Set up Electricity Lens from a config entry."""
    try:
        await async_get_energy_setup(hass)
    except EnergyInterfaceError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="energy_interface",
            translation_placeholders={"error": str(err)},
        ) from err

    coordinator = ElectricityLensCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _energy_prefs_updated() -> None:
        """Compute what changed; add or remove the forecast sensors."""
        try:
            forecasts = bool((await async_get_energy_setup(hass)).forecasts)
        except EnergyInterfaceError:
            forecasts = coordinator.forecast_sensors
        if forecasts != coordinator.forecast_sensors:
            hass.config_entries.async_schedule_reload(entry.entry_id)
        else:
            await coordinator.async_request_refresh()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_ENERGY_PREFS_UPDATED, _energy_prefs_updated
        )
    )
    # Computing the past can take a moment, so setup does not wait for it.
    # Until then the sensors say that they are calculating.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} initial calculation"
    )
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: ElectricityLensConfigEntry
) -> None:
    """Reload when a category was added, changed, or removed."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant, entry: ElectricityLensConfigEntry
) -> bool:
    """Bring an entry up to date, or refuse one this version cannot read.

    Entries of version 1.1, from before 0.12.0, are no longer migrated: they
    fail with a migration error and are set up again. 1.3 drops the price
    level thresholds, which the grid price replaced.
    """
    if entry.version != 1 or entry.minor_version < 2:
        return False

    minor_version = entry.minor_version
    options = entry.options
    if minor_version == 2:
        options = {
            key: value for key, value in options.items() if key not in RETIRED_OPTIONS
        }
        minor_version = 3

    if minor_version == 3:
        # Before 0.35.6, aggregate energy totals were not given an explicit
        # end timestamp. The user-entered totals cover the selected date
        # through the upgrade, so persist that cutoff before resuming meters.
        energy_keys = (
            HISTORICAL_SOLAR,
            HISTORICAL_EXPORT,
            HISTORICAL_BATTERY_CHARGE,
            HISTORICAL_BATTERY_DISCHARGE,
            HISTORICAL_ELECTRICITY,
            HISTORICAL_HEAT,
            HISTORICAL_SCOP,
            HISTORICAL_EV_CONSUMPTION,
            HISTORICAL_COMBUSTION_CONSUMPTION,
        )
        cutoff = dt_util.utcnow().timestamp()
        for subentry in tuple(entry.subentries.values()):
            historical = subentry.data.get(CONF_HISTORICAL_DATA)
            if not isinstance(historical, dict) or HISTORICAL_UNTIL in historical:
                continue
            since = historical.get(HISTORICAL_SINCE)
            if (
                not isinstance(since, int | float)
                or isinstance(since, bool)
                or since <= 0
                or not any(key in historical for key in energy_keys)
            ):
                continue
            data = {
                **subentry.data,
                CONF_HISTORICAL_DATA: {
                    **historical,
                    HISTORICAL_UNTIL: cutoff,
                },
            }
            hass.config_entries.async_update_subentry(entry, subentry, data=data)
        minor_version = 4

    if minor_version != entry.minor_version or options != entry.options:
        hass.config_entries.async_update_entry(
            entry, options=options, minor_version=minor_version
        )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ElectricityLensConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: ElectricityLensConfigEntry
) -> None:
    """Remove the statistics the integration wrote, its progress, and its issues."""
    async_remove_issues(hass)
    async_remove_statistics(hass, await async_statistic_ids(hass))
    await coordinator_store(hass, entry).async_remove()
