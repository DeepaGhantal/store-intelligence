"""
detect.py — Main detection + tracking pipeline.

Processes CCTV clips using YOLOv8n (person detection) + custom ByteTrack-style
tracker with Re-ID. Emits structured events to a JSONL file.

Usage:
    python detect.py --store STORE_BLR_002 --layout ../data/store_layout.json \
                     --clips ../data/clips/ --output ../data/events.jsonl

Camera naming convention (positional, 1-indexed per store):
    CAM 1.mp4 → CAM_ENTRY_01
    CAM 2.mp4 → CAM_FLOOR_02
    CAM 3.mp4 → CAM_BILLING_03
    CAM 4.mp4 → CAM_ENTRY_01  (second store, reuses slot)
    CAM 5.mp4 → CAM_FLOOR_02
"""
import argparse
import json
import os
import sys
import cv2
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add pipeline dir to path
sys.path.insert(0, str(Path(__file__).parent))
from tracker import Tracker
from emit import EventEmitter, make_event

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False


# ── Zone geometry helpers ────────────────────────────────────────────────────

def get_zone_regions(camera_type: str, frame_w: int, frame_h: int) -> dict:
    """
    Returns named bounding boxes for each zone based on camera type.
    Coordinates are fractions of frame dimensions, then scaled.
    These are heuristic splits — in production, calibrated from store_layout.json.
    """
    if camera_type == "entry":
        # Entry camera: use middle 40% of frame as threshold band
        # People enter from bottom (outside) moving up, or top (inside) moving down
        threshold_y = int(frame_h * 0.5)
        return {
            "ENTRY_ZONE": [0, threshold_y, frame_w, frame_h],
            "THRESHOLD":  [0, threshold_y - 40, frame_w, threshold_y + 40],
        }
    elif camera_type == "floor":
        # Floor camera: divide into quadrants for zone mapping
        mid_x, mid_y = frame_w // 2, frame_h // 2
        return {
            "SKINCARE":      [0,     0,     mid_x, mid_y],
            "MAKEUP":        [mid_x, 0,     frame_w, mid_y],
            "HAIRCARE":      [0,     mid_y, mid_x, frame_h],
            "PERSONAL_CARE": [mid_x, mid_y, frame_w, frame_h],
        }
    elif camera_type == "billing":
        billing_y = int(frame_h * 0.4)
        return {
            "BILLING":       [0, 0,        frame_w, billing_y],
            "BILLING_QUEUE": [0, billing_y, frame_w, frame_h],
        }
    return {}


def bbox_in_zone(bbox, zone_box):
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return zone_box[0] <= cx <= zone_box[2] and zone_box[1] <= cy <= zone_box[3]


def detect_direction(prev_cy, curr_cy, threshold_y, camera_type):
    """For entry camera: moving toward threshold from inside = EXIT, from outside = ENTRY."""
    if camera_type != "entry":
        return None
    if prev_cy is None:
        return None
    if prev_cy < threshold_y and curr_cy >= threshold_y:
        return "EXIT"
    if prev_cy >= threshold_y and curr_cy < threshold_y:
        return "ENTRY"
    return None


def is_staff_heuristic(bbox, frame_h, track_history):
    """
    Heuristic staff detection:
    - Staff tend to move across the full frame width repeatedly
    - Staff bboxes often appear at consistent y positions (behind counters)
    - In absence of uniform detection model, use movement pattern
    """
    if len(track_history) < 10:
        return False
    ys = [h[1] for h in track_history]
    xs = [h[0] for h in track_history]
    y_range = max(ys) - min(ys)
    x_range = max(xs) - min(xs)
    # Staff move a lot horizontally but stay in a narrow vertical band
    if x_range > 0.6 * frame_h and y_range < 0.15 * frame_h:
        return True
    return False


# ── Main pipeline ────────────────────────────────────────────────────────────

