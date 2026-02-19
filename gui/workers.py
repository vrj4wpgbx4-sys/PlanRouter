from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


@dataclass
class PlanRouteInput:
    origin: str
    destination: str
    stops: List[str]


@dataclass
class PlanRouteResult:
    plan: Dict[str, Any]
    routes: List[Dict[str, Any]]
    toll: Dict[str, Any]
    sanity: Optional[Dict[str, Any]]
    stop_based: bool
    weather_summary: str
    traffic_error: str


class WorkerSignals(QObject):
    status = Signal(str)
    error = Signal(str)
    result = Signal(object)  # PlanRouteResult


class PlanRouteWorker(QRunnable):
    """
    Runs routing + weather + (optional) traffic fetch off the UI thread.
    """

    def __init__(self, inputs: PlanRouteInput):
        super().__init__()
        self.inputs = inputs
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            # Local imports inside worker to avoid Qt thread import edge cases
            from route_planner_ai.routing_client import RoutingClient
            from route_planner_ai.conditions_client import ConditionsClient, ConditionsError
            from route_planner_ai.traffic_client import TrafficClient

            origin = self.inputs.origin
            destination = self.inputs.destination
            stops = self.inputs.stops

            self.signals.status.emit("Initializing routing client…")
            client = RoutingClient()
            conditions_client = ConditionsClient()

            traffic_client = None
            traffic_error = ""
            try:
                traffic_client = TrafficClient()
            except Exception as e:
                traffic_error = str(e)

            self.signals.status.emit("Contacting routing service…")
            if stops and hasattr(client, "get_route_with_stops"):
                plan = client.get_route_with_stops(origin, stops, destination)
                stop_based = True
            else:
                plan = client.get_routes(origin, destination)
                stop_based = False

            routes = plan.get("routes", []) or []
            toll = plan.get("toll", {"available": False}) or {"available": False}
            sanity = plan.get("sanity")

            if not routes:
                self.signals.error.emit("No routes found.")
                return

            # Weather
            self.signals.status.emit("Fetching weather along route…")
            try:
                if hasattr(client, "geocode"):
                    o_lat, o_lon = client.geocode(origin)
                    d_lat, d_lon = client.geocode(destination)
                    weather_summary = conditions_client.get_route_weather(o_lat, o_lon, d_lat, d_lon)
                else:
                    weather_summary = "Weather lookup not available: routing client has no geocode()."
            except ConditionsError as e:
                weather_summary = f"Weather lookup not available:\n{e}"
            except Exception as e:
                weather_summary = f"Weather lookup not available:\n{e}"

            # Note: traffic is computed per-route in UI (risk comparison loop). We only surface init errors here.
            res = PlanRouteResult(
                plan=plan,
                routes=routes,
                toll=toll,
                sanity=sanity,
                stop_based=stop_based,
                weather_summary=weather_summary,
                traffic_error=traffic_error,
            )
            self.signals.result.emit(res)

        except Exception as e:
            self.signals.error.emit(str(e))
