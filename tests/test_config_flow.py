"""Tests für den Config Flow (Setup, Re-Auth, Reconfigure, Moduswechsel, Optionen).

Die Cloud-Pfade laufen bewusst gegen die HTTP-Ebene (aioclient_mock) statt gegen
einen gemockten Client: So ist mitgetestet, dass der Flow genau den Key
speichert, den die Cloud nach einer eventuellen Key-Rotation noch akzeptiert.
"""
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.abb_smart_ems.api_client import (
    SmartEmsApiError,
    SmartEmsAuthError,
)
from custom_components.abb_smart_ems.const import (
    API_KEY_DOCS_URL,
    CLOUD_BASE,
    CONF_ABB_ID,
    CONF_API_KEY,
    CONF_HOST,
    CONF_MODE,
    CONF_SCAN_INTERVAL,
    CONF_SCHEME,
    DOMAIN,
    MODE_CLOUD,
    MODE_LOCAL,
)

_PATCH_LOCAL = "custom_components.abb_smart_ems.config_flow.SmartEmsLocal"

ABB_ID = "002A0001"
AUTH_URL = f"{CLOUD_BASE}/v3/auth/refresh"
STREAM_URL = f"{CLOUD_BASE}/v3/users/{ABB_ID}/data/stream"
TOKEN = {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}

_CLOUD_INPUT = {
    CONF_ABB_ID: ABB_ID,
    CONF_API_KEY: "key-1",
}

_LOCAL_INPUT = {
    CONF_HOST: "192.168.1.100",
    CONF_SCHEME: "http",
    CONF_API_KEY: "",
}


def _cloud_entry(hass, *, abb_id: str = ABB_ID, api_key: str = "key-1", **kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_MODE: MODE_CLOUD, CONF_ABB_ID: abb_id, CONF_API_KEY: api_key},
        unique_id=f"{DOMAIN}_{abb_id}",
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


def _local_entry(hass, *, api_key: str | None = None) -> MockConfigEntry:
    data = {CONF_MODE: MODE_LOCAL, CONF_HOST: "192.168.1.100", CONF_SCHEME: "http"}
    if api_key is not None:
        data[CONF_API_KEY] = api_key
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id="local_192.168.1.100")
    entry.add_to_hass(hass)
    return entry


def _auth_requests(aioclient_mock) -> list[dict]:
    """JSON-Bodies aller Aufrufe von /v3/auth/refresh, in Reihenfolge."""
    return [
        body for method, url, body, *_ in aioclient_mock.mock_calls
        if method == "POST" and url.path == "/v3/auth/refresh"
    ]


async def _start_user_flow(hass):
    """Flow starten und zurückgeben — bleibt beim Modus-Selector."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    return result["flow_id"]


async def _start_cloud_step(hass):
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_CLOUD}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "cloud"
    return result


# ---------------------------------------------------------------------------
# Modus-Selector
# ---------------------------------------------------------------------------

async def test_user_flow_mode_selector(hass):
    """Erster Schritt zeigt Modus-Auswahl."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert CONF_MODE in result["data_schema"].schema


# ---------------------------------------------------------------------------
# Cloud-Flow
# ---------------------------------------------------------------------------

async def test_cloud_form_asks_only_for_id_and_key(hass):
    """Die ABB-Cloud kennt nur den API Key — kein E-Mail-/Passwort-Feld im Formular."""
    result = await _start_cloud_step(hass)

    fields = {str(k) for k in result["data_schema"].schema}
    assert fields == {CONF_ABB_ID, CONF_API_KEY}
    assert result["description_placeholders"] == {"kb_url": API_KEY_DOCS_URL}


async def test_cloud_flow_success(hass, aioclient_mock):
    """Cloud Happy-Path: Modus → Gateway-ID + Key → Entry angelegt."""
    aioclient_mock.post(AUTH_URL, json=TOKEN)
    aioclient_mock.get(STREAM_URL, json={"pW": 0})
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == f"ABB Smart EMS {ABB_ID}"
    assert result["data"] == {
        CONF_MODE: MODE_CLOUD,
        CONF_ABB_ID: ABB_ID,
        CONF_API_KEY: "key-1",
    }
    assert result["result"].unique_id == f"{DOMAIN}_{ABB_ID}"


