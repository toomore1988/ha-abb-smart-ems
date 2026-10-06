"""Tests für den Cloud-API-Client: API-Key-Tausch, Key-Rotation, Fehler-Mapping, 401-Retry."""
import time
from types import SimpleNamespace

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.abb_smart_ems import api_client as api_mod
from custom_components.abb_smart_ems.api_client import (
    SmartEmsApiError,
    SmartEmsAuthError,
    SmartEmsCloud,
    SmartEmsRateLimit,
)

BASE = "https://cloud.test"
AUTH_URL = f"{BASE}/v3/auth/refresh"
STREAM_URL = f"{BASE}/v3/users/002A0001/data/stream"
DEVICES_URL = f"{BASE}/v3/users/002A0001/devices"

_TOKEN_RESPONSE = {
    "access_token": "tok",
    "token_type": "Bearer",
    "expires_in": 3600,
}


def _client(hass, **kwargs) -> SmartEmsCloud:
    return SmartEmsCloud(
        async_get_clientsession(hass),
        base=BASE,
        abb_id="002A0001",
        api_key="key-1",
        **kwargs,
    )


def _calls(aioclient_mock) -> list[tuple[str, str]]:
    return [(m, u.path) for m, u, *_ in aioclient_mock.mock_calls]


def _advance_clock(monkeypatch, seconds: float) -> None:
    """Uhr NUR im api_client-Modul vorstellen (nicht global via time.time)."""
    real_time = time.time
    monkeypatch.setattr(
        api_mod, "time", SimpleNamespace(time=lambda: real_time() + seconds)
    )


async def test_login_and_stream_success(hass, aioclient_mock):
    """Happy path: Key-Tausch liefert Token, Stream liefert Daten."""
    aioclient_mock.post(AUTH_URL, json=_TOKEN_RESPONSE)
    aioclient_mock.get(STREAM_URL, json={"pW": 1234})

    client = _client(hass)
    await client.login()
    data = await client.stream_user_v3()

    assert data == {"pW": 1234}


async def test_exchange_sends_api_key_as_refresh_token(hass, aioclient_mock):
    """Der API Key geht als refresh_token an /v3/auth/refresh, danach nur noch der Bearer-Token."""
    aioclient_mock.post(AUTH_URL, json=_TOKEN_RESPONSE)
    aioclient_mock.get(STREAM_URL, json={})

    client = _client(hass)
    await client.stream_user_v3()

    auth_call, stream_call = aioclient_mock.mock_calls
    assert auth_call[2] == {"grant_type": "refresh_token", "refresh_token": "key-1"}
    assert stream_call[3]["Authorization"] == "Bearer tok"
    assert "key-1" not in str(stream_call[3])


async def test_devices_use_v3_users_endpoint(hass, aioclient_mock):
    """Geräteliste kommt von /v3/users/{abbId}/devices (kein /v1/info/sensors bei ABB)."""
    aioclient_mock.post(AUTH_URL, json=_TOKEN_RESPONSE)
    aioclient_mock.get(DEVICES_URL, json=[{"_id": "d1", "type": "battery"}])

    devices = await _client(hass).list_devices()

    assert devices == [{"_id": "d1", "type": "battery"}]


async def test_token_is_cached_between_requests(hass, aioclient_mock):
    """Der Access-Token wird wiederverwendet — /v3/auth/refresh erlaubt nur 50 Abfragen/h."""
    aioclient_mock.post(AUTH_URL, json=_TOKEN_RESPONSE)
    aioclient_mock.get(STREAM_URL, json={})

    client = _client(hass)
    for _ in range(5):
        await client.stream_user_v3()

    assert _calls(aioclient_mock).count(("POST", "/v3/auth/refresh")) == 1


async def test_static_key_is_not_reported_as_rotated(hass, aioclient_mock):
    """Statischer Key (Standard): kein oder identischer refresh_token → kein Callback."""
    aioclient_mock.post(AUTH_URL, json={**_TOKEN_RESPONSE, "refresh_token": "key-1"})
    rotated: list[str] = []

    client = _client(hass, on_key_rotated=rotated.append)
    await client.login()

    assert rotated == []
    assert client.api_key == "key-1"


async def test_rotated_key_is_adopted_and_reported(hass, aioclient_mock):
    """Key-Rotation ("Erneuerung erlauben"): neuer Key wird übernommen und gemeldet.

    Der alte Key ist nach dem Tausch serverseitig ungültig — der nächste Tausch
    muss zwingend mit dem neuen Key laufen.
    """
    aioclient_mock.post(AUTH_URL, json={**_TOKEN_RESPONSE, "refresh_token": "key-2"})
    rotated: list[str] = []

    client = _client(hass, on_key_rotated=rotated.append)
    await client.login()

    assert rotated == ["key-2"]
    assert client.api_key == "key-2"

    client._invalidate_token()
    await client._ensure_token()
    assert aioclient_mock.mock_calls[-1][2]["refresh_token"] == "key-2"


