#!/usr/bin/env python3
"""
VictoriaMetrics Metrics Fetcher
Queries VictoriaMetrics (Prometheus-compatible API) and outputs node/infra metrics.

Usage:
  python fetch_vm_metrics.py [--url URL] [--range-hours N] [--step STEP] [--output json|text]
                              [--category node,openstack,ceph,keepalived]

Defaults:
  --url           http://192.168.88.128:8428
  --range-hours   1
  --step          60   (seconds, used for range queries)
  --output        text
  --category      auto (auto-detect from available metrics)
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from collections import defaultdict

VM_BASE_URL = "http://192.168.88.128:8428"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def vm_get(base_url: str, path: str, params: dict = None) -> dict:
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


def instant_query(base_url: str, expr: str) -> list:
    """Run an instant PromQL query. Returns list of {metric, value} dicts."""
    data = vm_get(base_url, "/api/v1/query", {"query": expr})
    return data.get("data", {}).get("result", [])


def range_query(base_url: str, expr: str, start: int, end: int, step: int) -> list:
    """Run a range PromQL query. Returns list of {metric, values} dicts."""
    params = {
        "query": expr,
        "start": start,
        "end": end,
        "step": step,
    }
    data = vm_get(base_url, "/api/v1/query_range", params)
    return data.get("data", {}).get("result", [])


def get_label_values(base_url: str, label: str) -> list:
    data = vm_get(base_url, f"/api/v1/label/{label}/values")
    return data.get("data", [])


def get_metric_names(base_url: str) -> list:
    return get_label_values(base_url, "__name__")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_bytes(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def fmt_pct(v: float) -> str:
    return f"{v:.1f}%"


def scalar(result: list, instance: str = None, label_key: str = "instance") -> float | None:
    """Extract a float scalar from an instant query result."""
    for r in result:
        if instance is None or r.get("metric", {}).get(label_key) == instance:
            try:
                return float(r["value"][1])
            except (KeyError, IndexError, ValueError):
                pass
    return None


def series_avg(values: list) -> float | None:
    """Average of a time-series value list [[ts, val], ...]."""
    nums = []
    for _, v in values:
        try:
            nums.append(float(v))
        except ValueError:
            pass
    return sum(nums) / len(nums) if nums else None


def series_max(values: list) -> float | None:
    nums = []
    for _, v in values:
        try:
            nums.append(float(v))
        except ValueError:
            pass
    return max(nums) if nums else None


# ---------------------------------------------------------------------------
# Node metrics collection
# ---------------------------------------------------------------------------

NODE_QUERIES = {
    # Instant
    "cpu_usage_pct": '100 - avg by(instance)(irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
    "mem_used_pct": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
    "mem_total_bytes": "node_memory_MemTotal_bytes",
    "mem_avail_bytes": "node_memory_MemAvailable_bytes",
    "load1": "node_load1",
    "load5": "node_load5",
    "load15": "node_load15",
    "uptime_seconds": "time() - node_boot_time_seconds",
    "cpu_count": 'count by(instance)(node_cpu_seconds_total{mode="idle"})',
    # Disk (root mount)
    "disk_root_used_pct": '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100',
    "disk_root_total_bytes": 'node_filesystem_size_bytes{mountpoint="/"}',
    "disk_root_avail_bytes": 'node_filesystem_avail_bytes{mountpoint="/"}',
    # Network (non-loopback, aggregate)
    "net_rx_bps": 'sum by(instance)(irate(node_network_receive_bytes_total{device!="lo"}[5m]))',
    "net_tx_bps": 'sum by(instance)(irate(node_network_transmit_bytes_total{device!="lo"}[5m]))',
    # Disk I/O
    "disk_io_util": "avg by(instance)(irate(node_disk_io_time_seconds_total[5m])) * 100",
}

NODE_RANGE_QUERIES = {
    "cpu_usage_pct": '100 - avg by(instance)(irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
    "mem_used_pct": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
    "load1": "node_load1",
    "disk_io_util": "avg by(instance)(irate(node_disk_io_time_seconds_total[5m])) * 100",
}


def collect_node_metrics(base_url: str, start: int, end: int, step: int) -> dict:
    print("[INFO] Collecting node metrics...", file=sys.stderr)

    # Get instance list
    instances = get_label_values(base_url, "instance")
    print(f"[INFO] Discovered instances: {instances}", file=sys.stderr)

    # Instant queries
    instant_results = {}
    for key, expr in NODE_QUERIES.items():
        instant_results[key] = instant_query(base_url, expr)

    # Range queries for trend
    range_results = {}
    for key, expr in NODE_RANGE_QUERIES.items():
        range_results[key] = range_query(base_url, expr, start, end, step)

    # Per-instance data
    nodes = {}
    for inst in instances:
        node = {"instance": inst}

        # Instant values
        for key in NODE_QUERIES:
            node[key] = scalar(instant_results.get(key, []), inst)

        # Trends (avg and max over time range)
        for key in NODE_RANGE_QUERIES:
            series_list = range_results.get(key, [])
            for r in series_list:
                if r.get("metric", {}).get("instance") == inst:
                    node[f"{key}_avg"] = series_avg(r["values"])
                    node[f"{key}_max"] = series_max(r["values"])
                    break

        nodes[inst] = node

    return {"instances": instances, "nodes": nodes}


# ---------------------------------------------------------------------------
# Keepalived metrics
# ---------------------------------------------------------------------------

KEEPALIVED_QUERIES = {
    "keepalived_up": "keepalived_up",
    "vrrp_state": "keepalived_vrrp_state",
    "vrrp_advert_sent": "irate(keepalived_vrrp_advert_sent_total[5m])",
    "vrrp_advert_rcvd": "irate(keepalived_vrrp_advert_rcvd_total[5m])",
}


def collect_keepalived_metrics(base_url: str) -> dict:
    print("[INFO] Collecting keepalived metrics...", file=sys.stderr)
    results = {}
    for key, expr in KEEPALIVED_QUERIES.items():
        results[key] = instant_query(base_url, expr)
    return results


# ---------------------------------------------------------------------------
# Ceph metrics
# ---------------------------------------------------------------------------

CEPH_QUERIES = {
    "health_status": "ceph_health_status",
    "osd_up": "ceph_osd_up",
    "osd_in": "ceph_osd_in",
    "pg_total": "ceph_pg_total",
    "pg_active": "ceph_pg_active",
    "pg_clean": "ceph_pg_clean",
    "cluster_capacity_bytes": "ceph_cluster_capacity_bytes",
    "cluster_used_bytes": "ceph_cluster_used_bytes",
    "io_read_bytes": "irate(ceph_cluster_total_bytes_read[5m])",
    "io_write_bytes": "irate(ceph_cluster_total_bytes_written[5m])",
}


def collect_ceph_metrics(base_url: str) -> dict:
    print("[INFO] Collecting Ceph metrics...", file=sys.stderr)
    results = {}
    for key, expr in CEPH_QUERIES.items():
        results[key] = instant_query(base_url, expr)
    return results


# ---------------------------------------------------------------------------
# OpenStack metrics
# ---------------------------------------------------------------------------

OPENSTACK_QUERIES = {
    "identity_up": "openstack_identity_up",
    "nova_agents_total": "openstack_nova_agents_total",
    "nova_instances_total": "openstack_nova_instances_total",
    "nova_services_total": "openstack_nova_services_total",
    "neutron_networks_total": "openstack_neutron_networks_total",
    "neutron_ports_total": "openstack_neutron_ports_total",
    "cinder_volumes_total": "openstack_cinder_volumes_total",
    "glance_images_total": "openstack_glance_images_total",
}


def collect_openstack_metrics(base_url: str) -> dict:
    print("[INFO] Collecting OpenStack metrics...", file=sys.stderr)
    results = {}
    for key, expr in OPENSTACK_QUERIES.items():
        results[key] = instant_query(base_url, expr)
    return results


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

CATEGORY_PREFIXES = {
    "node": "node_",
    "keepalived": "keepalived_",
    "ceph": "ceph_",
    "openstack": "openstack_",
}


def detect_categories(metric_names: list) -> list:
    detected = []
    name_set = set(metric_names)
    for cat, prefix in CATEGORY_PREFIXES.items():
        if any(n.startswith(prefix) for n in name_set):
            detected.append(cat)
    return detected


# ---------------------------------------------------------------------------
# Text report builder
# ---------------------------------------------------------------------------

def build_text_report(data: dict, base_url: str, hours: float, categories: list) -> str:
    lines = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    start_str = datetime.fromtimestamp(data["query_start"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    end_str = datetime.fromtimestamp(data["query_end"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines.append("=== VictoriaMetrics Metrics Report ===")
    lines.append(f"Address     : {base_url}")
    lines.append(f"Report time : {now_str}")
    lines.append(f"Query range : {start_str}  ~  {end_str}  (last {hours}h)")
    lines.append(f"Categories  : {', '.join(categories)}")
    lines.append(f"All metrics : {data.get('metric_count', 'N/A')} unique metric names")
    lines.append("")

    # --- NODE ---
    if "node" in data:
        nd = data["node"]
        instances = nd.get("instances", [])
        lines.append(f"[NODE METRICS]  instances={len(instances)}")
        lines.append("")
        for inst, n in nd.get("nodes", {}).items():
            lines.append(f"  Instance: {inst}")
            # Uptime
            up = n.get("uptime_seconds")
            if up is not None:
                d, rem = divmod(int(up), 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                lines.append(f"    Uptime        : {d}d {h}h {m}m")
            # CPU
            cpu = n.get("cpu_usage_pct")
            cpu_max = n.get("cpu_usage_pct_max")
            cpu_avg = n.get("cpu_usage_pct_avg")
            cpus = n.get("cpu_count")
            lines.append(f"    CPU cores     : {int(cpus) if cpus else 'N/A'}")
            lines.append(f"    CPU usage now : {fmt_pct(cpu) if cpu is not None else 'N/A'}")
            lines.append(f"    CPU usage avg : {fmt_pct(cpu_avg) if cpu_avg is not None else 'N/A'}  max={fmt_pct(cpu_max) if cpu_max else 'N/A'}")
            # Memory
            mem_pct = n.get("mem_used_pct")
            mem_total = n.get("mem_total_bytes")
            mem_avail = n.get("mem_avail_bytes")
            lines.append(f"    Mem total     : {fmt_bytes(mem_total) if mem_total else 'N/A'}")
            lines.append(f"    Mem used      : {fmt_pct(mem_pct) if mem_pct is not None else 'N/A'}  avail={fmt_bytes(mem_avail) if mem_avail else 'N/A'}")
            # Load
            load1 = n.get("load1")
            load5 = n.get("load5")
            load15 = n.get("load15")
            lines.append(f"    Load 1/5/15   : {load1:.2f if load1 else 'N/A'} / {load5:.2f if load5 else 'N/A'} / {load15:.2f if load15 else 'N/A'}")
            # Disk
            disk_pct = n.get("disk_root_used_pct")
            disk_total = n.get("disk_root_total_bytes")
            disk_avail = n.get("disk_root_avail_bytes")
            lines.append(f"    Disk / total  : {fmt_bytes(disk_total) if disk_total else 'N/A'}")
            lines.append(f"    Disk / used   : {fmt_pct(disk_pct) if disk_pct is not None else 'N/A'}  avail={fmt_bytes(disk_avail) if disk_avail else 'N/A'}")
            # I/O
            io_util = n.get("disk_io_util")
            lines.append(f"    Disk I/O util : {fmt_pct(io_util) if io_util is not None else 'N/A'}")
            # Network
            rx = n.get("net_rx_bps")
            tx = n.get("net_tx_bps")
            lines.append(f"    Net RX/TX     : {fmt_bytes(rx)+'/s' if rx else 'N/A'} / {fmt_bytes(tx)+'/s' if tx else 'N/A'}")
            lines.append("")

    # --- KEEPALIVED ---
    if "keepalived" in data:
        kd = data["keepalived"]
        lines.append("[KEEPALIVED METRICS]")
        up_list = kd.get("keepalived_up", [])
        lines.append(f"  Instances UP: {len(up_list)}")
        for r in kd.get("vrrp_state", []):
            m = r.get("metric", {})
            v = r.get("value", [None, None])[1]
            state_str = "MASTER" if str(v) == "1" else ("BACKUP" if str(v) == "2" else f"state={v}")
            lines.append(f"  VRRP {m.get('name','?')} @ {m.get('instance','?')}: {state_str}")
        lines.append("")

    # --- CEPH ---
    if "ceph" in data:
        cd = data["ceph"]
        lines.append("[CEPH METRICS]")
        health = cd.get("health_status", [])
        if health:
            hv = health[0].get("value", [None, "?"])[1]
            health_label = {"0": "HEALTH_OK", "1": "HEALTH_WARN", "2": "HEALTH_ERR"}.get(str(hv), f"code={hv}")
            lines.append(f"  Cluster health: {health_label}")
        cap = None
        used = None
        for r in cd.get("cluster_capacity_bytes", []):
            try:
                cap = float(r["value"][1])
            except Exception:
                pass
        for r in cd.get("cluster_used_bytes", []):
            try:
                used = float(r["value"][1])
            except Exception:
                pass
        if cap and used:
            lines.append(f"  Capacity      : {fmt_bytes(cap)}  used={fmt_bytes(used)}  ({used/cap*100:.1f}%)")
        osd_up = sum(1 for r in cd.get("osd_up", []) if r.get("value", [None, "0"])[1] == "1")
        osd_in = sum(1 for r in cd.get("osd_in", []) if r.get("value", [None, "0"])[1] == "1")
        lines.append(f"  OSD           : up={osd_up}  in={osd_in}")
        for key in ("pg_total", "pg_active", "pg_clean"):
            vals = cd.get(key, [])
            if vals:
                try:
                    lines.append(f"  {key:12s}: {int(float(vals[0]['value'][1]))}")
                except Exception:
                    pass
        lines.append("")

    # --- OPENSTACK ---
    if "openstack" in data:
        od = data["openstack"]
        lines.append("[OPENSTACK METRICS]")
        for key, label in [
            ("identity_up", "Identity up"),
            ("nova_instances_total", "Nova instances"),
            ("nova_services_total", "Nova services"),
            ("neutron_networks_total", "Neutron networks"),
            ("neutron_ports_total", "Neutron ports"),
            ("cinder_volumes_total", "Cinder volumes"),
            ("glance_images_total", "Glance images"),
        ]:
            res = od.get(key, [])
            if res:
                try:
                    v = int(float(res[0]["value"][1]))
                    lines.append(f"  {label:20s}: {v}")
                except Exception:
                    pass
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch VictoriaMetrics metrics for AI analysis.")
    parser.add_argument("--url", default=VM_BASE_URL, help="VictoriaMetrics base URL")
    parser.add_argument("--range-hours", type=float, default=1.0, help="Hours to look back for range queries (default: 1)")
    parser.add_argument("--step", type=int, default=60, help="Range query step in seconds (default: 60)")
    parser.add_argument("--output", choices=["json", "text"], default="text")
    parser.add_argument("--category", default="auto",
                        help="Comma-separated: node,openstack,ceph,keepalived or 'auto'")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    now = int(time.time())
    range_start = int(now - args.range_hours * 3600)

    print(f"[INFO] VictoriaMetrics: {base_url}", file=sys.stderr)
    print(f"[INFO] Range: last {args.range_hours}h  step={args.step}s", file=sys.stderr)

    # Health check
    try:
        urllib.request.urlopen(f"{base_url}/health", timeout=5)
        print("[INFO] VictoriaMetrics is reachable.", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] Health check failed: {e}", file=sys.stderr)

    # Discover metric names
    all_metrics = get_metric_names(base_url)
    print(f"[INFO] Total metric names: {len(all_metrics)}", file=sys.stderr)

    # Determine categories
    if args.category == "auto":
        categories = detect_categories(all_metrics)
        print(f"[INFO] Auto-detected categories: {categories}", file=sys.stderr)
    else:
        categories = [c.strip() for c in args.category.split(",")]
        print(f"[INFO] Requested categories: {categories}", file=sys.stderr)

    # Collect
    result = {
        "vm_url": base_url,
        "query_start": range_start,
        "query_end": now,
        "range_hours": args.range_hours,
        "metric_count": len(all_metrics),
        "categories_detected": categories,
    }

    if "node" in categories:
        result["node"] = collect_node_metrics(base_url, range_start, now, args.step)

    if "keepalived" in categories:
        result["keepalived"] = collect_keepalived_metrics(base_url)

    if "ceph" in categories:
        result["ceph"] = collect_ceph_metrics(base_url)

    if "openstack" in categories:
        result["openstack"] = collect_openstack_metrics(base_url)

    # Output
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(build_text_report(result, base_url, args.range_hours, categories))


if __name__ == "__main__":
    main()