def process_clip(
    video_path: str,
    store_id: str,
    camera_id: str,
    camera_type: str,
    clip_start_time: datetime,
    emitter: EventEmitter,
    fps_override: float = None,
):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[WARN] Cannot open {video_path}")
        return

    fps = fps_override or cap.get(cv2.CAP_PROP_FPS) or 15.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"[INFO] Processing {Path(video_path).name} | {camera_id} | {total_frames} frames @ {fps:.1f}fps")

    zones = get_zone_regions(camera_type, frame_w, frame_h)
    tracker = Tracker(max_lost=int(fps * 3), iou_threshold=0.15, reentry_gap_frames=int(fps * 10))

    # Load YOLO model (downloads yolov8n.pt on first run ~6MB)
    if YOLO_AVAILABLE:
        model = YOLO("yolov8n.pt")
    else:
        model = None
        print("[WARN] ultralytics not available — using mock detections")

    # Per-track state for event logic
    track_prev_cy = {}       # visitor_id → previous center_y
    track_zone = {}          # visitor_id → current zone
    track_zone_entry_frame = {}  # visitor_id → frame when entered zone
    track_dwell_emitted = {}     # visitor_id → last dwell emit frame
    track_entered = set()        # visitor_ids that emitted ENTRY (or REENTRY)
    track_exited = set()         # visitor_ids that emitted EXIT
    track_history = {}           # visitor_id → list of (cx, cy)
    track_reentry_flagged = set()
    track_billing_entry_frame = {}  # visitor_id → frame when they entered BILLING_QUEUE
    track_billing_zone = set()      # visitor_ids currently in BILLING_QUEUE

    # Try multiple threshold positions to catch crossings at different depths
    threshold_y = zones.get("THRESHOLD", [0, int(frame_h * 0.5), frame_w, frame_h])[1]

    frame_idx = 0
    # Entry camera: process every 3rd frame (~10fps) to catch threshold crossings
    # Floor/billing: every 6th frame (5fps) is sufficient for zone dwell
    PROCESS_EVERY = max(1, int(fps / 10)) if camera_type == "entry" else max(1, int(fps / 5))

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        if frame_idx % PROCESS_EVERY != 0:
            continue

        ts = clip_start_time + timedelta(seconds=frame_idx / fps)

        # ── Detection ──────────────────────────────────────────────────────
        detections = []
        if model:
            results = model(frame, classes=[0], verbose=False, conf=0.20)  # lower threshold for face-blurred footage
            for r in results:
                for box in r.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    conf = float(box.conf[0])
                    detections.append({"bbox": [x1, y1, x2, y2], "confidence": conf, "is_staff": False})
        else:
            # Mock: simulate 2 people walking through
            mock_x = int((frame_idx / total_frames) * frame_w)
            detections = [{"bbox": [mock_x, 100, mock_x + 60, 280], "confidence": 0.82, "is_staff": False}]

        # ── Tracking ───────────────────────────────────────────────────────
        active_tracks = tracker.update(detections, frame_idx)

        for track in active_tracks:
            vid = track.visitor_id
            bbox = track.bbox
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2

            # Update movement history for staff heuristic
            if vid not in track_history:
                track_history[vid] = []
            track_history[vid].append((cx / frame_w, cy / frame_h))
            if len(track_history[vid]) > 60:
                track_history[vid] = track_history[vid][-60:]

            # Staff detection
            track.is_staff = is_staff_heuristic(bbox, frame_h, track_history[vid])

            # ── Entry / Exit events (entry camera only) ────────────────────
            if camera_type == "entry":
                prev_cy = track_prev_cy.get(vid)
                direction = detect_direction(prev_cy, cy, threshold_y, camera_type)
                track_prev_cy[vid] = cy

                if direction == "ENTRY" and vid not in track_entered:
                    track_entered.add(vid)
                    event_type = "REENTRY" if vid in track_exited else "ENTRY"
                    if event_type == "REENTRY":
                        track_reentry_flagged.add(vid)
                        track_exited.discard(vid)  # allow future re-exits to trigger REENTRY again
                    emitter.emit(make_event(
                        store_id=store_id, camera_id=camera_id,
                        visitor_id=vid, event_type=event_type,
                        timestamp=ts, zone_id=None, dwell_ms=0,
                        is_staff=track.is_staff, confidence=track.confidence,
                        session_seq=track.session_seq,
                    ))

                elif direction == "EXIT" and vid in track_entered:
                    track_exited.add(vid)
                    track_entered.discard(vid)  # allow re-entry cycle to restart
                    emitter.emit(make_event(
                        store_id=store_id, camera_id=camera_id,
                        visitor_id=vid, event_type="EXIT",
                        timestamp=ts, zone_id=None, dwell_ms=0,
                        is_staff=track.is_staff, confidence=track.confidence,
                        session_seq=track.session_seq,
                    ))

            # ── Zone events (floor + billing cameras) ─────────────────────
            if camera_type in ("floor", "billing"):
                current_zone = None
                for zone_name, zone_box in zones.items():
                    if bbox_in_zone(bbox, zone_box):
                        current_zone = zone_name
                        break

                prev_zone = track_zone.get(vid)

                if current_zone != prev_zone:
                    # Zone exit
                    if prev_zone and vid in track_zone_entry_frame:
                        entry_f = track_zone_entry_frame[vid]
                        dwell_ms = int((frame_idx - entry_f) / fps * 1000)
                        emitter.emit(make_event(
                            store_id=store_id, camera_id=camera_id,
                            visitor_id=vid, event_type="ZONE_EXIT",
                            timestamp=ts, zone_id=prev_zone, dwell_ms=dwell_ms,
                            is_staff=track.is_staff, confidence=track.confidence,
                            session_seq=track.session_seq,
                        ))

                    # Zone enter
                    if current_zone:
                        track_zone_entry_frame[vid] = frame_idx
                        track_dwell_emitted[vid] = frame_idx

                        # Queue depth for billing
                        queue_depth = None
                        if current_zone == "BILLING_QUEUE":
                            # Count everyone already in BILLING_QUEUE (including this visitor)
                            queue_depth = sum(
                                1 for t in active_tracks
                                if bbox_in_zone(t.bbox, zones.get("BILLING_QUEUE", [0,0,0,0]))
                            )
                            track_billing_entry_frame[vid] = frame_idx
                            track_billing_zone.add(vid)
                            evt = "BILLING_QUEUE_JOIN"  # always emit join when entering queue
                        else:
                            evt = "ZONE_ENTER"

                        emitter.emit(make_event(
                            store_id=store_id, camera_id=camera_id,
                            visitor_id=vid, event_type=evt,
                            timestamp=ts, zone_id=current_zone, dwell_ms=0,
                            is_staff=track.is_staff, confidence=track.confidence,
                            queue_depth=queue_depth,
                            sku_zone=current_zone if camera_type == "floor" else None,
                            session_seq=track.session_seq,
                        ))

                    # BILLING_QUEUE_ABANDON: visitor left queue without transacting
                    # Detected heuristically: left BILLING_QUEUE after >10s but no EXIT follows
                    if prev_zone == "BILLING_QUEUE" and current_zone not in ("BILLING", None):
                        if vid in track_billing_zone:
                            dwell_in_queue = int((frame_idx - track_billing_entry_frame.get(vid, frame_idx)) / fps * 1000)
                            if dwell_in_queue > 10000:  # was in queue >10s then left sideways
                                emitter.emit(make_event(
                                    store_id=store_id, camera_id=camera_id,
                                    visitor_id=vid, event_type="BILLING_QUEUE_ABANDON",
                                    timestamp=ts, zone_id="BILLING_QUEUE", dwell_ms=dwell_in_queue,
                                    is_staff=track.is_staff, confidence=track.confidence,
                                    session_seq=track.session_seq,
                                ))
                            track_billing_zone.discard(vid)

                    track_zone[vid] = current_zone

                # ZONE_DWELL every 30s of continuous presence
                elif current_zone and vid in track_zone_entry_frame:
                    frames_in_zone = frame_idx - track_dwell_emitted.get(vid, frame_idx)
                    if frames_in_zone >= int(fps * 30):
                        dwell_ms = int(frames_in_zone / fps * 1000)
                        emitter.emit(make_event(
                            store_id=store_id, camera_id=camera_id,
                            visitor_id=vid, event_type="ZONE_DWELL",
                            timestamp=ts, zone_id=current_zone, dwell_ms=dwell_ms,
                            is_staff=track.is_staff, confidence=track.confidence,
                            session_seq=track.session_seq,
                        ))
                        track_dwell_emitted[vid] = frame_idx

        if frame_idx % (int(fps) * 60) == 0:
            mins = frame_idx / fps / 60
            print(f"  [{camera_id}] {mins:.1f} min processed, active tracks: {len(tracker.tracks)}")

    cap.release()
    print(f"[INFO] Done {Path(video_path).name} — {frame_idx} frames, {len(track_entered)} entries detected")


