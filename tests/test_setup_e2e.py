"""End-to-End: Cloud-Entry mit allen Plattformen gegen eine gemockte ABB-Cloud.

Deckt ab, was die Einzeltests nicht sehen: dass aus den Antworten von
/v3/users/{abbId}/devices und /v3/users/{abbId}/data/stream tatsächlich
Geräte und Entitäten entstehen, dass Schreibbefehle an die richtigen
/control-Endpunkte gehen und dass die Diagnose keine Geheimnisse verrät.

Die Payloads folgen dem Swagger der myBuildings Energy API (v1.88.11).
"""
import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.abb_smart_ems import async_setup_entry
from custom_components.abb_smart_ems.const import (
    CLOUD_BASE,
    CONF_ABB_ID,
    CONF_API_KEY,
    CONF_MODE,
    DOMAIN,
    MODE_CLOUD,
    PLATFORMS,
)
from custom_components.abb_smart_ems.diagnostics import (
    async_get_config_entry_diagnostics,
)

ABB_ID = "002A0001"
API_KEY = "super-secret-key"
MAC = "29:14:15:14:11:3D"

AUTH_URL = f"{CLOUD_BASE}/v3/auth/refresh"
STREAM_URL = f"{CLOUD_BASE}/v3/users/{ABB_ID}/data/stream"
DEVICES_URL = f"{CLOUD_BASE}/v3/users/{ABB_ID}/devices"
STATS_URL = f"{CLOUD_BASE}/v1/statistics/gateways/{ABB_ID}"

BAT = "aaaaaaaaaaaaaaaaaaaaaaa1"
SWITCH = "aaaaaaaaaaaaaaaaaaaaaaa2"
WALLBOX = "aaaaaaaaaaaaaaaaaaaaaaa3"
METER = "aaaaaaaaaaaaaaaaaaaaaaa4"

DEVICES = [
    {
        "_id": BAT,
        "type": "battery",
        "tag": "Hausspeicher",
        "deviceKey": "BYD HVS",
        "mac": MAC,
        "ip": "192.168.1.17",
        "priority": 3,
        "data": {
            "batteryMode": 1,
            "batteryManualMode": 2,
            "upperSocLimit": 95,
            "lowerSocLimit": 15,
            "dischargeSocLimit": 30,
            "morningSocLimit": 80,
            "chargingSocLimit": 100,
            "tariffPriceLimit": 0.25,
        },
    },
    {
        "_id": SWITCH,
        "type": "switches",
        "tag": "Poolpumpe",
        "data": {"chargingMode": 2},
    },
    {
        "_id": WALLBOX,
        "type": "carcharging",
        "tag": "Wallbox Garage",
        "deviceKey": "ABB Terra AC",
        "data": {"chargingMode": 1, "constantCurrentSetting": 10},
    },
    # Typ ohne Steuerung: bekommt Sensoren, aber kein Select/Number
    {"_id": METER, "type": "smartmeter", "tag": "Hauszähler"},
]

STREAM = {
    "v": 3,
    "t": "2026-08-01T10:00:00Z",
    "iv": 10,
    "pW": 4200,
    "cW": 1500,
    "bcW": 700,
    "bdW": 0,
    "iW": 0,
    "eW": 2000,
    "soc": 64,
    "devices": [
        {"_id": BAT, "signal": "connected", "power": 700, "soc": 64},
        {"_id": SWITCH, "signal": "connected", "power": 350, "switchState": 1},
        {"_id": WALLBOX, "signal": "not connected", "power": 0},
        {"_id": METER, "signal": "connected", "power": -2000},
    ],
}

STATS = {
    "production": 12000,
    "consumption": 8000,
    "selfConsumption": 6000,
    "selfConsumptionRate": 50.0,
    "autarchyDegree": 75.0,
}


