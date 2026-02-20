from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time as dt_time
from math import floor
from pathlib import Path
import json
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QUrl, QObject, Signal, QRunnable, QThreadPool, QTime, QDate
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
    QComboBox,
    QSpinBox,
    QDoubleSpinBox,
    QTimeEdit,
    QDateEdit,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

APP_VERSION = "0.1.0"

# -----------------------------------------------------------------------------
# Import bootstrap so it works both as a package and as a flat script tree
# -----------------------------------------------------------------------------
_pkg_dir = Path(__file__).resolve().parents[1]  # .../route_planner_ai
if str(_pkg_dir) not in sys.path:
    sys.path.insert(0, str(_pkg_dir))

try:
    from routing_client import RoutingClient, RoutingError  # type: ignore
except Exception:
    from route_planner_ai.routing_client import RoutingClient, RoutingError  # type: ignore

try:
    from conditions_client import ConditionsClient, ConditionsError  # type: ignore
except Exception:
    from route_planner_ai.conditions_client import ConditionsClient, ConditionsError  # type: ignore

try:
    from traffic_client import TrafficClient  # type: ignore
except Exception:
    from route_planner_ai.traffic_client import TrafficClient  # type: ignore

try:
    from risk_scoring import compute_route_risk  # type: ignore
except Exception:
    from route_planner_ai.risk_scoring import compute_route_risk  # type: ignore

try:
    from conditions_client import CONDITIONS_CLIENT_VERSION  # type: ignore
