"""
ingestion.py — Ingest, validate, deduplicate events. Idempotent by event_id.
"""
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from .database import EventRecord
from .models import Event

logger = logging.getLogger("store_intelligence")


def ingest_events(events: list[Event], db: Session) -> dict:
    accepted = 0
    duplicate = 0
    rejected = 0
    errors = []

    # Fetch existing event_ids in one query for dedup
    ids = [e.event_id for e in events]
    existing = {
        r.event_id
        for r in db.query(EventRecord.event_id).filter(EventRecord.event_id.in_(ids)).all()
    }

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    records = []
    for e in events:
        if e.event_id in existing:
            duplicate += 1
            continue
        try:
            records.append(EventRecord(
                event_id=e.event_id,
                store_id=e.store_id,
                camera_id=e.camera_id,
                visitor_id=e.visitor_id,
                event_type=e.event_type,
                timestamp=e.timestamp,
                zone_id=e.zone_id,
                dwell_ms=e.dwell_ms,
                is_staff=e.is_staff,
                confidence=e.confidence,
                queue_depth=e.metadata.queue_depth,
                sku_zone=e.metadata.sku_zone,
                session_seq=e.metadata.session_seq,
                ingested_at=now_str,
            ))
            existing.add(e.event_id)  # prevent intra-batch dupes
            accepted += 1
        except Exception as ex:
            rejected += 1
            errors.append({"event_id": e.event_id, "error": str(ex)})

    if records:
        try:
            db.bulk_save_objects(records)
            db.commit()
        except SQLAlchemyError as ex:
            db.rollback()
            logger.error(f"DB commit failed: {ex}")
            raise

    return {"accepted": accepted, "duplicate": duplicate, "rejected": rejected, "errors": errors}
