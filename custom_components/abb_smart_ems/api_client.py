# api_client.py
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
LOCAL_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Lebensdauer des Access-Tokens, falls die API kein expires_in mitliefert.
# Laut ABB-Swagger laufen Access-Tokens nach 1 Stunde ab.
DEFAULT_TOKEN_LIFETIME = 3600  # Sekunden

AUTH_PATH = "/v3/auth/refresh"

# Rate-Limits laut ABB Smart Energy Knowledge Database ("Smart EMS API"):
#   - 500 Abfragen pro Stunde pro Endpunkt
#   - /v3/auth/refresh nur 50 Abfragen pro Stunde → Access-Token MUSS gecacht werden
# Schlägt der Token-Tausch aus einem anderen Grund als "Key ungültig" fehl
# (Netzwerk, 5xx, 429), würde jeder Poll (Default 10 s = 360/h) einen neuen
# Tausch auslösen und das 50er-Limit dauerhaft reißen. Deshalb nach einem
# Fehlschlag erst nach einer Wartezeit erneut versuchen.
AUTH_RETRY_BACKOFF = 120  # Sekunden → höchstens 30 Tauschversuche pro Stunde
AUTH_RATE_LIMIT_BACKOFF = 600  # Sekunden nach HTTP 429 auf dem Auth-Endpunkt


async def _check_status(
    r: aiohttp.ClientResponse,
    *,
    context: str,
    auth_statuses: tuple[int, ...] = (401, 403),
) -> None:
    """HTTP-Status auf die passende Smart-EMS-Exception mappen; wirft bei Fehler."""
    if r.status in auth_statuses:
        raise SmartEmsAuthError(f"{context} auth failed (HTTP {r.status})")
    if r.status == 429:
        raise SmartEmsRateLimit("Rate limited")
    if r.status >= 400:
        text = await r.text()
        raise SmartEmsApiError(f"{context} failed {r.status}: {text}")


