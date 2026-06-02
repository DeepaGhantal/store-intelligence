"""
health.py — Service health endpoint with STALE_FEED detection.
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func
from .database import EventRecord
from .models import HealthResponse, StoreHealth

STALE_THRESHOLD_SECONDS = 600  # 10 minutes


def get_health(db: Session) -> HealthResponse:
    now = datetime.now(timezone.utc)

    # Get last event timestamp per store
    rows = (
        db.query(EventRecord.store_id, func.max(EventRecord.timestamp))
        .group_by(EventRecord.store_id)
        .all()
    )

    store_healths = []
    for store_id, last_ts in rows:
        if last_ts:
            last_dt = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            lag = (now - last_dt).total_seconds()
            # Only flag STALE_FEED if lag is positive (event is in the past)
            feed_status = "STALE_FEED" if lag > STALE_THRESHOLD_SECONDS else "OK"
            # Negative lag means events are timestamped in the future (clock skew) — treat as OK
            if lag < 0:
                lag = 0.0
                feed_status = "OK"
        else:
            lag = None
            feed_status = "STALE_FEED"

        store_healths.append(StoreHealth(
            store_id=store_id,
            status="UP",
            last_event_timestamp=last_ts,
            lag_seconds=round(lag, 1) if lag is not None else None,
            feed_status=feed_status,
        ))

    if not store_healths:
        store_healths.append(StoreHealth(
            store_id="N/A",
            status="NO_DATA",
            last_event_timestamp=None,
            lag_seconds=None,
            feed_status="STALE_FEED",
        ))

    return HealthResponse(
        service="store-intelligence-api",
        status="UP",
        stores=store_healths,
        checked_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
