# api/route_service.py
"""
RoutePlanner service layer.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Tuple, List, Dict, Any

from routing_client import RoutingClient, RoutingError

from .models import (
    RouteRequest,
    RouteResponse,
    ConditionsSummary,
    RiskComponent,
)


def _compute_route_metrics(req: RouteRequest) -> Tuple[float, float, Dict[str, Any]]:
    client = RoutingClient(profile="driving-hgv")

    if req.stops:
        routes_result = client.get_route_with_stops(
            origin_text=req.origin,
            stops=[s.location for s in req.stops],
            destination_text=req.destination,
        )
    else:
        routes_result = client.get_routes(
            origin_text=req.origin,
            destination_text=req.destination,
        )

    routes = routes_result.get("routes") or []
    if not routes:
        raise RoutingError("Routing returned no routes.")

    primary = routes[0]
    summary = primary.get("summary") or {}
    geometry = primary.get("geometry") or {}

    distance_miles = float(summary.get("distance_miles") or 0.0)
    if distance_miles <= 0:
        raise RoutingError("Routing returned zero distance for primary route.")

    avg_speed = req.avg_speed_mph if req.avg_speed_mph > 0 else 1.0
    eta_hours = distance_miles / avg_speed

    return distance_miles, eta_hours, geometry


def _compute_conditions(req: RouteRequest) -> ConditionsSummary:
    alerts: List[str] = []

    weather_summary = "Weather data not yet wired into API layer."
    traffic_summary = "Traffic data not yet wired into API layer."

    return ConditionsSummary(
        weather_summary=weather_summary,
        traffic_summary=traffic_summary,
        alerts=alerts,
    )


def _compute_risk(
    req: RouteRequest,
    distance_miles: float,
    conditions: ConditionsSummary,
) -> Tuple[float, str, List[RiskComponent]]:
    risk_score = 10.0
    risk_band = "LOW"
    components: List[RiskComponent] = []

    if risk_score < 33:
        risk_band = "LOW"
    elif risk_score < 67:
        risk_band = "MEDIUM"
    else:
        risk_band = "HIGH"

    return risk_score, risk_band, components


# NEW — explanation engine
def _build_route_explanation(
    distance_miles: float,
    eta_hours: float,
    risk_band: str,
) -> str:
    hours = int(eta_hours)
    minutes = int((eta_hours - hours) * 60)

    time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    if risk_band == "LOW":
        risk_text = "low overall risk"
    elif risk_band == "MEDIUM":
        risk_text = "moderate risk conditions"
    else:
        risk_text = "elevated risk conditions"

    return (
        f"Approx. {int(distance_miles)} miles, {time_str} travel time. "
        f"Route currently shows {risk_text}."
    )


def _derive_recommended_action(
    mode: str,
    risk_band: str,
    conditions: ConditionsSummary,
) -> str:
    if mode == "dispatcher":
        if risk_band == "LOW":
            return "Route acceptable. Monitor conditions, but no changes required."
        if risk_band == "MEDIUM":
            return "Moderate risk. Consider adjusting departure time or route in coordination with driver."
        return "High risk. Re-evaluate route and timing before dispatch."

    if risk_band == "LOW":
        return "Good to go. Drive with normal caution."
    if risk_band == "MEDIUM":
        return "Conditions mixed. Stay alert and be prepared for delays."
    return "Elevated risk. Coordinate with dispatch before departure or continuing."


def plan_route(req: RouteRequest) -> RouteResponse:
    distance_miles, eta_hours, geometry = _compute_route_metrics(req)
    conditions = _compute_conditions(req)

    risk_score, risk_band, risk_components = _compute_risk(
        req=req,
        distance_miles=distance_miles,
        conditions=conditions,
    )

    recommended_action = _derive_recommended_action(
        mode=req.mode,
        risk_band=risk_band,
        conditions=conditions,
    )

    # NEW explanation
    explanation = _build_route_explanation(
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        risk_band=risk_band,
    )

    meta = {
        "origin": req.origin,
        "destination": req.destination,
        "stops": [asdict(s) for s in req.stops],
        "mode": req.mode,
        "avg_speed_mph": req.avg_speed_mph,
        "vehicle_profile": req.vehicle_profile,
        "geometry": geometry,
        "explanation": explanation,  # NEW
    }

    return RouteResponse(
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        risk_score=risk_score,
        risk_band=risk_band,  # type: ignore[arg-type]
        conditions=conditions,
        recommended_action=recommended_action,
        risk_components=risk_components,
        meta=meta,
    )