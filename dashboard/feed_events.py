"""
feed_events.py — Simulated real-time replay of structured events into the API.

If data/events.jsonl is empty, this script synthesizes a replay stream from
data/pos_transactions.csv so the dashboard can still be demonstrated.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

DEFAULT_API_URL = os.getenv("STORE_INTELLIGENCE_API_URL", "http://127.0.0.1:8000")
DEFAULT_EVENTS_PATH = os.getenv(
    "STORE_INTELLIGENCE_EVENTS_PATH",
    str(Path(__file__).resolve().parents[1] / "data" / "events.jsonl"),
)
DEFAULT_SPEED = float(os.getenv("STORE_INTELLIGENCE_FEED_SPEED", "60"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay event stream into the Store Intelligence API")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base API URL")
    parser.add_argument("--events", default=DEFAULT_EVENTS_PATH, help="Path to events.jsonl")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="Replay speed multiplier")
    return parser


def load_events(events_path: Path) -> list[dict[str, Any]]:
    if events_path.exists():
        events = _read_jsonl(events_path)
        if events:
            return events

    return build_seed_events_from_pos()


def _read_jsonl(events_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    events.sort(key=lambda event: event.get("timestamp", ""))
    if not events:
        return events
    # Shift all timestamps to today, preserving relative time-of-day offsets
    first_date = events[0]["timestamp"][:10]  # YYYY-MM-DD of earliest event
    today_str = date.today().isoformat()
    if first_date != today_str:
        delta_days = (date.today() - date.fromisoformat(first_date)).days
        shifted = []
        for e in events:
            e = dict(e)  # shallow copy — don't mutate the original
            orig_dt = datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
            new_dt = orig_dt + timedelta(days=delta_days)
            e["timestamp"] = new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            e["event_id"] = str(uuid.uuid4())  # new id so re-runs aren't duplicates
            shifted.append(e)
        return shifted
    return events


def _load_pos_transactions(pos_path: Path) -> list[dict[str, Any]]:
    if not pos_path.exists():
        return []

    transactions: list[dict[str, Any]] = []
    with pos_path.open("r", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("store_id") != "STORE_BLR_002":
                continue
            transactions.append(row)

    transactions.sort(key=lambda row: row.get("timestamp", ""))
    return transactions


def _make_event(
    *,
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.93,
    queue_depth: int | None = None,
    sku_zone: str | None = None,
    session_seq: int = 0,
) -> dict[str, Any]:
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


def build_seed_events_from_pos() -> list[dict[str, Any]]:
    repo_root = Path(__file__).resolve().parents[1]
    pos_path = repo_root / "data" / "pos_transactions.csv"
    transactions = _load_pos_transactions(pos_path)
    if not transactions:
        print(f"[INFO] No events to replay and no POS transactions found at {pos_path}")
        return []

    seed_events: list[dict[str, Any]] = []
    zone_cycle = ["SKINCARE", "MAKEUP", "HAIRCARE", "PERSONAL_CARE"]
    today = date.today()

    for index, txn in enumerate(transactions):
        txn_dt = datetime.strptime(txn["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        today_txn_dt = datetime.combine(today, txn_dt.time(), tzinfo=timezone.utc)
        visitor_id = f"VIS_{txn['transaction_id'][-5:]}"
        zone_id = zone_cycle[index % len(zone_cycle)]
        queue_depth = (index % 4) + 1

        seed_events.extend(
            [
                _make_event(
                    store_id=txn["store_id"],
                    camera_id="CAM_ENTRY_01",
                    visitor_id=visitor_id,
                    event_type="ENTRY",
                    timestamp=today_txn_dt - timedelta(minutes=7),
                    confidence=0.94,
                    session_seq=1,
                ),
                _make_event(
                    store_id=txn["store_id"],
                    camera_id="CAM_FLOOR_02",
                    visitor_id=visitor_id,
                    event_type="ZONE_ENTER",
                    timestamp=today_txn_dt - timedelta(minutes=6, seconds=15),
                    zone_id=zone_id,
                    confidence=0.92,
                    sku_zone=zone_id,
                    session_seq=2,
                ),
                _make_event(
                    store_id=txn["store_id"],
                    camera_id="CAM_FLOOR_02",
                    visitor_id=visitor_id,
                    event_type="ZONE_EXIT",
                    timestamp=today_txn_dt - timedelta(minutes=3),
                    zone_id=zone_id,
                    dwell_ms=195000,
                    confidence=0.91,
                    sku_zone=zone_id,
                    session_seq=3,
                ),
                _make_event(
                    store_id=txn["store_id"],
                    camera_id="CAM_BILLING_03",
                    visitor_id=visitor_id,
                    event_type="BILLING_QUEUE_JOIN",
                    timestamp=today_txn_dt - timedelta(minutes=2),
                    zone_id="BILLING_QUEUE",
                    confidence=0.95,
                    queue_depth=queue_depth,
                    sku_zone="BILLING",
                    session_seq=4,
                ),
                _make_event(
                    store_id=txn["store_id"],
                    camera_id="CAM_BILLING_03",
                    visitor_id=visitor_id,
                    event_type="ZONE_EXIT",
                    timestamp=today_txn_dt - timedelta(seconds=30),
                    zone_id="BILLING_QUEUE",
                    dwell_ms=90000,
                    confidence=0.90,
                    sku_zone="BILLING",
                    session_seq=5,
                ),
                _make_event(
                    store_id=txn["store_id"],
                    camera_id="CAM_ENTRY_01",
                    visitor_id=visitor_id,
                    event_type="EXIT",
                    timestamp=today_txn_dt + timedelta(seconds=30),
                    confidence=0.91,
                    session_seq=6,
                ),
            ]
        )

        # Every 4th visitor browses only — no billing
        if index % 4 == 3:
            browse_id = f"VIS_BROWSE_{index:03d}"
            browse_zone = zone_cycle[(index + 2) % len(zone_cycle)]
            seed_events.extend([
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_ENTRY_01",
                    visitor_id=browse_id, event_type="ENTRY",
                    timestamp=today_txn_dt - timedelta(minutes=10),
                    confidence=0.89, session_seq=1,
                ),
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_FLOOR_02",
                    visitor_id=browse_id, event_type="ZONE_ENTER",
                    timestamp=today_txn_dt - timedelta(minutes=9),
                    zone_id=browse_zone, confidence=0.87, sku_zone=browse_zone, session_seq=2,
                ),
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_FLOOR_02",
                    visitor_id=browse_id, event_type="ZONE_EXIT",
                    timestamp=today_txn_dt - timedelta(minutes=6),
                    zone_id=browse_zone, dwell_ms=180000, confidence=0.86,
                    sku_zone=browse_zone, session_seq=3,
                ),
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_ENTRY_01",
                    visitor_id=browse_id, event_type="EXIT",
                    timestamp=today_txn_dt - timedelta(minutes=5),
                    confidence=0.88, session_seq=4,
                ),
            ])

        # Every 7th visitor abandons the queue
        if index % 7 == 6:
            abandon_id = f"VIS_ABANDON_{index:03d}"
            seed_events.extend([
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_ENTRY_01",
                    visitor_id=abandon_id, event_type="ENTRY",
                    timestamp=today_txn_dt - timedelta(minutes=12),
                    confidence=0.91, session_seq=1,
                ),
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_FLOOR_02",
                    visitor_id=abandon_id, event_type="ZONE_ENTER",
                    timestamp=today_txn_dt - timedelta(minutes=11),
                    zone_id=zone_id, confidence=0.88, sku_zone=zone_id, session_seq=2,
                ),
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_FLOOR_02",
                    visitor_id=abandon_id, event_type="ZONE_EXIT",
                    timestamp=today_txn_dt - timedelta(minutes=8),
                    zone_id=zone_id, dwell_ms=180000, confidence=0.87,
                    sku_zone=zone_id, session_seq=3,
                ),
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_BILLING_03",
                    visitor_id=abandon_id, event_type="BILLING_QUEUE_JOIN",
                    timestamp=today_txn_dt - timedelta(minutes=5),
                    zone_id="BILLING_QUEUE", confidence=0.93,
                    queue_depth=queue_depth + 1, sku_zone="BILLING", session_seq=4,
                ),
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_BILLING_03",
                    visitor_id=abandon_id, event_type="BILLING_QUEUE_ABANDON",
                    timestamp=today_txn_dt - timedelta(minutes=3),
                    zone_id="BILLING_QUEUE", dwell_ms=120000, confidence=0.85, session_seq=5,
                ),
                _make_event(
                    store_id=txn["store_id"], camera_id="CAM_ENTRY_01",
                    visitor_id=abandon_id, event_type="EXIT",
                    timestamp=today_txn_dt - timedelta(minutes=2, seconds=30),
                    confidence=0.87, session_seq=6,
                ),
            ])

        # Staff event every 8 transactions
        if index % 8 == 0:
            seed_events.append(
                _make_event(
                    store_id=txn["store_id"],
                    camera_id="CAM_FLOOR_02",
                    visitor_id=f"VIS_STAFF_{index:02d}",
                    event_type="ZONE_ENTER",
                    timestamp=today_txn_dt - timedelta(minutes=5, seconds=30),
                    zone_id="SKINCARE",
                    is_staff=True,
                    confidence=0.72,
                    sku_zone="SKINCARE",
                    session_seq=1,
                )
            )

    seed_events.sort(key=lambda event: event["timestamp"])
    return seed_events


def post_batch(api_url: str, batch: list[dict[str, Any]]) -> dict[str, Any]:
    payload = json.dumps({"events": batch}).encode("utf-8")
    req = request.Request(
        f"{api_url.rstrip('/')}/events/ingest",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"POST /events/ingest failed with HTTP {exc.code}: {body}") from exc

    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("Expected JSON object from /events/ingest")
    return result


def replay_events(api_url: str, events: list[dict[str, Any]], speed: float) -> None:
    if not events:
        print("[INFO] No events to replay")
        return

    speed = max(speed, 1.0)
    previous_dt: datetime | None = None

    for event in events:
        timestamp = event.get("timestamp")
        current_dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ") if timestamp else None

        if previous_dt is not None and current_dt is not None:
            delta_seconds = max((current_dt - previous_dt).total_seconds(), 0.0)
            time.sleep(delta_seconds / speed)

        result = post_batch(api_url, [event])
        print(
            "[INGEST]",
            event.get("event_type"),
            event.get("visitor_id"),
            event.get("timestamp"),
            result,
        )

        if current_dt is not None:
            previous_dt = current_dt


def main() -> None:
    args = build_parser().parse_args()
    events_path = Path(args.events)
    events = load_events(events_path)
    replay_events(args.api_url, events, args.speed)


if __name__ == "__main__":
    main()
