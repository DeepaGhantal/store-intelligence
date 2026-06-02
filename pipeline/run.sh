#!/bin/bash
# run.sh — One command to process all clips and emit events
# Usage: bash pipeline/run.sh
# Place clips in data/clips/ as "CAM 1.mp4" through "CAM 5.mp4"

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."

python "$SCRIPT_DIR/detect.py" \
  --store STORE_BLR_002 \
  --layout "$ROOT/data/store_layout.json" \
  --clips "$ROOT/data/clips/" \
  --output "$ROOT/data/events.jsonl" \
  --start 2026-04-10T10:00:00Z

echo ""
echo "Events written to data/events.jsonl"
echo "Ingest into API:"
echo "  python -c \"import json,requests; events=[json.loads(l) for l in open('data/events.jsonl')]; [print(requests.post('http://localhost:8000/events/ingest',json={'events':events[i:i+500]}).json()) for i in range(0,len(events),500)]\""