@pytest.fixture
async def setup(hass, aioclient_mock):
    """Cloud-Entry vollständig aufsetzen (inkl. aller Plattformen)."""
    await hass.config.async_update(currency="SEK")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MODE: MODE_CLOUD, CONF_ABB_ID: ABB_ID, CONF_API_KEY: API_KEY},
        unique_id=f"{DOMAIN}_{ABB_ID}",
        title=f"ABB Smart EMS {ABB_ID}",
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    aioclient_mock.post(
        AUTH_URL,
        json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600},
    )
    aioclient_mock.get(DEVICES_URL, json=DEVICES)
    aioclient_mock.get(STREAM_URL, json=STREAM)
    aioclient_mock.get(STATS_URL, json=STATS)

    # Wie HA selbst: Plattformen werden unter gehaltenem setup_lock weitergereicht.
    # (ConfigEntries.async_setup ist in conftest.py global gemockt, daher direkt.)
    async with entry.setup_lock:
        assert await async_setup_entry(hass, entry) is True
    entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await hass.async_block_till_done()


def _state(hass, entry, platform: str, uid_suffix: str):
    """State einer Entität über ihre unique_id auflösen (sprachunabhängig)."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{uid_suffix}"
    )
    assert entity_id is not None, f"no {platform} entity with unique_id …_{uid_suffix}"
    return hass.states.get(entity_id)


def _has_entity(hass, entry, platform: str, uid_suffix: str) -> bool:
    return (
        er.async_get(hass).async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{uid_suffix}"
        )
        is not None
    )


def _puts(aioclient_mock) -> list[tuple[str, dict]]:
    return [
        (url.path, body)
        for method, url, body, *_ in aioclient_mock.mock_calls
        if method == "PUT"
    ]


async def test_site_sensors_from_stream_and_statistics(hass, setup):
    entry = setup

    assert float(_state(hass, entry, "sensor", "pW").state) == 4200
    assert float(_state(hass, entry, "sensor", "cW").state) == 1500
    assert float(_state(hass, entry, "sensor", "batW").state) == 700
    assert float(_state(hass, entry, "sensor", "gridW").state) == -2000
    # PV-Überschuss = 4200 − 1500 − 700
    assert float(_state(hass, entry, "sensor", "surplusW").state) == 2000
    assert float(_state(hass, entry, "sensor", "soc").state) == 64
    assert float(_state(hass, entry, "sensor", "stat_production").state) == 12000
    assert float(_state(hass, entry, "sensor", "stat_autarchy_degree").state) == 75.0


async def test_devices_are_named_and_linked_to_the_gateway(hass, setup):
    entry = setup
    registry = dr.async_get(hass)
    by_identifier = {
        ident: dev
        for dev in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for (_, ident) in dev.identifiers
    }

    site = by_identifier[f"site_{ABB_ID}"]
    assert site.manufacturer == "ABB"
    assert site.model == "Energy Edge EX.1 (Cloud API)"
    assert site.name == f"ABB Smart EMS {ABB_ID}"

    battery = by_identifier[f"device_{BAT}"]
    assert battery.name == "Hausspeicher"
    assert battery.model == "BYD HVS"
    assert battery.manufacturer is None
    assert battery.via_device_id == site.id

    assert by_identifier[f"device_{SWITCH}"].name == "Poolpumpe"
    assert by_identifier[f"device_{WALLBOX}"].name == "Wallbox Garage"
    assert by_identifier[f"device_{METER}"].name == "Hauszähler"


async def test_device_sensors_and_connectivity(hass, setup):
    entry = setup

    assert float(_state(hass, entry, "sensor", f"dev_{BAT}_power").state) == 700
    assert float(_state(hass, entry, "sensor", f"dev_{BAT}_soc").state) == 64
    assert float(_state(hass, entry, "sensor", f"dev_{METER}_power").state) == -2000
    assert _state(hass, entry, "sensor", f"dev_{SWITCH}_switchState").state == "1"
    assert _state(hass, entry, "binary_sensor", f"dev_{BAT}_signal").state == "on"
    assert _state(hass, entry, "binary_sensor", f"dev_{WALLBOX}_signal").state == "off"


async def test_controls_follow_abb_device_types(hass, setup):
    """Steuer-Entitäten entstehen anhand der Typnamen der ABB-Cloud
    (battery, switches, carcharging); unbekannte Typen bekommen keine."""
    entry = setup

    assert _state(hass, entry, "select", f"dev_{BAT}_mode").state == "Eco"
    assert _state(hass, entry, "select", f"dev_{BAT}_batteryManualMode").state == "AUS"
    assert _state(hass, entry, "select", f"dev_{SWITCH}_mode").state == "AUS"
    assert _state(hass, entry, "select", f"dev_{WALLBOX}_mode").state == "Nur Solar"
    assert float(_state(hass, entry, "number", f"dev_{WALLBOX}_constantCurrentSetting").state) == 10
    assert float(_state(hass, entry, "number", f"bat_{BAT}_dischargeSocLimit").state) == 30

    assert not _has_entity(hass, entry, "select", f"dev_{METER}_mode")


async def test_price_limit_uses_home_assistant_currency(hass, setup):
    """Preislimit in der in HA eingestellten Währung statt fest in CHF wie in der Vorlage.

    SEK als Testwährung: weder der alte feste Wert noch der HA-Default (EUR).
    """
    state = _state(hass, setup, "number", f"dev_{BAT}_tariffPriceLimit")

    assert float(state.state) == 0.25
    assert state.attributes["unit_of_measurement"] == "SEK/kWh"


async def test_switch_mode_is_written_to_switch_endpoint(hass, setup, aioclient_mock):
    entry = setup
    aioclient_mock.put(f"{CLOUD_BASE}/v1/control/switch/{SWITCH}", status=204)
    entity_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_dev_{SWITCH}_mode"
    )

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "Nur Solar"},
        blocking=True,
    )

    assert _puts(aioclient_mock) == [
        (f"/v1/control/switch/{SWITCH}", {"chargingMode": 3})
    ]
    assert hass.states.get(entity_id).state == "Nur Solar"


async def test_battery_mode_is_written_as_full_settings_object(hass, setup, aioclient_mock):
    """Batterie: read-modify-write, damit die Cloud keine anderen Felder auf Defaults setzt."""
    entry = setup
    aioclient_mock.put(f"{CLOUD_BASE}/v2/control/battery/{BAT}", status=204)
    entity_id = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_dev_{BAT}_mode"
    )

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": "Peak-Shaving"},
        blocking=True,
    )

    assert _puts(aioclient_mock) == [
        (
            f"/v2/control/battery/{BAT}",
            {**DEVICES[0]["data"], "batteryMode": 2},
        )
    ]


async def test_wallbox_current_carries_charging_mode(hass, setup, aioclient_mock):
    """Wallbox: chargingMode ist im PUT Pflicht und wird aus den Metadaten mitgesendet."""
    entry = setup
    aioclient_mock.put(f"{CLOUD_BASE}/v1/control/car-charger/{WALLBOX}", status=204)
    entity_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{entry.entry_id}_dev_{WALLBOX}_constantCurrentSetting"
    )

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 16}, blocking=True
    )

    assert _puts(aioclient_mock) == [
        (
            f"/v1/control/car-charger/{WALLBOX}",
            {"constantCurrentSetting": 16, "chargingMode": 1},
        )
    ]


async def test_diagnostics_contain_no_secrets(hass, setup):
    """Diagnose-Dateien werden öffentlich in Issues geteilt: kein Key, keine
    Gateway-ID, kein Token, keine MAC-Adresse."""
    diag = await async_get_config_entry_diagnostics(hass, setup)
    dump = json.dumps(diag, default=str)

    assert API_KEY not in dump
    assert ABB_ID not in dump
    assert MAC not in dump
    assert '"tok"' not in dump
    # Die für die Fehlersuche nötigen Strukturdaten bleiben erhalten
    assert diag["device_metadata"][BAT]["type"] == "battery"
    assert diag["device_metadata"][BAT]["raw"]["data"]["batteryMode"] == 1
    assert diag["client"]["mode"] == "cloud"
