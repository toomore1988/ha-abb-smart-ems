# ABB Smart EMS – Home Assistant (Custom Integration)

> **Kein offizielles ABB-Produkt.** Community-Integration ohne Gewährleistung. ABB bietet zur Energy API keinen Kundensupport.

> **Status: v0.1.0, noch nicht an einer echten Anlage erprobt.** Der Code ist gegen die Swagger-Spezifikation der ABB-Cloud (v1.88.11) gebaut und durch automatisierte Tests mit nachgebildeten API-Antworten abgedeckt. Was vor dem ersten produktiven Einsatz an einer echten Anlage zu prüfen ist, steht unter [Offene Punkte](#offene-punkte).

Bindet das ABB Smart EMS mit dem Gateway **Energy Edge EX.1** in Home Assistant ein — wahlweise über die **Cloud-API** (voller Funktionsumfang inkl. Steuerung) oder direkt über die **lokale REST-API** des Gateways (nur Sensoren, kein Internet nötig).

- **Cloud-API**: [myBuildings Energy API](https://api.energy.mybuildings.abb.com/abb/documentation) – Sensoren und Steuerung
- **Lokale API**: `GET /v2/point` und `GET /v2/devices` direkt am Gateway – nur Sensoren
- **Anmeldung an der Cloud: ausschließlich per Cloud-API-Schlüssel.** Eine Anmeldung mit E-Mail und Passwort gibt es in dieser Integration nicht.

---

## Voraussetzungen

- Home Assistant ≥ 2025.8
- ABB Smart EMS mit Gateway Energy Edge EX.1

**Cloud-Modus** (voller Funktionsumfang):
- Gateway-ID (`abbId`)
- Cloud-API-Schlüssel

**Lokaler Modus** (nur Sensoren):
- Aktivierte lokale Schnittstelle am Gateway
- IP-Adresse des Gateways im lokalen Netzwerk
- optional: lokaler API-Schlüssel

---

## Installation

### HACS (empfohlen)

1. HACS → Integrationen → `⋮` → **Benutzerdefinierte Repositories**
2. URL: `https://github.com/toomore1988/ha-abb-smart-ems` · Kategorie: **Integration**
3. „ABB Smart EMS" suchen → installieren → Home Assistant neu starten

### Manuell

Ordner `custom_components/abb_smart_ems` in `<config>/custom_components/` kopieren, HA neu starten.

---

## Einrichtung

### Gateway-ID finden

1. Im [myBuildings-Portal](https://mybuildings.abb.com) anmelden
2. Registerkarte **«Mein Zuhause»** → Abo-Bereich **«Dein Smart Energy …»**
3. Unter **«Hardware»** beim Energy Edge EX.1 auf **«Verwalten»** klicken
4. In der Geräteübersicht steht die Gateway-ID im Feld **«ID»** (z. B. `002A…`), darunter die **IP-Adresse** für den lokalen Modus

### Cloud-API-Schlüssel erstellen

Quelle: Wissensartikel [Smart EMS API](https://abbag.zohodesk.eu/portal/de/kb/articles/api).

1. Im Smart-EMS-Webportal oben rechts das **Profil** öffnen
2. **«Cloud-API-Schlüssel»** wählen
3. Mit **«+»** einen neuen Schlüssel anlegen und die Parameter festlegen:
   - **«Erneuerung erlauben»**: am besten **nicht** aktivieren (siehe [Schlüssel-Erneuerung](#schlüssel-erneuerung-rotation))
   - Falls der Dialog Berechtigungen anbietet: Für die Steuerung aus Home Assistant (Betriebsmodi, Parameter, Ladeziele) braucht der Schlüssel neben Lese- auch Schreibrechte
4. Den Schlüssel kopieren und beim Einrichten der Integration in das Feld **Cloud-API-Schlüssel** einfügen

> Fehlt der Menüpunkt «Cloud-API-Schlüssel», den Support kontaktieren — die Funktion wird auf Anfrage freigeschaltet.

### Lokale Schnittstelle aktivieren (nur für den lokalen Modus)

Quelle: Kurzanleitung „ABB-free@home® / ABB Smart EMS" (2CKA000073B0357), Kapitel 2.

1. Geräteübersicht des Energy Edge EX.1 im myBuildings-Portal öffnen (wie unter [Gateway-ID finden](#gateway-id-finden))
2. **«Aktiviere die Schnittstelle (http API) …»** einschalten
3. Optional **«API-Schlüssel generieren»** — dieser Schlüssel sichert nur die lokale Schnittstelle und ist **nicht** der Cloud-API-Schlüssel
4. IP-Adresse und ggf. Schlüssel notieren

### Ersteinrichtung in Home Assistant

Einstellungen → Geräte & Dienste → **Integration hinzufügen** → **ABB Smart EMS**

Im ersten Schritt den **Verbindungsmodus** wählen:

#### Cloud-Modus

| Feld | Pflicht | Beschreibung |
|---|---|---|
| Gateway-ID (abbId) | Ja | ID des Energy Edge EX.1 aus dem myBuildings-Portal |
| Cloud-API-Schlüssel | Ja | Zuvor erstellter Schlüssel (siehe oben) |

Die Integration tauscht den Schlüssel beim Einrichten gegen einen Access-Token und ruft einmal die Live-Daten ab. Schlägt das fehl, wird kein Eintrag angelegt.

#### Lokaler Modus

| Feld | Pflicht | Beschreibung |
|---|---|---|
| IP-Adresse / Hostname | Ja | Gateway-IP im lokalen Netzwerk (z. B. `192.168.1.100`) |
| Protokoll | Ja | `http` oder `https` (Standard: `http`) |
| Lokaler API-Schlüssel | Nein | Nur falls am Gateway ein Schlüssel generiert wurde (`X-API-Key`-Header) |

Die Integration testet beim Einrichten direkt die Verbindung (`GET /v2/point`). Bei HTTPS wird das selbst-signierte Zertifikat des Gateways akzeptiert.

### Neu konfigurieren

Einstellungen → Geräte & Dienste → **ABB Smart EMS** → `⋮` → **Neu konfigurieren**

- **Cloud**: Gateway-ID und Cloud-API-Schlüssel ändern — ein leeres Schlüssel-Feld behält den gespeicherten Schlüssel. Beim Wechsel der Gateway-ID bleiben die Entitäten erhalten.
- **Lokal**: IP-Adresse/Hostname, Protokoll und Schlüssel ändern (z. B. nach einem IP-Wechsel des Gateways).
- **Wechsel Cloud ↔ Lokal**: Im ersten Schritt den anderen Modus auswählen und dessen Zugangsdaten eingeben. Sensoren und Geräte behalten ihre Entity-IDs (Historie bleibt erhalten).

  > **Achtung beim Wechsel auf Lokal:** Betriebsmodi, Parameter und Ladeziel-Termine (Select/Number/Datetime-Entitäten) gibt es nur im Cloud-Modus. Nach dem Wechsel sind sie nicht verfügbar — **Skripte, Automationen und Dashboards, die diese Steuerelemente verwenden, funktionieren dann nicht mehr.** Beim Zurückwechseln zu Cloud werden dieselben Entity-IDs wiederverwendet.

Die neuen Werte werden vor dem Speichern gegen die API geprüft; danach lädt die Integration automatisch neu.

### Erneute Authentifizierung

Lehnt die Cloud den Schlüssel ab (gelöscht, abgelaufen, ungültig), stoppt Home Assistant die Abfragen und zeigt eine Benachrichtigung. Über den Link darin lässt sich ein neuer Schlüssel eintragen — ohne die Integration zu löschen.

### Abfrageintervall (Optionen)

Konfigurieren → **Optionen** → Abfrageintervall in Sekunden (Standard und Untergrenze: **10 s**).

---

## Rate-Limits der Cloud

Laut Wissensartikel erlaubt die Cloud **500 Abfragen pro Stunde und Endpunkt**; der Token-Tausch (`/v3/auth/refresh`) nur **50 pro Stunde**. Die Integration ist darauf ausgelegt:

| Endpunkt | Abfragen durch die Integration | Limit |
|---|---|---|
| Live-Daten (`/v3/users/{abbId}/data/stream`) | 360/h bei 10 s Intervall | 500/h |
| Geräte und Einstellungen (`/v3/users/{abbId}/devices`) | höchstens 60/h, zusätzlich einmal nach jedem Schreibbefehl | 500/h |
| Tagesstatistik (`/v1/statistics/gateways/{abbId}`) | 12/h | 500/h |
| Token-Tausch (`/v3/auth/refresh`) | etwa 1/h (Access-Token gilt 1 h und wird wiederverwendet) | 50/h |

Greifen weitere Anwendungen mit hoher Frequenz auf dieselben Endpunkte zu, kann das Limit trotzdem erreicht werden. Dann das Abfrageintervall erhöhen. Schlägt der Token-Tausch fehl, wartet die Integration mindestens 2 Minuten (nach einem Limit-Fehler 10 Minuten) bis zum nächsten Versuch.

### Schlüssel-Erneuerung (Rotation)

Ist am Schlüssel **«Erneuerung erlauben»** aktiv, gibt die Cloud bei jedem Token-Tausch einen neuen Schlüssel aus und entwertet den bisherigen sofort. Die Integration unterstützt das: Sie übernimmt den neuen Schlüssel und speichert ihn umgehend in der Konfiguration.

Empfohlen ist trotzdem ein Schlüssel **ohne** Erneuerung:

- Ein rotierender Schlüssel lässt sich nicht parallel in einer zweiten Anwendung verwenden — die Anwendungen entwerten sich gegenseitig den Schlüssel.
- Geht genau die Antwort verloren, die den neuen Schlüssel enthält (Verbindungsabbruch), ist der alte Schlüssel bereits ungültig. Dann muss ein neuer Schlüssel erstellt und über **Neu authentifizieren** eingetragen werden.

---

## Funktionsumfang

| Entitätstyp | Cloud | Lokal |
|---|:---:|:---:|
| Echtzeit-Leistungssensoren (PV, Verbrauch, Netz, Batterie) | ✓ | ✓ ¹ |
| Energie-Intervallwerte (Wh, per Default deaktiviert) | ✓ | ✓ |
| Batterie-SOC, Geräteübersicht | ✓ | ✓ |
| Gerätesensoren (Leistung, SOC, Temperatur, …) | ✓ | ✓ |
| Verbindungsstatus pro Gerät | ✓ | ✓ |
| Tages-Energiesensoren (PV, Verbrauch, Netz, Batterie) | ✓ | ✓ ² |
| Tagesstatistiken (Autarkiegrad, Eigenverbrauchsquote) | ✓ | ✓ ² |
| Betriebsmodi-Select (Wallbox, Batterie, …) | ✓ | – |
| Parameter-Number (SOC-Grenzen, Konstantstrom, …) | ✓ | – |
| Datetime-Entitäten (Ladeziel-Termin) | ✓ | – |

> ¹ Netz Import/Export (W) liefert die lokale API nicht direkt — die Werte werden aus der Energiebilanz berechnet (cW + bcW − pW − bdW).
> ² Im lokalen Modus werden die Tages-Energiewerte durch Integration der Leistungswerte über die Zeit berechnet. Die Tageszähler überleben Neustarts und Reloads.

### Unterstützte Gerätetypen

Alle am Gateway eingerichteten Geräte werden automatisch erkannt. Die Typnamen stammen aus der Cloud-API.

| Gerät | Typ in der API | Sensoren | Steuerung (nur Cloud) |
|---|---|---|---|
| Batteriespeicher | `battery` | SOC, Leistung | Betriebsmodus, SOC-Grenzen, Eco-, Peak-Shaving-, Tarif-Parameter |
| Wallbox | `carcharging`, `car` | Leistung, SOC, Restreichweite | Lademodus, Konstantstrom, Ladeziele |
| V2X-Wallbox | `v2x` | Leistung | Lademodus |
| Wärmepumpe | `heatpump` | Betriebszustand, Temperatur | Betriebsmodus |
| Warmwasser | `waterheater` | Leistung, Temperatur | Betriebsmodus, Leistung |
| Smart Plug | `smartplug` | Leistung, Schaltzustand | Schaltmodus |
| Schalter | `switches` | Schaltzustand | Schaltmodus |
| Wechselrichter | `inverter` | Leistung | Einspeisebegrenzung |
| Übrige (Smart Meter, Submeter, Temperatur, …) | `smartmeter`, `submeter`, `temperature`, … | was das Gerät liefert | – |

Kind-Geräte erscheinen in Home Assistant unter dem Namen, der im Smart EMS vergeben wurde, mit dem Gerätemodell aus der Cloud. Als Hersteller wird bewusst nichts eingetragen: Es sind Geräte beliebiger Hersteller, die das Gateway steuert.

---

## Entitäten

### Anlage (Site-Level)

#### Echtzeit-Leistung

| Entität | Einheit | Beschreibung |
|---|---|---|
| PV-Leistung | W | Aktuelle Erzeugungsleistung |
| Hausverbrauch | W | Aktueller Gesamtverbrauch |
| Batterie-Leistung | W | Positiv = Laden, negativ = Entladen |
| Netz Import | W | Bezug aus dem Netz |
| Netz Export | W | Einspeisung ins Netz |
| Netzleistung | W | Positiv = Bezug, negativ = Einspeisung |
| PV-Überschuss | W | PV-Leistung − Hausverbrauch − Batterie-Leistung; positiv = Überschuss |

#### Energie-Intervallwerte

Rohwerte des letzten Stream-Intervalls (≈ 10 s). Für Dashboards ungeeignet, daher **per Default deaktiviert** — fürs Energie-Dashboard die Tages-Energiesensoren verwenden.

| Entität | Einheit |
|---|---|
| PV-Energie (Intervall) | Wh |
| Verbrauch (Intervall) | Wh |
| Netzbezug (Intervall) | Wh |
| Netzeinspeisung (Intervall) | Wh |
| Batterie geladen (Intervall) | Wh |
| Batterie entladen (Intervall) | Wh |

#### Tages-Energie und Statistiken

Klasse `total_increasing` — direkt im Energie-Dashboard verwendbar. Im Cloud-Modus kommen Erzeugung, Verbrauch und Eigenverbrauch aus `/v1/statistics/gateways/{abbId}` (alle 5 Minuten); Netzbezug und -einspeisung werden aus den Leistungswerten integriert, Batterie-Energie aus den Intervallwerten summiert.

| Entität | Einheit | Beschreibung |
|---|---|---|
| PV Tageserzeugung | Wh | Gesamterzeugung des heutigen Tages |
| Verbrauch heute | Wh | Gesamtverbrauch des heutigen Tages |
| Eigenverbrauch heute | Wh | Direkt selbst genutzter PV-Strom |
| Netzbezug heute | Wh | Aus dem Netz bezogene Energie |
| Netzeinspeisung heute | Wh | Ins Netz eingespeiste Energie |
| Batterie geladen heute | Wh | In die Batterie geladene Energie |
| Batterie entladen heute | Wh | Aus der Batterie entnommene Energie |
| Eigenverbrauchsquote | % | Anteil des PV-Stroms, der selbst verbraucht wurde |
| Autarkiegrad | % | Anteil des Verbrauchs, der aus PV/Batterie gedeckt wurde |

#### Sonstige

| Entität | Einheit | Beschreibung |
|---|---|---|
| Batterie-SOC | % | Aktueller Ladestand der Batterie |
| Geräte (Stream-Übersicht) | – | Anzahl der gemeldeten Geräte inkl. Rohdaten-Attributen. Diagnose-Sensor, **per Default deaktiviert** |

### Geräte (dynamisch)

Pro Gerät entstehen automatisch Sensoren, sobald das entsprechende Feld in den Live-Daten vorhanden ist. Neue Geräte werden zur Laufzeit erkannt.

| Sensor | Einheit | Bedingung |
|---|---|---|
| Leistung | W | Feld `power` |
| SOC | % | Feld `soc` |
| Temperatur | °C | Feld `temperature` |
| Aktivstatus | – | Feld `activeDevice` (1 = aktiv/laden, 0 = aus, −1 = entladen) |
| Tagesverbrauch | Wh | Feld `iWhTotal` |
| Tageseinspeisung | Wh | Feld `eWhTotal` |
| Betriebszustand | – | Feld `operationState` (Wärmepumpe) |
| Schaltzustand | – | Feld `switchState` |
| Heizungskorrektur | – | Feld `heatingAdjustment` |
| Restreichweite | km | Feld `remainingRange` |
| Verbindung (Binärsensor) | – | Feld `signal`: Ein = `connected` |

> **Tagesverbrauch / Tageseinspeisung:** Die Integration summiert die Zuwächse von `iWhTotal`/`eWhTotal` und setzt die Summe um Mitternacht auf 0. Nicht plausible Sprünge (Zählerreset, kurzzeitige `0` mit Rücksprung auf den Gesamtstand) zählen nicht mit.

### Betriebsmodi (Select, nur Cloud)

Pro steuerbarem Gerät ein Modus-Select. Die Zahlenwerte entsprechen der API.

| Gerät | Modi |
|---|---|
| Batterie | 0 Standard · 1 Eco · 2 Peak-Shaving · 3 Manuell · 4 Tarif-Optimiert · 5 Standard (aktiv) · 6 KI-Optimierung |
| Batterie, „Manuell Richtung" | 0 Laden · 1 Entladen · 2 AUS |
| Wallbox | 0 Immer laden · 1 Nur Solar · 2 Solar & Tarif · 3 Nie laden · 4 Konstanter Strom · 5 Minimal & Solar · 6 Ladeziel (kWh) · 7 Ladeziel (SoC) · 8 Aria |
| V2X | 0 Immer laden · 1 Solar-Optimiert · 2 Solar & Tarif · 3 Manuell · 4 Ziel-SOC |
| Wärmepumpe | 0 Kein Modus · 1 EIN · 2 AUS · 3 Nur Solar · 4 Solar & Tarif · 5 Keine Steuerung · 6 Normalbetrieb · 7 OEM 14 · 8 KI-Optimierung |
| Warmwasser | 1 EIN · 2 AUS · 3 Nur Solar · 4 Solar & Tarif · 5 Keine Steuerung · 6 ECO · 7 KI-Optimierung |
| Smart Plug | 1 EIN · 2 AUS · 3 Nur Solar · 4 Solar & Tarif · 5 Keine Steuerung |
| Schalter | 0 Kein Modus ¹ · 1 EIN · 2 AUS · 3 Nur Solar · 4 Solar & Tarif · 5 Keine Steuerung |

> ¹ «Kein Modus» wird nur angezeigt. Beim Schreiben nimmt die API für Schalter die Werte 1 – 5 an.

### Parameter (Number / Datetime, nur Cloud)

Die Werte wirken jeweils nur, wenn im Smart EMS der passende Modus aktiv ist.

| Gerät | Parameter | Einheit | Bereich |
|---|---|---|---|
| Wechselrichter | Einspeisebegrenzung | % | 0 – 100 |
| Batterie | Eco Entlade-, Morgen-, Lade-Limit | % | 0 – 100 |
| Batterie | SOC-Ober- und -Untergrenze | % | 0 – 100 |
| Batterie | Peak-Shaving Netzlimit, Nachladeleistung | W | 0 – 20 000 |
| Batterie | Peak-Shaving SOC-Entladegrenze, SOC-Maximum | % | 0 – 100 |
| Batterie | Manuell Lade-, Entladeleistung | W | 0 – 20 000 |
| Batterie | Tarif Preislimit | Währung/kWh ¹ | 0 – 2,00 |
| Batterie | Tarif SOC-Maximum | % | 0 – 100 |
| Wallbox | Konstantstrom | A | 6 – 32 |
| Wallbox | Ladeziel SOC, Ladeziel SOC Maximum | % | 0 – 100 |
| Wallbox | Ladeziel SOC Termin | Datum/Zeit | |
| Wallbox | Ladeziel kWh Menge, Maximum | kWh | 1 – 100 |
| Wallbox | Ladeziel kWh Termin | Datum/Zeit | |
| Warmwasser | Leistung | % | 0 – 100 |

> ¹ Angezeigt wird die in Home Assistant eingestellte Währung (z. B. `EUR/kWh`).

---

## Hinweise

- **Sprache**: Entitätsnamen folgen der HA-Sprache (Deutsch und Englisch enthalten). Die **Optionen** der Modus-Selects (z. B. „Nur Solar") sind feste deutsche Werte.
- **Batterie-Schreibschutz**: Batterie-Einstellungen werden immer als vollständiges Settings-Objekt geschrieben (read-modify-write). Direkt nach dem Start — bevor die Einstellungen geladen sind — wird ein Schreibversuch mit einer Fehlermeldung abgelehnt, statt fremde Felder auf Werks-Defaults zurückzusetzen.
- **Steuerung in zwei Systemen**: Wird ein Verbraucher sowohl vom Smart EMS als auch von Home-Assistant-Automationen geschaltet, können sich die Regeln gegenseitig überschreiben. Energetisch gesteuerte Lasten am besten nur an einer Stelle regeln.
- **Cloud-Abhängigkeit (Cloud-Modus)**: Bei Cloud-Ausfall sind alle Werte nicht verfügbar. Der lokale Modus ist davon nicht betroffen.

---

## Beispiele

Entity-IDs hängen von Gateway-ID, Gerätenamen und HA-Sprache ab. Die Beispiele gehen von der Gateway-ID `002A0001` und deutscher Sprache aus — die eigenen IDs stehen unter Einstellungen → Geräte & Dienste → ABB Smart EMS.

### Nativer Trigger/Condition: PV-Überschuss (ab HA 2026.7)

Die Integration liefert `abb_smart_ems.surplus_available` (Trigger) und `abb_smart_ems.is_surplus_present` (Condition). Beide berechnen den Überschuss (PV-Leistung − Hausverbrauch − Batterie-Leistung) selbst und bringen eine Mindestdauer mit (Default 2 Minuten), damit eine einzelne Wolkenlücke nicht sofort auslöst.

```yaml
automation:
  alias: "Wallbox Solar-Modus bei PV-Überschuss"
  trigger:
    - trigger: abb_smart_ems.surplus_available
      target:
        device_id: <device_id des ABB-Smart-EMS-Geräts>
      options:
        threshold: 500  # W
        for: "00:02:00"
  action:
    - action: select.select_option
      target:
        entity_id: select.wallbox_garage_wallbox_modus
      data:
        option: "Nur Solar"
```

### Klassisch mit `numeric_state`

```yaml
automation:
  alias: "Wallbox Solar-Modus bei PV-Überschuss"
  trigger:
    - platform: numeric_state
      entity_id: sensor.abb_smart_ems_002a0001_netzleistung_bezug_einspeisung
      below: -500        # > 500 W Einspeisung
      for: "00:02:00"
  action:
    - action: select.select_option
      target:
        entity_id: select.wallbox_garage_wallbox_modus
      data:
        option: "Nur Solar"
```

### Benachrichtigung: Batterie voll, Überschuss geht ins Netz

```yaml
automation:
  alias: "Benachrichtigung: Batterie voll, PV-Überschuss"
  trigger:
    - platform: numeric_state
      entity_id: sensor.abb_smart_ems_002a0001_batterie_soc
      above: 95
  condition:
    - condition: numeric_state
      entity_id: sensor.abb_smart_ems_002a0001_netz_export
      above: 300
  action:
    - action: notify.mobile_app_mein_telefon
      data:
        title: "ABB Smart EMS"
        message: "Batterie voll – {{ states('sensor.abb_smart_ems_002a0001_netz_export') }} W Überschuss ins Netz."
```

---

## Fehlerbehebung

### «Der API-Schlüssel wurde abgelehnt» beim Einrichten

Schlüssel **und** Gateway-ID prüfen: Die Meldung erscheint auch, wenn die Cloud den Zugriff auf die angegebene Gateway-ID verweigert. ID exakt aus dem myBuildings-Portal übernehmen.

### Alle Entitäten zeigen «Nicht verfügbar»

Protokoll auf `WARNING`/`ERROR` von `custom_components.abb_smart_ems` prüfen. Häufigste Ursachen: Cloud nicht erreichbar, Rate-Limit erreicht oder Schlüssel abgelehnt → Einstellungen → Geräte & Dienste → **Neu authentifizieren**.

### Selects und Parameter fehlen oder zeigen «Unbekannt»

Steuer-Entitäten gibt es nur im Cloud-Modus und nur für die oben gelisteten Gerätetypen. Bleibt ein Modus «Unbekannt», liefert die Cloud für dieses Gerät die Einstellung nicht im erwarteten Feld — bitte die Diagnose herunterladen (Geräte & Dienste → ABB Smart EMS → `⋮` → **Diagnose herunterladen**) und ein Issue eröffnen. Schlüssel, Gateway-ID und MAC-Adressen sind darin geschwärzt.

### Ein Gerät taucht nicht auf

Neue Geräte werden spätestens nach etwa 60 Sekunden erkannt. Sonst die Integration neu laden und prüfen, ob das Gerät im Smart EMS dem Gateway zugeordnet ist.

### Schreibbefehle schlagen fehl

Wurde der Schlüssel mit eingeschränkten Berechtigungen erstellt, kann er lesen, aber nicht steuern. Dann einen Schlüssel mit Schreibrechten erstellen und über **Neu konfigurieren** eintragen. Details zum Fehler stehen im Protokoll von `custom_components.abb_smart_ems`.

---

## Offene Punkte

Vor dem ersten produktiven Einsatz an einer echten Anlage zu prüfen:

1. **Einstellungsfelder der Geräte.** Die Swagger-Spezifikation beschreibt das Objekt `data` in `/v3/users/{abbId}/devices` nur als „gerätespezifisches Konfigurationsobjekt", ohne Feldliste. Die Integration erwartet dort dieselben Feldnamen wie in den Schreib-Endpunkten (`batteryMode`, `chargingMode`, `heatPumpChargingMode`, …). Stimmt das nicht, bleiben Selects und Parameter «Unbekannt» — Sensoren sind davon nicht betroffen.
2. **Statuscode bei abgelehntem Schlüssel.** Die Integration wertet 400, 401 und 403 beim Token-Tausch als „Schlüssel ungültig" und startet den Reauth-Dialog. Welchen Code die Cloud tatsächlich liefert, ist nicht dokumentiert.
3. **Typ `car`.** Fahrzeuge bekommen wie Wallboxen ein Lademodus-Select. Ob die Cloud das für jedes Fahrzeug annimmt, ist an einer Anlage mit angebundenem Fahrzeug zu prüfen.

---

## Deinstallation

1. Einstellungen → Geräte & Dienste → **ABB Smart EMS** → `⋮` → **Löschen**
2. Home Assistant neu starten
3. Den Ordner `custom_components/abb_smart_ems` entfernen (bei manueller Installation) oder die Integration in HACS deinstallieren

---

## Entwicklung

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
ruff check custom_components/abb_smart_ems/
```

---

## Herkunft und Lizenz

Diese Integration ist ein Fork von [ha-solarmanager](https://github.com/Soardiac/ha-solarmanager) von @Soardiac (Stand v1.12.0, Commit `257f2aa`), angepasst an die ABB-Cloud. ABB Smart EMS basiert auf der Plattform von Solar Manager; die Schnittstellen sind eng verwandt, aber nicht identisch.

Wesentliche Änderungen gegenüber der Vorlage:

- Cloud-Basis-URL `api.energy.mybuildings.abb.com`, Gateway-ID `abbId` statt `smId`
- Anmeldung nur per Cloud-API-Schlüssel; E-Mail/Passwort-Login samt Migrationshinweis entfernt
- Geräte und Einstellungen aus `/v3/users/{abbId}/devices` statt `/v1/info/sensors/{smId}` (den Endpunkt bietet die ABB-Cloud nicht)
- Unterstützung für Schlüssel-Erneuerung (Rotation), Wartezeit nach fehlgeschlagenem Token-Tausch
- Gerätetyp `switches`, Preislimit in der HA-Währung, Kind-Geräte ohne Herstellerangabe

Lizenz: MIT, siehe [LICENSE.txt](LICENSE.txt). Der Copyright-Vermerk der Vorlage bleibt erhalten.
