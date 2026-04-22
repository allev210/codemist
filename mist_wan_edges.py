#!/usr/bin/env python3
"""
mist_wan_edges.py
-----------------
Lists all WAN edge (gateway) devices in a Mist org with:
  - Device identity  (name, model, serial, MAC, site)
  - Connection state (status, IP, firmware, uptime)
  - Last config push (event type, timestamp, result text, warnings/errors)

Usage:
    python3 mist_wan_edges.py
    python3 mist_wan_edges.py --org ORG_ID --token TOKEN
    python3 mist_wan_edges.py --eu           # EU region
    python3 mist_wan_edges.py --json         # machine-readable JSON output
    python3 mist_wan_edges.py --days 60      # extend event look-back (default 90d)

Token can also be set via environment variable:
    export MIST_TOKEN="eyJhbGci..."
    python3 mist_wan_edges.py
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

# ── Config events we care about, in priority order ──────────────────────────
CONFIG_EVENT_PRIORITY = [
    "GW_CONFIGURED",          # config successfully applied on device
    "GW_CONFIG_FAILED",       # config transform / push failed
    "GW_CONFIG_CHANGED_BY_USER",  # a user triggered a config change in Mist UI
]

REGION_BASES = {
    "global": "https://api.mist.com/api/v1",
    "eu":     "https://api.eu.mist.com/api/v1",
    "apac":   "https://api.ac5.mist.com/api/v1",
}

SITE_NAMES = {}   # populated at runtime


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def mist_get(base: str, token: str, path: str, params: dict = None) -> dict:
    url = base + path
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if qs:
            url += "?" + qs
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {e.reason} — {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def fetch_all_pages(base, token, path, result_key="results", extra_params=None):
    """Paginate through a Mist endpoint, handling all response shapes.

    Mist uses several envelope styles depending on endpoint:
      - Plain list              → GET /orgs/{id}/stats/devices  (no wrapper)
      - {"data": [...]}         → stats endpoints
      - {"items": [...]}        → config list endpoints (sites, devices, …)
      - {"results": [...]}      → search/event endpoints
    We try each key in order so callers don't need to specify one.
    """
    import urllib.parse
    all_items = []
    page = 1
    while True:
        params = {"limit": 100, "page": page, **(extra_params or {})}
        data = mist_get(base, token, path, params)
        if isinstance(data, list):
            rows = data
        else:
            # try every known envelope key, caller hint first
            for key in (result_key, "data", "items", "results"):
                if key and key in data and isinstance(data[key], list):
                    rows = data[key]
                    break
            else:
                rows = []
        all_items.extend(rows)
        # Mist paginates via has_more (search) or total_count vs accumulated count (config)
        total = data.get("total_count") or data.get("total") if not isinstance(data, list) else None
        has_more = (not isinstance(data, list) and data.get("has_more", False)) or \
                   (total is not None and len(all_items) < total)
        if not has_more or len(rows) == 0:
            break
        page += 1
    return all_items


# ── Data fetchers ─────────────────────────────────────────────────────────────

def get_org_id(base, token):
    data = mist_get(base, token, "/self")
    privs = data.get("privileges", [])
    if not privs:
        raise RuntimeError("No org privileges found for this token.")
    # Prefer the first org-scope privilege
    for p in privs:
        if p.get("scope") == "org":
            return p["org_id"], p.get("name", p["org_id"])
    return privs[0]["org_id"], privs[0].get("name", privs[0]["org_id"])


def get_sites(base, token, org_id):
    # /orgs/{id}/sites returns {"items": [...], "total_count": N}
    sites = fetch_all_pages(base, token, f"/orgs/{org_id}/sites", result_key="items")
    return {s["id"]: s.get("name", s["id"]) for s in sites}


def get_gateways(base, token, org_id):
    # /orgs/{id}/stats/devices returns {"data": [...], "total": N}
    return fetch_all_pages(
        base, token,
        f"/orgs/{org_id}/stats/devices",
        result_key="data",
        extra_params={"type": "gateway"},
    )


def get_last_config_events(base, token, org_id, mac, days=90):
    """
    Return the most recent config-related events for a device MAC.
    Returns a dict keyed by event type with the latest occurrence.
    """
    import urllib.parse
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - days * 86400

    path = f"/orgs/{org_id}/devices/events/search"
    params = {
        "mac": mac,
        "limit": 50,
        "start": start,
        "end": now,
    }
    try:
        data = mist_get(base, token, path, params)
    except RuntimeError:
        return {}

    results = data.get("results", [])
    # Collect most recent of each relevant event type
    latest = {}
    for ev in results:
        etype = ev.get("type", "")
        if etype in CONFIG_EVENT_PRIORITY:
            if etype not in latest:
                latest[etype] = ev
    return latest


def get_device_config(base, token, site_id, device_id):
    """Fetch device-level config record (modified_time, deviceprofile, etc.).
    /sites/{id}/devices/{id} returns the device object directly — no envelope wrapper."""
    try:
        data = mist_get(base, token, f"/sites/{site_id}/devices/{device_id}")
        # Response is the device object directly; normalise so callers can use .get()
        if isinstance(data, dict):
            return data
        return {}
    except RuntimeError:
        return {}


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def fmt_uptime(seconds) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    return " ".join(parts) or "< 1m"


def config_summary(events: dict) -> dict:
    """
    From the dict of {event_type: event}, pick the most informative
    last-config fields to surface.
    """
    result = {
        "last_config_event":      None,
        "last_config_time":       None,
        "last_config_status":     None,
        "last_config_version":    None,
        "last_config_text":       None,
        "last_user_change_time":  None,
        "last_user_change_audit": None,
    }

    # Best applied / failed event
    for etype in ("GW_CONFIGURED", "GW_CONFIG_FAILED"):
        ev = events.get(etype)
        if ev:
            result["last_config_event"]   = etype
            result["last_config_time"]    = ev.get("timestamp")
            result["last_config_version"] = ev.get("version")
            raw_text = ev.get("text", "")
            # Trim to first 300 chars for display
            result["last_config_text"]    = raw_text.strip()[:300] if raw_text else None
            if etype == "GW_CONFIGURED":
                result["last_config_status"] = "SUCCESS" if "Problems" not in raw_text else "SUCCESS (with warnings)"
            else:
                result["last_config_status"] = "FAILED"
            break

    # User-triggered change
    ev = events.get("GW_CONFIG_CHANGED_BY_USER")
    if ev:
        result["last_user_change_time"]  = ev.get("timestamp")
        result["last_user_change_audit"] = ev.get("audit_id")

    return result


# ── Output ────────────────────────────────────────────────────────────────────

def print_table(gateways_data: list):
    SEP = "─" * 100

    for gw in gateways_data:
        g   = gw["device"]
        cfg = gw["config_summary"]
        dev = gw["device_config"]

        status_icon = "✓" if g.get("status") == "connected" else "✗"
        print(SEP)
        print(f"  {status_icon}  {g.get('name') or '(unnamed)':30s}  {g.get('model',''):12s}  {g.get('mac','')}")
        print()

        # Identity
        print(f"    {'Site':<25} {SITE_NAMES.get(g.get('site_id',''), g.get('site_id','—'))}")
        print(f"    {'Serial':<25} {g.get('serial','—')}")
        print(f"    {'Device ID':<25} {g.get('id','—')}")
        print(f"    {'Mist-managed':<25} {str(g.get('managed', '?'))}")

        # Connection
        print()
        print(f"    {'Status':<25} {g.get('status','—').upper()}")
        print(f"    {'IP address':<25} {g.get('ip','—')}")
        print(f"    {'Firmware':<25} {g.get('version','—')}")
        print(f"    {'Uptime':<25} {fmt_uptime(g.get('uptime'))}")
        print(f"    {'Last seen':<25} {fmt_ts(g.get('last_seen'))}")

        # Config record
        mod_time = dev.get("modified_time")
        print()
        print(f"    {'Config record modified':<25} {fmt_ts(mod_time)}")
        print(f"    {'Device profile':<25} {dev.get('deviceprofile_name') or dev.get('deviceprofile_id') or '—'}")
        print(f"    {'disable_auto_config':<25} {dev.get('disable_auto_config', '—')}")

        # Last config event
        print()
        if cfg["last_config_event"]:
            print(f"    {'Last config event':<25} {cfg['last_config_event']}")
            print(f"    {'Config push time':<25} {fmt_ts(cfg['last_config_time'])}")
            print(f"    {'Config push status':<25} {cfg['last_config_status']}")
            print(f"    {'Firmware at push':<25} {cfg['last_config_version'] or '—'}")
            if cfg["last_config_text"]:
                lines = cfg["last_config_text"].splitlines()
                print(f"    {'Config output':<25} {lines[0]}")
                for line in lines[1:]:
                    if line.strip():
                        print(f"    {'':<25} {line}")
        else:
            print(f"    {'Last config event':<25} No config event found in look-back window")

        if cfg["last_user_change_time"]:
            print(f"    {'Last user change':<25} {fmt_ts(cfg['last_user_change_time'])}")
            print(f"    {'  audit_id':<25} {cfg['last_user_change_audit'] or '—'}")

    print(SEP)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import urllib.parse  # ensure available in scope

    parser = argparse.ArgumentParser(
        description="List WAN edge gateways and their last config status from Mist Cloud"
    )
    parser.add_argument("--token", default=os.environ.get("MIST_TOKEN", ""),
                        help="Mist API token (or set MIST_TOKEN env var)")
    parser.add_argument("--org",   default=os.environ.get("MIST_ORG_ID", ""),
                        help="Org UUID (auto-discovered from token if omitted)")
    parser.add_argument("--eu",    action="store_true", help="Use EU API endpoint")
    parser.add_argument("--apac",  action="store_true", help="Use APAC API endpoint")
    parser.add_argument("--base",  default="", help="Custom API base URL")
    parser.add_argument("--days",  type=int, default=90,
                        help="Event look-back window in days (default: 90)")
    parser.add_argument("--json",  action="store_true", help="Output raw JSON instead of table")
    args = parser.parse_args()

    # Resolve base URL
    if args.base:
        base = args.base.rstrip("/")
    elif args.eu:
        base = REGION_BASES["eu"]
    elif args.apac:
        base = REGION_BASES["apac"]
    else:
        base = REGION_BASES["global"]

    token = args.token
    if not token:
        print("ERROR: No API token supplied. Use --token or set MIST_TOKEN.", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to {base} …", file=sys.stderr)

    # Discover org
    org_id = args.org
    org_name = ""
    if not org_id:
        org_id, org_name = get_org_id(base, token)
    else:
        try:
            info = mist_get(base, token, f"/orgs/{org_id}")
            org_name = info.get("name", org_id)
        except RuntimeError:
            org_name = org_id

    print(f"Org: {org_name} ({org_id})", file=sys.stderr)

    # Sites map
    global SITE_NAMES
    print("Fetching sites …", file=sys.stderr)
    SITE_NAMES = get_sites(base, token, org_id)

    # Gateways
    print("Fetching gateway inventory …", file=sys.stderr)
    gateways = get_gateways(base, token, org_id)
    if not gateways:
        print("No gateway devices found.", file=sys.stderr)
        sys.exit(0)

    print(f"Found {len(gateways)} gateway(s). Fetching config events …", file=sys.stderr)

    results = []
    for gw in gateways:
        mac     = gw.get("mac", "")
        site_id = gw.get("site_id", "")
        dev_id  = gw.get("id", "")
        name    = gw.get("name") or mac

        print(f"  → {name} ({mac}) …", file=sys.stderr)

        events  = get_last_config_events(base, token, org_id, mac, days=args.days)
        cfg_sum = config_summary(events)

        # Fetch device config record for modified_time / profile info
        dev_cfg = {}
        if site_id and dev_id:
            dev_cfg = get_device_config(base, token, site_id, dev_id)

        results.append({
            "device":         gw,
            "config_summary": cfg_sum,
            "config_events":  events,
            "device_config":  dev_cfg,
        })

    # Sort: connected first, then by name
    results.sort(key=lambda r: (
        0 if r["device"].get("status") == "connected" else 1,
        r["device"].get("name") or r["device"].get("mac", "")
    ))

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print()
        print(f"  WAN EDGE GATEWAY REPORT — {org_name}")
        print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  Gateways:  {len(results)}    Look-back: {args.days} days")
        print()
        print_table(results)
        print()

        # Quick summary table
        print("  SUMMARY")
        print(f"  {'Name':<30} {'Model':<12} {'Status':<14} {'Config status':<28} {'Last push'}")
        print(f"  {'─'*30} {'─'*12} {'─'*14} {'─'*28} {'─'*22}")
        for r in results:
            g   = r["device"]
            cfg = r["config_summary"]
            print(
                f"  {(g.get('name') or '(unnamed)'):<30}"
                f" {g.get('model',''):<12}"
                f" {g.get('status','—').upper():<14}"
                f" {(cfg['last_config_status'] or '— no event found'):<28}"
                f" {fmt_ts(cfg['last_config_time'])}"
            )
        print()


if __name__ == "__main__":
    main()
