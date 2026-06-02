"""
heatmap.py — Zone visit frequency + avg dwell, normalised 0-100.
"""
from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import func
from .database import EventRecord
from .models import HeatmapResponse, HeatmapZone

MIN_SESSIONS_FOR_HIGH_CONFIDENCE = 20


def get_heatmap(store_id: str, db: Session) -> HeatmapResponse:
    today = date.today().isoformat()

    rows = (
        db.query(
            EventRecord.zone_id,
            func.count(func.distinct(EventRecord.visitor_id)).label("visit_count"),
            func.avg(EventRecord.dwell_ms).label("avg_dwell_ms"),
        )
        .filter(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
            EventRecord.zone_id.isnot(None),
            EventRecord.event_type.in_(["ZONE_EXIT", "ZONE_DWELL"]),
            EventRecord.timestamp.like(f"{today}%"),
        )
        .group_by(EventRecord.zone_id)
        .all()
    )

    if not rows:
        return HeatmapResponse(store_id=store_id, zones=[])

    max_visits = max(r.visit_count for r in rows) or 1

    zones = []
    for r in rows:
        normalised = round((r.visit_count / max_visits) * 100, 1)
        avg_dwell_s = round((r.avg_dwell_ms or 0) / 1000, 1)
        confidence = "HIGH" if r.visit_count >= MIN_SESSIONS_FOR_HIGH_CONFIDENCE else "LOW"
        zones.append(HeatmapZone(
            zone_id=r.zone_id,
            visit_frequency=r.visit_count,
            avg_dwell_seconds=avg_dwell_s,
            normalised_score=normalised,
            data_confidence=confidence,
        ))

    zones.sort(key=lambda z: z.normalised_score, reverse=True)
    return HeatmapResponse(store_id=store_id, zones=zones)
