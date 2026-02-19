"""
route_planner_ai/gui/main_window.py

Full rewrite:
- Import bootstrapping so legacy absolute imports keep working
- Background worker (QThreadPool) so GUI does not freeze
- Weather error transparency
- Risk scoring compatibility: supports 3-return or 4-return signatures
- Dispatch report includes "Recommended actions" + Policy & System Context footer
- MAP:
  - draws route + markers: O, 1..N, D (stops in entered order)
  - FORCE REFRESH on every plan + selection:
      * queue payload until web view load finished
      * always redraw using a nonce
- UI:
  - Adds CLEAR button to wipe last route and reset UI for new route entry
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import floor
from pathlib import Path
import json
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QUrl, QObject, Signal, QRunnable, QThreadPool
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QMainWindow,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QListWidget,
    QTextEdit,
    QSplitter,
    QSizePolicy,
)
from PySide6.QtWebEngineWidgets import QWebEngineView


# -----------------------------------------------------------------------------
# Import bootstrap
# -----------------------------------------------------------------------------
_pkg_dir = Path(__file__).resolve().parents[1]  # .../route_planner_ai
if str(_pkg_dir) not in sys.path:
    sys.path.insert(0, str(_pkg_dir))

try:
    from route_planner_ai.routing_client import RoutingClient, RoutingError  # type: ignore
except Exception:
    from routing_client import RoutingClient, RoutingError  # type: ignore

try:
    from route_planner_ai.conditions_client import ConditionsClient, ConditionsError  # type: ignore
except Exception:
    from conditions_client import ConditionsClient, ConditionsError  # type: ignore

try:
    from route_planner_ai.traffic_client import TrafficClient  # type: ignore
except Exception:
    from traffic_client import TrafficClient  # type: ignore

try:
    from route_planner_ai.risk_scoring import compute_route_risk  # type: ignore
except Exception:
    from risk_scoring import compute_route_risk  # type: ignore


try:
    from route_planner_ai.conditions_client import CONDITIONS_CLIENT_VERSION  # type: ignore
except Exception:
    try:
        from conditions_client import CONDITIONS_CLIENT_VERSION  # type: ignore
    except Exception:
        CONDITIONS_CLIENT_VERSION = None  # type: ignore


# -----------------------------------------------------------------------------
# Worker plumbing
# -----------------------------------------------------------------------------

class _WorkerSignals(QObject):
    started = Signal(str)
    finished = Signal(object)  # PlanResult
    failed = Signal(str)


@dataclass
class PlanInput:
    origin: str
    destination: str
    stops: List[str]


@dataclass
class PlanResult:
    origin: str
    destination: str
    stops: List[str]
    routes: List[Dict[str, Any]]
    toll_info: Dict[str, Any]
    sanity: Optional[Dict[str, Any]]
    stop_based: bool
    weather_summary: str
    per_route: List[Dict[str, Any]]


class PlanWorker(QRunnable):
    def __init__(self, payload: PlanInput):
        super().__init__()
        self.payload = payload
        self.signals = _WorkerSignals()

    def run(self) -> None:
        origin = self.payload.origin
        destination = self.payload.destination
        stops = self.payload.stops

        try:
            self.signals.started.emit("Contacting routing service...")

            try:
                client = RoutingClient()
            except Exception as e:
                self.signals.failed.emit(f"Routing client setup error:\n\n{e}")
                return

            conditions_client = ConditionsClient()

            try:
                traffic_client = TrafficClient()
                traffic_client_error = ""
            except Exception as e:
                traffic_client = None
                traffic_client_error = str(e)

            # Routing
            self.signals.started.emit("Planning route(s)...")
            try:
                if stops and hasattr(client, "get_route_with_stops"):
                    plan = client.get_route_with_stops(origin, stops, destination)  # type: ignore
                    stop_based = True
                else:
                    plan = client.get_routes(origin, destination)
                    stop_based = False

                routes = plan.get("routes", []) or []
                toll_info = plan.get("toll", {"available": False}) or {"available": False}
                sanity = plan.get("sanity")
            except Exception as e:
                self.signals.failed.emit(f"Routing error:\n\n{e}")
                return

            if not routes:
                self.signals.failed.emit("No routes found.")
                return

            # Weather
            self.signals.started.emit("Fetching weather snapshot...")
            weather_summary = ""
            try:
                if hasattr(client, "geocode"):
                    origin_lat, origin_lon = client.geocode(origin)  # type: ignore
                    dest_lat, dest_lon = client.geocode(destination)  # type: ignore
                    weather_summary = conditions_client.get_route_weather(
                        origin_lat, origin_lon, dest_lat, dest_lon
                    )
                else:
                    weather_summary = "Weather lookup not available:\nRouting client has no geocode()."
            except (RoutingError, ConditionsError) as e:
                stamp = f" [{CONDITIONS_CLIENT_VERSION}]" if CONDITIONS_CLIENT_VERSION else ""
                weather_summary = f"Weather lookup not available{stamp}:\n{e}"
            except Exception as e:
                stamp = f" [{CONDITIONS_CLIENT_VERSION}]" if CONDITIONS_CLIENT_VERSION else ""
                weather_summary = f"Weather lookup not available{stamp}:\n{type(e).__name__}: {e}"

            # Traffic + risk per route
            self.signals.started.emit("Evaluating traffic + route risk...")
            per_route: List[Dict[str, Any]] = []

            for route in routes:
                summary = route.get("summary", {}) or {}
                miles = float(summary.get("distance_miles", 0.0) or 0.0)
                minutes = float(summary.get("duration_minutes", 0.0) or 0.0)
                geometry = route.get("geometry", None)

                traffic_stats = None
                if traffic_client is not None:
                    try:
                        if hasattr(traffic_client, "get_traffic"):
                            traffic_summary, traffic_stats = traffic_client.get_traffic(geometry)  # type: ignore
                        else:
                            traffic_summary = traffic_client.summarize_incidents_for_route(geometry)  # type: ignore
                    except Exception as e:
                        traffic_summary = f"Traffic incidents near route:\n- Error during traffic lookup: {e}"
                else:
                    traffic_summary = f"Traffic incidents near route:\n- Traffic client unavailable: {traffic_client_error}"

                risk_actions: List[str] = []
                try:
                    try:
                        score, label, explanation, risk_actions = compute_route_risk(
                            miles,
                            minutes,
                            weather_summary,
                            traffic_summary,
                            traffic_stats=traffic_stats,
                        )
                    except TypeError:
                        try:
                            score, label, explanation, risk_actions = compute_route_risk(
                                miles,
                                minutes,
                                weather_summary,
                                traffic_summary,
                            )
                        except ValueError:
                            score, label, explanation = compute_route_risk(
                                miles,
                                minutes,
                                weather_summary,
                                traffic_summary,
                            )
                            risk_actions = []
                except Exception as e:
                    score, label, explanation = (0, "UNKNOWN", f"risk scoring error: {e}")
                    risk_actions = []

                per_route.append(
                    {
                        "traffic_summary": traffic_summary,
                        "traffic_stats": traffic_stats,
                        "risk_score": int(score or 0),
                        "risk_label": str(label),
                        "risk_explanation": str(explanation),
                        "risk_actions": risk_actions or [],
                    }
                )

            result = PlanResult(
                origin=origin,
                destination=destination,
                stops=stops,
                routes=routes,
                toll_info=toll_info,
                sanity=sanity if isinstance(sanity, dict) else None,
                stop_based=stop_based,
                weather_summary=weather_summary,
                per_route=per_route,
            )
            self.signals.finished.emit(result)

        except Exception as e:
            self.signals.failed.emit(f"Unexpected error:\n\n{type(e).__name__}: {e}")


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------

class StopRow(QWidget):
    def __init__(self, remove_callback, parent=None):
        super().__init__(parent)
        self._remove_callback = remove_callback

        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Stop (City, ST / address / ZIP)")

        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._on_remove)

        layout.addWidget(self.input)
        layout.addWidget(self.remove_btn)
        self.setLayout(layout)

    def _on_remove(self):
        self._remove_callback(self)

    def value(self) -> str:
        return self.input.text().strip()


# -----------------------------------------------------------------------------
# Main Window
# -----------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Route Planner AI (Dispatch)")
        self.resize(1400, 850)

        self._stop_rows: List[StopRow] = []
        self._current_routes: List[Dict[str, Any]] = []
        self._last_plan: Optional[PlanResult] = None

        self._threadpool = QThreadPool.globalInstance()
        self._plan_button_enabled = True

        # MAP refresh controls
        self._map_ready: bool = False
        self._pending_map_payload: Optional[Tuple[Any, List[Dict[str, Any]], int]] = None
        self._map_nonce: int = 0

        left = QWidget()
        left_layout = QVBoxLayout()
        left.setLayout(left_layout)

        form = QFormLayout()

        self.origin_input = QLineEdit()
        self.origin_input.setPlaceholderText("Origin (City, ST / address / ZIP)")
        form.addRow(QLabel("Origin:"), self.origin_input)

        self.destination_input = QLineEdit()
        self.destination_input.setPlaceholderText("Destination (City, ST / address / ZIP)")
        form.addRow(QLabel("Destination:"), self.destination_input)

        left_layout.addLayout(form)

        stops_header = QHBoxLayout()
        stops_header.addWidget(QLabel("Stops (optional, multi-drop):"))
        self.add_stop_btn = QPushButton("+ Add Stop")
        self.add_stop_btn.clicked.connect(self._add_stop_row)
        stops_header.addWidget(self.add_stop_btn)
        stops_header.addStretch(1)
        left_layout.addLayout(stops_header)

        self.stops_container = QWidget()
        self.stops_layout = QVBoxLayout()
        self.stops_layout.setContentsMargins(0, 0, 0, 0)
        self.stops_container.setLayout(self.stops_layout)
        left_layout.addWidget(self.stops_container)

        actions = QHBoxLayout()
        self.plan_btn = QPushButton("Plan Route")
        self.plan_btn.clicked.connect(self.on_plan_route_clicked)
        actions.addWidget(self.plan_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        actions.addWidget(self.clear_btn)

        self.copy_btn = QPushButton("Copy Report")
        self.copy_btn.clicked.connect(self._on_copy_clicked)
        actions.addWidget(self.copy_btn)

        left_layout.addLayout(actions)

        left_layout.addWidget(QLabel("Routes:"))
        self.route_list = QListWidget()
        self.route_list.currentRowChanged.connect(self._on_route_selected)
        self.route_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        left_layout.addWidget(self.route_list, stretch=1)

        right = QWidget()
        right_layout = QVBoxLayout()
        right.setLayout(right_layout)

        self.map_view = QWebEngineView()
        self.map_view.setMinimumHeight(420)
        right_layout.addWidget(self.map_view)

        self.conditions_text = QTextEdit()
        self.conditions_text.setReadOnly(True)
        self.conditions_text.setPlaceholderText("Dispatch report will appear here...")
        right_layout.addWidget(self.conditions_text, stretch=1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        container = QWidget()
        root_layout = QVBoxLayout()
        container.setLayout(root_layout)
        root_layout.addWidget(splitter)
        self.setCentralWidget(container)

        self._load_map()

    # -------------------------
    # Map
    # -------------------------

    def _load_map(self) -> None:
        self._map_ready = False
        html = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Route Map</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    html, body { height: 100%; margin: 0; }
    #map { height: 100%; width: 100%; }
    .stop-label {
      background: white;
      border: 1px solid #444;
      border-radius: 10px;
      padding: 2px 7px;
      font-size: 12px;
      font-weight: 800;
    }
  </style>
</head>
<body>
<div id="map"></div>
<script>
  const map = L.map('map').setView([46.8721, -113.9940], 6);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap'
  }).addTo(map);

  let routeLayer = null;
  let markerLayer = L.layerGroup().addTo(map);

  function clearAll() {
    if (routeLayer) {
      map.removeLayer(routeLayer);
      routeLayer = null;
    }
    markerLayer.clearLayers();
  }

  function addLabeledMarker(lat, lon, label, popupText) {
    const m = L.marker([lat, lon]).addTo(markerLayer);
    if (popupText) m.bindPopup(popupText);

    const icon = L.divIcon({
      className: 'stop-label',
      html: label,
      iconSize: [24, 24],
      iconAnchor: [12, 12]
    });
    L.marker([lat, lon], { icon }).addTo(markerLayer);
  }

  window.setRouteAndMarkers = function(coords, markers, nonce) {
    const _ = nonce; // nonce forces redraw
    clearAll();

    if (coords && coords.length >= 2) {
      const latlon = coords.map(pt => [pt[1], pt[0]]);
      routeLayer = L.polyline(latlon, { weight: 5 }).addTo(map);
      map.fitBounds(routeLayer.getBounds(), { padding: [20, 20] });
    }

    if (markers && markers.length) {
      for (const m of markers) {
        addLabeledMarker(m.lat, m.lon, m.label, m.popup);
      }
    }
  }
</script>
</body>
</html>
"""
        self.map_view.loadFinished.connect(self._on_map_load_finished)
        self.map_view.setHtml(html, baseUrl=QUrl("https://local.map/"))

    def _on_map_load_finished(self, ok: bool) -> None:
        self._map_ready = bool(ok)
        if not self._map_ready:
            return
        if self._pending_map_payload is None:
            return
        geometry, markers, nonce = self._pending_map_payload
        self._pending_map_payload = None
        self._push_map_now(geometry, markers, nonce)

    def _push_map(self, route_geometry: Any, markers: List[Dict[str, Any]]) -> None:
        self._map_nonce += 1
        nonce = self._map_nonce

        if not self._map_ready:
            self._pending_map_payload = (route_geometry, markers, nonce)
            return

        self._push_map_now(route_geometry, markers, nonce)

    def _push_map_now(self, route_geometry: Any, markers: List[Dict[str, Any]], nonce: int) -> None:
        coords: List[Any] = []
        if isinstance(route_geometry, dict) and isinstance(route_geometry.get("coordinates"), list):
            coords = route_geometry["coordinates"]

        js_coords = json.dumps(coords)
        js_markers = json.dumps(markers)
        self.map_view.page().runJavaScript(f"window.setRouteAndMarkers({js_coords}, {js_markers}, {int(nonce)});")

    # -------------------------
    # Stops UI
    # -------------------------

    def _add_stop_row(self):
        row = StopRow(remove_callback=self._remove_stop_row)
        self._stop_rows.append(row)
        self.stops_layout.addWidget(row)

    def _remove_stop_row(self, row):
        if row in self._stop_rows:
            self._stop_rows.remove(row)
        row.setParent(None)
        row.deleteLater()

    def _collect_stops(self) -> List[str]:
        return [r.value() for r in self._stop_rows if r.value()]

    def _clear_all_stops(self) -> None:
        # remove in reverse to avoid layout churn issues
        for row in list(self._stop_rows)[::-1]:
            self._remove_stop_row(row)

    # -------------------------
    # Actions
    # -------------------------

    def on_plan_route_clicked(self) -> None:
        origin = self.origin_input.text().strip()
        destination = self.destination_input.text().strip()
        stops = self._collect_stops()

        if not origin or not destination:
            self.conditions_text.setPlainText("Please enter both origin and destination.")
            return

        if not self._plan_button_enabled:
            return

        self._plan_button_enabled = False
        self.plan_btn.setEnabled(False)

        self.route_list.clear()
        self._current_routes = []
        self._last_plan = None
        self.conditions_text.setPlainText("Starting...")

        # Force-clear map on new run
        self._push_map({"type": "LineString", "coordinates": []}, [])

        worker = PlanWorker(PlanInput(origin=origin, destination=destination, stops=stops))
        worker.signals.started.connect(self._on_worker_status)
        worker.signals.failed.connect(self._on_worker_failed)
        worker.signals.finished.connect(self._on_worker_finished)
        self._threadpool.start(worker)

    def _on_clear_clicked(self) -> None:
        """
        Clear last route and reset UI so another route can be entered immediately.
        """
        # allow re-plan instantly
        self._plan_button_enabled = True
        self.plan_btn.setEnabled(True)

        # clear state
        self._current_routes = []
        self._last_plan = None

        # clear UI inputs
        self.origin_input.clear()
        self.destination_input.clear()
        self._clear_all_stops()

        # clear outputs
        self.route_list.clear()
        self.conditions_text.setPlainText("Cleared. Enter a new route and click 'Plan Route'.")

        # clear map
        self._push_map({"type": "LineString", "coordinates": []}, [])

    def _on_worker_status(self, msg: str) -> None:
        self.conditions_text.setPlainText(msg)

    def _on_worker_failed(self, msg: str) -> None:
        self.conditions_text.setPlainText(msg)
        self.plan_btn.setEnabled(True)
        self._plan_button_enabled = True

    def _on_worker_finished(self, result: PlanResult) -> None:
        self._last_plan = result
        self._current_routes = result.routes or []

        self.route_list.clear()
        for idx, route in enumerate(self._current_routes):
            summary = route.get("summary", {}) or {}
            miles = float(summary.get("distance_miles", 0.0) or 0.0)
            minutes = float(summary.get("duration_minutes", 0.0) or 0.0)
            hours = floor(minutes / 60) if minutes else 0
            mins = int(round(minutes - hours * 60)) if minutes else 0
            self.route_list.addItem(f"Route {idx + 1} - {miles:.1f} miles, {hours}h {mins}m")

        if self._current_routes:
            self.route_list.setCurrentRow(0)
            self._render_selected_route(0)

        self.conditions_text.setPlainText(self._build_dispatch_report(result))

        self.plan_btn.setEnabled(True)
        self._plan_button_enabled = True

    # -------------------------
    # Route selection + map rendering
    # -------------------------

    def _on_route_selected(self, row: int) -> None:
        if row < 0:
            return
        self._render_selected_route(row)

    def _render_selected_route(self, index: int) -> None:
        if not self._current_routes or not self._last_plan:
            return
        if index < 0 or index >= len(self._current_routes):
            return

        route = self._current_routes[index]
        geometry = route.get("geometry")

        markers: List[Dict[str, Any]] = []

        def _add_marker(lat: float, lon: float, label: str, popup: str) -> None:
            markers.append({"lat": float(lat), "lon": float(lon), "label": label, "popup": popup})

        try:
            client = RoutingClient()
        except Exception:
            client = None  # type: ignore

        origin_txt = self._last_plan.origin
        dest_txt = self._last_plan.destination
        stops_txt = self._last_plan.stops or []

        if client is not None and hasattr(client, "geocode"):
            try:
                o_lat, o_lon = client.geocode(origin_txt)  # type: ignore
                _add_marker(o_lat, o_lon, "O", f"Origin: {origin_txt}")
            except Exception:
                pass

            for i, s in enumerate(stops_txt, start=1):
                try:
                    s_lat, s_lon = client.geocode(s)  # type: ignore
                    _add_marker(s_lat, s_lon, str(i), f"Stop {i}: {s}")
                except Exception:
                    continue

            try:
                d_lat, d_lon = client.geocode(dest_txt)  # type: ignore
                _add_marker(d_lat, d_lon, "D", f"Destination: {dest_txt}")
            except Exception:
                pass

        if not markers and isinstance(geometry, dict) and isinstance(geometry.get("coordinates"), list):
            coords = geometry["coordinates"]
            if len(coords) >= 2:
                o = coords[0]
                d = coords[-1]
                if isinstance(o, (list, tuple)) and len(o) == 2:
                    _add_marker(o[1], o[0], "O", f"Origin: {origin_txt}")
                if isinstance(d, (list, tuple)) and len(d) == 2:
                    _add_marker(d[1], d[0], "D", f"Destination: {dest_txt}")

        self._push_map(geometry, markers)

    # -------------------------
    # Dispatch Report
    # -------------------------

    def _build_dispatch_report(self, result: PlanResult) -> str:
        origin = result.origin
        destination = result.destination
        stops = result.stops
        routes = result.routes
        toll_info = result.toll_info or {"available": False}
        sanity = result.sanity
        weather_summary = result.weather_summary
        per_route = result.per_route or []

        primary = (routes[0].get("summary", {}) if routes else {}) or {}
        miles_primary = float(primary.get("distance_miles", 0.0) or 0.0)
        minutes_primary = float(primary.get("duration_minutes", 0.0) or 0.0)
        hours_primary = floor(minutes_primary / 60) if minutes_primary else 0
        mins_primary = int(round(minutes_primary - hours_primary * 60)) if minutes_primary else 0

        geom = routes[0].get("geometry") if routes else None
        geometry_info = "coordinates (LineString)" if isinstance(geom, dict) else "not available"

        threshold = 3.0
        if isinstance(sanity, dict) and "ratio" in sanity and "threshold" in sanity:
            ratio = sanity.get("ratio")
            threshold = float(sanity.get("threshold", 3.0) or 3.0)
            straight = sanity.get("straight_line_miles", "N/A")
            sanity_line = f"Sanity check: PASS ({ratio}×, threshold {threshold}×) | Straight-line: {straight} miles"
        else:
            sanity_line = f"Sanity safeguard: ACTIVE ({threshold}× straight-line anomaly block)"

        dispatch_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        route_id = str(uuid.uuid4())[:8]
        trip_type = "Multi-drop" if stops else "Direct"

        toll_block = "Tolls:\n"
        if isinstance(toll_info, dict) and toll_info.get("available"):
            toll_block += f"- {toll_info.get('detail') or 'Toll info available.'}\n"
        else:
            toll_block += "- Toll info not available from routing service for this profile/region.\n"

        traffic_summary_primary = ""
        risk_text_primary = ""
        actions_block = ""
        route_lines: List[str] = []
        best_choice: Optional[Tuple[int, float, int, str, str]] = None

        for idx, route in enumerate(routes):
            summary = route.get("summary", {}) or {}
            miles = float(summary.get("distance_miles", 0.0) or 0.0)
            minutes = float(summary.get("duration_minutes", 0.0) or 0.0)
            hours = floor(minutes / 60) if minutes else 0
            mins = int(round(minutes - hours * 60)) if minutes else 0

            pr = per_route[idx] if idx < len(per_route) else {}
            traffic_summary = pr.get("traffic_summary", "Traffic: (not available)")
            score = int(pr.get("risk_score", 0) or 0)
            label = str(pr.get("risk_label", "UNKNOWN"))
            expl = str(pr.get("risk_explanation", ""))
            actions = pr.get("risk_actions", []) or []

            route_lines.append(
                f"- Route {idx + 1}: {miles:.1f} miles, {hours}h {mins}m — Risk {label} ({score}/100); {expl}"
            )

            if idx == 0:
                traffic_summary_primary = traffic_summary
                risk_text_primary = (
                    "Route risk assessment:\n"
                    f"- Risk level: {label} ({score}/100)\n"
                    f"- Factors: {expl}\n"
                )
                if actions:
                    actions_block = "Recommended actions (dispatcher guidance):\n" + "\n".join(
                        [f"- {a}" for a in actions]
                    ) + "\n"

            candidate = (score, miles, idx, label, expl)
            if best_choice is None:
                best_choice = candidate
            else:
                if candidate[0] < best_choice[0] or (candidate[0] == best_choice[0] and candidate[1] < best_choice[1]):
                    best_choice = candidate

        recommended_block = ""
        if best_choice:
            score, miles, idx, label, expl = best_choice
            recommended_block = (
                "\nRecommended route (based on lowest risk, then shortest distance):\n"
                f"- Route {idx + 1}: {miles:.1f} miles — Risk {label} ({score}/100); {expl}\n"
            )

        conditions_ver = CONDITIONS_CLIENT_VERSION or "unknown"
        policy_footer = (
            "\n\nPolicy & System Context:\n"
            "- Routing provider: OpenRouteService (ORS) directions (GeoJSON)\n"
            "- ORS profile: driving-hgv\n"
            "- Alternatives: disabled (HGV default)\n"
            f"- Sanity threshold: {threshold}×\n"
            "- Geocoding: ORS Pelias\n"
            f"- Weather: Open-Meteo (ConditionsClient {conditions_ver})\n"
            "- Traffic: TomTom Incident Details API (cached)\n"
        )

        header = (
            "=== DISPATCH REPORT ===\n"
            f"Dispatch timestamp: {dispatch_timestamp}\n"
            f"Route ID: {route_id}\n"
            f"Trip type: {trip_type}\n"
            "Vehicle profile: Truck (HGV)\n"
            f"{sanity_line}\n"
            "-----------------------------\n\n"
        )

        return (
            header
            + f"Route: {origin} → {destination}\n\n"
            + "Primary route:\n"
            + f"- Distance: {miles_primary:.1f} miles\n"
            + f"- Estimated drive time: {hours_primary}h {mins_primary}m\n"
            + f"- Geometry: {geometry_info}\n\n"
            + toll_block + "\n"
            + f"{weather_summary}\n\n"
            + f"{traffic_summary_primary}\n\n"
            + f"{risk_text_primary}\n"
            + (actions_block + "\n" if actions_block else "")
            + recommended_block + "\n"
            + f"Route comparison summary for: {origin} → {destination}\n"
            + "\n".join(route_lines)
            + policy_footer
        )

    def _on_copy_clicked(self) -> None:
        QApplication.clipboard().setText(self.conditions_text.toPlainText())
