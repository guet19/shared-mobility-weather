# Wetter und Shared Mobility in Schweizer Städten

**Forschungsfrage:** Wie stark beeinflusst das Wetter die Nutzung von Sharing-Velos, E-Bikes und E-Scootern, und unterscheidet sich dieser Effekt zwischen Schweizer Städten und Fahrzeugtypen?

Gruppenprojekt im Modul Data Analytics (ZHAW, 3DA-WIN).

## Hypothesen

- **H1:** Niederschlag senkt die Zahl der Fahrten pro Stunde deutlich, Temperatur erhöht sie.
- **H2:** E-Scooter reagieren stärker auf Regen und Kälte als (E-)Velos.
- **H3:** Der Wettereffekt unterscheidet sich zwischen den Städten (z.B. topografisch, Grösse, Bevölkerungsstruktur).

## Datenquellen

| Quelle | Inhalt | Erhebung | Schlüssel für Join |
|---|---|---|---|
| sharedmobility.ch (GBFS-API, BFE) | Position aller freien Sharing-Fahrzeuge der Schweiz | **selbst gesammelt**, alle 10 Min., über mehrere Wochen | `bfs_nr` (Point-in-Polygon), Zeit (Stunde UTC) |
| MeteoSchweiz Open Data (SwissMetNet, stündlich) | Temperatur, Niederschlag, Wind, Sonnenschein | Download via API (rückwirkend verfügbar) | Station → nächstgelegene Stadt, Zeit (Stunde UTC) |
| swisstopo swissBOUNDARIES3D (api3.geo.admin.ch) | Gemeindegrenzen | API | `bfs_nr` |
| BFS Regionalporträts (Gemeindekennzahlen) | Einwohner, Dichte, Altersstruktur u.a. | Download | `bfs_nr` |
| api.sharedmobility.ch `identify` | Fahrzeugtyp pro `vehicle_type_id` | API | `vehicle_type_id` |

## Wie „Nutzung" gemessen wird

Die API zeigt nur **freie** Fahrzeuge. Ein Fahrzeug in Benutzung verschwindet aus dem Feed. Der Collector vergleicht jeden Snapshot mit dem vorherigen und speichert zwei Masse:

1. **Abfahrten (`flows`, Hauptmass):** Anzahl Fahrzeuge pro Anbieter × Fahrzeugtyp × Gemeinde, die seit dem letzten Snapshot verschwunden sind (`n_departed`), neu erschienen sind (`n_arrived`) oder sich mit gleicher ID ≥150 m bewegt haben (`n_moved`). Dieses Mass funktioniert unabhängig von den Fahrzeug-IDs. Das ist wichtig, weil die meisten Anbieter (Voi, Velospot, Nextbike, Bolt, Dott, Donkey) nach jeder Fahrt eine **neue ID** vergeben (Datenschutzregel des GBFS-Standards). Gemessen am 13.11.2026: Bei diesen Anbietern tauchen nur 0–9 % der verschwundenen IDs wieder auf, bei Lime und Pickebike rund 80 %.
2. **Einzelne Fahrten (`trips`):** nur für Anbieter mit stabilen IDs (v.a. Lime, Pickebike). Verschwindet ein Fahrzeug und taucht es ≥150 m entfernt wieder auf, ist das eine Fahrt (`reappeared`). Ist es in beiden Snapshots sichtbar, aber ≥150 m verschoben, ist es eine kurze Fahrt (`moved`). Daraus lassen sich Distanzen, Start- und Zielorte sowie die Plausibilität der Abfahrten prüfen.

Anbieter, die ihre IDs bei *jeder Abfrage* neu vergeben (Bird), werden automatisch erkannt und nur in der Verfügbarkeit gezählt. `provider_stats` protokolliert pro Lauf und Anbieter, wie viele IDs verschwunden, neu oder verschoben sind. Daraus wird auch die Aktualität des Feeds pro Anbieter beurteilt (Velospot aktualisiert z.B. nur in Schüben).

Die Zielvariable der Modelle ist z.B. **Abfahrten pro Stunde pro 100 verfügbare Fahrzeuge** (Auslastung) je Stadt × Fahrzeugtyp × Stunde.

Bekannte Verzerrungen, die in der Aufbereitung behandelt werden:
- Umverteilung/Laden durch Betreiber sieht aus wie eine Fahrt/Abfahrt → Filter über Geschwindigkeit (>25–30 km/h), Distanz und nächtliche Massenverschiebungen; ein Fahrzeug, das als defekt markiert wird, zählt ebenfalls als Abfahrt
- Rundfahrten mit Rückgabe am Startort (<150 m) werden nicht erkannt
- Unregelmässige Abstände zwischen Snapshots → Aggregation auf Stundenebene