def main():
    parser = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    parser.add_argument("--store",   default="STORE_BLR_002")
    parser.add_argument("--layout",  default="../data/store_layout.json")
    parser.add_argument("--clips",   default="../data/clips/")
    parser.add_argument("--output",  default="../data/events.jsonl")
    parser.add_argument("--start",   default="2026-04-10T10:00:00Z",
                        help="ISO-8601 UTC start time for clip 1")
    args = parser.parse_args()

    with open(args.layout) as f:
        layout = json.load(f)

    store = next(s for s in layout["stores"] if s["store_id"] == args.store)
    cameras = store["cameras"]

    # Map clip filenames to camera configs
    clip_dir = Path(args.clips)
    # Support both flat dir and the provided CCTV Footage subfolder
    if not clip_dir.exists():
        alt = clip_dir.parent.parent / "CCTV Footage-20260529T160731Z-3-00144614ea" / "CCTV Footage"
        if alt.exists():
            clip_dir = alt

    clip_map = {
        "CAM 1.mp4": ("CAM_ENTRY_01",   "entry"),
        "CAM 2.mp4": ("CAM_FLOOR_02",   "floor"),
        "CAM 3.mp4": ("CAM_BILLING_03", "billing"),
        "CAM 4.mp4": ("CAM_ENTRY_01",   "entry"),
        "CAM 5.mp4": ("CAM_FLOOR_02",   "floor"),
    }

    clip_start = datetime.strptime(args.start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    emitter = EventEmitter(args.output)

    for clip_name, (cam_id, cam_type) in clip_map.items():
        clip_path = clip_dir / clip_name
        if not clip_path.exists():
            print(f"[SKIP] {clip_path} not found")
            continue
        process_clip(
            video_path=str(clip_path),
            store_id=args.store,
            camera_id=cam_id,
            camera_type=cam_type,
            clip_start_time=clip_start,
            emitter=emitter,
        )

    emitter.close()
    print(f"\n[DONE] Events written to {args.output}")


if __name__ == "__main__":
    main()