async def test_cloud_flow_strips_whitespace(hass, aioclient_mock):
    """Mitkopierte Leerzeichen/Zeilenumbrüche um ID und Key werden entfernt."""
    aioclient_mock.post(AUTH_URL, json=TOKEN)
    aioclient_mock.get(STREAM_URL, json={})
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ABB_ID: f"  {ABB_ID}\n", CONF_API_KEY: " key-1 "},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ABB_ID] == ABB_ID
    assert result["data"][CONF_API_KEY] == "key-1"
    assert _auth_requests(aioclient_mock)[0]["refresh_token"] == "key-1"


async def test_cloud_flow_blank_input_is_rejected_without_api_call(hass, aioclient_mock):
    """Nur Leerzeichen als Key → Fehler, ohne das knappe Auth-Limit anzutasten."""
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ABB_ID: ABB_ID, CONF_API_KEY: "   "}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "credentials_required"
    assert aioclient_mock.call_count == 0


async def test_cloud_flow_auth_error(hass, aioclient_mock):
    """Key abgelehnt → Formular bleibt mit Fehler 'auth'."""
    aioclient_mock.post(AUTH_URL, status=401)
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "auth"


async def test_cloud_flow_wrong_gateway_id(hass, aioclient_mock):
    """Gültiger Key, aber fremde/falsche Gateway-ID (403 am Stream) → Fehler 'auth'."""
    aioclient_mock.post(AUTH_URL, json=TOKEN)
    aioclient_mock.get(STREAM_URL, status=403)
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "auth"


async def test_cloud_flow_cannot_connect(hass, aioclient_mock):
    """API nicht erreichbar → Fehler 'cannot_connect'."""
    aioclient_mock.post(AUTH_URL, status=503)
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_cloud_flow_rate_limited(hass, aioclient_mock):
    """429 → eigener Fehler 'rate_limit' statt irreführendem 'cannot_connect'."""
    aioclient_mock.post(AUTH_URL, status=429)
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "rate_limit"


async def test_cloud_flow_duplicate(hass, aioclient_mock):
    """Gleiche Gateway-ID bereits eingerichtet → already_configured."""
    _cloud_entry(hass)
    aioclient_mock.post(AUTH_URL, json=TOKEN)
    aioclient_mock.get(STREAM_URL, json={})
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_cloud_flow_stores_rotated_key(hass, aioclient_mock):
    """Key-Rotation: gespeichert wird der neue Key, nicht der eingetippte.

    Mit "Erneuerung erlauben" entwertet schon der Tausch während der
    Einrichtung den eingetippten Key. Würde der Flow den eingetippten Key
    speichern, scheiterte gleich der erste Start der Integration.
    """
    aioclient_mock.post(AUTH_URL, json={**TOKEN, "refresh_token": "key-2"})
    aioclient_mock.get(STREAM_URL, json={})
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == "key-2"


async def test_cloud_flow_resubmit_after_rotation_uses_new_key(hass, aioclient_mock):
    """Rotation + Fehler danach: erneutes Absenden desselben Keys nutzt den Nachfolger.

    Ablauf: Tausch rotiert key-1 → key-2, der Stream-Abruf scheitert (hier:
    Cloud kurz nicht erreichbar). Der Nutzer hat nur key-1 und schickt das
    Formular erneut ab — der Flow muss mit key-2 weitermachen, key-1 ist tot.
    """
    aioclient_mock.post(AUTH_URL, json={**TOKEN, "refresh_token": "key-2"})
    aioclient_mock.get(STREAM_URL, status=503)
    result = await _start_cloud_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )
    assert result["errors"]["base"] == "cannot_connect"

    aioclient_mock.clear_requests()
    aioclient_mock.post(AUTH_URL, json={**TOKEN, "refresh_token": "key-3"})
    aioclient_mock.get(STREAM_URL, json={})

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _CLOUD_INPUT
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert _auth_requests(aioclient_mock)[0]["refresh_token"] == "key-2"
    assert result["data"][CONF_API_KEY] == "key-3"


# ---------------------------------------------------------------------------
# Lokaler Flow
# ---------------------------------------------------------------------------