def normalize_local_host(host: str) -> str:
    """Kanonischer Host ohne Scheme/Slash — für unique_id & Anzeige."""
    host = host.strip().rstrip("/")
    for prefix in ("http://", "https://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host.lower()


class SmartEmsAuthError(Exception):
    """Auth- oder Tokenfehler."""


class SmartEmsRateLimit(Exception):
    """API-Rate-Limit überschritten (HTTP 429)."""


class SmartEmsApiError(Exception):
    """Sonstiger API-Fehler (inkl. Netzwerkfehler/Timeouts)."""


class SmartEmsCloud:
    """
    Minimaler Async-Client für die ABB myBuildings Energy API (Smart EMS Cloud).

    Erwartet EINE gemeinsame Base-URL (https://api.energy.mybuildings.abb.com) für:
      - POST /v3/auth/refresh              (API Key → kurzlebiger Access-Token)
      - GET  /v3/users/{abbId}/data/stream
      - GET  /v3/users/{abbId}/devices
      - GET  /v1/statistics/gateways/{abbId}
      - PUT  /v1|v2/control/…/{sensorId}

    Einziger Auth-Weg ist der API Key: Die ABB-Cloud kennt keinen
    E-Mail/Passwort-Login (kein /v1/oauth/login). Der Key wird als Refresh-Token
    behandelt und gegen einen Access-Token getauscht, statt ihn bei jedem
    Request mitzuschicken (Empfehlung der API-Doku).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base: str,
        abb_id: str,
        api_key: str,
        on_key_rotated: Callable[[str], None] | None = None,
    ):
        self._s = session
        self._base = base.rstrip("/")
        self.abb_id = abb_id
        self.api_key = api_key
        # Wird aufgerufen, wenn die API beim Token-Tausch einen neuen Key
        # ausgibt (Key-Rotation). Der Aufrufer MUSS den neuen Key persistieren:
        # der alte ist ab diesem Moment serverseitig ungültig.
        self._on_key_rotated = on_key_rotated

        # Token-Zustand
        self._access: str | None = None
        self._token_type: str = "Bearer"
        self._exp_ts: float = 0.0  # Epoch-Sekunden
        self._auth_lock = asyncio.Lock()
        # Frühester Zeitpunkt für den nächsten Tauschversuch nach einem Fehlschlag
        self._auth_retry_at: float = 0.0

    # -------------------- Auth --------------------

    async def _exchange_api_key(self) -> None:
        """Token-Tausch mit Backoff nach Fehlschlägen (siehe AUTH_RETRY_BACKOFF)."""
        wait = self._auth_retry_at - time.time()
        if wait > 0:
            raise SmartEmsApiError(
                f"POST {AUTH_PATH} skipped: backing off for another {wait:.0f} s "
                "after a failed token exchange"
            )
        try:
            await self._do_exchange_api_key()
        except SmartEmsRateLimit:
            self._auth_retry_at = time.time() + AUTH_RATE_LIMIT_BACKOFF
            raise
        except SmartEmsApiError:
            self._auth_retry_at = time.time() + AUTH_RETRY_BACKOFF
            raise
        self._auth_retry_at = 0.0

    async def _do_exchange_api_key(self) -> None:
        """POST /v3/auth/refresh — API Key als Refresh-Token → Access-Token.

        Bei aktivierter Key-Rotation ("Erneuerung erlauben") liefert die API
        zusätzlich einen neuen refresh_token und entwertet den gesendeten sofort.
        Der neue Key wird übernommen und über on_key_rotated nach außen gemeldet.
        """
        _LOGGER.debug("Auth: exchanging API key via %s", AUTH_PATH)
        url = f"{self._base}{AUTH_PATH}"
        payload = {"grant_type": "refresh_token", "refresh_token": self.api_key}
        try:
            async with self._s.post(url, json=payload, timeout=REQUEST_TIMEOUT) as r:
                # Ein formal korrekter Tausch, der mit 400 abgelehnt wird, kann
                # nur am Key liegen (ungültig/abgelaufen/bereits rotiert) —
                # OAuth-Backends melden das üblicherweise als 400 invalid_grant.
                # Deshalb hier auch 400 als Auth-Fehler werten, damit HA den
                # Reauth-Dialog anbietet statt endlos neu zu versuchen.
                await _check_status(
                    r, context=f"POST {AUTH_PATH}", auth_statuses=(400, 401, 403)
                )
                data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise SmartEmsApiError(f"POST {AUTH_PATH} unreachable: {err}") from err

        access = data.get("access_token") if isinstance(data, dict) else None
        if not access:
            raise SmartEmsAuthError(f"No access_token in {AUTH_PATH} response")
        self._access = access
        self._token_type = data.get("token_type") or "Bearer"
        try:
            exp_sec = int(data.get("expires_in") or DEFAULT_TOKEN_LIFETIME)
        except (TypeError, ValueError):
            exp_sec = DEFAULT_TOKEN_LIFETIME
        self._exp_ts = time.time() + exp_sec - 30

        new_key = data.get("refresh_token")
        if new_key and new_key != self.api_key:
            _LOGGER.debug("Auth: API key was rotated by the server")
            self.api_key = new_key
            if self._on_key_rotated is not None:
                self._on_key_rotated(new_key)

    async def login(self) -> None:
        """Authentifiziert: tauscht den API Key gegen einen Access-Token."""
        async with self._auth_lock:
            await self._exchange_api_key()

    def _invalidate_token(self) -> None:
        self._access = None
        self._exp_ts = 0.0

    async def _ensure_token(self) -> None:
        """Token rechtzeitig erneuern (Lock verhindert parallele Tauschvorgänge)."""
        if self._access and time.time() < self._exp_ts:
            return
        async with self._auth_lock:
            # Ein anderer Task kann den Token inzwischen erneuert haben
            if self._access and time.time() < self._exp_ts:
                return
            await self._exchange_api_key()

    def _bearer_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"{self._token_type} {self._access}",
            "accept": "application/json",
        }

    # -------------------- Authentifizierte Requests --------------------

    async def _authed_request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        parse_json: bool = True,
    ) -> Any:
        """Request mit Bearer-Token, Netzwerk-Fehler-Mapping und einmaligem
        Retry bei 401 (Token kann serverseitig invalidiert worden sein)."""
        url = f"{self._base}/{path.lstrip('/')}"
        await self._ensure_token()
        for attempt in (0, 1):
            try:
                async with self._s.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=self._bearer_headers(),
                    timeout=REQUEST_TIMEOUT,
                ) as r:
                    if r.status == 401 and attempt == 0:
                        _LOGGER.debug(
                            "401 on %s %s — refreshing token, retrying once",
                            method,
                            path,
                        )
                        self._invalidate_token()
                        await self._ensure_token()
                        continue
                    await _check_status(r, context=f"{method} {path}")
                    if not parse_json or r.status == 204:
                        return None
                    return await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise SmartEmsApiError(f"{method} {path} unreachable: {err}") from err
        return None  # nicht erreichbar (Schleife endet immer mit return/raise)

    # -------------------- v3 Stream --------------------

    async def stream_user_v3(self) -> dict[str, Any]:
        """
        GET /v3/users/{abbId}/data/stream
        Liefert Felder wie: v, t, iv, pW, cW, iW, eW, bcW, bdW, soc, ... und devices[].
        """
        return await self._authed_request("GET", f"/v3/users/{self.abb_id}/data/stream")

    async def list_devices(self) -> list[dict]:
        """GET /v3/users/{abbId}/devices → Geräte samt Konfiguration.

        Felder je Gerät: _id, type, tag (String), deviceKey, carName, priority,
        data{…Einstellungen…}. Die ABB-Cloud bietet KEIN /v1/info/sensors/{id}.
        """
        return await self._authed_request("GET", f"/v3/users/{self.abb_id}/devices")

    async def _put_control(self, path: str, payload: dict) -> None:
        """Generischer PUT-Helper für alle /control/… Endpunkte."""
        await self._authed_request("PUT", path, json=payload, parse_json=False)

    async def put_battery_settings(self, sensor_id: str, payload: dict) -> None:
        """PUT /v2/control/battery/{sensorId}"""
        await self._put_control(f"/v2/control/battery/{sensor_id}", payload)

    async def put_car_charger_mode(self, sensor_id: str, payload: dict) -> None:
        """PUT /v1/control/car-charger/{sensorId}"""
        await self._put_control(f"/v1/control/car-charger/{sensor_id}", payload)

    async def put_heat_pump_mode(self, sensor_id: str, payload: dict) -> None:
        """PUT /v1/control/heat-pump/{sensorId}"""
        await self._put_control(f"/v1/control/heat-pump/{sensor_id}", payload)

    async def put_water_heater_mode(self, sensor_id: str, payload: dict) -> None:
        """PUT /v1/control/water-heater/{sensorId}"""
        await self._put_control(f"/v1/control/water-heater/{sensor_id}", payload)

    async def put_smart_plug_mode(self, sensor_id: str, payload: dict) -> None:
        """PUT /v1/control/smart-plug/{sensorId}"""
        await self._put_control(f"/v1/control/smart-plug/{sensor_id}", payload)

    async def put_switch_mode(self, sensor_id: str, payload: dict) -> None:
        """PUT /v1/control/switch/{sensorId}"""
        await self._put_control(f"/v1/control/switch/{sensor_id}", payload)

    async def put_v2x_mode(self, sensor_id: str, payload: dict) -> None:
        """PUT /v2/control/v2x/{sensorId}"""
        await self._put_control(f"/v2/control/v2x/{sensor_id}", payload)

    async def put_inverter_settings(self, sensor_id: str, payload: dict) -> None:
        """PUT /v1/control/inverter/{sensorId}"""
        await self._put_control(f"/v1/control/inverter/{sensor_id}", payload)

    async def get_gateway_statistics(
        self, from_dt: str, to_dt: str, accuracy: str = "high"
    ) -> dict:
        """GET /v1/statistics/gateways/{abbId}?from=...&to=...&accuracy=...

        accuracy: 'low' (>1 month), 'medium' (<=1 month), 'high' (<=1 week)
        Returns: production, consumption, selfConsumption (Wh), selfConsumptionRate, autarchyDegree (%)
        """
        return await self._authed_request(
            "GET",
            f"/v1/statistics/gateways/{self.abb_id}",
            params={"from": from_dt, "to": to_dt, "accuracy": accuracy},
        )


class SmartEmsLocal:
    """Read-only client for the local REST API (v2) of the Energy Edge EX.1 gateway."""

    def __init__(
        self,
        host: str,
        session: aiohttp.ClientSession,
        *,
        scheme: str = "http",
        api_key: str | None = None,
    ) -> None:
        self._base = f"{scheme}://{normalize_local_host(host)}"
        self._s = session
        self._api_key = api_key

    def _headers(self) -> dict:
        return {"X-API-Key": self._api_key} if self._api_key else {}

    async def get_point(self) -> dict:
        """GET /v2/point → aktueller Datenpunkt (identisches Feldformat wie Cloud-Stream)."""
        try:
            async with self._s.get(
                f"{self._base}/v2/point",
                headers=self._headers(),
                ssl=False,
                timeout=LOCAL_TIMEOUT,
            ) as r:
                await _check_status(r, context="GET /v2/point")
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise SmartEmsApiError(f"Local API unreachable: {err}") from err

    async def list_devices(self) -> list[dict]:
        """GET /v2/devices → normalisiert auf Cloud-Format {_id, name, type}."""
        try:
            async with self._s.get(
                f"{self._base}/v2/devices",
                headers=self._headers(),
                ssl=False,
                timeout=LOCAL_TIMEOUT,
            ) as r:
                await _check_status(r, context="GET /v2/devices")
                data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise SmartEmsApiError(f"Local devices unreachable: {err}") from err
        return [
            {
                "_id": d["deviceId"],
                "name": d.get("description") or d.get("name") or d["deviceId"],
                "type": d.get("type", ""),
            }
            for d in (data if isinstance(data, list) else [])
            if isinstance(d, dict) and d.get("deviceId")
        ]
