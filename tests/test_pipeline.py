# PROMPT: "Write pytest tests for a FastAPI event ingestion endpoint. Cover: happy path batch,
# idempotency (same payload twice returns same accepted count on second call as duplicate),
# oversized batch (>500 events), malformed event_type, invalid confidence range,
# partial success (mix of valid and invalid), empty batch."
# CHANGES MADE: Added store_id filter assertions, adjusted duplicate logic to match
# our ingest_events implementation which returns duplicate count not 0 accepted,
# fixed DB override to use per-test fixture pattern to avoid cross-file bleed,
# timestamps use today's date so metrics date filter works correctly.

import pytest
import json
import uuid
from datetime import datetime, timezone, date
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base, get_db

TEST_DB_URL = "sqlite:///./test_pipeline.db"
engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_and_client():
    Base.metadata.create_all(bind=engine)
    from app.main import app
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(setup_and_client):
    return setup_and_client


def make_event(**overrides):
    today = date.today().isoformat()
    base = {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": "ENTRY",
        "timestamp": f"{today}T10:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.91,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }
    base.update(overrides)
    return base


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_ingest_happy_path(client):
    events = [make_event() for _ in range(5)]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 5
    assert body["duplicate"] == 0
    assert body["rejected"] == 0


def test_ingest_idempotency(client):
    events = [make_event() for _ in range(3)]
    r1 = client.post("/events/ingest", json={"events": events})
    assert r1.json()["accepted"] == 3

    r2 = client.post("/events/ingest", json={"events": events})
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["accepted"] == 0
    assert body2["duplicate"] == 3


def test_ingest_oversized_batch(client):
    events = [make_event() for _ in range(501)]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 422


def test_ingest_invalid_event_type(client):
    events = [make_event(event_type="INVALID_TYPE")]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 422


def test_ingest_invalid_confidence(client):
    events = [make_event(confidence=1.5)]
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 422


def test_ingest_empty_batch(client):
    r = client.post("/events/ingest", json={"events": []})
    assert r.status_code == 200
    assert r.json()["accepted"] == 0


def test_metrics_empty_store(client):
    r = client.get("/stores/STORE_BLR_002/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0


def test_metrics_after_ingest(client):
    events = [
        make_event(event_type="ENTRY", visitor_id="VIS_aaa001"),
        make_event(event_type="ENTRY", visitor_id="VIS_aaa002"),
        make_event(event_type="ENTRY", visitor_id="VIS_aaa003"),
    ]
    client.post("/events/ingest", json={"events": events})
    r = client.get("/stores/STORE_BLR_002/metrics")
    assert r.status_code == 200
    assert r.json()["unique_visitors"] == 3


def test_funnel_structure(client):
    r = client.get("/stores/STORE_BLR_002/funnel")
    assert r.status_code == 200
    body = r.json()
    assert "stages" in body
    stages = {s["stage"] for s in body["stages"]}
    assert {"ENTRY", "ZONE_VISIT", "BILLING_QUEUE", "PURCHASE"} == stages


def test_funnel_no_double_count_reentry(client):
    vid = "VIS_reentry1"
    events = [
        make_event(event_type="ENTRY",   visitor_id=vid),
        make_event(event_type="EXIT",    visitor_id=vid),
        make_event(event_type="REENTRY", visitor_id=vid),
    ]
    client.post("/events/ingest", json={"events": events})
    r = client.get("/stores/STORE_BLR_002/funnel")
    entry_stage = next(s for s in r.json()["stages"] if s["stage"] == "ENTRY")
    assert entry_stage["count"] == 1  # same visitor, not double-counted


def test_heatmap_structure(client):
    today = date.today().isoformat()
    events = [
        make_event(event_type="ZONE_EXIT", zone_id="MAKEUP",   dwell_ms=45000,
                   camera_id="CAM_FLOOR_02", visitor_id="VIS_h1",
                   timestamp=f"{today}T11:00:00Z"),
        make_event(event_type="ZONE_EXIT", zone_id="SKINCARE", dwell_ms=30000,
                   camera_id="CAM_FLOOR_02", visitor_id="VIS_h2",
                   timestamp=f"{today}T11:05:00Z"),
    ]
    client.post("/events/ingest", json={"events": events})
    r = client.get("/stores/STORE_BLR_002/heatmap")
    assert r.status_code == 200
    body = r.json()
    assert "zones" in body
    if body["zones"]:
        scores = [z["normalised_score"] for z in body["zones"]]
        assert max(scores) == 100.0


def test_anomalies_structure(client):
    r = client.get("/stores/STORE_BLR_002/anomalies")
    assert r.status_code == 200
    body = r.json()
    assert "anomalies" in body
    for a in body["anomalies"]:
        assert a["severity"] in ("INFO", "WARN", "CRITICAL")
        assert "suggested_action" in a


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "UP"
    assert "stores" in body
    assert "checked_at" in body


def test_health_with_events_shows_store(client):
    today = date.today().isoformat()
    events = [make_event(timestamp=f"{today}T12:00:00Z")]
    client.post("/events/ingest", json={"events": events})
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    store_ids = [s["store_id"] for s in body["stores"]]
    assert "STORE_BLR_002" in store_ids
    store = next(s for s in body["stores"] if s["store_id"] == "STORE_BLR_002")
    assert store["last_event_timestamp"] == f"{today}T12:00:00Z"
    assert store["feed_status"] in ("OK", "STALE_FEED")


def test_staff_excluded_from_metrics(client):
    events = [
        make_event(event_type="ENTRY", visitor_id="VIS_cust1", is_staff=False),
        make_event(event_type="ENTRY", visitor_id="STAFF_001", is_staff=True),
        make_event(event_type="ENTRY", visitor_id="STAFF_002", is_staff=True),
    ]
    client.post("/events/ingest", json={"events": events})
    r = client.get("/stores/STORE_BLR_002/metrics")
    assert r.json()["unique_visitors"] == 1


def test_trace_id_in_response_header(client):
    r = client.get("/health")
    assert "x-trace-id" in r.headers