async def test_local_flow_success(hass):
    """Lokaler Happy-Path: IP + http → Entry angelegt."""
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_LOCAL}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "local"

    with patch(_PATCH_LOCAL) as mock_cls:
        mock_cls.return_value.get_point = AsyncMock(return_value={})

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _LOCAL_INPUT
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "ABB Smart EMS Local (192.168.1.100)"
    assert result["data"][CONF_MODE] == MODE_LOCAL
    assert result["data"][CONF_HOST] == "192.168.1.100"
    assert result["data"][CONF_SCHEME] == "http"
    assert result["data"][CONF_API_KEY] is None


async def test_local_flow_https(hass):
    """HTTPS-Modus: Scheme separat gespeichert, Host bleibt IP-only."""
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_LOCAL}
    )

    with patch(_PATCH_LOCAL) as mock_cls:
        mock_cls.return_value.get_point = AsyncMock(return_value={})

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "https://192.168.1.100/", CONF_SCHEME: "https", CONF_API_KEY: ""},
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.1.100"
    assert result["data"][CONF_SCHEME] == "https"


async def test_local_flow_with_api_key(hass):
    """Lokaler Flow mit API Key → wird gespeichert."""
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_LOCAL}
    )

    with patch(_PATCH_LOCAL) as mock_cls:
        mock_cls.return_value.get_point = AsyncMock(return_value={})

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.168.1.100", CONF_SCHEME: "http", CONF_API_KEY: "secret123"},
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == "secret123"


async def test_local_flow_duplicate(hass):
    """Gleiches Gateway bereits konfiguriert → already_configured."""
    _local_entry(hass)

    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_LOCAL}
    )

    with patch(_PATCH_LOCAL) as mock_cls:
        mock_cls.return_value.get_point = AsyncMock(return_value={})

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.168.1.100", CONF_SCHEME: "https", CONF_API_KEY: ""},
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_local_flow_cannot_connect(hass):
    """Gateway nicht erreichbar → Fehler 'cannot_connect'."""
    flow_id = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_LOCAL}
    )

    with patch(_PATCH_LOCAL) as mock_cls:
        mock_cls.return_value.get_point = AsyncMock(side_effect=SmartEmsApiError)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _LOCAL_INPUT
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


# ---------------------------------------------------------------------------
# Reauth
# ---------------------------------------------------------------------------

async def _start_reauth(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )


async def test_reauth_cloud_success(hass, aioclient_mock):
    """Re-Auth (Cloud): neuer Key wird geprüft und gespeichert, Gateway-ID bleibt."""
    entry = _cloud_entry(hass, api_key="old-key")
    aioclient_mock.post(AUTH_URL, json=TOKEN)

    result = await _start_reauth(hass, entry)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert {str(k) for k in result["data_schema"].schema} == {CONF_API_KEY}
    assert result["description_placeholders"]["abb_id"] == ABB_ID

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new-key"
    assert entry.data[CONF_ABB_ID] == ABB_ID
    assert _auth_requests(aioclient_mock)[0]["refresh_token"] == "new-key"


async def test_reauth_cloud_auth_error_keeps_form(hass, aioclient_mock):
    """Re-Auth (Cloud) mit falschem Key → Formular bleibt, alter Key unangetastet."""
    entry = _cloud_entry(hass, api_key="old-key")
    aioclient_mock.post(AUTH_URL, status=400)

    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "wrong-key"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "auth"
    assert entry.data[CONF_API_KEY] == "old-key"


async def test_reauth_local_updates_api_key(hass):
    """Re-Auth (Lokal): neuer API Key wird validiert und gespeichert."""
    entry = _local_entry(hass, api_key="old-key")

    result = await _start_reauth(hass, entry)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_local"

    with patch(_PATCH_LOCAL) as mock_cls, patch.object(
        hass.config_entries, "async_schedule_reload"
    ):
        mock_cls.return_value.get_point = AsyncMock(return_value={})

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new-key"
    # Host/Scheme bleiben unangetastet
    assert entry.data[CONF_HOST] == "192.168.1.100"
    assert entry.data[CONF_SCHEME] == "http"


