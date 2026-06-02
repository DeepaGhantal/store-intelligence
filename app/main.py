"""
main.py — FastAPI entrypoint with structured logging, trace IDs, graceful degradation.
"""
import logging
import time
import uuid
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from pydantic import ValidationError

from .database import init_db, get_db
from .models import IngestRequest, IngestResponse
from .ingestion import ingest_events
from .metrics import get_store_metrics
from .funnel import get_funnel
from .heatmap import get_heatmap
from .anomalies import get_anomalies
from .health import get_health

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":%(message)s}',
)
logger = logging.getLogger("store_intelligence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info('"DB initialised"')
    yield


app = FastAPI(
    title="Store Intelligence API",
    description="Apex Retail — offline store analytics from CCTV events",
    version="1.0.0",
    lifespan=lifespan,
)


# ── Middleware: trace_id + structured request logging ─────────────────────────
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id
    start = time.time()

    try:
        response = await call_next(request)
    except Exception as exc:
        latency = round((time.time() - start) * 1000, 1)
        logger.error(json.dumps({
            "trace_id": trace_id,
            "endpoint": request.url.path,
            "method": request.method,
            "latency_ms": latency,
            "status_code": 500,
            "error": str(exc),
        }))
        return JSONResponse(status_code=500, content={"error": "Internal server error", "trace_id": trace_id})

    latency = round((time.time() - start) * 1000, 1)
    store_id = request.path_params.get("store_id", "N/A")
    log_entry = {
        "trace_id": trace_id,
        "store_id": store_id,
        "endpoint": request.url.path,
        "method": request.method,
        "latency_ms": latency,
        "status_code": response.status_code,
    }
    # Attach event_count for ingest endpoint if set by the handler
    event_count = getattr(request.state, "event_count", None)
    if event_count is not None:
        log_entry["event_count"] = event_count
    logger.info(json.dumps(log_entry))
    response.headers["X-Trace-Id"] = trace_id
    return response


def db_error_response(trace_id: str):
    return JSONResponse(
        status_code=503,
        content={
            "error": "Database unavailable",
            "trace_id": trace_id,
            "message": "Service temporarily unavailable. Please retry.",
        },
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/events/ingest", response_model=IngestResponse, status_code=200)
async def ingest(request: Request, payload: IngestRequest, db: Session = Depends(get_db)):
    trace_id = getattr(request.state, "trace_id", "N/A")
    try:
        result = ingest_events(payload.events, db)
        request.state.event_count = len(payload.events)  # picked up by middleware log
        return IngestResponse(**result)
    except OperationalError:
        return db_error_response(trace_id)


@app.get("/stores/{store_id}/metrics")
async def metrics(store_id: str, request: Request, db: Session = Depends(get_db)):
    trace_id = getattr(request.state, "trace_id", "N/A")
    try:
        return get_store_metrics(store_id, db)
    except OperationalError:
        return db_error_response(trace_id)


@app.get("/stores/{store_id}/funnel")
async def funnel(store_id: str, request: Request, db: Session = Depends(get_db)):
    trace_id = getattr(request.state, "trace_id", "N/A")
    try:
        return get_funnel(store_id, db)
    except OperationalError:
        return db_error_response(trace_id)


@app.get("/stores/{store_id}/heatmap")
async def heatmap(store_id: str, request: Request, db: Session = Depends(get_db)):
    trace_id = getattr(request.state, "trace_id", "N/A")
    try:
        return get_heatmap(store_id, db)
    except OperationalError:
        return db_error_response(trace_id)


@app.get("/stores/{store_id}/anomalies")
async def anomalies(store_id: str, request: Request, db: Session = Depends(get_db)):
    trace_id = getattr(request.state, "trace_id", "N/A")
    try:
        return get_anomalies(store_id, db)
    except OperationalError:
        return db_error_response(trace_id)


@app.get("/health")
async def health(request: Request, db: Session = Depends(get_db)):
    trace_id = getattr(request.state, "trace_id", "N/A")
    try:
        return get_health(db)
    except OperationalError:
        return db_error_response(trace_id)
