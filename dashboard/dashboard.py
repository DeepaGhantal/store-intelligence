"""
dashboard.py — terminal live dashboard for store intelligence.

This version uses only the Python standard library so it can run in a clean
virtualenv without installing extra UI packages.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib import error, request

DEFAULT_API_URL = os.getenv("STORE_INTELLIGENCE_API_URL", "http://127.0.0.1:8000")
DEFAULT_STORE_ID = os.getenv("STORE_INTELLIGENCE_STORE_ID", "STORE_BLR_002")
DEFAULT_REFRESH_SECONDS = float(os.getenv("STORE_INTELLIGENCE_REFRESH_SECONDS", "3"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live terminal dashboard for Store Intelligence")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base API URL")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="Store ID to monitor")
    parser.add_argument("--refresh", type=float, default=DEFAULT_REFRESH_SECONDS, help="Refresh interval in seconds")
    return parser


def fetch_json(api_url: str, path: str) -> dict[str, Any]:
    req = request.Request(f"{api_url.rstrip('/')}{path}", method="GET")
    try:
        with request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"GET {path} failed with HTTP {exc.code}: {body}") from exc

    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object response from {path}")
    return payload


def fetch_dashboard_data(api_url: str, store_id: str) -> dict[str, Any]:
    return {
        "health": fetch_json(api_url, "/health"),
        "metrics": fetch_json(api_url, f"/stores/{store_id}/metrics"),
        "funnel": fetch_json(api_url, f"/stores/{store_id}/funnel"),
        "anomalies": fetch_json(api_url, f"/stores/{store_id}/anomalies"),
        "heatmap": fetch_json(api_url, f"/stores/{store_id}/heatmap"),
    }


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def format_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "N/A"


def format_number(value: Any, suffix: str = "") -> str:
    try:
        return f"{float(value):.1f}{suffix}"
    except Exception:
        return "N/A"


def pick_store_health(health: dict[str, Any], store_id: str) -> dict[str, Any]:
    stores = health.get("stores", [])
    if not isinstance(stores, list):
        return {}
    for row in stores:
        if isinstance(row, dict) and row.get("store_id") == store_id:
            return row
    return stores[0] if stores and isinstance(stores[0], dict) else {}


def render_lines(payload: dict[str, Any], api_url: str, store_id: str) -> list[str]:
    health = payload["health"]
    metrics = payload["metrics"]
    funnel = payload["funnel"]
    anomalies = payload["anomalies"]
    heatmap = payload["heatmap"]
    store_health = pick_store_health(health, store_id)

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("STORE INTELLIGENCE LIVE DASHBOARD")
    lines.append("=" * 78)
    lines.append(f"API            : {api_url}")
    lines.append(f"Store          : {store_id}")
    lines.append(f"Service Status : {health.get('status', 'UNKNOWN')}")
    lines.append(f"Feed Status    : {store_health.get('feed_status', 'UNKNOWN')}")
    lag_seconds = store_health.get("lag_seconds")
    if lag_seconds is not None and lag_seconds < 0:
        lag_display = f"{abs(lag_seconds):.0f}s ahead of server clock"
    elif lag_seconds is not None:
        lag_display = f"{lag_seconds:.0f}s"
    else:
        lag_display = "N/A"
    lines.append(f"Feed Lag       : {lag_display}")
    lines.append(f"Last Event     : {store_health.get('last_event_timestamp', 'N/A')} (UTC)")
    lines.append(f"Checked At     : {health.get('checked_at', 'N/A')} (UTC)")
    lines.append("")

    lines.append("KEY METRICS")
    lines.append("-" * 78)
    lines.append(f"Unique Visitors   : {metrics.get('unique_visitors', 0)}")
    lines.append(f"Conversion Rate   : {format_percent(metrics.get('conversion_rate', 0.0))}")
    lines.append(f"Avg Dwell         : {format_number(metrics.get('avg_dwell_seconds', 0.0), 's')}")
    lines.append(f"Queue Depth       : {metrics.get('queue_depth_current', 0)}")
    lines.append(f"Abandonment Rate  : {format_percent(metrics.get('abandonment_rate', 0.0))}")
    lines.append(f"Transactions      : {metrics.get('total_transactions', 0)}")
    lines.append("")

    lines.append("FUNNEL")
    lines.append("-" * 78)
    for stage in funnel.get("stages", []):
        if not isinstance(stage, dict):
            continue
        lines.append(
            f"{stage.get('stage', ''):<16} "
            f"count={stage.get('count', 0):<6} "
            f"drop-off={stage.get('drop_off_pct', 0.0):>6.1f}%"
        )
    lines.append("")

    lines.append("HEATMAP")
    lines.append("-" * 78)
    for zone in heatmap.get("zones", []):
        if not isinstance(zone, dict):
            continue
        lines.append(
            f"{zone.get('zone_id', ''):<18} "
            f"visits={zone.get('visit_frequency', 0):<5} "
            f"avg_dwell={zone.get('avg_dwell_seconds', 0.0):>6.1f}s "
            f"score={zone.get('normalised_score', 0.0):>6.1f} "
            f"confidence={zone.get('data_confidence', 'LOW')}"
        )
    lines.append("")

    lines.append("ANOMALIES")
    lines.append("-" * 78)
    anomaly_rows = anomalies.get("anomalies", [])
    if not anomaly_rows:
        lines.append("No active anomalies")
    else:
        for item in anomaly_rows[:8]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"[{item.get('severity', 'INFO')}] "
                f"{item.get('anomaly_type', '')}: "
                f"{item.get('description', '')} "
                f"-> {item.get('suggested_action', '')}"
            )

    lines.append("")
    lines.append("Press Ctrl+C to exit. Run feed_events.py in another terminal to watch values change.")
    return lines


def run_dashboard(api_url: str, store_id: str, refresh_seconds: float) -> None:
    while True:
        try:
            payload = fetch_dashboard_data(api_url, store_id)
            clear_screen()
            for line in render_lines(payload, api_url, store_id):
                print(line)
        except Exception as exc:
            clear_screen()
            print("=" * 78)
            print("STORE INTELLIGENCE LIVE DASHBOARD")
            print("=" * 78)
            print("Dashboard error:")
            print(exc)
            print("")
            print(f"Expected API URL: {api_url}")
            print("Start the API first with: uvicorn app.main:app --reload")
            print("Press Ctrl+C to exit.")
        time.sleep(max(refresh_seconds, 0.5))


def main() -> None:
    args = build_parser().parse_args()
    try:
        run_dashboard(args.api_url, args.store_id, args.refresh)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
