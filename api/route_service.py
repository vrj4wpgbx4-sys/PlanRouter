# api/route_service.py
"""
RoutePlanner service layer.
"""

from __future__ import annotations

from dataclasses import asdict
import re
from typing import Tuple, List, Dict, Any

from routing_client import RoutingClient, RoutingError

from .models import (
    RouteRequest,
    RouteResponse,
    ConditionsSummary,
    RiskComponent,
)


# ----------------------------
# ROUTE METRICS + SEGMENTS
# ----------------------------
def _compute_route_metrics(req: RouteRequest) -> Tuple[float, float, Dict[str, Any], List[Dict[str, Any]]]:
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
    segments = primary.get("segments") or []

    distance_miles = float(summary.get("distance_miles") or 0.0)
    if distance_miles <= 0:
        raise RoutingError("Routing returned zero distance for primary route.")

    avg_speed = req.avg_speed_mph if req.avg_speed_mph > 0 else 1.0
    eta_hours = distance_miles / avg_speed

    return distance_miles, eta_hours, geometry, segments


# ----------------------------
# CONDITIONS (stub)
# ----------------------------
def _compute_conditions(req: RouteRequest) -> ConditionsSummary:
    return ConditionsSummary(
        weather_summary="Weather data not yet wired into API layer.",
        traffic_summary="Traffic data not yet wired into API layer.",
        alerts=[],
    )


# ----------------------------
# RISK (baseline)
# ----------------------------
def _compute_risk(
    req: RouteRequest,
    distance_miles: float,
    conditions: ConditionsSummary,
) -> Tuple[float, str, List[RiskComponent]]:
    return 10.0, "LOW", []


# ----------------------------
# 🔥 HIGHWAY EXTRACTION
# ----------------------------
def _extract_highways(segments: List[Dict[str, Any]]) -> List[str]:
    highways = set()

    for seg in segments:
        for step in seg.get("steps", []):
            instruction = step.get("instruction", "")

            matches = re.findall(r"\bI[- ]?\d+\b", instruction)
            for m in matches:
                normalized = m.replace(" ", "-")
                highways.add(normalized.upper())

    return sorted(highways)


# ----------------------------
# STATE HELPERS (keep existing)
# ----------------------------
def _extract_state_from_location(location_text: str) -> str | None:
    text = location_text.strip().lower()
    parts = [p.strip() for p in text.split(",") if p.strip()]

    if parts:
        last = parts[-1]
        if len(last) == 2:
            return last.upper()

    return None


def _infer_state_path(origin_state: str | None, destination_state: str | None) -> List[str]:
    if origin_state and destination_state:
        if origin_state == destination_state:
            return [origin_state]
        return [origin_state, destination_state]
    return []


# ----------------------------
# 🔥 NEW EXPLANATION ENGINE
# ----------------------------
def _build_route_explanation(
    distance_miles: float,
    eta_hours: float,
    highways: List[str],
    states: List[str],
    risk_band: str,
) -> str:

    hours = int(eta_hours)
    minutes = int((eta_hours - hours) * 60)

    highways_str = ", ".join(highways) if highways else "regional highways"
    states_str = " → ".join(states) if states else "multi-state route"

    return (
        f"{int(distance_miles)} miles (~{hours}h {minutes}m)\n\n"
        f"Primary Highways:\n- {highways_str}\n\n"
        f"States:\n- {states_str}\n\n"
        f"Overall: {risk_band.lower()} operational risk"
    )


# ----------------------------
# ACTION
# ----------------------------
def _derive_recommended_action(
    mode: str,
    risk_band: str,
    conditions: ConditionsSummary,
) -> str:
    if risk_band == "LOW":
        return "Good to go."
    if risk_band == "MEDIUM":
        return "Stay alert."
    return "Re-evaluate route."


# ----------------------------
# MAIN ENTRY
# ----------------------------
def plan_route(req: RouteRequest) -> RouteResponse:
    distance_miles, eta_hours, geometry, segments = _compute_route_metrics(req)

    conditions = _compute_conditions(req)
    risk_score, risk_band, risk_components = _compute_risk(
        req=req,
        distance_miles=distance_miles,
        conditions=conditions,
    )

    highways = _extract_highways(segments)

    origin_state = _extract_state_from_location(req.origin)
    destination_state = _extract_state_from_location(req.destination)
    states = _infer_state_path(origin_state, destination_state)

    explanation = _build_route_explanation(
        distance_miles,
        eta_hours,
        highways,
        states,
        risk_band,
    )

    meta = {
        "origin": req.origin,
        "destination": req.destination,
        "geometry": geometry,
        "highways": highways,
        "states": states,
        "explanation": explanation,
    }

    return RouteResponse(
        distance_miles=distance_miles,
        eta_hours=eta_hours,
        risk_score=risk_score,
        risk_band=risk_band,
        conditions=conditions,
        recommended_action=_derive_recommended_action(
            req.mode, risk_band, conditions
        ),
        risk_components=risk_components,
        meta=meta,
    )