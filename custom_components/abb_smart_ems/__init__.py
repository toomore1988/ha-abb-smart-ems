# __init__.py
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)

from .const import DOMAIN, PLATFORMS
from .coordinator import SmartEmsCoordinator, daily_store
from .entity import site_device_info

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ABB Smart EMS from a config entry."""
    coord = SmartEmsCoordinator(hass, entry)

    await coord.async_config_entry_first_refresh()

    entry.runtime_data = coord

    # Site-Gerät explizit registrieren bevor Plattformen geladen werden, damit die
    # Kind-Geräte in allen Plattformen darauf verweisen können. Die Registry-ID
    # merken: child_device_info() verknüpft darüber via via_device_id (ab HA 2026.8).
    info = site_device_info(coord)
    registry = dr.async_get(hass)
    site_device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=info["identifiers"],
        name=info["name"],
        manufacturer=info["manufacturer"],
        model=info["model"],
    )
    coord.site_device_id = site_device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload ABB Smart EMS config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Persistierte Tageszähler löschen, wenn der Eintrag entfernt wird."""
    await daily_store(hass, entry.entry_id).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing a device if it is no longer reported by the API."""
    coord: SmartEmsCoordinator | None = getattr(config_entry, "runtime_data", None)
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        if identifier.startswith("site_"):
            # Das aktuelle Site-Gerät gehört fest zum Config-Entry. Nach einem
            # Moduswechsel (Cloud <-> Lokal) ändert sich site_id jedoch, und
            # das alte Site-Gerät bleibt als Karteileiche zurück -- die darf
            # entfernt werden.
            if coord is None:
                return False
            return identifier != f"site_{coord.site_id}"
        if identifier.startswith("device_"):
            if coord is None:
                # Entry nicht geladen (z. B. Setup-Fehler) — keine Metadaten,
                # Entfernen erlauben; Geräte werden beim nächsten Setup neu angelegt
                return True
            dev_id = identifier[len("device_"):]
            return dev_id not in coord.device_meta
    return True
