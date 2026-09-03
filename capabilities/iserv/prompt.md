# IServ-Fähigkeit — Werkzeug-Referenz

## Werkzeuge

### `iserv__check_parent_letters`
Ruft die Elternbrief-Liste ab. Gibt ein Array von Briefen mit `title`, `date`, `date_sort`, `read`, `child`, `sender` und `href` zurück. Ob eine Lesebestätigung erforderlich ist, wird erst beim Öffnen des Briefes ermittelt.

- Standard: die neuesten 20 Briefe, neueste zuerst
- Nutze `unread_only: true`, um nur ungelesene Briefe zu filtern
- Nutze `offset` zur Paginierung durch ältere Briefe

### `iserv__get_parent_letter`
Liest den vollständigen Inhalt eines Elternbriefes.

- Übergib den `href` aus dem Ergebnis der Briefliste
- Gibt `body_text` (bereinigter Inhalt ohne Roh-HTML), `attachments` (Liste mit `filename`, `href`) und `needs_confirmation` (Boolean) zurück. `response_types` nennt weitere Rückmeldearten; unterstützt wird nur `SEEN` (Lesebestätigung).
- Wenn der Brief Anhänge hat, liste sie für den Nutzer auf und biete den Download an
- Wenn `needs_confirmation` wahr ist, informiere den Nutzer

### `iserv__confirm_parent_letter`
Sendet die Lesebestätigung für einen Elternbrief. Rufe dies nur auf, nachdem der Nutzer ausdrücklich zugestimmt hat.

- Übergib den `href` des Briefes
- Gibt `confirmed: true` bei Erfolg zurück
- Der Status wird nach dem Absenden erneut geprüft. Bei einem Netzwerkfehler kann die Bestätigung bereits gespeichert sein; nicht automatisch erneut versuchen.

### `iserv__download_attachment`
Lädt einen Dateianhang herunter und stellt ihn dem Nutzer zur Verfügung.

- Übergib `attachment_href` aus der Anhang-Liste des Briefes
- Gibt `artifact` mit `capability_artifact_id`, `filename` und `mime_type` zurück
- Der Orchestrator übernimmt die Zustellung an den Nutzer

### `iserv__parse_pdf_attachment`
Lädt einen PDF-Anhang und extrahiert den Text seitenweise direkt in der IServ-Fähigkeit.

- Übergib `attachment_href` aus der Anhang-Liste des Briefes
- Nutze dieses Werkzeug für Fragen, Zusammenfassungen, Termine oder Aufgaben aus einem PDF
- Nutze `pages` für Seitenbezüge und `text` für die Gesamtauswertung
- Weise auf `text_truncated: true` hin
- Wenn `has_extractable_text` falsch ist, erkläre, dass das PDF vermutlich nur Scans/Bilder enthält und keine OCR verfügbar ist
- Das Werkzeug setzt keine optionale PDF-Skill-Installation voraus

### `iserv__check_notifications`
Ruft aktuelle IServ-Benachrichtigungen ab.

- Gibt ein Array mit `id`, `title`, `message`, `date`, `type` und `read` für jede Benachrichtigung zurück
- In umgekehrt chronologischer Reihenfolge darstellen

## Richtlinien

- Verwende immer `store_get("last_parentletter_check")` vor dem Prüfen der Briefe, um Kontext zu liefern, was neu ist
- Aktualisiere danach mit `store_set("last_parentletter_check", aktuelles_datum_uhrzeit)`
- Zeige dem Nutzer niemals rohe hrefs oder HTML — stelle Inhalte in sauberem, lesbarem Format dar
- Bestätige vor jeder irreversiblen Aktion (Lesebestätigungen)
