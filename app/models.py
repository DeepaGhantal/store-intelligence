"""
models.py — Pydantic schemas for events and API responses.
"""
from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, List, Any
from datetime import datetime
import uuid


VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
}


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class Event(BaseModel):
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float
    metadata: EventMetadata = EventMetadata()

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v):
        if v not in VALID_EVENT_TYPES:
            raise ValueError(f"Invalid event_type: {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v):
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be 0.0–1.0")
        return v

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError("timestamp must be ISO-8601 UTC: YYYY-MM-DDTHH:MM:SSZ")
        return v


class IngestRequest(BaseModel):
    events: List[Event]

    @model_validator(mode="after")
    def check_batch_size(self):
        if len(self.events) > 500:
            raise ValueError("Batch size exceeds 500 events")
        return self


class IngestResponse(BaseModel):
    accepted: int
    duplicate: int
    rejected: int
    errors: List[dict] = []


class ZoneDwell(BaseModel):
    zone_id: str
    avg_dwell_seconds: float
    visit_count: int


class StoreMetrics(BaseModel):
    store_id: str
    date: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_seconds: float
    zone_dwell: List[ZoneDwell]
    queue_depth_current: int
    abandonment_rate: float
    total_transactions: int


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages: List[FunnelStage]


class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_seconds: float
    normalised_score: float
    data_confidence: str  # HIGH / LOW


class HeatmapResponse(BaseModel):
    store_id: str
    zones: List[HeatmapZone]


class Anomaly(BaseModel):
    anomaly_id: str
    anomaly_type: str
    severity: str  # INFO / WARN / CRITICAL
    description: str
    suggested_action: str
    detected_at: str


class AnomalyResponse(BaseModel):
    store_id: str
    anomalies: List[Anomaly]


class StoreHealth(BaseModel):
    store_id: str
    status: str
    last_event_timestamp: Optional[str]
    lag_seconds: Optional[float]
    feed_status: str  # OK / STALE_FEED


class HealthResponse(BaseModel):
    service: str
    status: str
    stores: List[StoreHealth]
    checked_at: str