async def test_network_error_maps_to_api_error(hass, aioclient_mock):
    """aiohttp.ClientError beim Tausch → SmartEmsApiError (nicht 'unknown')."""
    aioclient_mock.post(AUTH_URL, exc=aiohttp.ClientError("connection refused"))

    with pytest.raises(SmartEmsApiError):
        await _client(hass).login()


async def test_stream_network_error_maps_to_api_error(hass, aioclient_mock):
    """Netzwerkfehler bei authentifizierten Requests → SmartEmsApiError."""
    aioclient_mock.post(AUTH_URL, json=_TOKEN_RESPONSE)
    aioclient_mock.get(STREAM_URL, exc=aiohttp.ClientError("timeout"))

    client = _client(hass)
    await client.login()
    with pytest.raises(SmartEmsApiError):
        await client.stream_user_v3()


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_rejected_key_raises_auth_error(hass, aioclient_mock, status):
    """Abgelehnter Key beim Tausch → SmartEmsAuthError (löst in HA den Reauth-Dialog aus).

    400 zählt mit: OAuth-Backends melden einen ungültigen Refresh-Token
    üblicherweise als 400 invalid_grant.
    """
    aioclient_mock.post(AUTH_URL, status=status)

    with pytest.raises(SmartEmsAuthError):
        await _client(hass).login()


async def test_missing_access_token_raises_auth_error(hass, aioclient_mock):
    """Antwort ohne access_token → SmartEmsAuthError."""
    aioclient_mock.post(AUTH_URL, json={"token_type": "Bearer"})

    with pytest.raises(SmartEmsAuthError):
        await _client(hass).login()


async def test_stream_429_raises_rate_limit(hass, aioclient_mock):
    """429 → SmartEmsRateLimit."""
    aioclient_mock.post(AUTH_URL, json=_TOKEN_RESPONSE)
    aioclient_mock.get(STREAM_URL, status=429)

    client = _client(hass)
    await client.login()
    with pytest.raises(SmartEmsRateLimit):
        await client.stream_user_v3()


async def test_401_triggers_single_token_refresh_retry(hass, aioclient_mock):
    """401 auf dem Stream → einmal Token erneuern + Retry, erst dann AuthError.

    Verhindert, dass ein transienter 401 sofort den Reauth-Flow auslöst.
    """
    aioclient_mock.post(AUTH_URL, json=_TOKEN_RESPONSE)
    aioclient_mock.get(STREAM_URL, status=401)

    client = _client(hass)
    await client.login()
    with pytest.raises(SmartEmsAuthError):
        await client.stream_user_v3()

    # Aufrufe: 2× Stream (Original + Retry), 2× Tausch (Login + Erneuerung dazwischen)
    calls = _calls(aioclient_mock)
    assert calls.count(("GET", "/v3/users/002A0001/data/stream")) == 2
    assert calls.count(("POST", "/v3/auth/refresh")) == 2


async def test_failed_exchange_backs_off(hass, aioclient_mock, monkeypatch):
    """Nach einem fehlgeschlagenen Tausch wird der Auth-Endpunkt nicht bei jedem Poll erneut belastet.

    Ohne Backoff würde ein 10-s-Poll bei gestörter Cloud 360 Tauschversuche pro
    Stunde auslösen — das Limit des Endpunkts liegt bei 50.
    """
    aioclient_mock.post(AUTH_URL, status=500)
    client = _client(hass)

    with pytest.raises(SmartEmsApiError):
        await client.stream_user_v3()
    with pytest.raises(SmartEmsApiError, match="backing off"):
        await client.stream_user_v3()
    assert _calls(aioclient_mock).count(("POST", "/v3/auth/refresh")) == 1

    # Nach Ablauf der Wartezeit wird wieder getauscht
    _advance_clock(monkeypatch, api_mod.AUTH_RETRY_BACKOFF + 1)
    with pytest.raises(SmartEmsApiError):
        await client.stream_user_v3()
    assert _calls(aioclient_mock).count(("POST", "/v3/auth/refresh")) == 2


async def test_rate_limited_exchange_backs_off_longer(hass, aioclient_mock, monkeypatch):
    """429 auf dem Auth-Endpunkt → längere Wartezeit als bei sonstigen Fehlern."""
    aioclient_mock.post(AUTH_URL, status=429)
    client = _client(hass)

    with pytest.raises(SmartEmsRateLimit):
        await client.login()

    _advance_clock(monkeypatch, api_mod.AUTH_RETRY_BACKOFF + 1)
    with pytest.raises(SmartEmsApiError, match="backing off"):
        await client.login()
    assert _calls(aioclient_mock).count(("POST", "/v3/auth/refresh")) == 1


async def test_rejected_key_does_not_back_off(hass, aioclient_mock):
    """Auth-Fehler setzen keinen Backoff — HA stoppt das Polling ohnehin und fragt nach einem neuen Key."""
    aioclient_mock.post(AUTH_URL, status=401)
    client = _client(hass)

    for _ in range(2):
        with pytest.raises(SmartEmsAuthError):
            await client.login()

    assert _calls(aioclient_mock).count(("POST", "/v3/auth/refresh")) == 2
