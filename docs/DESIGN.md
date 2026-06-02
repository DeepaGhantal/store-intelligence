# DESIGN.md — Store Intelligence System Architecture

## Overview

This system converts raw CCTV footage from Apex Retail's Brigade Road, Bangalore store into a live analytics API. The pipeline has four stages: detection, event emission, ingestion, and query.

```
CCTV Clips → detect.py (YOLOv8n + Tracker) → events.jsonl → POST /events/ingest → SQLite → GET /stores/{id}/metrics|funnel|heatmap|anomalies
```

---

## Stage 1 — Detection Pipeline (`pipeline/`)

**Model:** YOLOv8n (nano) for person detection. Chosen for speed on CPU — processes 5 frames/sec from 15fps clips, giving adequate temporal resolution without requiring a GPU.

**Tracker:** Custom ByteTrack-style IoU + appearance tracker (`tracker.py`). Each detection is matched to existing tracks by IoU score, with an appearance distance penalty based on bounding box aspect ratio and relative position. Unmatched detections spawn new tracks. Tracks lost for >2 seconds move to a "lost pool" for re-entry matching.

**Re-entry detection:** Two-layer system. At the tracker level (`tracker.py`), unmatched detections are compared against the "lost pool" (tracks that disappeared within the last 6 seconds) using appearance distance. If matched, the same `visitor_id` is restored. At the event level (`detect.py`), ENTRY/EXIT state is tracked per visitor: after an EXIT, the visitor is removed from `track_entered` so a subsequent crossing emits REENTRY (not a second ENTRY). This correctly handles multiple re-entry cycles — each EXIT + ENTRY pair produces exactly one REENTRY event.

**Queue abandon detection:** `BILLING_QUEUE_ABANDON` is emitted when a visitor leaves `BILLING_QUEUE` zone sideways (to any zone that is not `BILLING`) after spending more than 10 seconds in the queue. This is a heuristic — it can't distinguish a genuine abandon from a customer who momentarily steps out. The 10-second floor avoids false positives from brief occlusion events. POS correlation at the API layer provides a second signal: visitors who reached billing but are not in the conversion set are likely abandonments.

**Staff detection:** Heuristic based on movement pattern — staff move across a wide horizontal range but stay in a narrow vertical band (behind counters). This avoids needing a separate uniform classifier.

**Zone mapping:** Camera type determines zone geometry. Entry camera uses a horizontal threshold line to determine ENTRY/EXIT direction. Floor camera divides the frame into quadrants mapped to product zones. Billing camera splits into BILLING and BILLING_QUEUE regions.

**Event emission:** `emit.py` writes JSONL to disk. Each event is a complete, self-contained record matching the required schema.

---

## Stage 2 — Event Schema

Events are emitted as JSONL with full schema compliance. Key design decisions:
- `event_id` is UUID v4 — globally unique, enables idempotent ingestion
- `timestamp` is derived from clip start time + frame offset — deterministic and reproducible
- `confidence` is always included, never suppressed — low-confidence events are flagged, not dropped
- `is_staff` is a boolean on every event — allows downstream filtering without joins

---

## Stage 3 — Intelligence API (`app/`)

**Framework:** FastAPI with SQLAlchemy ORM. Chosen for automatic OpenAPI docs, Pydantic validation, and async support.

**Storage:** SQLite (file-based). Sufficient for single-store demo; swappable to PostgreSQL via `DATABASE_URL` env var without code changes.

**Ingestion (`POST /events/ingest`):**
- Validates via Pydantic before touching the DB
- Deduplicates by `event_id` in a single `IN` query — O(1) per batch
- Partial success: valid events are committed even if some fail validation
- Idempotent: calling twice with the same payload returns `duplicate=N, accepted=0` on second call

**Metrics (`GET /stores/{id}/metrics`):**
- Filters `is_staff=False` on all queries
- Conversion rate uses POS correlation: visitor in BILLING zone within 5 minutes before a transaction timestamp
- All queries filter on today's date — real-time, not cached

**Funnel (`GET /stores/{id}/funnel`):**
- Session is the unit: `DISTINCT visitor_id` at each stage
- Re-entries are deduplicated — same `visitor_id` counts once regardless of ENTRY/REENTRY events

**Anomalies (`GET /stores/{id}/anomalies`):**
- Queue spike: latest `queue_depth` from `BILLING_QUEUE_JOIN` events
- Conversion drop: today vs all-time rolling average (proxy for 7-day avg)
- Dead zone: no zone visits in last 30 minutes, only triggered when store has >5 visitors (avoids false alarms on empty store)
- High abandonment: >40% of queue joins result in abandonment

**Health (`GET /health`):**
- Reports lag in seconds between now and last event timestamp per store
- `STALE_FEED` if lag > 600 seconds (10 minutes)

---

## Stage 4 — Live Dashboard (`dashboard/`)

Two components:
1. `feed_events.py` — replays `events.jsonl` into the API at configurable speed (default 60x: 1 hour of store time in 1 minute)
2. `dashboard.py` — Rich terminal UI polling the API every 3 seconds, rendering metrics, funnel, heatmap, and anomalies in a live layout

---

## Production Readiness

- **Structured logging:** Every request logs `trace_id`, `store_id`, `endpoint`, `latency_ms`, `status_code` as JSON
- **Graceful degradation:** `OperationalError` (DB down) returns HTTP 503 with structured body, no stack traces
- **Containerised:** `docker compose up` starts everything. No manual steps beyond `git clone`
- **Test coverage:** >70% statement coverage across ingestion, metrics, anomaly detection

---

## AI-Assisted Decisions

**1. Re-entry detection approach**
I asked Claude to compare three approaches: (a) pure IoU matching with a time gap, (b) appearance embedding via OSNet/torchreid, (c) heuristic appearance distance using bbox geometry. Claude recommended (b) for production accuracy but noted it requires GPU inference and adds significant complexity. I chose (c) — the heuristic approach — because the challenge runs on CPU and the geometry-based fingerprint (aspect ratio + relative position) is sufficient for the 6-second re-entry window in retail footage. I agreed with Claude's framing of the trade-off but overrode the recommendation based on deployment constraints.

**2. Storage engine selection**
Claude initially suggested PostgreSQL with a Redis cache layer for real-time metrics. I pushed back: for a single-store demo with <1000 events/day, SQLite with indexed queries is faster to set up, has zero operational overhead, and the `DATABASE_URL` env var makes it trivially swappable. Claude agreed this was the right call for the challenge scope. I kept the architecture PostgreSQL-compatible by using SQLAlchemy ORM throughout.

**3. Conversion rate computation**
The challenge spec says "visitor in billing zone in 5-minute window before transaction." Claude suggested using a sliding window join in SQL. I implemented it in Python instead — load billing events into a dict keyed by visitor_id, then iterate transactions. This is O(visitors × transactions) but both sets are small (<100 each per day), and it's far more readable and testable than a complex SQL window function. Claude flagged that this wouldn't scale to 40 stores × real-time; I documented this as a known limitation in CHOICES.md.
