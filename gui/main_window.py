from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    QGroupBox,
    QGridLayout,
    QLineEdit,
    QPushButton,
    QListWidget,
    QTextEdit,
    QSplitter,
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
_pkg_dir = Path(__file__).resolve().parents[1]  # project root (e.g. C:\Code\PlanRouter)
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
        CONDITIONS_CLIENT_VERSION = "unknown"


# -----------------------------------------------------------------------------
# Map HTML (Leaflet, no integrity attributes)
# -----------------------------------------------------------------------------

MAP_HTML = """<!DOCTYPE html>
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
    .weather-icon {
      background: transparent;
      border: none;
      font-size: 18px;
    }
  </style>
</head>
<body>
<div id="map"></div>
<script>
  var map = L.map('map').setView([46.87, -113.99], 6);

  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  function clearLayers() {
    if (window._routeLayer) {
      map.removeLayer(window._routeLayer);
      window._routeLayer = null;
    }
    if (window._markerLayerGroup) {
      map.removeLayer(window._markerLayerGroup);
      window._markerLayerGroup = null;
    }
  }

  function renderRoute(geojson, markers) {
    clearLayers();

    if (geojson && geojson.coordinates && geojson.coordinates.length > 0) {
      window._routeLayer = L.geoJSON(geojson).addTo(map);
    }

    if (markers && markers.length > 0) {
      window._markerLayerGroup = L.layerGroup().addTo(map);
      markers.forEach(function(m) {
        const lat = m.lat;
        const lon = m.lon;
        const label = m.label || "";
        const popupText = m.popup || "";
        const iconType = m.icon_type || "stop";
        const iconHtml = m.icon_html || "";

        if (iconType === "weather" && iconHtml) {
          const icon = L.divIcon({
            className: 'weather-icon',
            html: iconHtml,
            iconSize: [24, 24],
            iconAnchor: [12, 12]
          });
          const marker = L.marker([lat, lon], { icon: icon });
          if (popupText) {
            marker.bindPopup(popupText);
          }
          marker.addTo(window._markerLayerGroup);
        } else {
          const circleMarker = L.circleMarker([lat, lon], {
            radius: 6,
            weight: 2,
            color: '#0044cc',
            fillColor: '#66a3ff',
            fillOpacity: 0.9
          });
          if (popupText) {
            circleMarker.bindPopup(popupText);
          }
          circleMarker.addTo(window._markerLayerGroup);

          if (label) {
            const labelIcon = L.divIcon({
              className: 'stop-label',
              html: label,
              iconAnchor: [10, 10]
            });
            const labelMarker = L.marker([lat, lon], { icon: labelIcon });
            labelMarker.addTo(window._markerLayerGroup);
          }
        }
      });
    }

    if (window._routeLayer) {
      map.fitBounds(window._routeLayer.getBounds(), { padding: [20, 20] });
    } else if (window._markerLayerGroup) {
      map.fitBounds(window._markerLayerGroup.getBounds(), { padding: [20, 20] });
    }
  }

  window.renderRoute = renderRoute;
</script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# Worker signals / models
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
                traffic_client: Optional[TrafficClient] = TrafficClient()
                traffic_client_error: Optional[str] = None
            except Exception as e:
                traffic_client = None
                traffic_client_error = str(e)

            # ---------------- Routing ----------------
            self.signals.started.emit("Planning route...")

            try:
                if stops:
                    # Multi-drop / stop-based route.
                    if mode == "driver":
                        # In Driver mode, ask provider for alternatives if supported.
                        try:
                            plan = client.get_route_with_stops(
                                origin,
                                stops,
                                destination,
                                alternatives=True,  # type: ignore
                            )
                        except TypeError:
                            # Legacy signature without alternatives.
                            plan = client.get_route_with_stops(origin, stops, destination)  # type: ignore
                    else:
                        # Multi-drop behavior stays unchanged.
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
                    stamp = f" [{CONDITIONS_CLIENT_VERSION}]" if CONDITIONS_CLIENT_VERSION else ""
                    weather_summary = (
                        f"Weather lookup not available{stamp}:\n"
                        "- Routing client has no geocode()."
                    )
            except (RoutingError, ConditionsError) as e:
                stamp = f" [{CONDITIONS_CLIENT_VERSION}]" if CONDITIONS_CLIENT_VERSION else ""
                weather_summary = (
                    f"Weather lookup not available{stamp}:\n"
                    f"- {e}"
                )
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

                # Risk scoring
                risk_actions: List[str] = []
                try:
                    try:
                        # 5-return signature (score, label, explanation, actions, stats)
                        score, label, explanation, risk_actions, _stats = compute_route_risk(
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
            self.signals.failed.emit(f"Unexpected planning error:\n\n{e}")


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
        self.remove_btn.clicked.connect(self._on_remove_clicked)

        layout.addWidget(self.input, stretch=1)
        layout.addWidget(self.remove_btn)
        self.setLayout(layout)

    def _on_remove_clicked(self) -> None:
        self._remove_callback(self)


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
        self._avg_speed_mph: int = 70  # default driver speed preference
        self._mpg: int = 7
        self._fuel_price: float = 4.00

        self._stop_rows: List[StopRow] = []
        self._current_routes: List[Dict[str, Any]] = []
        self._last_plan: Optional[PlanResult] = None
        self._eta_marker_info: Optional[Dict[str, Any]] = None

        self._threadpool = QThreadPool.globalInstance()
        self._plan_button_enabled: bool = True

        self._build_ui()

    # -------------------------------------------------------------------------
    # UI construction
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

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

        # Driver controls: avg speed, MPG, fuel price, departure date/time
        driver_row = QHBoxLayout()
        driver_label = QLabel("Driver settings (when in Driver mode):")
        driver_row.addWidget(driver_label)
        driver_row.addStretch(1)
        left_layout.addLayout(driver_row)

        # Average speed
        avg_speed_row = QHBoxLayout()
        avg_speed_label = QLabel("Average speed (mph):")
        self.avg_speed_input = QSpinBox()
        self.avg_speed_input.setRange(30, 85)
        self.avg_speed_input.setSingleStep(5)
        self.avg_speed_input.setValue(self._avg_speed_mph)
        self.avg_speed_input.valueChanged.connect(self._on_avg_speed_changed)
        avg_speed_row.addWidget(avg_speed_label)
        avg_speed_row.addWidget(self.avg_speed_input)
        avg_speed_row.addStretch(1)
        left_layout.addLayout(avg_speed_row)

        # MPG
        mpg_row = QHBoxLayout()
        mpg_label = QLabel("Truck fuel economy (mpg):")
        self.mpg_input = QSpinBox()
        self.mpg_input.setRange(3, 12)
        self.mpg_input.setSingleStep(1)
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

        # Stops header
        stops_header = QHBoxLayout()
        stops_label = QLabel("Stops (optional, in order):")
        stops_header.addWidget(stops_label)

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

        # Route list
        self.route_list = QListWidget()
        self.route_list.currentRowChanged.connect(self._on_route_selected)
        left_layout.addWidget(self.route_list, stretch=1)

        splitter.addWidget(left)

        # ---------------- Right Panel ----------------
        right = QWidget()
        right_layout = QVBoxLayout()
        right.setLayout(right_layout)

        # Route summary panel
        self.summary_group = QGroupBox("Route Summary")
        summary_layout = QGridLayout()
        self.summary_group.setLayout(summary_layout)

        self.summary_distance_label = QLabel("Distance:")
        self.summary_distance_value = QLabel("—")

        self.summary_duration_label = QLabel("Drive time:")
        self.summary_duration_value = QLabel("—")

        self.summary_risk_label = QLabel("Risk:")
        self.summary_risk_value = QLabel("—")

        self.summary_eta_label = QLabel("ETA (driver):")
        self.summary_eta_value = QLabel("—")

        self.summary_fuel_label = QLabel("Fuel (est):")
        self.summary_fuel_value = QLabel("—")

        summary_layout.addWidget(self.summary_distance_label, 0, 0)
        summary_layout.addWidget(self.summary_distance_value, 0, 1)
        summary_layout.addWidget(self.summary_duration_label, 1, 0)
        summary_layout.addWidget(self.summary_duration_value, 1, 1)
        summary_layout.addWidget(self.summary_risk_label, 2, 0)
        summary_layout.addWidget(self.summary_risk_value, 2, 1)
        summary_layout.addWidget(self.summary_eta_label, 3, 0)
        summary_layout.addWidget(self.summary_eta_value, 3, 1)
        summary_layout.addWidget(self.summary_fuel_label, 4, 0)
        summary_layout.addWidget(self.summary_fuel_value, 4, 1)

        right_layout.addWidget(self.summary_group)

        # Map view
        self.map_view = QWebEngineView()
        self.map_view.setMinimumHeight(420)
        right_layout.addWidget(self.map_view)

        # Conditions / report
        self.conditions_text = QTextEdit()
        self.conditions_text.setReadOnly(True)
        self.conditions_text.setPlaceholderText("Dispatch report will appear here...")
        right_layout.addWidget(self.conditions_text, stretch=1)

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
        self._reset_summary_panel()

    # -------------------------------------------------------------------------
    # Mode / driver param handlers
    # -------------------------------------------------------------------------

    def _on_mode_changed(self, index: int) -> None:
        self._mode = "dispatcher" if index == 0 else "driver"
        self._update_depart_controls_enabled()

    def _update_depart_controls_enabled(self) -> None:
        is_driver = (self._mode == "driver")
        self.avg_speed_input.setEnabled(is_driver)
        self.mpg_input.setEnabled(is_driver)
        self.fuel_price_input.setEnabled(is_driver)
        self.depart_date_edit.setEnabled(is_driver)
        self.depart_time_edit.setEnabled(is_driver)

    def _on_avg_speed_changed(self, value: int) -> None:
        self._avg_speed_mph = int(value)

    def _on_mpg_changed(self, value: int) -> None:
        self._mpg = int(value)

    def _on_fuel_price_changed(self, value: float) -> None:
        self._fuel_price = float(value)

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def on_plan_route_clicked(self) -> None:
        origin = self.origin_input.text().strip()
        destination = self.destination_input.text().strip()

        stops: List[str] = []
        for row in self._stop_rows:
            text = row.input.text().strip()
            if text:
                stops.append(text)

        if not origin or not destination:
            self.conditions_text.setPlainText("Please provide both origin and destination.")
            return

        if not self._plan_button_enabled:
            self.conditions_text.setPlainText("A route is already being planned. Please wait.")
            return

        payload = PlanInput(
            origin=origin,
            destination=destination,
            stops=stops,
            mode=self._mode,
        )

        self._plan_button_enabled = False
        self.plan_btn.setEnabled(False)

        self.route_list.clear()
        self._current_routes = []
        self._last_plan = None
        self._eta_marker_info = None
        self._reset_summary_panel()
        self.conditions_text.setPlainText("Starting...")

        # Clear map on new run
        self._push_map({"type": "LineString", "coordinates": []}, [])

        worker = PlanWorker(payload)
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
        self._eta_marker_info = None

        self.origin_input.clear()
        self.destination_input.clear()
        self._clear_all_stops()

        self.route_list.clear()
        self.conditions_text.setPlainText("Cleared. Enter a new route and click 'Plan Route'.")
        self._reset_summary_panel()

        self._push_map({"type": "LineString", "coordinates": []}, [])

    def _on_worker_status(self, msg: str) -> None:
        self.conditions_text.setPlainText(msg)

    def _on_worker_failed(self, msg: str) -> None:
        self.conditions_text.setPlainText(msg)
        self._reset_summary_panel()
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

        # Always compute the report first so ETA marker info is available
        self.conditions_text.setPlainText(self._build_dispatch_report(result))
        self._update_summary_panel_from_plan(result)

        if self._current_routes:
            self.route_list.setCurrentRow(0)
            self._render_selected_route(0)

        self.plan_btn.setEnabled(True)
        self._plan_button_enabled = True

    # -------------------------------------------------------------------------
    # Summary panel helpers
    # -------------------------------------------------------------------------

    def _reset_summary_panel(self) -> None:
        if not hasattr(self, "summary_distance_value"):
            return
        self.summary_distance_value.setText("—")
        self.summary_duration_value.setText("—")
        self.summary_risk_value.setText("—")
        self.summary_eta_value.setText("—")
        self.summary_fuel_value.setText("—")

    def _update_summary_panel_from_plan(self, result: PlanResult) -> None:
        if not hasattr(self, "summary_distance_value"):
            return

        routes = result.routes or []
        per_route = result.per_route or []

        primary_summary = (routes[0].get("summary", {}) if routes else {}) or {}
        miles_primary = float(primary_summary.get("distance_miles", 0.0) or 0.0)
        minutes_primary = float(primary_summary.get("duration_minutes", 0.0) or 0.0)

        # Distance / duration
        if miles_primary > 0:
            self.summary_distance_value.setText(f"{miles_primary:.1f} miles")
        else:
            self.summary_distance_value.setText("—")

        if minutes_primary > 0:
            hours = int(minutes_primary // 60)
            mins = int(round(minutes_primary - hours * 60))
            self.summary_duration_value.setText(f"{hours}h {mins}m")
        else:
            self.summary_duration_value.setText("—")

        # Risk (primary route)
        if per_route and per_route[0]:
            pr0 = per_route[0]
            label = str(pr0.get("risk_label", "UNKNOWN"))
            score = int(pr0.get("risk_score", 0) or 0)
            self.summary_risk_value.setText(f"{label} ({score}/100)")
        else:
            self.summary_risk_value.setText("—")

        # Driver-mode ETA and fuel
        if self._mode == "driver" and miles_primary > 0 and self._avg_speed_mph > 0:
            try:
                depart_date = self.depart_date_edit.date()
                depart_time = self.depart_time_edit.time()
                depart_dt = datetime(
                    depart_date.year(),
                    depart_date.month(),
                    depart_date.day(),
                    depart_time.hour(),
                    depart_time.minute(),
                )
                drive_hours = miles_primary / float(self._avg_speed_mph)
                drive_minutes = int(round(drive_hours * 60.0))
                eta_dt = depart_dt + timedelta(minutes=drive_minutes)
                eta_str = eta_dt.strftime("%Y-%m-%d %H:%M")
                self.summary_eta_value.setText(eta_str)
            except Exception:
                self.summary_eta_value.setText("—")
        else:
            self.summary_eta_value.setText("—")

        if (
            self._mode == "driver"
            and miles_primary > 0
            and self._mpg > 0
            and self._fuel_price > 0
        ):
            try:
                gallons = miles_primary / float(self._mpg)
                cost = gallons * self._fuel_price
                self.summary_fuel_value.setText(f"{gallons:.1f} gal (~${cost:.2f})")
            except Exception:
                self.summary_fuel_value.setText("—")
        else:
            self.summary_fuel_value.setText("—")

    # -------------------------------------------------------------------------
    # Weather icon helper
    # -------------------------------------------------------------------------

    @staticmethod
    def _weather_icon_from_desc(desc: str) -> str:
        text = (desc or "").lower()
        if "snow" in text or "flurries" in text or "sleet" in text or "blizzard" in text:
            return "❄️"
        if "thunder" in text or "storm" in text:
            return "⛈️"
        if "rain" in text or "shower" in text or "drizzle" in text:
            return "🌧️"
        if "fog" in text or "mist" in text:
            return "🌫️"
        if "cloud" in text or "overcast" in text:
            return "☁️"
        if "wind" in text or "breezy" in text or "gust" in text:
            return "💨"
        return "🌡️"

    # -------------------------------------------------------------------------
    # Route selection + map rendering
    # -------------------------------------------------------------------------

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

        def _add_marker(
            lat: float,
            lon: float,
            label: str,
            popup: str,
            icon_type: str = "stop",
            icon_html: str = "",
        ) -> None:
            markers.append(
                {
                    "lat": float(lat),
                    "lon": float(lon),
                    "label": label,
                    "popup": popup,
                    "icon_type": icon_type,
                    "icon_html": icon_html or "",
                }
            )

        try:
            client = RoutingClient()
        except Exception:
            client = None  # type: ignore

        origin_txt = self._last_plan.origin
        dest_txt = self._last_plan.destination
        stops_txt = self._last_plan.stops or []

        # Base O / stops / D markers via geocoding
        if client is not None and hasattr(client, "geocode"):
            try:
                o_lat, o_lon = client.geocode(origin_txt)  # type: ignore
                _add_marker(o_lat, o_lon, "O", f"Origin: {origin_txt}", icon_type="stop")
            except Exception:
                pass

            for i, s in enumerate(stops_txt, start=1):
                try:
                    s_lat, s_lon = client.geocode(s)  # type: ignore
                    _add_marker(s_lat, s_lon, str(i), f"Stop {i}: {s}", icon_type="stop")
                except Exception:
                    continue

            try:
                d_lat, d_lon = client.geocode(dest_txt)  # type: ignore
                _add_marker(d_lat, d_lon, "D", f"Destination: {dest_txt}", icon_type="stop")
            except Exception:
                pass

        # Fallback O / D markers from geometry if geocode fails
        if not markers and isinstance(geometry, dict) and isinstance(geometry.get("coordinates"), list):
            coords = geometry["coordinates"]
            if len(coords) >= 2:
                o = coords[0]
                d = coords[-1]
                if isinstance(o, (list, tuple)) and len(o) == 2:
                    _add_marker(o[1], o[0], "O", f"Origin: {origin_txt}", icon_type="stop")
                if isinstance(d, (list, tuple)) and len(d) == 2:
                    _add_marker(d[1], d[0], "D", f"Destination: {dest_txt}", icon_type="stop")

        # ETA weather markers (Driver mode only)
        if (
            self._mode == "driver"
            and self._eta_marker_info is not None
            and isinstance(geometry, dict)
            and isinstance(geometry.get("coordinates"), list)
        ):
            coords = geometry.get("coordinates") or []
            if len(coords) >= 2:
                try:
                    mid_idx = len(coords) // 2
                    mid = coords[mid_idx]
                    d = coords[-1]

                    mid_info = self._eta_marker_info.get("midpoint") if self._eta_marker_info else None
                    dest_info = self._eta_marker_info.get("destination") if self._eta_marker_info else None

                    if (
                        mid_info is not None
                        and isinstance(mid, (list, tuple))
                        and len(mid) == 2
                    ):
                        mid_dt = mid_info.get("time")
                        mid_desc = mid_info.get("desc", "")
                        mid_time_str = (
                            mid_dt.strftime("%Y-%m-%d %H:%M")
                            if hasattr(mid_dt, "strftime")
                            else ""
                        )
                        popup = "Midpoint ETA"
                        if mid_time_str:
                            popup += f"\n{mid_time_str}"
                        if mid_desc:
                            popup += f"\n{mid_desc}"
                        icon = self._weather_icon_from_desc(mid_desc)
                        _add_marker(mid[1], mid[0], "", popup, icon_type="weather", icon_html=icon)

                    if (
                        dest_info is not None
                        and isinstance(d, (list, tuple))
                        and len(d) == 2
                    ):
                        dest_dt = dest_info.get("time")
                        dest_desc = dest_info.get("desc", "")
                        dest_time_str = (
                            dest_dt.strftime("%Y-%m-%d %H:%M")
                            if hasattr(dest_dt, "strftime")
                            else ""
                        )
                        popup = "Destination ETA"
                        if dest_time_str:
                            popup += f"\n{dest_time_str}"
                        if dest_desc:
                            popup += f"\n{dest_desc}"
                        icon = self._weather_icon_from_desc(dest_desc)
                        _add_marker(d[1], d[0], "", popup, icon_type="weather", icon_html=icon)

                except Exception:
                    pass

        self._push_map(geometry, markers)

    # -------------------------------------------------------------------------
    # Dispatch Report
    # -------------------------------------------------------------------------

    def _build_dispatch_report(self, result: PlanResult) -> str:
        self._eta_marker_info = None

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

        lines: List[str] = []

        utc_now = datetime.now(timezone.utc)
        lines.append("=== DISPATCH REPORT ===")
        lines.append(f"Dispatch timestamp: {utc_now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"Route ID: {uuid.uuid4()}")
        lines.append("")

        # Trip overview
        lines.append("Trip overview:")
        if stops:
            lines.append(f"- Origin:      {origin}")
            lines.append(f"- Destination: {destination}")
            lines.append(f"- Stops:       {', '.join(stops)}")
            lines.append("")
        else:
            lines.append(f"- Direct route from {origin} to {destination}")
            lines.append("")

        if result.stop_based:
            lines.append("Route structure:")
            lines.append("- Multi-drop / stop-based route (stops treated as required waypoints).")
            lines.append("")
        else:
            lines.append("Route structure:")
            lines.append("- Direct or optimized routing (stops treated as via-points if present).")
            lines.append("")

        # Primary route
        lines.append("Primary route (dispatcher view):")
        if miles_primary > 0 and minutes_primary > 0:
            lines.append(
                f"- Distance: {miles_primary:.1f} miles\n"
                f"- Drive time: {hours_primary}h {mins_primary}m"
            )
        else:
            lines.append("- No primary route distance/time available yet.")
        lines.append("")

        # Sanity check
        if sanity:
            ok = bool(sanity.get("ok", True))
            ratio = float(sanity.get("ratio", 0.0) or 0.0)
            straight = float(sanity.get("straight_line_miles", 0.0) or 0.0)
            routed = float(sanity.get("routed_distance_miles", 0.0) or 0.0)
            lines.append("Distance sanity check:")
            lines.append(f"- Straight line: {straight:.1f} miles")
            lines.append(f"- Routed:        {routed:.1f} miles")
            lines.append(f"- Ratio:         {ratio:.2f}x vs straight line")
            lines.append(f"- Status:        {'OK' if ok else 'SUSPICIOUS'}")
            lines.append("")
        else:
            lines.append("Distance sanity check:")
            lines.append("- No sanity-check data returned.")
            lines.append("")

        # Toll info
        toll_block = "Toll information:\n"
        if toll_info.get("available"):
            note = toll_info.get("note", "Toll info available.")
            toll_block += f"- {note}\n"
            details = toll_info.get("details")
            if details:
                toll_block += f"- Details: {details}\n"
        else:
            toll_block += "- Toll info not available from routing service for this profile/region.\n"

        traffic_summary_primary = ""
        risk_text_primary = ""
        actions_block = ""
        route_lines: List[str] = []

        primary_risk_score: Optional[int] = None
        miles_list: List[float] = []
        minutes_list: List[float] = []

        # Per-route summary
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

            miles_list.append(miles)
            minutes_list.append(minutes)

            # Route naming
            if self._mode == "driver":
                if idx == 0:
                    route_name = "Route 1 (Primary)"
                elif idx == 1:
                    route_name = "Route 2 (Alternate)"
                else:
                    route_name = f"Route {idx + 1}"
            else:
                route_name = f"Route {idx + 1}"

            route_lines.append(
                f"- {route_name}: {miles:.1f} miles, {hours}h {mins}m — "
                f"Risk {label} ({score}/100); {expl}"
            )

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

            candidate = (score, miles, idx, label, expl)
            if best_choice is None:
                best_choice = candidate
            else:
                if candidate[0] < best_choice[0] or (
                    candidate[0] == best_choice[0] and candidate[1] < best_choice[1]
                ):
                    best_choice = candidate

        # Route options summary
        lines.append("Route options summary:")
        if route_lines:
            lines.extend(route_lines)
        else:
            lines.append("- No route options available from service.")
        lines.append("")

        # Preferred route decision
        if best_choice is not None:
            best_score, best_miles, best_idx, best_label, best_expl = best_choice
            if best_idx == 0:
                choice_name = "Primary route appears acceptable."
            else:
                if self._mode == "driver":
                    choice_name = f"Alternate route {best_idx + 1} is suggested."
                else:
                    choice_name = f"Alternate route {best_idx + 1} is suggested."
            lines.append("Routing decision guidance:")
            lines.append(f"- {choice_name}")
            lines.append(f"- Rationale: {best_label} risk ({best_score}/100); {best_expl}")
            lines.append("")
        else:
            lines.append("Routing decision guidance:")
            lines.append("- No clear preferred route (insufficient data).")
            lines.append("")

        # Traffic + risk text for primary
        if traffic_summary_primary:
            lines.append("Traffic & delay summary (primary route):")
            lines.append(f"- {traffic_summary_primary}")
            lines.append("")
        if risk_text_primary:
            lines.append(risk_text_primary)
            lines.append("")
        if actions_block:
            lines.append(actions_block)
            lines.append("")

        # Toll block
        lines.append(toll_block)
        lines.append("")

        # Weather block
        if weather_summary:
            lines.append("Weather along route:")
            lines.append(weather_summary)
            lines.append("")
        else:
            lines.append("Weather along route:")
            lines.append("- No weather details returned from service.")
            lines.append("")

        # Simple deltas: distance / time vs primary
        if len(routes) > 1 and miles_list and minutes_list:
            base_miles = miles_list[0]
            base_minutes = minutes_list[0]
            lines.append("Route deltas vs primary:")
            for i in range(1, len(routes)):
                dm = miles_list[i] - base_miles
                dt = minutes_list[i] - base_minutes
                dm_sign = "+" if dm >= 0 else "-"
                dt_sign = "+" if dt >= 0 else "-"
                lines.append(
                    f"- Route {i + 1}: {dm_sign}{abs(dm):.1f} miles, {dt_sign}{abs(dt):.1f} minutes vs primary."
                )
            lines.append("")

        # Driver ETA + fuel block
        if self._mode == "driver" and miles_primary > 0:
            try:
                d = self.depart_date_edit.date()
                t = self.depart_time_edit.time()
                depart_dt = datetime(
                    d.year(), d.month(), d.day(), t.hour(), t.minute()
                )
                if self._avg_speed_mph > 0:
                    eta_hours = miles_primary / float(self._avg_speed_mph)
                    eta_minutes_total = int(round(eta_hours * 60.0))
                    eta_dt = depart_dt + timedelta(minutes=eta_minutes_total)

                    eta_h = eta_minutes_total // 60
                    eta_m = eta_minutes_total % 60
                    eta_str = eta_dt.strftime("%Y-%m-%d %H:%M")

                    lines.append("ETA driver planning (based on your settings):")
                    lines.append(f"- Average speed: {self._avg_speed_mph} mph")
                    lines.append(f"- Planned departure: {depart_dt.strftime('%Y-%m-%d %H:%M')}")
                    lines.append(f"- Approx driving time at this speed: {eta_h}h {eta_m}m")
                    lines.append(f"- Estimated arrival time: {eta_str}")
                    lines.append("")

                    # Prepare minimal ETA marker info for map icons
                    self._eta_marker_info = {
                        "midpoint": {
                            "time": depart_dt + timedelta(minutes=eta_minutes_total // 2),
                            "desc": "Midpoint ETA window",
                        },
                        "destination": {
                            "time": eta_dt,
                            "desc": "Destination ETA window",
                        },
                    }
            except Exception:
                pass

            if self._mpg > 0 and self._fuel_price > 0:
                gallons_primary = miles_primary / float(self._mpg)
                cost_primary = gallons_primary * self._fuel_price
                lines.append("Fuel estimate (driver planning):")
                lines.append(f"- Truck MPG: {self._mpg} mpg")
                lines.append(f"- Fuel price (est): ${self._fuel_price:.2f}/gal")
                lines.append(f"- Estimated fuel used (primary): {gallons_primary:.1f} gal")
                lines.append(f"- Estimated fuel cost (primary): ${cost_primary:.2f}")
                lines.append("")

        # Overall risk posture
        if primary_risk_score is not None:
            if primary_risk_score >= 80:
                lines.append("Overall risk posture: HIGH — consider delay, reroute, or extra caution.")
            elif primary_risk_score >= 50:
                lines.append("Overall risk posture: MODERATE — acceptable with standard caution.")
            else:
                lines.append("Overall risk posture: LOW — no extraordinary risk detected.")
        else:
            lines.append("Overall risk posture: UNKNOWN — insufficient risk data.")
        lines.append("")

        lines.append("Policy notes:")
        lines.append("- This tool provides guidance only. Final route decisions remain with dispatch and driver.")
        lines.append("- Always follow company policy, regulatory requirements, and real-time conditions.")
        lines.append("")
        lines.append(f"Conditions engine: {CONDITIONS_CLIENT_VERSION}")
        lines.append("")
        lines.append(f"Route Planner AI version: v{APP_VERSION}")
        lines.append("")

        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Stop management
    # -------------------------------------------------------------------------

    def _add_stop_row(self) -> None:
        row = StopRow(self._remove_stop_row)
        self._stop_rows.append(row)
        self.stops_layout.addWidget(row)

    def _remove_stop_row(self, row: StopRow) -> None:
        if row in self._stop_rows:
            self._stop_rows.remove(row)
        row.setParent(None)
        row.deleteLater()

    def _clear_all_stops(self) -> None:
        for row in self._stop_rows:
            row.setParent(None)
            row.deleteLater()
        self._stop_rows.clear()

    # -------------------------------------------------------------------------
    # Copy dispatch report
    # -------------------------------------------------------------------------

    def _on_copy_clicked(self) -> None:
        text = self.conditions_text.toPlainText()
        if not text.strip():
            return
        QApplication.clipboard().setText(text)

    # -------------------------------------------------------------------------
    # Map helpers
    # -------------------------------------------------------------------------

    def _load_map(self) -> None:
        self.map_view.setHtml(MAP_HTML, QUrl("about:blank"))

    def _push_map(self, geometry: Dict[str, Any], markers: List[Dict[str, Any]]) -> None:
        js = (
            "window.renderRoute("
            + json.dumps(geometry or {"type": "LineString", "coordinates": []})
            + ", "
            + json.dumps(markers or [])
            + ");"
        )
        self.map_view.page().runJavaScript(js)


# -----------------------------------------------------------------------------
# Main entry
# -----------------------------------------------------------------------------

def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())