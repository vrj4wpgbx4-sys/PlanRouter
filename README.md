# Route Planner AI (HGV-aware) — Local Prototype

This project provides a dispatcher-style desktop GUI for planning routes and generating a readable dispatch report:
- Routing via OpenRouteService (ORS) (primary)
- Optional HERE routing client (fallback / experimentation)
- Weather snapshots via Open-Meteo
- Traffic incidents via TomTom (if key set)
- SQLite caching for API responses
- Risk scoring (simple heuristic layer)

## Quick start

1) Create and activate a virtual environment
2) Install dependencies:
```bash
pip install -r requirements.txt
```

3) Set environment variables (PowerShell examples):
```powershell
$env:ORS_API_KEY="..."
$env:TOMTOM_API_KEY="..."   # optional
$env:HERE_API_KEY="..."     # optional
```

4) Run:
```bash
python -m route_planner_ai.app
```

## Notes

- The map view uses Leaflet + OpenStreetMap tiles. By default it loads Leaflet from a CDN, so internet access is required for the map tiles.
- Routing/weather/traffic are network calls; the GUI should keep these calls off the UI thread.
