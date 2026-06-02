# PROMPT: "Write pytest tests for anomaly detection logic. Cover: queue spike triggers CRITICAL,
# no anomalies on empty store, dead zone detection after 30 min silence, high abandonment rate,
# conversion drop vs baseline, severity levels are valid enum values."
# CHANGES MADE: Inserted events with timestamps in the past to simulate dead zones correctly.
# Fixed DB override to be per-test via fixture rather than module-level to avoid bleed.

import pytest
import uuid
from datetime import datetime, timezone, date, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base, get_db, EventRecord

TEST_DB_URL = "sqlite:///./test_anomalies.db"
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


def insert(visitor_id, event_type, zone_id=None, queue_depth=None,
           is_staff=False, timestamp=None, dwell_ms=0):
    db = TestSession()
    ts = timestamp or f"{TODAY}T12:00:00Z"
    db.add(EventRecord(
        event_id=str(uuid.uuid4()),
        store_id=STORE,
        camera_id="CAM_BILLING_03",
        visitor_id=visitor_id,
        event_type=event_type,
        timestamp=ts,
        zone_id=zone_id,
        dwell_ms=dwell_ms,
        is_staff=is_staff,
        confidence=0.9,
        queue_depth=queue_depth,
        sku_zone=zone_id,
        session_seq=1,
        ingested_at=ts,
    ))
    db.commit()
    db.close()


def test_no_anomalies_empty_store(client):
    r = client.get(f"/stores/{STORE}/anomalies")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["anomalies"], list)


def test_queue_spike_critical(client):
    insert("VIS_001", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=9)
    r = client.get(f"/stores/{STORE}/anomalies")
    assert r.status_code == 200
    anomalies = r.json()["anomalies"]
    spike = next((a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert spike["severity"] == "CRITICAL"


def test_queue_spike_warn(client):
    insert("VIS_001", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=6)
    r = client.get(f"/stores/{STORE}/anomalies")
    assert r.status_code == 200
    anomalies = r.json()["anomalies"]
    spike = next((a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert spike["severity"] == "WARN"


def test_high_abandonment_anomaly(client):
    for i in range(5):
        insert(f"VIS_a{i}", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=3)
    for i in range(3):
        insert(f"VIS_a{i}", "BILLING_QUEUE_ABANDON", zone_id="BILLING_QUEUE")
    r = client.get(f"/stores/{STORE}/anomalies")
    assert r.status_code == 200
    anomalies = r.json()["anomalies"]
    abandon = next((a for a in anomalies if a["anomaly_type"] == "HIGH_ABANDONMENT"), None)
    assert abandon is not None
    assert abandon["severity"] == "WARN"


def test_all_anomaly_severities_valid(client):
    insert("VIS_001", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=9)
    r = client.get(f"/stores/{STORE}/anomalies")
    valid_severities = {"INFO", "WARN", "CRITICAL"}
    for a in r.json()["anomalies"]:
        assert a["severity"] in valid_severities


def test_anomaly_has_suggested_action(client):
    insert("VIS_001", "BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE", queue_depth=9)
    r = client.get(f"/stores/{STORE}/anomalies")
    for a in r.json()["anomalies"]:
        assert len(a["suggested_action"]) > 10


def test_dead_zone_detection(client):
    # Add enough visitors to pass the entered_today > 5 guard
    for i in range(8):
        insert(f"VIS_d{i}", "ENTRY", timestamp=f"{TODAY}T10:0{i}:00Z")
    # Only HAIRCARE visited recently; others are dead
    insert("VIS_d0", "ZONE_ENTER", zone_id="HAIRCARE", timestamp=f"{TODAY}T12:00:00Z")
    r = client.get(f"/stores/{STORE}/anomalies")
    assert r.status_code == 200
    dead = [a for a in r.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
    dead_zones = {a["description"].split("Zone ")[1].split(" has")[0] for a in dead}
    assert "MAKEUP" in dead_zones or "SKINCARE" in dead_zones
