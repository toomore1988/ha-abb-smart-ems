"""Tests für Coordinator-Kernlogik: Batterie-Dedup, Merged-PUT-Guard, Auth-Mapping,
Geräte-Metadaten aus /v3/users/{abbId}/devices und Key-Rotation."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.abb_smart_ems.const import (
    CLOUD_BASE,
    CONF_ABB_ID,
    CONF_API_KEY,
    CONF_HOST,
    CONF_MODE,
    CONF_SCHEME,
    DOMAIN,
    MODE_CLOUD,
    MODE_LOCAL,
)
from custom_components.abb_smart_ems.coordinator import SmartEmsCoordinator

HOST = "192.168.1.100"
POINT_URL = f"http://{HOST}/v2/point"
DEVICES_URL = f"http://{HOST}/v2/devices"
LOGIN_URL = f"{CLOUD_BASE}/v3/auth/refresh"

ABB_ID = "002A0001"
STREAM_URL = f"{CLOUD_BASE}/v3/users/{ABB_ID}/data/stream"
CLOUD_DEVICES_URL = f"{CLOUD_BASE}/v3/users/{ABB_ID}/devices"
STATS_URL = f"{CLOUD_BASE}/v1/statistics/gateways/{ABB_ID}"
LOGIN_RESPONSE = {
    "access_token": "tok",
    "token_type": "Bearer",
    "expires_in": 3600,
}
EMPTY_STATS = {
    "production": 0,
    "consumption": 0,
    "selfConsumption": 0,
    "selfConsumptionRate": None,
    "autarchyDegree": None,
}


def _local_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MODE: MODE_LOCAL, CONF_HOST: HOST, CONF_SCHEME: "http"},
        unique_id=f"local_{HOST}",
    )
    entry.add_to_hass(hass)
    return entry


def _cloud_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_MODE: MODE_CLOUD,
            CONF_ABB_ID: ABB_ID,
            CONF_API_KEY: "key-1",
        },
        unique_id=f"{DOMAIN}_{ABB_ID}",
    )
    entry.add_to_hass(hass)
    return entry


async def test_battery_daily_sum_dedups_same_stream_point(hass, aioclient_mock):
    """Gleicher Stream-Timestamp t → bcWh/bdWh nicht doppelt summieren."""
    entry = _local_entry(hass)
    aioclient_mock.get(DEVICES_URL, json=[])
    aioclient_mock.get(
        POINT_URL,
        json={"t": "2026-07-05T10:00:00Z", "iv": 10, "bcWh": 5.0, "bdWh": 1.0},
    )

    coord = SmartEmsCoordinator(hass, entry)
    await coord.async_refresh()
    assert coord.last_update_success
    assert coord.data["stat_bat_charge"] == 5.0
    assert coord.data["stat_bat_discharge"] == 1.0

    # Zweiter Poll mit identischem t → Summen unverändert
    await coord.async_refresh()
    assert coord.data["stat_bat_charge"] == 5.0
    assert coord.data["stat_bat_discharge"] == 1.0

    # Neuer Datenpunkt (anderes t) → Summen wachsen
    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES_URL, json=[])
    aioclient_mock.get(
        POINT_URL,
        json={"t": "2026-07-05T10:00:10Z", "iv": 10, "bcWh": 5.0, "bdWh": 1.0},
    )
    await coord.async_refresh()
    assert coord.data["stat_bat_charge"] == 10.0
    assert coord.data["stat_bat_discharge"] == 2.0


async def test_battery_daily_sum_skips_points_without_timestamp(hass, aioclient_mock):
    """Punkte ohne Stream-Timestamp t → nicht summieren (kein Dedup möglich)."""
    entry = _local_entry(hass)
    aioclient_mock.get(DEVICES_URL, json=[])
    aioclient_mock.get(POINT_URL, json={"iv": 10, "bcWh": 5.0, "bdWh": 1.0})

    coord = SmartEmsCoordinator(hass, entry)
    await coord.async_refresh()
    await coord.async_refresh()

    assert coord.last_update_success
    assert coord.data["stat_bat_charge"] == 0.0
    assert coord.data["stat_bat_discharge"] == 0.0


async def test_cloud_grid_import_zero_at_night_despite_rest_stats_consumption(hass, aioclient_mock):
    """iW=0 (Netz unbeteiligt, Batterie deckt Verbrauch) → stat_grid_import bleibt 0,
    auch wenn die REST-Tagesstatistik consumption>0 und selfConsumption=0 zeigt (Upstream ha-solarmanager, Issue #25)."""
    entry = _cloud_entry(hass)
    aioclient_mock.post(LOGIN_URL, json=LOGIN_RESPONSE)
    aioclient_mock.get(CLOUD_DEVICES_URL, json=[])
    aioclient_mock.get(STREAM_URL, json={"t": "2026-08-01T02:00:00Z", "iv": 10, "iW": 0, "eW": 0})
    aioclient_mock.get(
        STATS_URL,
        json={
            "production": 0,
            "consumption": 500,
            "selfConsumption": 0,
            "selfConsumptionRate": 0,
            "autarchyDegree": 100,
        },
    )

    coord = SmartEmsCoordinator(hass, entry)
    await coord.async_refresh()
    assert coord.last_update_success
    assert coord.data["stat_grid_import"] == 0.0
    assert coord.data["stat_grid_export"] == 0.0
    assert coord.data["stat_consumption"] == 500  # REST-Wert bleibt unverändert Quelle

    await coord.async_refresh()
    assert coord.data["stat_grid_import"] == 0.0


async def test_cloud_grid_import_accumulates_over_time(hass, aioclient_mock):
    """iW>0 über ein definiertes Zeitintervall → stat_grid_import wächst um P*dt/3600."""
    entry = _cloud_entry(hass)
    aioclient_mock.post(LOGIN_URL, json=LOGIN_RESPONSE)
    aioclient_mock.get(CLOUD_DEVICES_URL, json=[])
    aioclient_mock.get(STREAM_URL, json={"t": "2026-08-01T10:00:00Z", "iv": 10, "iW": 1200, "eW": 0})
    aioclient_mock.get(
        STATS_URL,
        json={
            "production": 0,
            "consumption": 0,
            "selfConsumption": 0,
            "selfConsumptionRate": None,
            "autarchyDegree": None,
        },
    )

    with freeze_time("2026-08-01 10:00:00+00:00") as frozen:
        coord = SmartEmsCoordinator(hass, entry)
        await coord.async_refresh()
        assert coord.data["stat_grid_import"] == 0.0  # erster Poll: kein vorheriges _cloud_grid_t

        frozen.tick(delta=timedelta(seconds=30))
        await coord.async_refresh()
        assert coord.data["stat_grid_import"] == pytest.approx(10.0)  # 1200W * 30s/3600
        assert coord.data["stat_grid_export"] == 0.0


async def test_cloud_login_auth_error_maps_to_config_entry_auth_failed(hass, aioclient_mock):
    """Abgelehnter API Key beim ersten Tausch → ConfigEntryAuthFailed (löst den Reauth-Flow aus)."""
    entry = _cloud_entry(hass)
    aioclient_mock.post(LOGIN_URL, status=401)

    coord = SmartEmsCoordinator(hass, entry)
    await coord.async_refresh()
    await hass.async_block_till_done()

    assert not coord.last_update_success
    assert isinstance(coord.last_exception, ConfigEntryAuthFailed)


async def test_cloud_device_meta_from_v3_devices(hass, aioclient_mock):
    """Geräte-Metadaten aus /v3/users/{abbId}/devices: tag ist ein String, das
    Modell steht in deviceKey, Fahrzeuge tragen ihren Namen in carName."""
    entry = _cloud_entry(hass)
    aioclient_mock.post(LOGIN_URL, json=LOGIN_RESPONSE)
    aioclient_mock.get(STREAM_URL, json={"t": "2026-08-01T10:00:00Z", "iv": 10})
    aioclient_mock.get(STATS_URL, json=EMPTY_STATS)
    aioclient_mock.get(
        CLOUD_DEVICES_URL,
        json=[
            {
                "_id": "bat1",
                "type": "battery",
                "tag": "Hausspeicher",
                "deviceKey": "BYD HVS",
                "data": {"batteryMode": 1},
            },
            {"_id": "car1", "type": "car", "tag": "", "carName": "ID.4"},
            {"_id": "wh1", "type": "waterheater", "deviceKey": "myPV AC THOR"},
            {"_id": "sw1", "type": "switches"},
            {"_id": "old1", "type": "Battery", "tag": {"name": "Alt-Format"}},
            {"type": "inverter", "tag": "ohne ID"},
            "kein Objekt",
        ],
    )

    coord = SmartEmsCoordinator(hass, entry)
    await coord.async_refresh()

    assert coord.last_update_success
    assert set(coord.device_meta) == {"bat1", "car1", "wh1", "sw1", "old1"}
    assert coord.get_device_name("bat1") == "Hausspeicher"
    assert coord.get_device_model("bat1") == "BYD HVS"
    assert coord.device_meta["bat1"]["type"] == "battery"
    assert coord.device_meta["bat1"]["raw"]["data"] == {"batteryMode": 1}
    assert coord.get_device_name("car1") == "ID.4"
    assert coord.get_device_name("wh1") == "myPV AC THOR"
    assert coord.get_device_name("sw1") == "switches"
    assert coord.get_device_model("sw1") is None
    assert coord.get_device_name("old1") == "Alt-Format"


async def test_cloud_rotated_api_key_is_persisted_in_entry(hass, aioclient_mock):
    """Key-Rotation im laufenden Betrieb: der neue Key landet sofort im Config-Entry.

    Sonst stünde nach dem nächsten HA-Neustart der entwertete Key in der
    Konfiguration und die Integration wäre ausgesperrt.
    """
    entry = _cloud_entry(hass)
    aioclient_mock.post(LOGIN_URL, json={**LOGIN_RESPONSE, "refresh_token": "key-2"})
    aioclient_mock.get(CLOUD_DEVICES_URL, json=[])
    aioclient_mock.get(STREAM_URL, json={"t": "2026-08-01T10:00:00Z", "iv": 10})
    aioclient_mock.get(STATS_URL, json=EMPTY_STATS)

    coord = SmartEmsCoordinator(hass, entry)
    await coord.async_refresh()

    assert coord.last_update_success
    assert entry.data[CONF_API_KEY] == "key-2"
    assert entry.data[CONF_ABB_ID] == ABB_ID


async def test_cloud_polling_respects_rate_limits(hass, aioclient_mock):
    """Viele Polls → genau ein Token-Tausch, Geräte/Statistik nur nach Ablauf ihres TTL.

    Limits der ABB-Cloud: 500 Abfragen pro Stunde und Endpunkt, 50 pro Stunde
    für /v3/auth/refresh.
    """
    entry = _cloud_entry(hass)
    aioclient_mock.post(LOGIN_URL, json=LOGIN_RESPONSE)
    aioclient_mock.get(CLOUD_DEVICES_URL, json=[])
    aioclient_mock.get(STREAM_URL, json={"t": "2026-08-01T10:00:00Z", "iv": 10})
    aioclient_mock.get(STATS_URL, json=EMPTY_STATS)

    coord = SmartEmsCoordinator(hass, entry)
    for _ in range(6):
        await coord.async_refresh()

    paths = [(m, u.path) for m, u, *_ in aioclient_mock.mock_calls]
    assert paths.count(("POST", "/v3/auth/refresh")) == 1
    assert paths.count(("GET", f"/v3/users/{ABB_ID}/data/stream")) == 6
    assert paths.count(("GET", f"/v3/users/{ABB_ID}/devices")) == 1
    assert paths.count(("GET", f"/v1/statistics/gateways/{ABB_ID}")) == 1


async def test_put_battery_merged_refuses_without_cached_settings(hass):
    """Ohne gecachte Settings kein PUT — sonst reseten Backend-Defaults andere Felder."""
    entry = _cloud_entry(hass)
    coord = SmartEmsCoordinator(hass, entry)
    coord.client = AsyncMock()
    coord.device_meta = {}

    with pytest.raises(HomeAssistantError):
        await coord.async_put_battery_merged("dev1", {"batteryMode": 1})

    coord.client.put_battery_settings.assert_not_awaited()


async def test_put_battery_merged_sends_full_settings_object(hass):
    """Merged-PUT überlagert Änderungen über die gecachten Settings."""
    entry = _cloud_entry(hass)
    coord = SmartEmsCoordinator(hass, entry)
    coord.client = AsyncMock()
    coord.async_refresh_device_meta = AsyncMock()
    coord.device_meta = {
        "dev1": {
            "raw": {
                "data": {
                    "batteryMode": 0,
                    "upperSocLimit": 90,
                    "lowerSocLimit": 10,
                    "unrelatedField": "ignored",
                }
            }
        }
    }

    await coord.async_put_battery_merged("dev1", {"batteryMode": 3})

    coord.client.put_battery_settings.assert_awaited_once()
    _, payload = coord.client.put_battery_settings.await_args.args
    assert payload["batteryMode"] == 3
    assert payload["upperSocLimit"] == 90
    assert payload["lowerSocLimit"] == 10
    # Nur Whitelist-Felder (BATTERY_PUT_FIELDS) werden mitgesendet
    assert "unrelatedField" not in payload


async def test_put_battery_merged_carries_aria_settings_only_when_set(hass):
    """Aria-Felder (mpc…) werden zurückgeschrieben, wenn die Cloud Werte liefert —
    aber nie als null, das würde die Schema-Validierung der API ablehnen."""
    entry = _cloud_entry(hass)
    coord = SmartEmsCoordinator(hass, entry)
    coord.client = AsyncMock()
    coord.async_refresh_device_meta = AsyncMock()
    coord.device_meta = {
        "dev1": {
            "raw": {
                "data": {
                    "batteryMode": 6,
                    "mpcLowerSocLimit": 20,
                    "mpcUpperSocLimit": None,
                    "mpcChargingFromGrid": False,
                }
            }
        }
    }

    await coord.async_put_battery_merged("dev1", {"upperSocLimit": 95})

    _, payload = coord.client.put_battery_settings.await_args.args
    assert payload == {
        "batteryMode": 6,
        "upperSocLimit": 95,
        "mpcLowerSocLimit": 20,
        "mpcChargingFromGrid": False,
    }


def _point_with_device(ts: str, i_total: float, e_total: float) -> dict:
    return {
        "t": ts,
        "iv": 10,
        "devices": [{"_id": "dev1", "iWhTotal": i_total, "eWhTotal": e_total}],
    }


def _mock_point(aioclient_mock, point: dict) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES_URL, json=[])
    aioclient_mock.get(POINT_URL, json=point)


