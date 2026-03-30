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


# ----------------------------
# ROUTE METRICS
# ----------------------------
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

    risk_score = 10.0
    risk_band = "LOW"

    if risk_score < 33:
        risk_band = "LOW"
    elif risk_score < 67:
        risk_band = "MEDIUM"
    else:
        risk_band = "HIGH"

    return risk_score, risk_band, []


# ----------------------------
# NEW — STATE DETECTION
# ----------------------------
def _extract_states(req: RouteRequest) -> List[str]:
    text = f"{req.origin} {req.destination}".lower()

    states_map = {
        "mt": "MT",
        "id": "ID",
        "wy": "WY",
        "nd": "ND",
        "sd": "SD",
        "mn": "MN",
        "wi": "WI",
        "il": "IL",
        "wa": "WA",
        "or": "OR",
        "ca": "CA",
        "co": "CO",
    }

    found = []
    for key, val in states_map.items():
        if key in text:
            found.append(val)

    return list(dict.fromkeys(found))  # dedupe preserve order


# ----------------------------
# NEW — CORRIDOR DETECTION
# ----------------------------
def _detect_corridor(states: List[str]) -> str:
    if {"MT", "ND", "MN", "WI", "IL"}.issubset(set(states)):
        return "Northern freight corridor (I-90 / I-94)"
    if {"CA", "AZ", "TX"}.intersection(states):
        return "Southern corridor (I-10 / I-40 patterns)"
    return "Mixed regional highway network"


# ----------------------------
# 🔥 UPGRADED EXPLANATION ENGINE
# ----------------------------
def _build_route_explanation(
    req: RouteRequest,
    distance_miles: float,
    eta_hours: float,
    risk_band: str,
) -> str:

    hours = int(eta_hours)
    minutes = int((eta_hours - hours) * 60)
    time_str = f"{hours}h {minutes}m"

    states = _extract_states(req)
    corridor = _detect_corridor(states)

    # Terrain logic
    terrain_start = "mountain terrain early" if "MT" in states or "ID" in states else "mixed terrain early"
    terrain_end = "flatter midwest terrain approaching destination" if "IL" in states else "mixed terrain late"

    # Wind / exposure
    wind_note = "high crosswind exposure across plains" if "ND" in states or "SD" in states else "normal wind exposure"

    # Urban pressure
    urban_note = ""
    if "IL" in states:
        urban_note = "heavy traffic congestion approaching Chicago"

    # Risk text
    risk_text = {
        "LOW": "low operational risk",
        "MEDIUM": "moderate operational risk",
        "HIGH": "elevated operational risk",
    }[risk_band]

    states_str = " → ".join(states) if states else "multi-state route"

    return (
        f"{int(distance_miles)} miles (~{time_str})\n\n"
        f"Corridor:\n- {corridor}\n\n"
        f"States:\n- {states_str}\n\n"
        f"Driver Notes:\n"
        f"- {terrain_start}\n"
        f"- {terrain_end}\n"
        f"- {wind_note}\n"
        f"{f'- {urban_note}\n' if urban_note else ''}"
        f"\nOverall: {risk_text}"
    )


# ----------------------------
# ACTION
# ----------------------------
def _derive_recommended_action(
    mode: str,
    risk_band: str,
    conditions: ConditionsSummary,
) -> str:

    if mode == "dispatcher":
        if risk_band == "LOW":
            return "Route acceptable."
        if risk_band == "MEDIUM":
            return "Monitor conditions and consider adjustments."
        return "Re-evaluate route before dispatch."

    if risk_band == "LOW":
        return "Good to go."
    if risk_band == "MEDIUM":
        return "Stay alert."
    return "Coordinate with dispatch."


# ----------------------------
# MAIN ENTRY
# ----------------------------
def plan_route(req: RouteRequest) -> RouteResponse:
    distance_miles, eta_hours, geometry = _compute_route_metrics(req)
    conditions = _compute_conditions(req)

    risk_score, risk_band, risk_components = _compute_risk(
        req=req,
        distance_miles=distance_miles,
        conditions=conditions,
    )

    explanation = _build_route_explanation(
        req=req,
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