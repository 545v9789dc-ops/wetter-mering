# Zeitsteuerung der Wetterabrufe

Die lokalen Zielzeiten in Europe/Berlin bleiben 02:00, 05:00, 10:00,
15:00, 18:00 und 21:30. GitHub erhält weiterhin die UTC-Alternativen für
Sommer- und Winterzeit, jetzt aber als zwölf einzelne Cron-Ausdrücke.
Nur so bezeichnet `github.event.schedule` eindeutig eine UTC-Uhrzeit.

| Lokal | UTC Sommer | UTC Winter |
|---|---|---|
| 02:00 | 00:00 | 01:00 |
| 05:00 | 03:00 | 04:00 |
| 10:00 | 08:00 | 09:00 |
| 15:00 | 13:00 | 14:00 |
| 18:00 | 16:00 | 17:00 |
| 21:30 | 19:30 | 20:30 |

## Entscheidung

`scripts/weather_schedule.py` liest den Cron-Auslöser und den ursprünglichen
`created_at`-Zeitpunkt des Workflow-Laufs über die Actions-API (`actions: read`).
Die letzte tägliche UTC-Ausführung dieses Cron-Ausdrucks vor oder zu diesem
Zeitpunkt wird nach Europe/Berlin umgerechnet. Ausschließlich diese geplante
Ortszeit entscheidet über den Abruf. Die tatsächliche Startminute wird nicht
verwendet. Beispiel: Der Sommer-Auslöser `0 8 * * *` bleibt der 10-Uhr-Abruf,
auch wenn der Lauf erst um 11:35 Uhr startet.

Die API-Zeit bleibt auch bei späteren Wiederholungen und langen Runner-
Warteschlangen gleich. API-Fehler oder unbekannte Auslöser lassen den Schritt
fehlschlagen, statt einen Abruf still zu überspringen. Die Logs enthalten Cron,
Erstellungszeit, errechnete UTC-/Berlin-Zeit und Entscheidung.

GitHub liefert mit `event.schedule` einen Cron-Ausdruck, keinen datierten
Sollzeitpunkt. Daher wird vorausgesetzt, dass GitHub den Lauf weniger als
24 Stunden nach dem betreffenden täglichen Cron-Termin anlegt. Noch längere
Verzögerungen vor der Laufanlage sind nicht eindeutig rekonstruierbar.
Nach der Laufanlage gibt es diese Begrenzung für Runner-Wartezeiten nicht.
Ausgefallene oder von GitHub verworfene Auslöser werden nicht nachgeholt.

## Zeitumstellung

- Frühjahr: 02:00 existiert am Umstellungstag nicht; an diesem Tag gibt es
  fünf Abrufe, ohne Ersatzabruf um 03:00.
- Herbst: 02:00 tritt zweimal auf. Nur die erste Instanz (Sommerzeit,
  `fold=0`) wird zugelassen. Die zweite (`fold=1`) wird übersprungen, auch
  wenn der erste Lauf erst während oder nach der zweiten Stunde startet.
  Dies funktioniert an jedem tatsächlichen Umstellungstag, nicht nur am 31.10.
- `workflow_dispatch` läuft immer und benötigt keine Zeitabfrage bei der API.

Checkout wurde vor die Zeitprüfung verlegt, damit das geprüfte Skript verfügbar
ist. Die bisherigen Python-, Test-, Abruf- und Daten-Commit-Schritte samt ihren
Bedingungen bleiben erhalten; nicht passende UTC-Alternativen machen nur den
Checkout und die Zeitprüfung.

## Validierung

`python -m unittest discover -s tests -v`

27 Tests bestanden (18 vorhandene, 9 neue). Neue Abdeckung: alle zwölf
Cron-Ausdrücke an jedem Tag 2025–2027 mit 0, 31, 95, 360 und 1439 Minuten
Verzögerung; fünf/sechs tägliche Abrufe; keine doppelte Zielzeit; explizite
DST-Fälle am 29.03.2026 und 25.10.2026; Mitternachtswechsel; manuelle Läufe;
ursprünglicher API-Zeitpunkt; ungültige Eingaben; Workflow-Verdrahtung.
Die Tests rufen keine Wetterdienste ab. Ein echter GitHub-schedule-Lauf
wurde für diese Validierung nicht ausgelöst.

Referenz: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