async def test_device_daily_energy_starts_at_zero_and_resets_at_midnight(hass, aioclient_mock):
    """iWhTotal/eWhTotal sind kumulativ → summiert wird der Zuwachs, um Mitternacht ab 0."""
    await hass.config.async_set_time_zone("UTC")
    entry = _local_entry(hass)
    _mock_point(aioclient_mock, _point_with_device("2026-08-01T23:59:40Z", 1000.0, 200.0))

    with freeze_time("2026-08-01 23:59:40+00:00") as frozen:
        coord = SmartEmsCoordinator(hass, entry)
        await coord.async_refresh()
        assert coord.last_update_success
        dev = coord.data["devices"][0]
        assert dev["iWhToday"] == 0.0
        assert dev["eWhToday"] == 0.0

        # Gleicher Tag: Tageswert wächst um den Zuwachs des Zählers
        _mock_point(aioclient_mock, _point_with_device("2026-08-01T23:59:50Z", 1100.0, 220.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        dev = coord.data["devices"][0]
        assert dev["iWhToday"] == 100.0
        assert dev["eWhToday"] == 20.0

        # Tageswechsel ohne Zuwachs in diesem Schritt → Tageswert exakt 0
        _mock_point(aioclient_mock, _point_with_device("2026-08-02T00:00:00Z", 1100.0, 220.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        dev = coord.data["devices"][0]
        assert dev["iWhToday"] == 0.0
        assert dev["eWhToday"] == 0.0

        # Neuer Tag zählt vom Gesamtstand um Mitternacht weiter
        _mock_point(aioclient_mock, _point_with_device("2026-08-02T00:00:10Z", 1150.0, 230.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        dev = coord.data["devices"][0]
        assert dev["iWhToday"] == 50.0
        assert dev["eWhToday"] == 10.0


async def test_device_daily_energy_counts_real_gap_after_restart(hass, aioclient_mock):
    """Nach einer Lücke (Neustart/Ausfall) zählt der echte Zuwachs mit — die
    Plausibilitätsgrenze richtet sich nach der verstrichenen Zeit."""
    await hass.config.async_set_time_zone("UTC")
    entry = _local_entry(hass)
    _mock_point(aioclient_mock, _point_with_device("2026-08-01T08:00:00Z", 1000.0, 0.0))

    with freeze_time("2026-08-01 08:00:00+00:00") as frozen:
        coord = SmartEmsCoordinator(hass, entry)
        await coord.async_refresh()

        # 2 h Lücke, in der das Gerät real 20 kWh bezogen hat
        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:00Z", 21_000.0, 0.0))
        frozen.tick(delta=timedelta(hours=2))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 20_000.0


async def test_device_daily_energy_survives_counter_reset(hass, aioclient_mock):
    """Zählerreset im Gerät → Tageswert läuft weiter, statt negativ zu werden."""
    await hass.config.async_set_time_zone("UTC")
    entry = _local_entry(hass)
    _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:00Z", 1000.0, 0.0))

    with freeze_time("2026-08-01 10:00:00+00:00") as frozen:
        coord = SmartEmsCoordinator(hass, entry)
        await coord.async_refresh()

        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:10Z", 1100.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 100.0

        # Zähler springt auf 0 (Reset im Gerät) → kein negativer Wert, nur Resync
        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:20Z", 0.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 100.0

        # Danach zählt der Tageswert vom neuen Stand aus weiter
        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:30Z", 50.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 150.0


async def test_device_daily_energy_ignores_zero_glitch_and_recovery_jump(hass, aioclient_mock):
    """Kurzzeitige 0 mit Rücksprung auf den Gesamtstand darf keinen Tagesverbrauch
    in Höhe des Lebenszählers erzeugen (HA schreibt den Sprung sonst als Statistik)."""
    await hass.config.async_set_time_zone("UTC")
    entry = _local_entry(hass)
    _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:00Z", 2_300_000.0, 0.0))

    with freeze_time("2026-08-01 10:00:00+00:00") as frozen:
        coord = SmartEmsCoordinator(hass, entry)
        await coord.async_refresh()

        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:10Z", 2_300_300.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 300.0

        # Ausreißer: Stream meldet 0
        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:20Z", 0.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 300.0

        # Rücksprung auf den echten Gesamtstand → nicht plausibel, zählt nicht mit
        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:30Z", 2_300_400.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 300.0

        # Normalbetrieb läuft danach wieder
        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:40Z", 2_300_450.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 350.0


async def test_device_daily_energy_survives_prolonged_zero_outage(hass, aioclient_mock):
    """Gerät meldet über mehrere Polls 0 (getrennt) → auch der spätere Rücksprung
    auf den Gesamtstand darf nicht als Tagesverbrauch gezählt werden."""
    await hass.config.async_set_time_zone("UTC")
    entry = _local_entry(hass)
    _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:00Z", 2_300_000.0, 0.0))

    with freeze_time("2026-08-01 10:00:00+00:00") as frozen:
        coord = SmartEmsCoordinator(hass, entry)
        await coord.async_refresh()

        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:00:10Z", 2_300_300.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 300.0

        # Fünf Polls lang 0
        for i in range(5):
            _mock_point(aioclient_mock, _point_with_device(f"2026-08-01T10:00:{20 + i * 10}Z", 0.0, 0.0))
            frozen.tick(delta=timedelta(seconds=10))
            await coord.async_refresh()
            assert coord.data["devices"][0]["iWhToday"] == 300.0

        # Gerät meldet sich mit dem echten Gesamtstand zurück
        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:01:10Z", 2_300_500.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 300.0

        _mock_point(aioclient_mock, _point_with_device("2026-08-01T10:01:20Z", 2_300_560.0, 0.0))
        frozen.tick(delta=timedelta(seconds=10))
        await coord.async_refresh()
        assert coord.data["devices"][0]["iWhToday"] == 360.0
