from __future__ import annotations

from typing import List, Optional, Literal, Dict, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .models import RouteRequest, Stop
from .route_service import plan_route


class APIRouteRequest(BaseModel):
    origin: str = Field(...)
    destination: str = Field(...)
    stops: List[str] = Field(default_factory=list)
    mode: Literal["driver", "dispatcher"] = "driver"
    avg_speed_mph: float = 70.0
    vehicle_profile: Optional[str] = None
    notes: Optional[str] = None


class APIRouteResponse(BaseModel):
    distance_miles: float
    eta_hours: float
    risk_score: float
    risk_band: Literal["LOW", "MEDIUM", "HIGH"]
    weather_summary: str
    traffic_summary: str
    alerts: List[str]
    recommended_action: str
    risk_components: List[dict] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)
    geometry: Dict[str, Any] = Field(default_factory=dict)


app = FastAPI(
    title="RoutePlanner API",
    description="Backend API for RoutePlanner.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}


@app.post("/api/route-plan", response_model=APIRouteResponse)
def route_plan(body: APIRouteRequest) -> APIRouteResponse:
    stops = [Stop(location=s) for s in body.stops]

    internal_req = RouteRequest(
        origin=body.origin,
        destination=body.destination,
        stops=stops,
        mode=body.mode,
        avg_speed_mph=body.avg_speed_mph,
        vehicle_profile=body.vehicle_profile,
        notes=body.notes,
    )

    resp = plan_route(internal_req)
    geometry = resp.meta.get("geometry", {}) if isinstance(resp.meta, dict) else {}

    return APIRouteResponse(
        distance_miles=resp.distance_miles,
        eta_hours=resp.eta_hours,
        risk_score=resp.risk_score,
        risk_band=resp.risk_band,
        weather_summary=resp.conditions.weather_summary,
        traffic_summary=resp.conditions.traffic_summary,
        alerts=resp.conditions.alerts,
        recommended_action=resp.recommended_action,
        risk_components=[rc.__dict__ for rc in resp.risk_components],
        meta=resp.meta,
        geometry=geometry,
    )