# api/api_main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dataclasses import asdict

from .models import RouteRequest
from .route_service import plan_route

app = FastAPI(title="PlanRouter API")

# CORS (keep open for now)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "PlanRouter API running"}


@app.post("/api/route-plan")
def route_plan(req: RouteRequest):
    result = plan_route(req)

    # 🔥 CRITICAL FIX:
    # Convert entire response to dict WITHOUT rebuilding pieces
    response_dict = asdict(result)

    return response_dict