# CHOICES.md — Three Key Engineering Decisions

---

## Decision 1 — Detection Model: YOLOv8n

### Options Considered
| Model | Pros | Cons |
|---|---|---|
| YOLOv8n | Fast CPU inference, small (6MB), well-documented, person class built-in | Lower accuracy than larger variants |
| YOLOv8m/l | Higher mAP, better on partial occlusion | 3–10× slower, requires GPU for real-time |
| RT-DETR | Transformer-based, better on crowded scenes | Large model, complex setup |
| MediaPipe Pose | Lightweight, runs on CPU | Pose estimation, not ideal for counting/tracking |

### What AI Suggested
I asked Claude to evaluate these options for a retail CCTV scenario with 1080p/15fps footage on CPU. Claude recommended YOLOv8s as a balance point — slightly larger than nano but still CPU-viable. It also suggested considering RT-DETR for the billing queue scene specifically, where people are densely packed.

### What I Chose and Why
YOLOv8n. The challenge processes 5 clips × 20 minutes = 100 minutes of footage. On CPU, YOLOv8n runs at ~8fps on 1080p, which is sufficient when sampling at 5fps (every 3rd frame). The accuracy trade-off is acceptable because: (1) we're counting people, not identifying them; (2) the tracker compensates for missed detections by maintaining track continuity; (3) confidence scores are always emitted, so low-confidence frames are flagged rather than silently wrong.

For the billing queue specifically, I process every frame (not every 3rd) to improve queue depth accuracy — this is where the business metric is most sensitive.

**VLM usage:** I evaluated using GPT-4V for staff detection (send a frame, ask "is this person wearing a uniform?"). I tested this on 10 sample frames. It worked but at ~2 seconds per frame it's 30× too slow for video processing. I kept it as a post-processing option for flagging uncertain `is_staff` cases but used the movement heuristic for real-time processing. The GPT-4V prompt I used: *"Look at this retail store frame. Is the person highlighted in the red bounding box wearing a store uniform or staff lanyard? Answer YES or NO with confidence 0-1."*

---

## Decision 2 — Event Schema Design

### Options Considered

**Option A — Flat schema:** All fields at top level. Simple to query, verbose.

**Option B — Nested metadata:** Core fields flat, optional/event-specific fields in `metadata` object. Matches the challenge spec.

**Option C — Typed event variants:** Separate Pydantic models per event type (EntryEvent, ZoneDwellEvent, etc.). Type-safe but complex to ingest generically.

### What AI Suggested
Claude suggested Option C for type safety and suggested using a discriminated union in Pydantic. It argued this would catch schema errors at the model level rather than at query time.

### What I Chose and Why
Option B — the nested metadata approach from the challenge spec. My reasoning:

1. **Ingestion simplicity:** A single `EventRecord` table handles all event types. Discriminated unions would require either multiple tables or a JSONB column, both of which add complexity.
2. **Query patterns:** The analytics queries (metrics, funnel, heatmap) filter by `event_type` and aggregate `dwell_ms`, `zone_id`, `queue_depth`. These are all top-level or one level deep — no need for type-specific schemas at query time.
3. **Schema evolution:** Adding a new event type means adding a value to the enum, not a new model class.

I disagreed with Claude's recommendation here. The type-safety benefit of discriminated unions is real but the operational cost (more complex ingestion, harder to add event types) outweighs it for this use case.

**Schema decisions I'm most confident about:**
- `event_id` as UUID v4 — enables idempotent ingestion without a separate dedup table
- `is_staff` on every event — avoids a join to a staff registry that doesn't exist
- `confidence` always present — forces the consumer to handle uncertainty rather than hiding it

---

## Decision 3 — API Architecture: Synchronous SQLAlchemy vs Async

### Options Considered

**Option A — Sync SQLAlchemy + FastAPI:** Standard ORM, simple session management, well-understood.

**Option B — Async SQLAlchemy (asyncpg/aiosqlite):** True async DB access, better throughput under concurrent load.

**Option C — In-memory store (dict/Redis) + async background persistence:** Lowest latency for reads, complex consistency guarantees.

### What AI Suggested
Claude recommended Option B — async SQLAlchemy with aiosqlite for SQLite. It argued that FastAPI is async-native and mixing sync DB calls blocks the event loop, reducing throughput.

### What I Chose and Why
Option A — synchronous SQLAlchemy. My reasoning:

1. **Correctness over throughput:** The challenge evaluates correctness of metrics, not API throughput. A sync implementation is easier to reason about, test, and debug.
2. **SQLite limitation:** aiosqlite has known issues with concurrent writes. Since we're using SQLite for the demo, sync is actually safer.
3. **The bottleneck isn't the DB:** For a single store with <1000 events/day, the DB query time is <5ms. The event loop blocking is negligible.

**Where this breaks at scale (40 stores, real-time):** Claude's concern is valid at scale. At 40 stores × ~100 events/minute = 4000 events/minute, sync DB calls would become a bottleneck. The fix is: (1) switch to PostgreSQL + async SQLAlchemy, (2) add a write-ahead buffer (Redis list), (3) batch-commit every 500ms. I documented this as a known limitation rather than over-engineering for the demo.

**The one place I used async:** The `/events/ingest` endpoint uses `async def` at the FastAPI layer, which means the event loop is free during network I/O (receiving the request body). The DB call itself is sync but that's a <10ms operation.
