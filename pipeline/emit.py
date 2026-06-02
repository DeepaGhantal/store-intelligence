"""
emit.py — Event schema construction and emission to JSONL file / API.
"""
import uuid
import json
from datetime import datetime, timezone
from typing import Optional


EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
}


def make_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 1.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 0,
) -> dict:
    assert event_type in EVENT_TYPES, f"Unknown event type: {event_type}"
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(confidence, 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


class EventEmitter:
    def __init__(self, output_path: str):
        self.output_path = output_path
        self._fh = open(output_path, "w", encoding="utf-8")

    def emit(self, event: dict):
        self._fh.write(json.dumps(event) + "\n")
        self._fh.flush()

    def close(self):
        self._fh.close()
