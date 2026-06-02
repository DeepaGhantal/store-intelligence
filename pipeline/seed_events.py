"""
seed_events.py — Generate synthetic events from POS data for demo/testing (no clips needed).

Produces data/events.jsonl from data/pos_transactions.csv so the API
can be demonstrated without running the full detection pipeline.
"""
import csv
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STORE_ID = "STORE_BLR_002"
ZONE_CYCLE = ["SKINCARE", "MAKEUP", "HAIRCARE", "PERSONAL_CARE"]
ROOT = Path(__file__).resolve().parents[1]
POS_PATH = ROOT / "data" / "pos_transactions.csv"
OUTPUT_PATH = ROOT / "data" / "events.jsonl"


def make_event(*, store_id, camera_id, visitor_id, event_type, timestamp,
               zone_id=None, dwell_ms=0, is_staff=False, confidence=0.93,
               queue_depth=None, sku_zone=None, session_seq=0):
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
        "metadata": {"queue_depth": queue_depth, "sku_zone": sku_zone, "session_seq": session_seq},
    }


def main():
    if not POS_PATH.exists():
        print(f"[ERROR] {POS_PATH} not found")
        return

    today = date.today()
    events = []

    with open(POS_PATH) as f:
        transactions = [r for r in csv.DictReader(f) if r["store_id"] == STORE_ID]

    for i, txn in enumerate(transactions):
        orig_dt = datetime.strptime(txn["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        txn_dt = datetime.combine(today, orig_dt.time(), tzinfo=timezone.utc)
        vid = f"VIS_{txn['transaction_id'][-5:]}"
        zone = ZONE_CYCLE[i % len(ZONE_CYCLE)]
        q = (i % 4) + 1

        events += [
            make_event(store_id=STORE_ID, camera_id="CAM_ENTRY_01", visitor_id=vid,
                       event_type="ENTRY", timestamp=txn_dt - timedelta(minutes=7),
                       confidence=0.94, session_seq=1),
            make_event(store_id=STORE_ID, camera_id="CAM_FLOOR_02", visitor_id=vid,
                       event_type="ZONE_ENTER", timestamp=txn_dt - timedelta(minutes=6),
                       zone_id=zone, confidence=0.92, sku_zone=zone, session_seq=2),
            make_event(store_id=STORE_ID, camera_id="CAM_FLOOR_02", visitor_id=vid,
                       event_type="ZONE_EXIT", timestamp=txn_dt - timedelta(minutes=3),
                       zone_id=zone, dwell_ms=180000, confidence=0.91, sku_zone=zone, session_seq=3),
            make_event(store_id=STORE_ID, camera_id="CAM_BILLING_03", visitor_id=vid,
                       event_type="BILLING_QUEUE_JOIN", timestamp=txn_dt - timedelta(minutes=2),
                       zone_id="BILLING_QUEUE", confidence=0.95, queue_depth=q,
                       sku_zone="BILLING", session_seq=4),
            make_event(store_id=STORE_ID, camera_id="CAM_BILLING_03", visitor_id=vid,
                       event_type="ZONE_EXIT", timestamp=txn_dt - timedelta(seconds=30),
                       zone_id="BILLING_QUEUE", dwell_ms=90000, confidence=0.90,
                       sku_zone="BILLING", session_seq=5),
            make_event(store_id=STORE_ID, camera_id="CAM_ENTRY_01", visitor_id=vid,
                       event_type="EXIT", timestamp=txn_dt + timedelta(seconds=30),
                       confidence=0.91, session_seq=6),
        ]

        # Every 4th visitor browses but doesn't buy: ENTRY + ZONE_ENTER + ZONE_EXIT + EXIT, no billing
        if i % 4 == 3:
            browse_id = f"VIS_BROWSE_{i:03d}"
            browse_zone = ZONE_CYCLE[(i + 2) % len(ZONE_CYCLE)]
            events += [
                make_event(store_id=STORE_ID, camera_id="CAM_ENTRY_01", visitor_id=browse_id,
                           event_type="ENTRY", timestamp=txn_dt - timedelta(minutes=10),
                           confidence=0.89, session_seq=1),
                make_event(store_id=STORE_ID, camera_id="CAM_FLOOR_02", visitor_id=browse_id,
                           event_type="ZONE_ENTER", timestamp=txn_dt - timedelta(minutes=9),
                           zone_id=browse_zone, confidence=0.87, sku_zone=browse_zone, session_seq=2),
                make_event(store_id=STORE_ID, camera_id="CAM_FLOOR_02", visitor_id=browse_id,
                           event_type="ZONE_EXIT", timestamp=txn_dt - timedelta(minutes=6),
                           zone_id=browse_zone, dwell_ms=120000, confidence=0.86,
                           sku_zone=browse_zone, session_seq=3),
                make_event(store_id=STORE_ID, camera_id="CAM_ENTRY_01", visitor_id=browse_id,
                           event_type="EXIT", timestamp=txn_dt - timedelta(minutes=5),
                           confidence=0.88, session_seq=4),
            ]

        # Every 7th visitor joins the queue but abandons
        if i % 7 == 6:
            abandon_id = f"VIS_ABANDON_{i:03d}"
            events += [
                make_event(store_id=STORE_ID, camera_id="CAM_ENTRY_01", visitor_id=abandon_id,
                           event_type="ENTRY", timestamp=txn_dt - timedelta(minutes=12),
                           confidence=0.91, session_seq=1),
                make_event(store_id=STORE_ID, camera_id="CAM_FLOOR_02", visitor_id=abandon_id,
                           event_type="ZONE_ENTER", timestamp=txn_dt - timedelta(minutes=11),
                           zone_id=zone, confidence=0.88, sku_zone=zone, session_seq=2),
                make_event(store_id=STORE_ID, camera_id="CAM_FLOOR_02", visitor_id=abandon_id,
                           event_type="ZONE_EXIT", timestamp=txn_dt - timedelta(minutes=8),
                           zone_id=zone, dwell_ms=180000, confidence=0.87, sku_zone=zone, session_seq=3),
                make_event(store_id=STORE_ID, camera_id="CAM_BILLING_03", visitor_id=abandon_id,
                           event_type="BILLING_QUEUE_JOIN", timestamp=txn_dt - timedelta(minutes=5),
                           zone_id="BILLING_QUEUE", confidence=0.93, queue_depth=q + 1,
                           sku_zone="BILLING", session_seq=4),
                make_event(store_id=STORE_ID, camera_id="CAM_BILLING_03", visitor_id=abandon_id,
                           event_type="BILLING_QUEUE_ABANDON", timestamp=txn_dt - timedelta(minutes=3),
                           zone_id="BILLING_QUEUE", dwell_ms=120000, confidence=0.85,
                           session_seq=5),
                make_event(store_id=STORE_ID, camera_id="CAM_ENTRY_01", visitor_id=abandon_id,
                           event_type="EXIT", timestamp=txn_dt - timedelta(minutes=2, seconds=30),
                           confidence=0.87, session_seq=6),
            ]

        # Staff event every 8 transactions
        if i % 8 == 0:
            events.append(make_event(
                store_id=STORE_ID, camera_id="CAM_FLOOR_02",
                visitor_id=f"VIS_STAFF_{i:02d}", event_type="ZONE_ENTER",
                timestamp=txn_dt - timedelta(minutes=5), zone_id="SKINCARE",
                is_staff=True, confidence=0.72, sku_zone="SKINCARE", session_seq=1,
            ))

        # One re-entry every 10 transactions
        if i % 10 == 9:
            events += [
                make_event(store_id=STORE_ID, camera_id="CAM_ENTRY_01", visitor_id=vid,
                           event_type="EXIT", timestamp=txn_dt - timedelta(minutes=8),
                           confidence=0.88, session_seq=6),
                make_event(store_id=STORE_ID, camera_id="CAM_ENTRY_01", visitor_id=vid,
                           event_type="REENTRY", timestamp=txn_dt - timedelta(minutes=7, seconds=30),
                           confidence=0.85, session_seq=7),
            ]

    events.sort(key=lambda e: e["timestamp"])

    with open(OUTPUT_PATH, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    print(f"[DONE] {len(events)} events written to {OUTPUT_PATH}")
    entry_count = len([e for e in events if e['event_type'] == 'ENTRY'])
    reentry_count = len([e for e in events if e['event_type'] == 'REENTRY'])
    print(f"       {len(transactions)} transactions -> {entry_count} ENTRY + {reentry_count} REENTRY events")


if __name__ == "__main__":
    main()
