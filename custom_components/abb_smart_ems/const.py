# const.py
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "abb_smart_ems"

# Credentials / Config
CONF_ABB_ID = "abb_id"    # Gateway-ID der Anlage (Pfadparameter {abbId} der Cloud-API)
CONF_API_KEY = "api_key"  # Cloud: Pflicht (einziger Auth-Weg); Lokal: optional

# Optionen
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN = 10  # Sekunden – der v3-Stream liefert typischerweise iv≈10s

# API: eine gemeinsame Basis-URL für alles
# Doku: https://api.energy.mybuildings.abb.com/abb/documentation
CLOUD_BASE = "https://api.energy.mybuildings.abb.com"

# Wissensartikel "Smart EMS API" (Erzeugen des Cloud-API-Schlüssels, Rate-Limits)
API_KEY_DOCS_URL = "https://abbag.zohodesk.eu/portal/de/kb/articles/api"

# Plattformen
PLATFORMS = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.DATETIME,
]

# Verbindungsmodus
CONF_HOST = "host"
CONF_SCHEME = "scheme"
CONF_MODE = "mode"
MODE_CLOUD = "cloud"
MODE_LOCAL = "local"

# Branding (für device_info)
INTEGRATION_NAME = "ABB Smart EMS"
MANUFACTURER = "ABB"
MODEL = "Energy Edge EX.1 (Cloud API)"
MODEL_LOCAL = "Energy Edge EX.1 (Local API)"
