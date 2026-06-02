# PROMPT: "Write pytest tests for store metrics computation. Cover: zero-purchase store,
# all-staff clip (no customers), conversion rate calculation, zone dwell aggregation,
# abandonment rate, queue depth tracking."
# CHANGES MADE: Used direct DB inserts via EventRecord for precise control over test data.
# Adjusted date filter to use today's date dynamically since metrics filters on today.
# Fixed DB override to be per-test via fixture rather than module-level to avoid bleed.

import pytest
import uuid
from datetime import datetime, timezone, date
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base, get_db, EventRecord

TEST_DB_URL = "sqlite:///./test_metrics.db"
engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

TODAY = date.today().isoformat()
STORE = "STORE_BLR_002"


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


def insert_event(visitor_id, event_type, zone_id=None, dwell_ms=0,
                 is_staff=False, confidence=0.9, queue_depth=None, timestamp=None):
    db = TestSession()
    ts = timestamp or f"{TODAY}T12:00:00Z"
    db.add(EventRecord(
        event_id=str(uuid.uuid4()),
        store_id=STORE,
        camera_id="CAM_ENTRY_01",
        visitor_id=visitor_id,
        event_type=event_type,
        timestamp=ts,
        zone_id=zone_id,
        dwell_ms=dwell_ms,
        is_staff=is_staff,
        confidence=confidence,
        queue_depth=queue_depth,
        sku_zone=zone_id,
        session_seq=1,
        ingested_at=ts,
    ))
    db.commit()
    db.close()


def test_zero_purchase_store(client):
    insert_event("VIS_001", "ENTRY")
    insert_event("VIS_001", "EXIT")
    r = client.get(f"/stores/{STORE}/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 1
    assert body["conversion_rate"] == 0.0


def test_all_staff_clip(client):
    for i in range(5):
        insert_event(f"STAFF_{i:03d}", "ENTRY", is_staff=True)
    r = client.get(f"/stores/{STORE}/metrics")
    assert r.status_code == 200
    assert r.json()["unique_visitors"] == 0


def test_zone_dwell_aggregation(client):
    insert_event("VIS_002", "ENTRY")
    insert_event("VIS_002", "ZONE_EXIT", zone_id="MAKEUP",   dwell_ms=60000)
    insert_event("VIS_002", "ZONE_EXIT", zone_id="SKINCARE", dwell_ms=30000)
    insert_event("VIS_003", "ENTRY")
    insert_event("VIS_003", "ZONE_EXIT", zone_id="MAKEUP",   dwell_ms=120000)
    r = client.get(f"/stores/{STORE}/metrics")
    body = r.json()
    zone_map = {z["zone_id"]: z for z in body["zone_dwell"]}
    assert "MAKEUP" in zone_map
    assert zone_map["MAKEUP"]["avg_dwell_seconds"] == pytest.approx(90.0, abs=1.0)


def test_abandonment_rate(client):
    for i in range(4):
        insert_event(f"VIS_q{i}", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=2)
    for i in range(2):
        insert_event(f"VIS_q{i}", "BILLING_QUEUE_ABANDON", zone_id="BILLING_QUEUE")
    r = client.get(f"/stores/{STORE}/metrics")
    assert r.json()["abandonment_rate"] == pytest.approx(0.5, abs=0.01)


def test_queue_depth_current(client):
    insert_event("VIS_q1", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=3,
                 timestamp=f"{TODAY}T14:00:00Z")
    insert_event("VIS_q2", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=5,
                 timestamp=f"{TODAY}T14:05:00Z")
    r = client.get(f"/stores/{STORE}/metrics")
    assert r.json()["queue_depth_current"] == 5


def test_metrics_handles_empty_store_gracefully(client):
    r = client.get(f"/stores/STORE_NONEXISTENT/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
    assert body["abandonment_rate"] == 0.0
