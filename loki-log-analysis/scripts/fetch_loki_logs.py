#!/usr/bin/env python3
"""
Loki Log Fetcher - fetch file logs and filter by error/warning level.

Usage:
  python fetch_loki_logs.py [--url URL] [--hours N] [--limit N]
                            [--errors-only] [--max-patterns N] [--output json|text]

Error patterns recognized (case-insensitive):
  [ERROR] [ERR] [WARN] [WARNING] ERROR WARN WARNING err warn error warning
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone

DEFAULT_LOKI_URL = "http://192.168.88.128:3100"

# LogQL pipeline filter – matches all common error/warn patterns at Loki level
# Covers: [ERROR] [Error] ERROR Error err: WARN [WARNING] warning: etc.
LOKI_ERROR_FILTER = r'|~ `(?i)(\[(error|err|warn|warning)\]|\b(error|err|warning|warn)\b)`'

# Regex to classify a log line's level (Python-side, after fetching)
_LEVEL_RE = re.compile(
    r'(?i)\[(ERROR|ERR|CRITICAL|FATAL)\]'
    r'|\b(ERROR|CRITICAL|FATAL)\b'
    r'|\b(err)\s*[:=]',
    re.IGNORECASE
)
_WARN_RE = re.compile(
    r'(?i)\[(WARN|WARNING)\]|\b(WARN|WARNING)\b',
    re.IGNORECASE
)

# Pattern normalization: strip variable parts to find duplicate messages
_NORM_TS  = re.compile(r'\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?')
_NORM_IP  = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b')
_NORM_HEX = re.compile(r'\b[0-9a-fA-F]{8,}\b')
_NORM_NUM = re.compile(r'\b\d[\d.,]*\b')
_NORM_UUID= re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)


def normalize_line(line: str) -> str:
    """Strip variable parts to produce a canonical pattern for grouping."""
    s = _NORM_TS.sub('<TS>', line)
    s = _NORM_UUID.sub('<UUID>', s)
    s = _NORM_IP.sub('<IP>', s)
    s = _NORM_HEX.sub('<HEX>', s)
    s = _NORM_NUM.sub('<N>', s)
    return s[:160].strip()


def classify_level(line: str) -> str:
    if _LEVEL_RE.search(line):
        return "ERROR"
    if _WARN_RE.search(line):
        return "WARN"
    return "OTHER"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def loki_get(base_url: str, path: str, params: dict = None) -> dict:
    url = f"{base_url}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[ERROR] HTTP {e.code} on {url}: {body[:300]}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"[ERROR] Request failed {url}: {e}", file=sys.stderr)
        return {}


def get_all_labels(base_url: str) -> list:
    data = loki_get(base_url, "/loki/api/v1/labels")
    return data.get("data", [])


def get_label_values(base_url: str, label: str) -> list:
    data = loki_get(base_url, f"/loki/api/v1/label/{label}/values")
    return data.get("data", [])


def get_all_series(base_url: str, start_ns: int, end_ns: int) -> list:
    data = loki_get(base_url, "/loki/api/v1/series", {
        "match[]": '{__name__=~".+"}',
        "start": start_ns,
        "end": end_ns,
    })
    if not data.get("data"):
        data = loki_get(base_url, "/loki/api/v1/series", {
            "start": start_ns,
            "end": end_ns,
        })
    return data.get("data", [])


def build_selector_from_labels(labels: list) -> str:
    if "filename" in labels:
        return '{filename=~".+"}'
    if "job" in labels:
        return '{job=~".+"}'
    if labels:
        return '{' + labels[0] + '=~".+"}'
    return '{__name__=~".+"}'


def query_logs(base_url: str, log_selector: str, start_ns: int, end_ns: int,
               limit: int = 2000, errors_only: bool = True) -> list:
    """Query Loki. When errors_only=True, append LogQL pipeline filter."""
    selector = log_selector
    if errors_only:
        selector = log_selector + " " + LOKI_ERROR_FILTER

    params = {
        "query": selector,
        "start": start_ns,
        "end": end_ns,
        "limit": limit,
        "direction": "forward",
    }
    data = loki_get(base_url, "/loki/api/v1/query_range", params)
    entries = []
    for stream in data.get("data", {}).get("result", []):
        stream_labels = stream.get("stream", {})
        for ts_ns, line in stream.get("values", []):
            entries.append({
                "timestamp_ns": int(ts_ns),
                "timestamp": datetime.fromtimestamp(
                    int(ts_ns) / 1e9, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "labels": stream_labels,
                "line": line,
                "level": classify_level(line),
            })
    return entries


# ---------------------------------------------------------------------------
# Deduplication & grouping for large result sets
# ---------------------------------------------------------------------------

def deduplicate_entries(entries: list, max_patterns: int = 100) -> dict:
    """
    Group log entries by normalized pattern.
    Returns a dict keyed by source file containing pattern summaries.
    """
    # Group by source file
    by_file: dict[str, list] = defaultdict(list)
    for e in entries:
        fname = e["labels"].get("filename", e["labels"].get("job", "unknown"))
        by_file[fname].append(e)

    result = {}
    for fname, file_entries in sorted(by_file.items()):
        # Count occurrences of each normalized pattern
        pattern_counter: Counter = Counter()
        pattern_first: dict = {}
        pattern_last: dict = {}
        pattern_example: dict = {}
        level_counts = Counter()

        for e in file_entries:
            pat = normalize_line(e["line"])
            pattern_counter[pat] += 1
            level_counts[e["level"]] += 1
            if pat not in pattern_first:
                pattern_first[pat] = e["timestamp"]
                pattern_example[pat] = e["line"]
            pattern_last[pat] = e["timestamp"]

        # Take top N patterns by frequency
        top_patterns = pattern_counter.most_common(max_patterns)

        result[fname] = {
            "total_lines": len(file_entries),
            "level_counts": dict(level_counts),
            "top_patterns": [
                {
                    "pattern": pat,
                    "count": cnt,
                    "first_seen": pattern_first[pat],
                    "last_seen": pattern_last[pat],
                    "example": pattern_example[pat],
                }
                for pat, cnt in top_patterns
            ],
            # Keep up to 30 raw lines for timeline display
            "raw_sample": [
                {"timestamp": e["timestamp"], "level": e["level"], "line": e["line"]}
                for e in file_entries[:30]
            ],
        }
    return result


# ---------------------------------------------------------------------------
# Text report builder
# ---------------------------------------------------------------------------

def build_text_report(base_url: str, hours: float, all_entries: list,
                      grouped: dict, errors_only: bool) -> str:
    lines = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_error = sum(1 for e in all_entries if e["level"] == "ERROR")
    total_warn  = sum(1 for e in all_entries if e["level"] == "WARN")
    total_other = len(all_entries) - total_error - total_warn

    lines.append("=== Loki Log Report ===")
    lines.append(f"Address    : {base_url}")
    lines.append(f"Report time: {now_str}")
    lines.append(f"Period     : last {hours} hours")
    lines.append(f"Mode       : {'errors+warnings only' if errors_only else 'all logs'}")
    lines.append(f"Files      : {len(grouped)}")
    lines.append(f"Total lines: {len(all_entries)}")
    lines.append(f"  ERROR    : {total_error}")
    lines.append(f"  WARN     : {total_warn}")
    lines.append(f"  other    : {total_other}")
    lines.append("")

    for fname, info in grouped.items():
        lc = info["level_counts"]
        e_cnt = lc.get("ERROR", 0)
        w_cnt = lc.get("WARN", 0)
        status = "❌" if e_cnt > 0 else ("⚠️" if w_cnt > 0 else "✅")
        lines.append(f"--- {status} [{fname}]  total={info['total_lines']}  ERROR={e_cnt}  WARN={w_cnt} ---")

        if info["top_patterns"]:
            lines.append("  TOP ERROR/WARN PATTERNS (by frequency):")
            for p in info["top_patterns"]:
                lines.append(f"  [{p['count']}x] {p['first_seen']} ~ {p['last_seen']}")
                lines.append(f"       EXAMPLE: {p['example'][:200]}")
                lines.append(f"       PATTERN: {p['pattern'][:160]}")
        lines.append("")

    if all_entries:
        lines.append("--- TIMELINE: first 30 error/warn lines (chronological) ---")
        shown = [e for e in all_entries if e["level"] in ("ERROR", "WARN")][:30]
        for e in shown:
            lines.append(f"  {e['timestamp']}  [{e['level']:5s}]  {e['line'][:200]}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch Loki logs (error/warn filtered) for AI analysis."
    )
    parser.add_argument("--url", default=DEFAULT_LOKI_URL,
                        help=f"Loki base URL (default: {DEFAULT_LOKI_URL})")
    parser.add_argument("--hours", type=float, default=6.0,
                        help="Hours to look back (default: 6)")
    parser.add_argument("--limit", type=int, default=5000,
                        help="Max log lines per stream at Loki level (default: 5000)")
    parser.add_argument("--errors-only", action="store_true", default=True,
                        help="Filter at Loki level: only ERROR/WARN lines (default: True)")
    parser.add_argument("--all-logs", action="store_true", default=False,
                        help="Disable error filter, fetch all log levels")
    parser.add_argument("--max-patterns", type=int, default=100,
                        help="Max unique patterns to show per file (default: 100, range 10-500)")
    parser.add_argument("--output", choices=["json", "text"], default="text")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    errors_only = args.errors_only and not args.all_logs
    now_ns   = int(time.time() * 1e9)
    start_ns = int((time.time() - args.hours * 3600) * 1e9)

    print(f"[INFO] Loki     : {base_url}", file=sys.stderr)
    print(f"[INFO] Period   : last {args.hours} hours", file=sys.stderr)
    print(f"[INFO] Filter   : {'errors+warnings only (LogQL)' if errors_only else 'ALL logs'}", file=sys.stderr)

    labels = get_all_labels(base_url)
    print(f"[INFO] Labels   : {labels}", file=sys.stderr)

    selector = build_selector_from_labels(labels)
    print(f"[INFO] Selector : {selector}", file=sys.stderr)

    series = get_all_series(base_url, start_ns, now_ns)
    print(f"[INFO] Series   : {len(series)}", file=sys.stderr)

    filenames = []
    if "filename" in labels:
        filenames = get_label_values(base_url, "filename")
        print(f"[INFO] Files    : {filenames}", file=sys.stderr)

    all_entries = []
    if filenames:
        for fname in filenames:
            sel = '{filename="' + fname + '"}'
            print(f"[INFO] Fetching : {fname}", file=sys.stderr)
            entries = query_logs(base_url, sel, start_ns, now_ns,
                                 limit=args.limit, errors_only=errors_only)
            print(f"[INFO]   -> {len(entries)} lines", file=sys.stderr)
            all_entries.extend(entries)
    else:
        all_entries = query_logs(base_url, selector, start_ns, now_ns,
                                 limit=args.limit * max(1, len(series)),
                                 errors_only=errors_only)

    all_entries.sort(key=lambda x: x["timestamp_ns"])
    print(f"[INFO] Total    : {len(all_entries)} lines (after Loki-level filter)", file=sys.stderr)

    # Deduplicate / group
    grouped = deduplicate_entries(all_entries, max_patterns=args.max_patterns)

    if args.output == "json":
        result = {
            "loki_url": base_url,
            "query_hours": args.hours,
            "errors_only": errors_only,
            "labels": labels,
            "filenames": filenames,
            "total_lines": len(all_entries),
            "error_count": sum(1 for e in all_entries if e["level"] == "ERROR"),
            "warn_count":  sum(1 for e in all_entries if e["level"] == "WARN"),
            "grouped": grouped,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(build_text_report(base_url, args.hours, all_entries, grouped, errors_only))


if __name__ == "__main__":
    main()
