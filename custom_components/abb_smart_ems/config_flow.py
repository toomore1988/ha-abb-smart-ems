# config_flow.py
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api_client import (
    SmartEmsApiError,
    SmartEmsAuthError,
    SmartEmsCloud,
    SmartEmsLocal,
    SmartEmsRateLimit,
    normalize_local_host,
)
from .const import (
    API_KEY_DOCS_URL,
    CLOUD_BASE,
    CONF_ABB_ID,
    CONF_API_KEY,
    CONF_HOST,
    CONF_MODE,
    CONF_SCAN_INTERVAL,
    CONF_SCHEME,
    DEFAULT_SCAN,
    DOMAIN,
    INTEGRATION_NAME,
    MODE_CLOUD,
    MODE_LOCAL,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _schema_cloud(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Cloud-Felder: Gateway-ID und API Key — beides Pflicht.

    Die ABB-Cloud kennt keinen E-Mail/Passwort-Login; der API Key ist der
    einzige Auth-Weg. Der Key wird nach einem Fehler bewusst nicht vorbelegt.
    """
    defaults = defaults or {}
    return vol.Schema({
        vol.Required(CONF_ABB_ID, default=defaults.get(CONF_ABB_ID, "")): str,
        vol.Required(CONF_API_KEY): PASSWORD_SELECTOR,
    })


def _schema_local(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema({
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(CONF_SCHEME, default=defaults.get(CONF_SCHEME, "http")): vol.In(["http", "https"]),
        vol.Optional(CONF_API_KEY, default=""): PASSWORD_SELECTOR,
    })


def _clean(value: Any) -> str:
    """Eingabe trimmen — IDs und Keys werden gern mit Leerzeichen/Zeilenumbruch kopiert."""
    return str(value or "").strip()


def _cloud_title(abb_id: str) -> str:
    return f"{INTEGRATION_NAME} {abb_id}"


def _local_title(host: str) -> str:
    return f"{INTEGRATION_NAME} Local ({host})"


async def _validate_local(hass, *, host: str, scheme: str, api_key: str | None) -> None:
    """Lokale Zugangsdaten prüfen (GET /v2/point); wirft bei Fehler."""
    session = async_get_clientsession(hass)
    client = SmartEmsLocal(host, session, scheme=scheme, api_key=api_key)
    await client.get_point()


async def _validate_and_map_errors(coro) -> dict[str, str]:
    """Validierungs-Coroutine ausführen und Exceptions auf ein Errors-Dict mappen."""
    try:
        await coro
    except SmartEmsAuthError:
        return {"base": "auth"}
    except SmartEmsRateLimit:
        return {"base": "rate_limit"}
    except SmartEmsApiError:
        return {"base": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unexpected error validating ABB Smart EMS connection")
        return {"base": "unknown"}
    return {}


async def _local_errors(hass, *, host: str, scheme: str, api_key: str | None) -> dict[str, str]:
    """Lokale Verbindung validieren; liefert Errors-Dict (leer bei Erfolg)."""
    return await _validate_and_map_errors(
        _validate_local(hass, host=host, scheme=scheme, api_key=api_key)
    )


class SmartEmsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        # Key-Rotation: Hat die Cloud während der Validierung einen neuen Key
        # ausgegeben, ist der eingetippte Key ab dann ungültig. Scheitert ein
        # späterer Validierungsschritt (z. B. falsche Gateway-ID) und der Nutzer
        # schickt denselben Key erneut ab, wird hierüber der gültige Nachfolger
        # verwendet statt des bereits entwerteten Keys. {eingetippt: aktuell}
        self._rotated_keys: dict[str, str] = {}

    async def _cloud_validate(
        self,
        *,
        abb_id: str,
        api_key: str,
        check_stream: bool = False,
        entry: ConfigEntry | None = None,
    ) -> tuple[dict[str, str], str]:
        """Cloud-Zugangsdaten prüfen (Token-Tausch, optional Stream-Abruf).

        Liefert (Errors-Dict, zu speichernder API Key). Der zurückgegebene Key
        kann vom eingegebenen abweichen, wenn die Cloud ihn rotiert hat.

        `entry`: bestehender Entry, dessen gespeicherter Key hier ggf. erneut
        geprüft wird (Reconfigure mit leerem Key-Feld). Rotiert die Cloud genau
        diesen Key, wird der Nachfolger sofort im Entry gespeichert — auch wenn
        der Dialog danach an etwas anderem scheitert. Sonst liefe die bereits
        eingerichtete Integration mit einem entwerteten Key weiter.
        """
        typed = api_key
        effective = self._rotated_keys.get(typed, typed)
        entry_key_rotated = False

        def _remember(new_key: str) -> None:
            nonlocal entry_key_rotated
            self._rotated_keys[typed] = new_key
            if entry is not None and entry.data.get(CONF_API_KEY) in (typed, effective):
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_API_KEY: new_key}
                )
                entry_key_rotated = True

        client = SmartEmsCloud(
            async_get_clientsession(self.hass),
            base=CLOUD_BASE,
            abb_id=abb_id,
            api_key=effective,
            on_key_rotated=_remember,
        )

        async def _run() -> None:
            await client.login()
            if check_stream:
                await client.stream_user_v3()

        errors = await _validate_and_map_errors(_run())
        if errors and entry_key_rotated and entry is not None:
            # Dialog bleibt offen, der laufende Client hält aber noch den alten
            # Key im Speicher → Entry mit dem gespeicherten Nachfolger neu laden.
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return errors, client.api_key

    # ------------------------------------------------------------------
    # Ersteinrichtung
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Mode selector: Cloud or Local."""
        if user_input is not None:
            if user_input[CONF_MODE] == MODE_LOCAL:
                return await self.async_step_local()
            return await self.async_step_cloud()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_MODE, default=MODE_CLOUD): vol.In([MODE_CLOUD, MODE_LOCAL]),
            }),
        )

    async def async_step_cloud(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input:
            abb_id = _clean(user_input.get(CONF_ABB_ID))
            api_key = _clean(user_input.get(CONF_API_KEY))
            if not abb_id or not api_key:
                errors = {"base": "credentials_required"}
            else:
                errors, api_key = await self._cloud_validate(
                    abb_id=abb_id, api_key=api_key, check_stream=True
                )
            if not errors:
                await self.async_set_unique_id(f"{DOMAIN}_{abb_id}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=_cloud_title(abb_id),
                    data={
                        CONF_MODE: MODE_CLOUD,
                        CONF_ABB_ID: abb_id,
                        CONF_API_KEY: api_key,
                    },
                )

        return self.async_show_form(
            step_id="cloud",
            data_schema=_schema_cloud(user_input),
            errors=errors,
            description_placeholders={"kb_url": API_KEY_DOCS_URL},
        )

    async def async_step_local(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = normalize_local_host(user_input[CONF_HOST])
            scheme = user_input[CONF_SCHEME]
            api_key = _clean(user_input.get(CONF_API_KEY)) or None
            errors = await _local_errors(self.hass, host=host, scheme=scheme, api_key=api_key)
            if not errors:
                await self.async_set_unique_id(f"local_{host}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_local_title(host),
                    data={
                        CONF_MODE: MODE_LOCAL,
                        CONF_HOST: host,
                        CONF_SCHEME: scheme,
                        CONF_API_KEY: api_key,
                    },
                )

        return self.async_show_form(
            step_id="local",
            data_schema=_schema_local(user_input),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Re-Auth
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        if entry_data.get(CONF_MODE) == MODE_LOCAL:
            return await self.async_step_reauth_local()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        """Cloud: API Key wurde abgelehnt (ungültig, abgelaufen, gelöscht) → neuen Key abfragen."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = _clean(user_input.get(CONF_API_KEY))
            if not api_key:
                errors = {"base": "credentials_required"}
            else:
                errors, api_key = await self._cloud_validate(
                    abb_id=reauth_entry.data[CONF_ABB_ID], api_key=api_key
                )
            if not errors:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_API_KEY: api_key},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): PASSWORD_SELECTOR,
            }),
            errors=errors,
            description_placeholders={
                "abb_id": reauth_entry.data.get(CONF_ABB_ID, ""),
                "kb_url": API_KEY_DOCS_URL,
            },
        )

    async def async_step_reauth_local(self, user_input=None) -> ConfigFlowResult:
        """Lokaler Modus: API-Key wurde vom Gateway abgelehnt → neuen Key abfragen."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            # Leer = Gateway verlangt (nicht mehr) einen Key
            api_key = _clean(user_input.get(CONF_API_KEY)) or None
            errors = await _local_errors(
                self.hass,
                host=reauth_entry.data[CONF_HOST],
                scheme=reauth_entry.data.get(CONF_SCHEME, "http"),
                api_key=api_key,
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_API_KEY: api_key},
                )

        return self.async_show_form(
            step_id="reauth_local",
            data_schema=vol.Schema({
                vol.Optional(CONF_API_KEY, default=""): PASSWORD_SELECTOR,
            }),
            errors=errors,
            description_placeholders={"host": reauth_entry.data.get(CONF_HOST, "")},
        )

    # ------------------------------------------------------------------
    # Reconfigure
    # ------------------------------------------------------------------

    def _other_entry_has_unique_id(self, entry: ConfigEntry, unique_id: str) -> bool:
        return any(
            other.entry_id != entry.entry_id and other.unique_id == unique_id
            for other in self.hass.config_entries.async_entries(DOMAIN)
        )

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        """Modus-Selector: gleicher Modus -> In-Place-Reconfigure, sonst Moduswechsel."""
        entry = self._get_reconfigure_entry()
        current_mode = entry.data.get(CONF_MODE, MODE_CLOUD)

        if user_input is not None:
            target = user_input[CONF_MODE]
            if target == current_mode:
                if target == MODE_LOCAL:
                    return await self.async_step_reconfigure_local()
                return await self.async_step_reconfigure_cloud()
            if target == MODE_LOCAL:
                return await self.async_step_switch_to_local()
            return await self.async_step_switch_to_cloud()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_MODE, default=current_mode): vol.In([MODE_CLOUD, MODE_LOCAL]),
            }),
        )

    async def async_step_switch_to_cloud(self, user_input=None) -> ConfigFlowResult:
        """Moduswechsel Lokal -> Cloud: neue Zugangsdaten, kompletter Datenersatz."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            abb_id = _clean(user_input.get(CONF_ABB_ID))
            api_key = _clean(user_input.get(CONF_API_KEY))

            new_uid = f"{DOMAIN}_{abb_id}"
            if abb_id and self._other_entry_has_unique_id(entry, new_uid):
                return self.async_abort(reason="already_configured")

            if not abb_id or not api_key:
                errors = {"base": "credentials_required"}
            else:
                errors, api_key = await self._cloud_validate(
                    abb_id=abb_id, api_key=api_key, check_stream=True
                )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_uid,
                    title=_cloud_title(abb_id),
                    data={
                        CONF_MODE: MODE_CLOUD,
                        CONF_ABB_ID: abb_id,
                        CONF_API_KEY: api_key,
                    },
                )

        return self.async_show_form(
            step_id="switch_to_cloud",
            data_schema=_schema_cloud(user_input),
            errors=errors,
            description_placeholders={"kb_url": API_KEY_DOCS_URL},
        )

    async def async_step_switch_to_local(self, user_input=None) -> ConfigFlowResult:
        """Moduswechsel Cloud -> Lokal: neue Zugangsdaten, kompletter Datenersatz."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = normalize_local_host(user_input[CONF_HOST])
            scheme = user_input[CONF_SCHEME]
            api_key = _clean(user_input.get(CONF_API_KEY)) or None

            new_uid = f"local_{host}"
            if self._other_entry_has_unique_id(entry, new_uid):
                return self.async_abort(reason="already_configured")

            errors = await _local_errors(self.hass, host=host, scheme=scheme, api_key=api_key)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_uid,
                    title=_local_title(host),
                    data={
                        CONF_MODE: MODE_LOCAL,
                        CONF_HOST: host,
                        CONF_SCHEME: scheme,
                        CONF_API_KEY: api_key,
                    },
                )

        return self.async_show_form(
            step_id="switch_to_local",
            data_schema=_schema_local(),
            errors=errors,
        )

    async def async_step_reconfigure_cloud(self, user_input=None) -> ConfigFlowResult:
        """Cloud bleibt Cloud: Gateway-ID und/oder API Key ändern.

        Ein leeres Key-Feld behält den gespeicherten Key.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            abb_id = _clean(user_input.get(CONF_ABB_ID)) or entry.data.get(CONF_ABB_ID, "")
            api_key = _clean(user_input.get(CONF_API_KEY)) or entry.data.get(CONF_API_KEY) or ""

            new_uid = f"{DOMAIN}_{abb_id}"
            if self._other_entry_has_unique_id(entry, new_uid):
                return self.async_abort(reason="already_configured")

            if not abb_id or not api_key:
                errors = {"base": "credentials_required"}
            else:
                errors, api_key = await self._cloud_validate(
                    abb_id=abb_id, api_key=api_key, check_stream=True, entry=entry
                )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_uid,
                    title=_cloud_title(abb_id),
                    data_updates={
                        CONF_ABB_ID: abb_id,
                        CONF_API_KEY: api_key,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure_cloud",
            data_schema=vol.Schema({
                vol.Optional(CONF_ABB_ID, default=entry.data.get(CONF_ABB_ID, "")): str,
                vol.Optional(CONF_API_KEY, default=""): PASSWORD_SELECTOR,
            }),
            errors=errors,
        )

    async def async_step_reconfigure_local(self, user_input=None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = normalize_local_host(user_input[CONF_HOST])
            scheme = user_input[CONF_SCHEME]
            api_key = _clean(user_input.get(CONF_API_KEY)) or entry.data.get(CONF_API_KEY)

            new_uid = f"local_{host}"
            if self._other_entry_has_unique_id(entry, new_uid):
                return self.async_abort(reason="already_configured")

            errors = await _local_errors(self.hass, host=host, scheme=scheme, api_key=api_key)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_uid,
                    title=_local_title(host),
                    data_updates={
                        CONF_HOST: host,
                        CONF_SCHEME: scheme,
                        CONF_API_KEY: api_key,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure_local",
            data_schema=_schema_local(entry.data),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return SmartEmsOptionsFlow()


class SmartEmsOptionsFlow(config_entries.OptionsFlowWithReload):
    """Optionen: nur das Abfrageintervall. Zugangsdaten laufen über Reauth/Reconfigure."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                },
            )

        # Untergrenze 10 s: 360 Abfragen/h am Stream-Endpunkt, das Cloud-Limit
        # liegt bei 500 Abfragen pro Stunde und Endpunkt.
        schema = vol.Schema({
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN),
            ): vol.All(vol.Coerce(int), vol.Range(min=10)),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
