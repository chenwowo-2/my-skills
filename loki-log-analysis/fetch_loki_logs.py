#!/usr/bin/env python3
"""
Loki Log Fetcher - fetch all file logs from the past N hours.
Usage: python fetch_loki_logs.py [--url http://host:3100] [--hours 6] [--limit 2000] [--output json|text]
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

DEFAULT_LOKI_URL = "http://192.168.88.128:3100"


def loki_get(base_url: str, path: str, params: dict = None) -> dict:
    """Send a GET request to the Loki API and return parsed JSON."""
    url = f"{base_url}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[ERROR] HTTP {e.code} on {url}: {body}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"[ERROR] Request failed {url}: {e}", file=sys.stderr)
        return {}


def get_all_labels(base_url: str) -> list:
    """Fetch all label names from Loki."""
    data = loki_get(base_url, "/loki/api/v1/labels")
    return data.get("data", [])


def get_label_values(base_url: str, label: str) -> list:
    """Fetch all values for a given label."""
    data = loki_get(base_url, f"/loki/api/v1/label/{label}/values")
    return data.get("data", [])


def get_all_series(base_url: str, start_ns: int, end_ns: int) -> list:
    """Fetch all log series from Loki."""
    data = loki_get(base_url, "/loki/api/v1/series", {
        "match[]": '{__name__=~".+"}',
        "start": start_ns,
        "end": end_ns,
    })
    if not data.get("data"):
        # fallback: try without match filter
        data = loki_get(base_url, "/loki/api/v1/series", {
            "start": start_ns,
            "end": end_ns,
        })
    return data.get("data", [])


def query_logs(base_url: str, log_selector: str, start_ns: int, end_ns: int, limit: int = 2000) -> list:
    """
    Query Loki for log lines matching log_selector in [start_ns, end_ns].
    Returns a list of (timestamp_ns, labels, line) tuples.
    """
    params = {
        "query": log_selector,
        "start": start_ns,
        "end": end_ns,
        "limit": limit,
        "direction": "forward",
    }
    data = loki_get(base_url, "/loki/api/v1/query_range", params)
    entries = []
    result = data.get("data", {}).get("result", [])
    for stream in result:
        stream_labels = stream.get("stream", {})
        for ts_ns, line in stream.get("values", []):
            entries.append({
                "timestamp_ns": int(ts_ns),
                "timestamp": datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "labels": stream_labels,
                "line": line,
            })
    return entries


def build_selector_from_labels(labels: list) -> str:
    """
    Build a LogQL selector that matches any stream having a 'filename' label,
    or fall back to a broad match.
    """
    if "filename" in labels:
        return '{filename=~".+"}'
    if "job" in labels:
        return '{job=~".+"}'
    # Try the first available label
    if labels:
        return '{' + labels[0] + '=~".+"}'
    return '{__name__=~".+"}'


def main():
    parser = argparse.ArgumentParser(description="Fetch Loki logs for the past N hours.")
    parser.add_argument("--url", default=DEFAULT_LOKI_URL, help=f"Loki base URL (default: {DEFAULT_LOKI_URL})")
    parser.add_argument("--hours", type=float, default=6.0, help="Hours to look back (default: 6)")
    parser.add_argument("--limit", type=int, default=2000, help="Max log lines per stream (default: 2000)")
    parser.add_argument("--output", choices=["json", "text"], default="text", help="Output format")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    now_ns = int(time.time() * 1e9)
    start_ns = int((time.time() - args.hours * 3600) * 1e9)

    print(f"[INFO] Querying Loki: {base_url}", file=sys.stderr)
    print(f"[INFO] Time range: last {args.hours} hours", file=sys.stderr)
    print(f"[INFO] Start: {datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}", file=sys.stderr)
    print(f"[INFO] End  : {datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}", file=sys.stderr)

    # Step 1: discover all labels
    labels = get_all_labels(base_url)
    print(f"[INFO] Found labels: {labels}", file=sys.stderr)

    # Step 2: determine best selector
    selector = build_selector_from_labels(labels)
    print(f"[INFO] Using LogQL selector: {selector}", file=sys.stderr)

    # Step 3: discover all series
    series = get_all_series(base_url, start_ns, now_ns)
    print(f"[INFO] Found {len(series)} log series", file=sys.stderr)

    # Step 4: if filename label exists, collect unique filenames
    filenames = []
    if "filename" in labels:
        filenames = get_label_values(base_url, "filename")
        print(f"[INFO] Found {len(filenames)} unique filenames: {filenames}", file=sys.stderr)

    # Step 5: query logs
    all_entries = []
    if filenames:
        # Query per-file for richer output
        for fname in filenames:
            file_selector = '{filename="' + fname + '"}'
            print(f"[INFO] Fetching logs for file: {fname}", file=sys.stderr)
            entries = query_logs(base_url, file_selector, start_ns, now_ns, limit=args.limit)
            print(f"[INFO]   -> {len(entries)} log lines", file=sys.stderr)
            all_entries.extend(entries)
    else:
        # Broad query
        all_entries = query_logs(base_url, selector, start_ns, now_ns, limit=args.limit * max(1, len(series)))

    # Sort all entries by timestamp
    all_entries.sort(key=lambda x: x["timestamp_ns"])

    print(f"[INFO] Total log lines fetched: {len(all_entries)}", file=sys.stderr)

    # Output
    if args.output == "json":
        result = {
            "loki_url": base_url,
            "query_hours": args.hours,
            "labels": labels,
            "filenames": filenames,
            "series_count": len(series),
            "total_lines": len(all_entries),
            "entries": all_entries,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        # Text format: group by source file
        groups: dict = {}
        for entry in all_entries:
            fname = entry["labels"].get("filename", entry["labels"].get("job", "unknown"))
            groups.setdefault(fname, []).append(entry)

        print(f"=== Loki Log Report ===")
        print(f"Address : {base_url}")
        print(f"Period  : last {args.hours} hours")
        print(f"Labels  : {', '.join(labels) if labels else 'none'}")
        print(f"Files   : {len(groups)}")
        print(f"Total   : {len(all_entries)} lines")
        print()

        for source, entries in sorted(groups.items()):
            print(f"--- [{source}] ({len(entries)} lines) ---")
            for e in entries:
                print(f"{e['timestamp']}  {e['line']}")
            print()


if __name__ == "__main__":
    main()