except Exception:
    try:
        from route_planner_ai.conditions_client import CONDITIONS_CLIENT_VERSION  # type: ignore
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
    mode: str = "dispatcher"


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
        mode = getattr(self.payload, "mode", "dispatcher")

        try:
            self.signals.started.emit("Contacting routing service...")

            # Routing client
            try:
                client = RoutingClient()
            except Exception as e:
                self.signals.failed.emit(f"Routing client setup error:\n\n{e}")
                return

            conditions_client = ConditionsClient()

            # Traffic client is optional
            try:
                traffic_client = TrafficClient()
                traffic_client_error = ""
            except Exception as e:
                traffic_client = None
                traffic_client_error = str(e)

            # ---------------- Routing ----------------
            self.signals.started.emit("Planning route(s)...")
            try:
                if stops and hasattr(client, "get_route_with_stops"):
                    # Multi-drop behavior stays unchanged (no alternatives toggle here).
                    plan = client.get_route_with_stops(origin, stops, destination)  # type: ignore
                    stop_based = True
                else:
                    stop_based = False
                    # In Driver mode, attempt to request provider alternatives if supported.
                    if mode == "driver":
                        try:
                            plan = client.get_routes(origin, destination, alternatives=True)  # type: ignore
                        except TypeError:
                            plan = client.get_routes(origin, destination)
                    else:
                        plan = client.get_routes(origin, destination)

                routes = plan.get("routes", []) or []
                toll_info = plan.get("toll", {"available": False}) or {"available": False}
                sanity = plan.get("sanity")
            except Exception as e:
                self.signals.failed.emit(f"Routing error:\n\n{e}")
                return

            if not routes:
                self.signals.failed.emit("No routes found.")
                return

            # ---------------- Weather (snapshot) ----------------
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

            # ---------------- Traffic + Risk per route ----------------
            self.signals.started.emit("Evaluating traffic + route risk...")
            per_route: List[Dict[str, Any]] = []

            for route in routes:
                summary = route.get("summary", {}) or {}
                miles = float(summary.get("distance_miles", 0.0) or 0.0)
                minutes = float(summary.get("duration_minutes", 0.0) or 0.0)
                geometry = route.get("geometry", None)

                # Traffic
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
                    traffic_summary = (
                        "Traffic incidents near route:\n"
                        f"- Traffic client unavailable: {traffic_client_error}"
                    )

                # Risk
                risk_actions: List[str] = []
                try:
                    try:
                        # 4-return signature: score, label, explanation, actions
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
                            # 3-return legacy signature
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
    """Single stop input row with a remove button."""
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

        self.setWindowTitle(f"Route Planner AI v{APP_VERSION} (Truck Dispatch)")
        self.resize(1400, 850)

        # Mode + driver parameters
        self._mode: str = "dispatcher"
        self._avg_speed_mph: int = 60
        self._mpg: int = 7
        self._fuel_price: float = 4.00

        self._stop_rows: List[StopRow] = []
        self._current_routes: List[Dict[str, Any]] = []
        self._last_plan: Optional[PlanResult] = None

        self._threadpool = QThreadPool.globalInstance()
        self._plan_button_enabled = True

        # Map refresh controls
        self._map_ready: bool = False
        self._pending_map_payload: Optional[Tuple[Any, List[Dict[str, Any]], int]] = None
        self._map_nonce: int = 0

        # ---------------- Left Panel ----------------
        left = QWidget()
        left_layout = QVBoxLayout()
        left.setLayout(left_layout)

        # Mode selector (Dispatcher / Driver)
        mode_row = QHBoxLayout()
        mode_label = QLabel("Mode:")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Dispatcher")
        self.mode_combo.addItem("Driver")
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        left_layout.addLayout(mode_row)

        # Average speed
        speed_row = QHBoxLayout()
        speed_label = QLabel("Average Speed (mph):")
        self.speed_input = QSpinBox()
        self.speed_input.setRange(20, 85)
        self.speed_input.setValue(self._avg_speed_mph)
        self.speed_input.valueChanged.connect(self._on_speed_changed)
        speed_row.addWidget(speed_label)
        speed_row.addWidget(self.speed_input)
        speed_row.addStretch(1)
        left_layout.addLayout(speed_row)

        # Truck MPG (Driver fuel model)
        mpg_row = QHBoxLayout()
        mpg_label = QLabel("Truck MPG:")
        self.mpg_input = QSpinBox()
        self.mpg_input.setRange(3, 15)
        self.mpg_input.setValue(self._mpg)
        self.mpg_input.valueChanged.connect(self._on_mpg_changed)
        mpg_row.addWidget(mpg_label)
        mpg_row.addWidget(self.mpg_input)
        mpg_row.addStretch(1)
        left_layout.addLayout(mpg_row)

        # Fuel price (Driver fuel model)
        fuel_row = QHBoxLayout()
        fuel_label = QLabel("Fuel price (USD/gal):")
        self.fuel_price_input = QDoubleSpinBox()
        self.fuel_price_input.setRange(2.0, 10.0)
        self.fuel_price_input.setSingleStep(0.10)
        self.fuel_price_input.setDecimals(2)
        self.fuel_price_input.setValue(self._fuel_price)
        self.fuel_price_input.valueChanged.connect(self._on_fuel_price_changed)
        fuel_row.addWidget(fuel_label)
        fuel_row.addWidget(self.fuel_price_input)
        fuel_row.addStretch(1)
        left_layout.addLayout(fuel_row)

        # Departure date/time (Driver mode only)
        depart_row = QHBoxLayout()
        depart_label = QLabel("Departure (date/time):")
        self.depart_date_edit = QDateEdit()
        self.depart_date_edit.setCalendarPopup(True)
        self.depart_date_edit.setDate(QDate.currentDate())

        self.depart_time_edit = QTimeEdit()
        self.depart_time_edit.setDisplayFormat("HH:mm")
        self.depart_time_edit.setTime(QTime.currentTime())

        depart_row.addWidget(depart_label)
        depart_row.addWidget(self.depart_date_edit)
        depart_row.addWidget(self.depart_time_edit)
        depart_row.addStretch(1)
        left_layout.addLayout(depart_row)

        # Origin / destination
        form = QFormLayout()
        self.origin_input = QLineEdit()
        self.origin_input.setPlaceholderText("Origin (City, ST / address / ZIP)")
        form.addRow(QLabel("Origin:"), self.origin_input)

        self.destination_input = QLineEdit()
        self.destination_input.setPlaceholderText("Destination (City, ST / address / ZIP)")
        form.addRow(QLabel("Destination:"), self.destination_input)
        left_layout.addLayout(form)

        # Stops header + add button
        stops_header = QHBoxLayout()
        stops_header.addWidget(QLabel("Stops (optional, multi-drop):"))
        self.add_stop_btn = QPushButton("+ Add Stop")
        self.add_stop_btn.clicked.connect(self._add_stop_row)
        stops_header.addWidget(self.add_stop_btn)
        stops_header.addStretch(1)
        left_layout.addLayout(stops_header)

        # Stops container
        self.stops_container = QWidget()
        self.stops_layout = QVBoxLayout()
        self.stops_layout.setContentsMargins(0, 0, 0, 0)
        self.stops_container.setLayout(self.stops_layout)
        left_layout.addWidget(self.stops_container)

        # Action buttons
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

        # Routes list
        left_layout.addWidget(QLabel("Routes:"))
        self.route_list = QListWidget()
        self.route_list.currentRowChanged.connect(self._on_route_selected)
        self.route_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        left_layout.addWidget(self.route_list, stretch=1)

        # ---------------- Right Panel ----------------
        right = QWidget()
        right_layout = QVBoxLayout()
        right.setLayout(right_layout)

        # Map view
        self.map_view = QWebEngineView()
        self.map_view.setMinimumHeight(420)
        right_layout.addWidget(self.map_view)

        # Conditions / report
        self.conditions_text = QTextEdit()
        self.conditions_text.setReadOnly(True)
        self.conditions_text.setPlaceholderText("Dispatch report will appear here...")
        right_layout.addWidget(self.conditions_text, stretch=1)

        # Splitter
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

        # Initialize map
        self._load_map()

        # Initialize driver-specific control states
        self._update_depart_controls_enabled()

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
    const _ = nonce;
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
        self.map_view.page().runJavaScript(
            f"window.setRouteAndMarkers({js_coords}, {js_markers}, {int(nonce)});"
        )

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
        for row in list(self._stop_rows)[::-1]:
            self._remove_stop_row(row)

    # -------------------------
    # Mode / speed / fuel controls
    # -------------------------

    def _update_depart_controls_enabled(self) -> None:
        enabled = self._mode == "driver"
        self.depart_date_edit.setEnabled(enabled)
        self.depart_time_edit.setEnabled(enabled)
        self.mpg_input.setEnabled(enabled)
        self.fuel_price_input.setEnabled(enabled)

    def _on_mode_changed(self, index: int) -> None:
        text = self.mode_combo.currentText().strip().lower()
        if text.startswith("driver"):
            self._mode = "driver"
        else:
            self._mode = "dispatcher"
        self._update_depart_controls_enabled()

    def _on_speed_changed(self, value: int) -> None:
        self._avg_speed_mph = int(value)

    def _on_mpg_changed(self, value: int) -> None:
        self._mpg = int(value)

    def _on_fuel_price_changed(self, value: float) -> None:
        self._fuel_price = float(value)

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

        self._plan_button_enabled = True
        self.plan_btn.setEnabled(False)

        self.route_list.clear()
        self._current_routes = []
        self._last_plan = None
        self.conditions_text.setPlainText("Starting...")

        # Clear map on new run
        self._push_map({"type": "LineString", "coordinates": []}, [])

        worker = PlanWorker(
            PlanInput(origin=origin, destination=destination, stops=stops, mode=self._mode)
        )
        worker.signals.started.connect(self._on_worker_status)
        worker.signals.failed.connect(self._on_worker_failed)
        worker.signals.finished.connect(self._on_worker_finished)
        self._threadpool.start(worker)

    def _on_clear_clicked(self) -> None:
        """Clear last route and reset UI."""
        self._plan_button_enabled = True
        self.plan_btn.setEnabled(True)

        self._current_routes = []
        self._last_plan = None

        self.origin_input.clear()
        self.destination_input.clear()
        self._clear_all_stops()

        self.route_list.clear()
        self.conditions_text.setPlainText("Cleared. Enter a new route and click 'Plan Route'.")

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

            if self._mode == "driver":
                if idx == 0:
                    prefix = "Route 1 (Primary)"
                elif idx == 1:
                    prefix = "Route 2 (Alternate)"
                else:
                    prefix = f"Route {idx + 1}"
            else:
                prefix = f"Route {idx + 1}"

            self.route_list.addItem(f"{prefix} - {miles:.1f} miles, {hours}h {mins}m")

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

        # Fallback markers from geometry if geocode fails everywhere
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
            sanity_line = (
                f"Sanity check: PASS ({ratio}×, threshold {threshold}×) | "
                f"Straight-line: {straight} miles"
            )
        else:
            sanity_line = f"Sanity safeguard: ACTIVE ({threshold}× straight-line anomaly block)"

        now = datetime.now()
        dispatch_timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        route_id = str(uuid.uuid4())[:8]
        trip_type = "Multi-drop" if stops else "Direct"

        # Tolls block
        toll_block = "Tolls:\n"
        if isinstance(toll_info, dict) and toll_info.get("available"):
            toll_block += f"- {toll_info.get('detail') or 'Toll info available.'}\n"
        else:
            toll_block += "- Toll info not available from routing service for this profile/region.\n"

        traffic_summary_primary = ""
        risk_text_primary = ""
        actions_block = ""
        route_lines: List[str] = []

        # For delta comparisons
        primary_risk_score: Optional[int] = None
        primary_cost_estimate: Optional[float] = None
        miles_list: List[float] = []
        minutes_list: List[float] = []
        score_list: List[int] = []
        fuel_cost_list: List[Optional[float]] = []

        # Per-route summary (distance, risk, fuel if Driver)
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

            # Store for deltas
            miles_list.append(miles)
            minutes_list.append(minutes)
            score_list.append(score)

            gallons = None
            cost = None
            if self._mode == "driver" and self._mpg > 0 and self._fuel_price > 0 and miles > 0:
                gallons = miles / float(self._mpg)
                cost = gallons * self._fuel_price
            fuel_cost_list.append(cost)

            # Route naming respects Driver / Dispatcher mode
            if self._mode == "driver":
                if idx == 0:
                    route_name = "Route 1 (Primary)"
                elif idx == 1:
                    route_name = "Route 2 (Alternate)"
                else:
                    route_name = f"Route {idx + 1}"
            else:
                route_name = f"Route {idx + 1}"

            # Fuel estimate per route (Driver mode only)
            fuel_info_str = ""
            if gallons is not None and cost is not None:
                fuel_info_str = f"; est fuel {gallons:.1f} gal, ~${cost:.2f}"

            route_lines.append(
                f"- {route_name}: {miles:.1f} miles, {hours}h {mins}m — "
                f"Risk {label} ({score}/100); {expl}{fuel_info_str}"
            )

            # Primary route details
            if idx == 0:
                traffic_summary_primary = traffic_summary
                risk_text_primary = (
                    "Route risk assessment:\n"
                    f"- Risk level: {label} ({score}/100)\n"
                    f"- Factors: {expl}\n"
                )
                if actions:
                    actions_block = (
                        "Recommended actions (dispatcher guidance):\n"
                        + "\n".join([f"- {a}" for a in actions])
                        + "\n"
                    )
                primary_risk_score = score
                if cost is not None:
                    primary_cost_estimate = cost

            candidate = (score, miles, idx, label, expl)
            if best_choice is None:
                best_choice = candidate
            else:
                if candidate[0] < best_choice[0] or (
                    candidate[0] == best_choice[0] and candidate[1] < best_choice[1]
                ):
                    best_choice = candidate

        # Recommended route block
        recommended_block = ""
        fuel_best_suffix = ""
        if best_choice:
            score, miles, idx, label, expl = best_choice
            if self._mode == "driver":
                if idx == 0:
                    best_name = "Route 1 (Primary)"
                elif idx == 1:
                    best_name = "Route 2 (Alternate)"
                else:
                    best_name = f"Route {idx + 1}"
            else:
                best_name = f"Route {idx + 1}"

            if (
                self._mode == "driver"
                and self._mpg > 0
                and self._fuel_price > 0
                and miles > 0
            ):
                gallons_best = miles / float(self._mpg)
                cost_best = gallons_best * self._fuel_price
                fuel_best_suffix = f"; est fuel {gallons_best:.1f} gal, ~${cost_best:.2f}"

            recommended_block = (
                "\nRecommended route (based on lowest risk, then shortest distance):\n"
                f"- {best_name}: {miles:.1f} miles — Risk {label} ({score}/100); {expl}{fuel_best_suffix}\n"
            )

        # Driver-only ETA, fuel, and delta blocks
        driver_block = ""
        eta_weather_block = ""
        fuel_block = ""
        delta_block = ""

        if self._mode == "driver" and miles_primary > 0 and self._avg_speed_mph > 0:
            travel_hours = miles_primary / float(self._avg_speed_mph)

            # Build timezone-aware departure datetime (local timezone)
            try:
                qd = self.depart_date_edit.date()
                qt = self.depart_time_edit.time()
                local_tz = datetime.now().astimezone().tzinfo
                depart_dt = datetime(
                    year=qd.year(),
                    month=qd.month(),
                    day=qd.day(),
                    hour=qt.hour(),
                    minute=qt.minute(),
                    tzinfo=local_tz,
                )
            except Exception:
                depart_dt = datetime.now().astimezone()

            eta_dt = depart_dt + timedelta(hours=travel_hours)
            eta_str = eta_dt.strftime("%Y-%m-%d %H:%M")
            eta_h = int(travel_hours)
            eta_m = int(round((travel_hours - eta_h) * 60))
            depart_str = depart_dt.strftime("%Y-%m-%d %H:%M")

            driver_block = (
                "\nDriver view (based on average speed):\n"
                f"- Average speed: {self._avg_speed_mph} mph\n"
                f"- Planned departure: {depart_str}\n"
                f"- Approx driving time at this speed: {eta_h}h {eta_m}m\n"
                f"- Estimated arrival time: {eta_str}\n"
            )

            # Fuel estimate for primary route (Driver mode)
            if self._mpg > 0 and self._fuel_price > 0:
                gallons_primary = miles_primary / float(self._mpg)
                cost_primary = gallons_primary * self._fuel_price
                fuel_block = (
                    "\nFuel estimate (driver planning):\n"
                    f"- Truck MPG: {self._mpg} mpg\n"
                    f"- Fuel price (est): ${self._fuel_price:.2f}/gal\n"
                    f"- Estimated fuel used (primary): {gallons_primary:.1f} gal\n"
                    f"- Estimated fuel cost (primary): ${cost_primary:.2f}\n"
                )
                # If primary_cost_estimate was not set earlier, ensure it is available for deltas
                if primary_cost_estimate is None:
                    primary_cost_estimate = cost_primary

            # Time-aligned weather forecast for Driver mode, if supported by ConditionsClient
            try:
                origin_lat = origin_lon = dest_lat = dest_lon = None
                if isinstance(geom, dict) and isinstance(geom.get("coordinates"), list):
                    coords = geom.get("coordinates") or []
                    if len(coords) >= 2:
                        o = coords[0]
                        d = coords[-1]
                        if isinstance(o, (list, tuple)) and len(o) == 2:
                            origin_lon, origin_lat = float(o[0]), float(o[1])
                        if isinstance(d, (list, tuple)) and len(d) == 2:
                            dest_lon, dest_lat = float(d[0]), float(d[1])

                if (
                    origin_lat is not None
                    and origin_lon is not None
                    and dest_lat is not None
                    and dest_lon is not None
                    and hasattr(ConditionsClient, "get_route_weather_with_eta")
                ):
                    conds = ConditionsClient()
                    eta_midpoint = depart_dt + timedelta(hours=travel_hours / 2.0)

                    try:
                        eta_weather_text = conds.get_route_weather_with_eta(
                            origin_lat=origin_lat,
                            origin_lon=origin_lon,
                            dest_lat=dest_lat,
                            dest_lon=dest_lon,
                            eta_midpoint=eta_midpoint,
                            eta_destination=eta_dt,
                        )
                    except ConditionsError as e:
                        stamp = (
                            f" [{CONDITIONS_CLIENT_VERSION}]"
                            if CONDITIONS_CLIENT_VERSION
                            else ""
                        )
                        eta_weather_text = (
                            f"Time-aligned weather forecast unavailable{stamp}:\n{e}"
                        )

                    eta_weather_block = "\n" + eta_weather_text + "\n"

            except Exception as e:
                stamp = (
                    f" [{CONDITIONS_CLIENT_VERSION}]"
                    if CONDITIONS_CLIENT_VERSION
                    else ""
                )
                eta_weather_block = (
                    f"\nTime-aligned weather forecast unavailable{stamp}:\n"
                    f"{type(e).__name__}: {e}\n"
                )

            # Route delta block (vs primary), Driver mode only
            if len(routes) > 1 and miles_primary > 0 and score_list:
                primary_score_val = (
                    primary_risk_score if primary_risk_score is not None else score_list[0]
                )
                delta_lines: List[str] = []
                for idx in range(1, len(routes)):
                    miles = miles_list[idx]
                    minutes = minutes_list[idx]
                    score = score_list[idx]

                    dmiles = miles - miles_primary
                    dmins = minutes - minutes_primary
                    dscore = score - primary_score_val

                    dmiles_str = f"{'+' if dmiles >= 0 else ''}{dmiles:.1f} mi"
                    dmins_str = f"{'+' if dmins >= 0 else ''}{int(round(dmins))} min"
                    dscore_str = f"{'+' if dscore >= 0 else ''}{dscore} risk"

                    cost_delta_str = ""
                    if (
                        primary_cost_estimate is not None
                        and idx < len(fuel_cost_list)
                        and fuel_cost_list[idx] is not None
                    ):
                        dcost = fuel_cost_list[idx] - primary_cost_estimate
                        cost_delta_str = f", {'+' if dcost >= 0 else ''}${dcost:.2f} fuel"

                    # Route name for delta description
                    if idx == 1:
                        route_label = "Route 2 (Alternate)"
                    else:
                        route_label = f"Route {idx + 1}"

                    delta_lines.append(
                        f"- {route_label}: {dmiles_str}, {dmins_str}, {dscore_str}{cost_delta_str}"
                    )

                if delta_lines:
                    delta_block = (
                        "\nRoute deltas vs Route 1 (Primary):\n"
                        + "\n".join(delta_lines)
                        + "\n"
                    )

        conditions_ver = CONDITIONS_CLIENT_VERSION or "unknown"
        policy_footer = (
            "\n\nPolicy & System Context:\n"
            "- Routing provider: OpenRouteService (ORS) directions (GeoJSON)\n"
            "- ORS profile: driving-hgv\n"
            "- Alternatives: dispatcher: disabled; driver: attempts provider alternatives if supported\n"
            f"- Sanity threshold: {threshold}×\n"
            "- Geocoding: ORS Pelias\n"
            f"- Weather: Open-Meteo (ConditionsClient {conditions_ver})\n"
            "- Traffic: TomTom Incident Details API (cached)\n"
            "- Fuel modeling: driver-only; costs estimated from distance, MPG, and user fuel price\n"
            f"- UI mode: {self._mode.upper()} | Avg speed: {self._avg_speed_mph} mph\n"
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
            + f"Route: {origin} \u2192 {destination}\n\n"
            + "Primary route:\n"
            + f"- Distance: {miles_primary:.1f} miles\n"
            + f"- Estimated drive time: {hours_primary}h {mins_primary}m\n"
            + f"- Geometry: {geometry_info}\n\n"
            + toll_block
            + "\n"
            + f"{weather_summary}\n\n"
            + f"{traffic_summary_primary}\n\n"
            + f"{risk_text_primary}\n"
            + (actions_block + "\n" if actions_block else "")
            + driver_block
            + fuel_block
            + eta_weather_block
            + recommended_block
            + delta_block
            + "\n"
            + f"Route comparison summary for: {origin} \u2192 {destination}\n"
            + "\n".join(route_lines)
            + policy_footer
        )

    # -------------------------
    # Clipboard
    # -------------------------

    def _on_copy_clicked(self) -> None:
        QApplication.clipboard().setText(self.conditions_text.toPlainText())