async def test_reauth_local_auth_error_keeps_form(hass):
    """Re-Auth (Lokal) mit falschem Key → Formular bleibt mit Fehler 'auth'."""
    entry = _local_entry(hass)

    result = await _start_reauth(hass, entry)

    with patch(_PATCH_LOCAL) as mock_cls:
        mock_cls.return_value.get_point = AsyncMock(side_effect=SmartEmsAuthError)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "wrong-key"}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "auth"


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------

async def _start_reconfigure_flow(hass, entry):
    """Reconfigure-Flow starten wie die echte UI: nur Context, kein data=."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    return result["flow_id"]


async def _start_reconfigure_cloud(hass, entry):
    flow_id = await _start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_CLOUD}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure_cloud"
    return result


async def test_reconfigure_shows_mode_selector(hass):
    """Reconfigure zeigt zuerst den Modus-Selector, vorbelegt mit dem aktuellen Modus."""
    entry = _local_entry(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    mode_key = next(k for k in result["data_schema"].schema if k == CONF_MODE)
    assert mode_key.default() == MODE_LOCAL


async def test_reconfigure_cloud_new_gateway_id_keeps_key(hass, aioclient_mock):
    """Reconfigure (Cloud): neue Gateway-ID, Key-Feld leer → gespeicherter Key bleibt."""
    entry = _cloud_entry(hass)
    aioclient_mock.post(AUTH_URL, json=TOKEN)
    aioclient_mock.get(f"{CLOUD_BASE}/v3/users/002A0002/data/stream", json={})
    result = await _start_reconfigure_cloud(hass, entry)

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ABB_ID: "002A0002", CONF_API_KEY: ""}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ABB_ID] == "002A0002"
    assert entry.data[CONF_API_KEY] == "key-1"
    assert entry.unique_id == f"{DOMAIN}_002A0002"
    assert entry.title == "ABB Smart EMS 002A0002"
    assert _auth_requests(aioclient_mock)[0]["refresh_token"] == "key-1"


async def test_reconfigure_cloud_new_key(hass, aioclient_mock):
    """Reconfigure (Cloud): neuer Key ersetzt den gespeicherten."""
    entry = _cloud_entry(hass)
    aioclient_mock.post(AUTH_URL, json=TOKEN)
    aioclient_mock.get(STREAM_URL, json={})
    result = await _start_reconfigure_cloud(hass, entry)

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ABB_ID: ABB_ID, CONF_API_KEY: "key-new"}
        )

    assert result["type"] == FlowResultType.ABORT
    assert entry.data[CONF_API_KEY] == "key-new"


async def test_reconfigure_cloud_duplicate_gateway_id_aborts(hass, aioclient_mock):
    """Reconfigure auf eine Gateway-ID, die ein anderer Eintrag nutzt → Abbruch ohne API-Call."""
    _cloud_entry(hass, abb_id="002A0002", api_key="other")
    entry = _cloud_entry(hass)
    result = await _start_reconfigure_cloud(hass, entry)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ABB_ID: "002A0002", CONF_API_KEY: ""}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert aioclient_mock.call_count == 0


async def test_reconfigure_cloud_rotation_survives_failed_dialog(hass, aioclient_mock):
    """Rotation des gespeicherten Keys bleibt erhalten, auch wenn der Dialog scheitert.

    Leeres Key-Feld → der gespeicherte Key wird erneut getauscht und dabei
    rotiert; danach scheitert der Dialog an einer falschen Gateway-ID. Der
    neue Key muss trotzdem im Entry landen und der Entry neu geladen werden —
    sonst liefe die eingerichtete Integration mit einem entwerteten Key weiter.
    """
    entry = _cloud_entry(hass)
    aioclient_mock.post(AUTH_URL, json={**TOKEN, "refresh_token": "key-2"})
    aioclient_mock.get(f"{CLOUD_BASE}/v3/users/WRONG/data/stream", status=403)
    result = await _start_reconfigure_cloud(hass, entry)

    with patch.object(hass.config_entries, "async_schedule_reload") as mock_reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ABB_ID: "WRONG", CONF_API_KEY: ""}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "auth"
    assert entry.data[CONF_API_KEY] == "key-2"
    assert entry.data[CONF_ABB_ID] == ABB_ID
    mock_reload.assert_called_once_with(entry.entry_id)


async def test_reconfigure_local_success(hass):
    """Reconfigure (Lokal): Host-Wechsel aktualisiert Daten und unique_id."""
    entry = _local_entry(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_LOCAL}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure_local"

    with patch(_PATCH_LOCAL) as mock_cls, patch.object(
        hass.config_entries, "async_schedule_reload"
    ):
        mock_cls.return_value.get_point = AsyncMock(return_value={})

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: "192.168.1.50", CONF_SCHEME: "https", CONF_API_KEY: ""},
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.168.1.50"
    assert entry.data[CONF_SCHEME] == "https"
    assert entry.unique_id == "local_192.168.1.50"


# ---------------------------------------------------------------------------
# Reconfigure: Moduswechsel
# ---------------------------------------------------------------------------

async def test_switch_cloud_to_local_success(hass):
    """Moduswechsel Cloud -> Lokal: Daten komplett ersetzt, unique_id/Titel folgen."""
    entry = _cloud_entry(hass)

    flow_id = await _start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_LOCAL}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "switch_to_local"

    with patch(_PATCH_LOCAL) as mock_cls, patch.object(
        hass.config_entries, "async_schedule_reload"
    ):
        mock_cls.return_value.get_point = AsyncMock(return_value={})

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _LOCAL_INPUT
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_MODE] == MODE_LOCAL
    assert entry.data[CONF_HOST] == "192.168.1.100"
    # Cloud-Reste (Gateway-ID, Cloud-Key) dürfen nicht im lokalen Entry überleben
    assert CONF_ABB_ID not in entry.data
    assert entry.data[CONF_API_KEY] is None
    assert entry.unique_id == "local_192.168.1.100"
    assert entry.title == "ABB Smart EMS Local (192.168.1.100)"


async def test_switch_local_to_cloud_success(hass, aioclient_mock):
    """Moduswechsel Lokal -> Cloud: Daten komplett ersetzt, unique_id/Titel folgen."""
    entry = _local_entry(hass, api_key="local-key")
    aioclient_mock.post(AUTH_URL, json=TOKEN)
    aioclient_mock.get(STREAM_URL, json={})

    flow_id = await _start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_CLOUD}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "switch_to_cloud"

    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _CLOUD_INPUT
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_MODE: MODE_CLOUD,
        CONF_ABB_ID: ABB_ID,
        CONF_API_KEY: "key-1",
    }
    assert entry.unique_id == f"{DOMAIN}_{ABB_ID}"
    assert entry.title == f"ABB Smart EMS {ABB_ID}"
    # Der lokale Key darf nie an die Cloud gehen
    assert _auth_requests(aioclient_mock)[0]["refresh_token"] == "key-1"


async def test_switch_duplicate_unique_id_aborts(hass):
    """Moduswechsel auf eine unique_id, die ein anderer Eintrag nutzt -> Abbruch."""
    _local_entry(hass)
    entry = _cloud_entry(hass)

    flow_id = await _start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MODE: MODE_LOCAL}
    )
    assert result["step_id"] == "switch_to_local"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _LOCAL_INPUT
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Options-Flow
# ---------------------------------------------------------------------------

async def test_options_flow_sets_scan_interval(hass):
    """Optionen: nur das Abfrageintervall — der Key läuft über Reauth/Reconfigure."""
    entry = _cloud_entry(hass, options={CONF_SCAN_INTERVAL: 10})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert {str(k) for k in result["data_schema"].schema} == {CONF_SCAN_INTERVAL}

    with patch.object(hass.config_entries, "async_schedule_reload") as mock_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 30}
        )
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 30
    assert entry.data[CONF_API_KEY] == "key-1"
    assert mock_reload.call_count == 1


async def test_options_flow_rejects_interval_below_rate_limit_floor(hass):
    """Unter 10 s geht nicht: 500 Abfragen pro Stunde und Endpunkt sind das Cloud-Limit."""
    entry = _cloud_entry(hass, options={CONF_SCAN_INTERVAL: 10})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]

    assert schema({CONF_SCAN_INTERVAL: 10}) == {CONF_SCAN_INTERVAL: 10}
    try:
        schema({CONF_SCAN_INTERVAL: 5})
    except Exception as err:  # voluptuous.Invalid
        assert "at least 10" in str(err)
    else:
        raise AssertionError("scan interval below 10 s must be rejected")
