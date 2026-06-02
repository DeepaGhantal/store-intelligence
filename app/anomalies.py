"""
anomalies.py — Detect operational anomalies: queue spike, conversion drop, dead zones.
"""
import uuid
import json
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import func
from .database import EventRecord
from .models import AnomalyResponse, Anomaly


QUEUE_SPIKE_THRESHOLD = 5       # queue depth > 5 = CRITICAL
CONVERSION_DROP_THRESHOLD = 0.3 # 30% drop vs 7-day avg
DEAD_ZONE_MINUTES = 30          # no visits in 30 min

_LAYOUT_PATH = Path(__file__).parent.parent / "data" / "store_layout.json"


def _get_floor_zones(store_id: str) -> list[str]:
    """Load floor zone IDs from store_layout.json, fallback to defaults."""
    try:
        layout = json.loads(_LAYOUT_PATH.read_text())
        store = next((s for s in layout["stores"] if s["store_id"] == store_id), None)
        if store:
            return [
                z["zone_id"] for z in store["zones"]
                if z["zone_id"] not in ("ENTRY", "BILLING", "BILLING_QUEUE")
            ]
    except Exception:
        pass
    return ["SKINCARE", "MAKEUP", "HAIRCARE", "PERSONAL_CARE"]


def get_anomalies(store_id: str, db: Session) -> AnomalyResponse:
    now = datetime.now(timezone.utc)
    today = date.today().isoformat()
    anomalies = []

    # ── 1. Queue spike ────────────────────────────────────────────────────
    latest_queue = (
        db.query(EventRecord.queue_depth, EventRecord.timestamp)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
            EventRecord.timestamp.like(f"{today}%"),
        )
        .order_by(EventRecord.timestamp.desc())
        .first()
    )
    if latest_queue and latest_queue[0] and latest_queue[0] >= QUEUE_SPIKE_THRESHOLD:
        severity = "CRITICAL" if latest_queue[0] >= 8 else "WARN"
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity=severity,
            description=f"Billing queue depth is {latest_queue[0]} (threshold: {QUEUE_SPIKE_THRESHOLD})",
            suggested_action="Open additional billing counter or redirect customers to self-checkout.",
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))

    # ── 2. Conversion drop vs rolling average ─────────────────────────────
    # Today's conversion rate
    entered_today = db.query(func.count(func.distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
        EventRecord.event_type.in_(["ENTRY", "REENTRY"]),
        EventRecord.timestamp.like(f"{today}%"),
    ).scalar() or 0

    billing_today = db.query(func.count(func.distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
        EventRecord.zone_id.in_(["BILLING", "BILLING_QUEUE"]),
        EventRecord.timestamp.like(f"{today}%"),
    ).scalar() or 0

    conv_today = (billing_today / entered_today) if entered_today > 0 else 0.0

    # 7-day rolling: use all historical data as proxy
    all_entered = db.query(func.count(func.distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
        EventRecord.event_type.in_(["ENTRY", "REENTRY"]),
    ).scalar() or 0

    all_billing = db.query(func.count(func.distinct(EventRecord.visitor_id))).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
        EventRecord.zone_id.in_(["BILLING", "BILLING_QUEUE"]),
    ).scalar() or 0

    conv_avg = (all_billing / all_entered) if all_entered > 0 else 0.0

    if conv_avg > 0 and conv_today < conv_avg * (1 - CONVERSION_DROP_THRESHOLD):
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type="CONVERSION_DROP",
            severity="WARN",
            description=(
                f"Today's conversion rate {conv_today:.1%} is "
                f"{((conv_avg - conv_today)/conv_avg*100):.0f}% below 7-day avg {conv_avg:.1%}"
            ),
            suggested_action="Check for staff shortages, product stockouts, or pricing issues.",
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))

    # ── 3. Dead zones (no visits in last 30 min) ──────────────────────────
    cutoff = (now - timedelta(minutes=DEAD_ZONE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_zones = _get_floor_zones(store_id)

    active_zones = {
        r.zone_id
        for r in db.query(EventRecord.zone_id)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.zone_id.in_(all_zones),
            EventRecord.timestamp >= cutoff,
            EventRecord.timestamp.like(f"{today}%"),
        )
        .distinct()
        .all()
    }

    # Only flag dead zones if we have some traffic today (avoid false alarms on empty store)
    if entered_today > 5:
        for zone in all_zones:
            if zone not in active_zones:
                anomalies.append(Anomaly(
                    anomaly_id=str(uuid.uuid4()),
                    anomaly_type="DEAD_ZONE",
                    severity="INFO",
                    description=f"Zone {zone} has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
                    suggested_action=f"Check {zone} zone display, lighting, or staff presence.",
                    detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ))

    # ── 4. High abandonment rate ──────────────────────────────────────────
    abandons = db.query(func.count(EventRecord.event_id)).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "BILLING_QUEUE_ABANDON",
        EventRecord.timestamp.like(f"{today}%"),
    ).scalar() or 0

    queue_joins = db.query(func.count(EventRecord.event_id)).filter(
        EventRecord.store_id == store_id,
        EventRecord.event_type == "BILLING_QUEUE_JOIN",
        EventRecord.timestamp.like(f"{today}%"),
    ).scalar() or 0

    if queue_joins >= 3 and abandons / queue_joins > 0.4:
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type="HIGH_ABANDONMENT",
            severity="WARN",
            description=f"Queue abandonment rate is {abandons/queue_joins:.0%} ({abandons}/{queue_joins} joins).",
            suggested_action="Reduce billing wait time. Consider mobile checkout or express lane.",
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))

    return AnomalyResponse(store_id=store_id, anomalies=anomalies)
