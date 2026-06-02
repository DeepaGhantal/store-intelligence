"""
metrics.py — Real-time store metrics computation.
"""
from datetime import datetime, timezone, date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct
from .database import EventRecord
from .models import StoreMetrics, ZoneDwell
import csv, os
from pathlib import Path


POS_CSV = Path(__file__).parent.parent / "data" / "pos_transactions.csv"


def _load_transactions(store_id: str) -> list[dict]:
    if not POS_CSV.exists():
        return []
    today = date.today()
    txns = []
    with open(POS_CSV) as f:
        for row in csv.DictReader(f):
            if row["store_id"] == store_id:
                orig_dt = datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                delta = today - orig_dt.date()
                new_ts = (orig_dt + timedelta(days=delta.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                txns.append({
                    "transaction_id": row["transaction_id"],
                    "timestamp": new_ts,
                    "basket_value_inr": float(row["basket_value_inr"]),
                })
    return txns


def get_store_metrics(store_id: str, db: Session) -> StoreMetrics:
    today = date.today().isoformat()

    # All customer events for this store today
    base = (
        db.query(EventRecord)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.timestamp.like(f"{today}%"),
        )
    )

    # Unique visitors (by ENTRY events, deduplicated by visitor_id)
    entry_visitors = {
        r.visitor_id
        for r in base.filter(EventRecord.event_type.in_(["ENTRY", "REENTRY"])).all()
    }
    unique_visitors = len(entry_visitors)

    # Conversion: visitors in billing zone within 5 min before a POS txn
    txns = _load_transactions(store_id)
    converted_visitors = _compute_conversions(store_id, entry_visitors, txns, db, today)
    conversion_rate = (len(converted_visitors) / unique_visitors) if unique_visitors > 0 else 0.0

    # Avg dwell per zone
    zone_rows = (
        db.query(EventRecord.zone_id, func.avg(EventRecord.dwell_ms), func.count(EventRecord.event_id))
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.event_type.in_(["ZONE_EXIT", "ZONE_DWELL"]),
            EventRecord.zone_id.isnot(None),
            EventRecord.timestamp.like(f"{today}%"),
        )
        .group_by(EventRecord.zone_id)
        .all()
    )
    zone_dwell = [
        ZoneDwell(zone_id=z, avg_dwell_seconds=round((avg_ms or 0) / 1000, 1), visit_count=cnt)
        for z, avg_ms, cnt in zone_rows
    ]

    # Overall avg dwell (from ZONE_EXIT events)
    all_dwell = db.query(func.avg(EventRecord.dwell_ms)).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
        EventRecord.event_type == "ZONE_EXIT",
        EventRecord.timestamp.like(f"{today}%"),
    ).scalar()
    avg_dwell = round((all_dwell or 0) / 1000, 1)

    # Current queue depth (latest BILLING_QUEUE_JOIN queue_depth value)
    latest_queue = (
        db.query(EventRecord.queue_depth)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
            EventRecord.timestamp.like(f"{today}%"),
        )
        .order_by(EventRecord.timestamp.desc())
        .first()
    )
    queue_depth_current = (latest_queue[0] or 0) if latest_queue else 0

    # Abandonment rate
    abandons = db.query(func.count(EventRecord.event_id)).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
        EventRecord.event_type == "BILLING_QUEUE_ABANDON",
        EventRecord.timestamp.like(f"{today}%"),
    ).scalar() or 0

    queue_joins = db.query(func.count(EventRecord.event_id)).filter(
        EventRecord.store_id == store_id,
        EventRecord.is_staff == False,
        EventRecord.event_type == "BILLING_QUEUE_JOIN",
        EventRecord.timestamp.like(f"{today}%"),
    ).scalar() or 0

    abandonment_rate = (abandons / queue_joins) if queue_joins > 0 else 0.0

    return StoreMetrics(
        store_id=store_id,
        date=today,
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_seconds=avg_dwell,
        zone_dwell=zone_dwell,
        queue_depth_current=queue_depth_current,
        abandonment_rate=round(abandonment_rate, 4),
        total_transactions=len(txns),
    )


def _compute_conversions(store_id, entry_visitors, txns, db, today):
    """
    A visitor counts as converted if they were in BILLING zone
    within 5 minutes before a POS transaction timestamp.
    """
    converted = set()
    billing_events = (
        db.query(EventRecord.visitor_id, EventRecord.timestamp)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.zone_id.in_(["BILLING", "BILLING_QUEUE"]),
            EventRecord.timestamp.like(f"{today}%"),
        )
        .all()
    )

    # Build map: visitor_id → list of billing timestamps
    billing_map: dict[str, list[datetime]] = {}
    for vid, ts_str in billing_events:
        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        billing_map.setdefault(vid, []).append(dt)

    for txn in txns:
        txn_dt = datetime.strptime(txn["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        for vid, billing_times in billing_map.items():
            if vid in converted:
                continue
            for bt in billing_times:
                diff = (txn_dt - bt).total_seconds()
                if 0 <= diff <= 300:  # within 5 min before txn
                    converted.add(vid)
                    break

    # Conversion cannot exceed number of transactions
    if len(converted) > len(txns):
        converted = set(list(converted)[:len(txns)])
    return converted
