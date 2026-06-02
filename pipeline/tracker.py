"""
tracker.py — ByteTrack-style multi-object tracker with Re-ID via appearance + IoU.
Assigns stable visitor_id tokens across frames and detects re-entry.
"""
import uuid
import time
import numpy as np
from dataclasses import dataclass, field
from typing import Optional


def iou(boxA, boxB):
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])
    inter = max(0, xB - xA) * max(0, yB - yA)
    if inter == 0:
        return 0.0
    areaA = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    areaB = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    return inter / float(areaA + areaB - inter)


def box_center(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


@dataclass
class Track:
    track_id: int
    visitor_id: str
    bbox: list
    last_seen_frame: int
    is_staff: bool = False
    confidence: float = 1.0
    zone: Optional[str] = None
    session_seq: int = 0
    lost_frames: int = 0
    # appearance fingerprint: avg bbox aspect ratio + relative position
    appearance: list = field(default_factory=list)
    exited: bool = False
    entry_frame: int = 0


class Tracker:
    def __init__(self, max_lost=30, iou_threshold=0.3, reentry_gap_frames=90):
        self.tracks: list[Track] = []
        self.lost_tracks: list[Track] = []   # recently exited, for re-ID
        self.next_id = 1
        self.max_lost = max_lost
        self.iou_threshold = iou_threshold
        self.reentry_gap = reentry_gap_frames  # ~6s at 15fps

    def _appearance_vec(self, bbox):
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        aspect = w / (h + 1e-6)
        cx, cy = box_center(bbox)
        return [aspect, cx, cy]

    def _appearance_dist(self, a, b):
        if not a or not b:
            return 1.0
        return float(np.linalg.norm(np.array(a) - np.array(b)))

    def update(self, detections, frame_idx):
        """
        detections: list of dicts {bbox:[x1,y1,x2,y2], confidence:float, is_staff:bool}
        Returns list of matched Track objects (one per detection).
        """
        # Mark all active tracks as potentially lost
        for t in self.tracks:
            t.lost_frames += 1

        matched_track_ids = set()
        result = []

        for det in detections:
            bbox = det["bbox"]
            best_track = None
            best_score = -1

            for t in self.tracks:
                if t.track_id in matched_track_ids:
                    continue
                iou_score = iou(bbox, t.bbox)
                if iou_score < self.iou_threshold:
                    continue
                app_dist = self._appearance_dist(self._appearance_vec(bbox), t.appearance)
                score = iou_score - 0.1 * app_dist
                if score > best_score:
                    best_score = score
                    best_track = t

            if best_track:
                best_track.bbox = bbox
                best_track.last_seen_frame = frame_idx
                best_track.lost_frames = 0
                best_track.confidence = det["confidence"]
                best_track.is_staff = det.get("is_staff", False)
                best_track.appearance = self._appearance_vec(bbox)
                best_track.session_seq += 1
                matched_track_ids.add(best_track.track_id)
                result.append(best_track)
            else:
                # Check re-entry against recently exited tracks
                reentry_track = self._check_reentry(bbox, frame_idx)
                if reentry_track:
                    reentry_track.bbox = bbox
                    reentry_track.last_seen_frame = frame_idx
                    reentry_track.lost_frames = 0
                    reentry_track.exited = False
                    reentry_track.session_seq += 1
                    reentry_track.confidence = det["confidence"]
                    self.tracks.append(reentry_track)
                    self.lost_tracks.remove(reentry_track)
                    matched_track_ids.add(reentry_track.track_id)
                    result.append(reentry_track)
                else:
                    # New track
                    vid = f"VIS_{uuid.uuid4().hex[:6]}"
                    t = Track(
                        track_id=self.next_id,
                        visitor_id=vid,
                        bbox=bbox,
                        last_seen_frame=frame_idx,
                        is_staff=det.get("is_staff", False),
                        confidence=det["confidence"],
                        appearance=self._appearance_vec(bbox),
                        entry_frame=frame_idx,
                    )
                    self.next_id += 1
                    self.tracks.append(t)
                    matched_track_ids.add(t.track_id)
                    result.append(t)

        # Remove tracks lost too long; move to lost_tracks for re-ID window
        still_active = []
        for t in self.tracks:
            if t.lost_frames > self.max_lost:
                t.exited = True
                self.lost_tracks.append(t)
            else:
                still_active.append(t)
        self.tracks = still_active

        # Prune old lost tracks beyond re-entry window
        self.lost_tracks = [
            t for t in self.lost_tracks
            if (frame_idx - t.last_seen_frame) < self.reentry_gap * 3
        ]

        return result

    def _check_reentry(self, bbox, frame_idx):
        """Match a new detection against recently exited tracks by appearance."""
        app = self._appearance_vec(bbox)
        best = None
        best_dist = 0.5  # threshold
        for t in self.lost_tracks:
            gap = frame_idx - t.last_seen_frame
            if gap > self.reentry_gap:
                continue
            dist = self._appearance_dist(app, t.appearance)
            if dist < best_dist:
                best_dist = dist
                best = t
        return best