## Projektstruktur

```
collector/
  build_reference.py   # einmalig: Gemeindepolygone + Fahrzeugtyp-Mapping
  collect.py           # ein Snapshot (alle 10 Min. ausführen)
data/
  reference/           # municipalities.geojson, vehicle_types.csv
  raw/                 # availability/, flows/, trips/, provider_stats/, runs/  (je eine Datei pro Lauf)
.github/workflows/collect.yml   # automatische Sammlung via GitHub Actions
run_collector_local.sh          # Alternative: Sammlung auf eigenem Rechner
notebooks/             # (folgt) 01_preparation, 02_database, 03_eda, 04_models
```

## Datensammlung starten

**Variante A – GitHub Actions (empfohlen, läuft ohne eigenen Rechner):**
1. Öffentliches GitHub-Repository erstellen und diesen Ordner pushen (öffentlich = unbegrenzte Actions-Minuten + Bonuspunkt).
2. Unter *Actions* den Workflow `collect-shared-mobility` aktivieren und einmal manuell starten (*Run workflow*).
3. Die Daten erscheinen als Commits unter `data/raw/`.

**Variante B – lokal (Mac muss eingeschaltet bleiben):**
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-collector.txt
nohup ./run_collector_local.sh > collector.log 2>&1 &   # starten
kill $(cat collector.pid)                               # stoppen
```

## Geplante Analyse

1. **Aufbereitung & Integration:** Rohdaten zusammenführen, Fahrzeugtypen harmonisieren, Umverteilungen filtern, auf Stadt × Typ × Stunde aggregieren, mit Wetter (nächste Station) und BFS-Daten joinen. Datensätze vor/nach jedem Join dokumentieren.
2. **SQLite-Datenbank:** Tabellen `trips`, `availability`, `weather`, `municipalities`, Abfragen mit SQL aus Python (Joins, Aggregationen).
3. **EDA:** Tagesgänge, Heatmaps Stunde × Wochentag, Fahrten vs. Temperatur/Niederschlag, Boxplots Regen/kein Regen je Fahrzeugtyp, Karten der Start-/Zielorte.
4. **Modellierung:** Multiple lineare Regression (statsmodels) mit Interaktionen Wetter × Fahrzeugtyp und Wetter × Stadt für den Gruppenvergleich, zusätzlich Random Forest. Evaluation mit R², RMSE, MAE auf einem zeitlich getrennten Testset.
5. **Zusatz:** ANOVA (Unterschiede zwischen Städten), Korrelationsanalyse (mit p-Werten), k-Means (Städte/Stunden nach Nutzungsprofil), Karten mit folium.

## Abdeckung der Bewertungskriterien

| Kriterium | Umsetzung |
|---|---|
| Web-API, selbst gesammelt | GBFS-Feed sharedmobility.ch, mehrere Wochen, alle 10 Min. |
| ≥2 Quellen integriert | Sharing + MeteoSchweiz + BFS + swisstopo, Joins über `bfs_nr`/Zeit/Station |
| SQLite + SQL aus Python | `notebooks/02_database` |
| EDA (grafisch + nicht-grafisch) | `notebooks/03_eda` |
| Regression mit Gruppenvergleich | Interaktionsmodelle Städte × Fahrzeugtypen |
| Gütemasse | R², adj. R², RMSE, MAE, Train/Test zeitlich getrennt |
| *Bonus:* Kreativität | eigene Fahrtenerkennung aus Snapshots, automatisierte Sammlung |
| *Bonus:* Geodaten | Point-in-Polygon, Karten der Fahrten (folium) |
| *Bonus:* ANOVA / Korrelation / Chi² | Städtevergleich, Wetter-Korrelationen |
| *Bonus:* k-Means | Clustering der Nutzungsprofile |
| *Bonus:* GitHub öffentlich | dieses Repository |
| *Bonus:* PostgreSQL | optional zusätzlich zu SQLite |

## Einschränkungen

- Erhebung im November/Dezember: kalte Jahreszeit, geringere Temperaturspanne als im Sommer.
- Fahrten werden aus Snapshots abgeleitet und nicht vom Betreiber geliefert, die Zahlen sind daher eine Schätzung (Unter-/Übererfassung wird diskutiert).
