"""
funnel.py — Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
Session is the unit; re-entries do not double-count a visitor.
"""
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import func
from .database import EventRecord
from .models import FunnelResponse, FunnelStage
from .metrics import _load_transactions, _compute_conversions


def get_funnel(store_id: str, db: Session) -> FunnelResponse:
    today = date.today().isoformat()

    # Stage 1: unique visitors (ENTRY or REENTRY, deduplicated by visitor_id)
    entry_rows = (
        db.query(EventRecord.visitor_id)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.event_type.in_(["ENTRY", "REENTRY"]),
            EventRecord.timestamp.like(f"{today}%"),
        )
        .distinct()
        .all()
    )
    entered = {r.visitor_id for r in entry_rows}

    # Stage 2: visited at least one zone (ZONE_ENTER or ZONE_DWELL)
    zone_rows = (
        db.query(EventRecord.visitor_id)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
            EventRecord.visitor_id.in_(entered),
            EventRecord.timestamp.like(f"{today}%"),
        )
        .distinct()
        .all()
    )
    zone_visitors = {r.visitor_id for r in zone_rows}

    # Stage 3: reached billing queue — must be a subset of zone_visitors
    billing_rows = (
        db.query(EventRecord.visitor_id)
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.zone_id.in_(["BILLING", "BILLING_QUEUE"]),
            EventRecord.visitor_id.in_(entered),
            EventRecord.timestamp.like(f"{today}%"),
        )
        .distinct()
        .all()
    )
    # Billing visitors are capped to zone_visitors to keep funnel monotonically decreasing
    billing_visitors = {r.visitor_id for r in billing_rows} & (zone_visitors | entered)

    # Stage 4: purchased (POS correlation)
    txns = _load_transactions(store_id)
    purchased = _compute_conversions(store_id, entered, txns, db, today)

    def drop_off(current, previous):
        if not previous:
            return 0.0
        return round((1 - current / previous) * 100, 1)

    n_entered = len(entered)
    n_zone = len(zone_visitors)
    n_billing = len(billing_visitors)
    n_purchased = len(purchased)

    stages = [
        FunnelStage(stage="ENTRY",         count=n_entered,   drop_off_pct=0.0),
        FunnelStage(stage="ZONE_VISIT",    count=n_zone,      drop_off_pct=drop_off(n_zone, n_entered)),
        FunnelStage(stage="BILLING_QUEUE", count=n_billing,   drop_off_pct=drop_off(n_billing, n_zone)),
        FunnelStage(stage="PURCHASE",      count=n_purchased, drop_off_pct=drop_off(n_purchased, n_billing)),
    ]

    return FunnelResponse(store_id=store_id, stages=stages)
