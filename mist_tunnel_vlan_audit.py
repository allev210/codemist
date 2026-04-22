#!/usr/bin/env python3
"""
mist_tunnel_vlan_audit.py  (v3)
================================
Audits Mist APs for VLAN conflicts between tunneled and non-tunneled SSIDs
when the AP is connected to a switch access port.

What changed in v3
------------------
- Removed the /orgs/{id}/stats/ports endpoint (empty in many tenants).
- LLDP data is now read directly from the AP device stats:
    ap_stat["lldp_stat"]["chassis_id"]  →  switch MAC
    ap_stat["lldp_stat"]["port_id"]     →  switch port (e.g. ge-0/0/6)
- The switch port type (access vs trunk) is determined by looking up that
  specific port in the switch device config (port_config[port_id].usage)
  and resolving it against the site's network template port-usage profiles.
- Access port = usage profile that carries exactly one VLAN (no trunk).

Logic
-----
1. Fetch all sites → site map.
2. Fetch org mxtunnels → tunnel name map.
3. Per site: GET /sites/{id}/wlans/derived → effective SSID list.
4. Fetch all AP device stats (org-wide).
5. For each AP:
   a. Read lldp_stat → switch MAC + port ID.
   b. Fetch the switch device config → port_config[port_id].usage.
   c. Fetch site network template → port_usages definitions.
   d. Determine if usage profile is access (single vlan_id, type=access)
      or trunk.
   e. Get effective SSIDs for the AP's site.
   f. Classify SSIDs as tunneled / non-tunneled; collect VLANs.
   g. Report overlapping VLANs.

Usage
-----
    export MIST_API_TOKEN="your_token_here"
    python3 mist_tunnel_vlan_audit.py

    python3 mist_tunnel_vlan_audit.py --token YOUR_TOKEN
    python3 mist_tunnel_vlan_audit.py --org-id <uuid> [--site-id <uuid>]
    python3 mist_tunnel_vlan_audit.py --json
    python3 mist_tunnel_vlan_audit.py --csv
    python3 mist_tunnel_vlan_audit.py --conflicts-only

Requirements
------------
    pip install requests
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("requests not installed. Run:  pip install requests")


# ── Colour helpers ─────────────────────────────────────────────────────────────
_IS_TTY = sys.stdout.isatty()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text

RED    = lambda t: _c("31", t)
YELLOW = lambda t: _c("33", t)
GREEN  = lambda t: _c("32", t)
CYAN   = lambda t: _c("36", t)
BOLD   = lambda t: _c("1",  t)
DIM    = lambda t: _c("2",  t)


# ── Mist API client ────────────────────────────────────────────────────────────

BASE = "https://api.mist.com/api/v1"

class MistClient:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Token {token}"})

    def get(self, path, params=None):
        r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_self(self):
        return self.get("/self")

    # ── Org ────────────────────────────────────────────────────────────────────

    def list_sites(self, org_id):
        data = self.get(f"/orgs/{org_id}/sites", {"limit": 200})
        return data if isinstance(data, list) else data.get("results", [])

    def list_mxtunnels(self, org_id):
        try:
            data = self.get(f"/orgs/{org_id}/mxtunnels", {"limit": 200})
            return data if isinstance(data, list) else data.get("results", [])
        except requests.HTTPError:
            return []

    def list_org_ap_stats(self, org_id):
        """All AP device stats org-wide (paginated)."""
        out, page = [], 1
        while True:
            data = self.get(
                f"/orgs/{org_id}/stats/devices",
                {"type": "ap", "limit": 100, "page": page},
            )
            batch = data if isinstance(data, list) else data.get("results", [])
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    # ── Site ───────────────────────────────────────────────────────────────────

    def list_site_wlans_derived(self, site_id):
        """
        GET /sites/{site_id}/wlans/derived
        Returns the merged effective WLAN list (org templates + site overrides).
        """
        try:
            data = self.get(f"/sites/{site_id}/wlans/derived", {"resolve": "true"})
            return data if isinstance(data, list) else data.get("results", [])
        except requests.HTTPError as e:
            print(f"      Warning: derived WLANs unavailable for site {site_id}: {e}")
            return []

    def get_site_ap_stat(self, site_id, ap_id):
        """
        Full AP stats for a single device — includes lldp_stat with switch
        MAC + port ID.
        """
        try:
            data = self.get(f"/sites/{site_id}/stats/devices/{ap_id}")
            return data if isinstance(data, dict) else {}
        except requests.HTTPError:
            return {}

    def get_site_setting(self, site_id):
        """Site settings — contains networktemplate_id if one is applied."""
        try:
            return self.get(f"/sites/{site_id}/setting")
        except requests.HTTPError:
            return {}

    def get_networktemplate(self, org_id, tmpl_id):
        """Fetch a network template for its port_usages definitions."""
        try:
            data = self.get(f"/orgs/{org_id}/networktemplates/{tmpl_id}")
            return data.get("data", data)
        except requests.HTTPError:
            return {}

    def get_switch_config(self, site_id, switch_mac):
        """
        Fetch switch device config for the given MAC.
        Returns the config dict (with port_config) or {}.
        """
        # The device config endpoint needs a device UUID.
        # Construct it from the MAC using Mist's convention:
        #   device_id = 00000000-0000-0000-1000-<mac>
        mac_clean = switch_mac.replace(":", "").replace("-", "").lower()
        dev_id    = f"00000000-0000-0000-1000-{mac_clean}"
        try:
            data = self.get(f"/sites/{site_id}/devices/{dev_id}")
            return data.get("data", data)
        except requests.HTTPError:
            return {}


# ── Port / usage helpers ───────────────────────────────────────────────────────

# Keywords that identify trunk-type port usage profile names
_TRUNK_KEYWORDS = ("trunk", "uplink", "evpn", "ae", "lacp", "fabric", "mist-edge",
                   "to_access", "edge", "nopoe")

def _profile_is_access(profile_name: str, port_usages: dict) -> bool:
    """
    Determine if a port-usage profile name represents an access port.

    Strategy (in order):
    1. Look up the profile in port_usages dict → check `mode` or `vlan_id`.
    2. Fall back to keyword heuristic on the profile name itself.

    A profile is ACCESS when:
    - port_usages[name]["mode"] == "access"  (explicit)
    - OR port_usages[name] has a single vlan_id and no trunk_vlans / vlan_ids
    - OR the profile name contains no trunk-like keywords
    """
    name_lower = profile_name.lower()

    if profile_name in port_usages:
        p = port_usages[profile_name]
        mode = p.get("mode", "")
        if mode == "access":
            return True
        if mode == "trunk":
            return False
        # No explicit mode — inspect vlan fields
        has_trunk_vlans = bool(p.get("all_networks") or p.get("vlan_ids") or
                               p.get("trunk") or p.get("networks"))
        has_single_vlan = bool(p.get("vlan_id") or p.get("vlan_network"))
        if has_trunk_vlans:
            return False
        if has_single_vlan:
            return True

    # Fallback: name heuristic
    return not any(kw in name_lower for kw in _TRUNK_KEYWORDS)


def _access_vlan_from_profile(profile_name: str, port_usages: dict):
    """Return the access VLAN int if determinable, else None."""
    if profile_name in port_usages:
        p = port_usages[profile_name]
        try:
            return int(p.get("vlan_id") or p.get("vlan_network") or 0) or None
        except (ValueError, TypeError):
            return None
    # Try extracting VLAN from common naming patterns like "vlan150", "HQ240"
    import re
    m = re.search(r"(\d{2,4})", profile_name)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 4094:
            return v
    return None


# ── WLAN / VLAN helpers ────────────────────────────────────────────────────────

def _vlans_from_wlan(wlan: dict) -> set:
    """Extract all concrete VLAN IDs from a WLAN config (as ints)."""
    vlans = set()
    def _add(v):
        try:
            vlans.add(int(v))
        except (ValueError, TypeError):
            pass
    _add(wlan.get("vlan_id"))
    for v in wlan.get("vlan_ids") or []:
        _add(v)
    dv = wlan.get("dynamic_vlan") or {}
    if dv.get("enabled"):
        for v in (dv.get("vlans") or {}).keys():
            _add(v)
        for v in dv.get("default_vlan_ids") or []:
            _add(v)
    return vlans


def _is_tunneled(wlan: dict) -> bool:
    return wlan.get("interface", "") in ("mxtunnel", "wxtunnel", "mxtunnel_b")


def _tunnel_label(wlan: dict, tunnel_map: dict) -> str:
    tid = wlan.get("mxtunnel_id")
    if not tid:
        ids = wlan.get("mxtunnel_ids") or []
        tid = ids[0] if ids else None
    if not tid:
        tid = wlan.get("wxtunnel_id")
    return tunnel_map.get(tid, tid or "unknown")


# ── Core audit ─────────────────────────────────────────────────────────────────

def run_audit(client: MistClient, org_id: str, target_site_id: str = None) -> list:

    # 1. Sites
    print(BOLD("\n[1/5] Fetching sites..."))
    all_sites = client.list_sites(org_id)
    site_map  = {s["id"]: s.get("name", s["id"]) for s in all_sites}
    sites_to_audit = (
        [s for s in all_sites if s["id"] == target_site_id]
        if target_site_id else all_sites
    )
    if not sites_to_audit:
        sys.exit(f"No sites found (site-id filter: {target_site_id})")
    print(f"      {len(sites_to_audit)} site(s) in scope.")

    # 2. Mist tunnel definitions
    print(BOLD("[2/5] Fetching Mist tunnel definitions..."))
    tunnels    = client.list_mxtunnels(org_id)
    tunnel_map = {t["id"]: t.get("name", t["id"]) for t in tunnels}
    print(f"      Found {len(tunnels)} tunnel(s): "
          + (", ".join(t.get("name", "?") for t in tunnels) or "none"))

    # 3. Effective WLANs per site
    print(BOLD("[3/5] Fetching effective (derived) WLANs per site..."))
    site_wlans: dict = {}
    for site in sites_to_audit:
        sid   = site["id"]
        wlans = client.list_site_wlans_derived(sid)
        wlans = [w for w in wlans if w.get("ssid") and w.get("enabled", True)]
        site_wlans[sid] = wlans
        print(f"      {site.get('name', sid)}: {len(wlans)} active SSID(s)")

    # 4. AP stats (org-wide, then filter)
    print(BOLD("[4/5] Fetching AP device stats..."))
    all_aps = client.list_org_ap_stats(org_id)
    if target_site_id:
        all_aps = [a for a in all_aps if a.get("site_id") == target_site_id]
    print(f"      Found {len(all_aps)} AP(s).")

    # 5. Per-AP analysis
    print(BOLD("[5/5] Analysing APs (fetching full stats + switch configs)..."))

    # Cache switch configs and port-usage maps to avoid redundant API calls
    switch_config_cache: dict = {}
    port_usage_cache: dict    = {}   # keyed by site_id

    def _get_port_usages(site_id: str) -> dict:
        """
        Build a merged port_usages dict for a site:
          site networktemplate port_usages  (lowest priority)
          + switch device-level port_usages (already in switch config)
        Returns {profile_name: profile_dict}
        """
        if site_id in port_usage_cache:
            return port_usage_cache[site_id]

        usages = {}
        # Try site setting → networktemplate_id
        setting = client.get_site_setting(site_id)
        tmpl_id = setting.get("networktemplate_id")
        if tmpl_id:
            tmpl   = client.get_networktemplate(org_id, tmpl_id)
            usages = dict(tmpl.get("port_usages") or {})

        port_usage_cache[site_id] = usages
        return usages

    def _get_switch_config(site_id: str, switch_mac: str) -> dict:
        key = switch_mac.replace(":", "").lower()
        if key not in switch_config_cache:
            cfg = client.get_switch_config(site_id, key)
            switch_config_cache[key] = cfg
        return switch_config_cache[key]

    findings = []
    for ap in all_aps:
        ap_mac_raw = ap.get("mac", "")
        ap_name    = ap.get("name", ap_mac_raw)
        ap_model   = ap.get("model", "")
        ap_id      = ap.get("id", "")
        site_id    = ap.get("site_id", "")
        site_name  = site_map.get(site_id, site_id)
        status     = ap.get("status", "unknown")

        # ── Full AP stat (for lldp_stat) ───────────────────────────────────────
        # The org-level stats endpoint may include lldp_stat; if not, fetch
        # the site-level detail which always has it.
        lldp = ap.get("lldp_stat") or {}
        if not lldp and status == "connected" and site_id:
            full_stat = client.get_site_ap_stat(site_id, ap_id)
            lldp      = full_stat.get("lldp_stat") or {}

        switch_mac  = (lldp.get("chassis_id") or "").replace(":", "").lower()
        switch_name = lldp.get("system_name", switch_mac or "?")
        port_id     = lldp.get("port_id", "")         # e.g. "ge-0/0/6"
        ap_port     = lldp.get("ap_port_name", "")    # e.g. "eth0"

        # ── Determine port type from switch config ──────────────────────────────
        on_access      = False
        access_vlan    = None
        usage_profile  = None
        port_found     = False

        if switch_mac and port_id and site_id:
            port_found   = True
            sw_cfg       = _get_switch_config(site_id, switch_mac)
            port_cfg     = sw_cfg.get("port_config") or {}
            port_usages  = _get_port_usages(site_id)

            # Merge device-level port_usages into site template usages
            dev_port_usages = sw_cfg.get("port_usages") or {}
            merged_usages   = {**port_usages, **dev_port_usages}

            # Find the port entry — may be a single port or a range key
            # e.g. "ge-0/0/6" or "ge-0/0/8, ge-0/0/10"
            port_entry = None
            for key, val in port_cfg.items():
                ports_in_key = [p.strip() for p in key.split(",")]
                if port_id in ports_in_key:
                    port_entry = val
                    break

            if port_entry:
                usage_profile = port_entry.get("usage", "")
                on_access     = _profile_is_access(usage_profile, merged_usages)
                access_vlan   = _access_vlan_from_profile(usage_profile, merged_usages)
            else:
                # Port not in device-level port_config → uses site/template default
                # Default ports on Mist switches are typically access (default profile)
                usage_profile = "default"
                on_access     = True

        # ── SSIDs for this AP ──────────────────────────────────────────────────
        wlans_for_site = site_wlans.get(site_id, [])
        def _ap_applies(w):
            ap_ids = w.get("ap_ids") or []
            return not ap_ids or ap_id in ap_ids
        wlans_for_ap = [w for w in wlans_for_site if _ap_applies(w)]

        # ── Classify ───────────────────────────────────────────────────────────
        has_tunnel        = any(_is_tunneled(w) for w in wlans_for_ap)
        tunneled_ssids    = []
        nontunneled_ssids = []

        for w in wlans_for_ap:
            vlans = _vlans_from_wlan(w)
            ssid  = w.get("ssid", "")
            if _is_tunneled(w):
                tunneled_ssids.append({
                    "ssid":   ssid,
                    "vlans":  sorted(vlans),
                    "tunnel": _tunnel_label(w, tunnel_map),
                })
            else:
                nontunneled_ssids.append({
                    "ssid":  ssid,
                    "vlans": sorted(vlans),
                })

        # ── VLAN overlap ───────────────────────────────────────────────────────
        tunneled_vlans    = {v for s in tunneled_ssids    for v in s["vlans"]}
        nontunneled_vlans = {v for s in nontunneled_ssids for v in s["vlans"]}
        overlapping       = sorted(tunneled_vlans & nontunneled_vlans)

        # ── Risk ───────────────────────────────────────────────────────────────
        if overlapping and on_access:
            risk = "CONFLICT"
        elif overlapping:
            risk = "OVERLAP_TRUNK"
        elif not has_tunnel:
            risk = "NO_TUNNEL"
        elif not port_found:
            risk = "NO_LLDP"
        elif not on_access:
            risk = "NOT_ACCESS"
        else:
            risk = "OK"

        findings.append({
            "ap_name":           ap_name,
            "ap_mac":            ap_mac_raw,
            "ap_model":          ap_model,
            "ap_id":             ap_id,
            "site_id":           site_id,
            "site_name":         site_name,
            "status":            status,
            "ap_port":           ap_port,
            "switch_name":       switch_name,
            "switch_mac":        switch_mac,
            "switch_port":       port_id,
            "usage_profile":     usage_profile,
            "has_tunnel":        has_tunnel,
            "on_access_port":    on_access,
            "access_vlan":       access_vlan,
            "tunneled_ssids":    tunneled_ssids,
            "nontunneled_ssids": nontunneled_ssids,
            "overlapping_vlans": overlapping,
            "risk":              risk,
        })

        col  = {"CONFLICT": RED, "OVERLAP_TRUNK": YELLOW}.get(risk, DIM)
        icon = {"CONFLICT": "⚠", "OVERLAP_TRUNK": "~",
                "OK": "✓", "NOT_ACCESS": "✓"}.get(risk, "-")
        print(f"      {col(icon)} {ap_name:<22} [{risk}]  "
              f"sw:{switch_name}/{port_id or '?'}  "
              f"usage:{usage_profile or '?'}  "
              f"overlap:{overlapping or '—'}")

    return findings


# ── Output formatters ──────────────────────────────────────────────────────────

RISK_COLOUR = {
    "CONFLICT":      RED,
    "OVERLAP_TRUNK": YELLOW,
    "NOT_ACCESS":    GREEN,
    "NO_LLDP":       DIM,
    "NO_TUNNEL":     DIM,
    "OK":            GREEN,
}
RISK_ICON = {
    "CONFLICT":      "⚠  CONFLICT      ",
    "OVERLAP_TRUNK": "~  OVERLAP_TRUNK ",
    "NOT_ACCESS":    "✓  NOT_ACCESS    ",
    "NO_LLDP":       "?  NO_LLDP       ",
    "NO_TUNNEL":     "-  NO_TUNNEL     ",
    "OK":            "✓  OK            ",
}


def print_report(findings):
    conflicts = [f for f in findings if f["risk"] == "CONFLICT"]
    overlaps  = [f for f in findings if f["risk"] == "OVERLAP_TRUNK"]

    print()
    print(BOLD("━" * 72))
    print(BOLD("  Mist AP Tunnel / VLAN Conflict Audit"))
    print(BOLD(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M')}"))
    print(BOLD("━" * 72))
    print()
    n = len(findings)
    print(f"  APs audited      : {BOLD(str(n))}")
    print(f"  CONFLICTS        : "
          f"{RED(str(len(conflicts))) if conflicts else GREEN('0')}"
          "  (access port + VLAN in both tunneled & non-tunneled SSID)")
    print(f"  OVERLAP on trunk : "
          f"{YELLOW(str(len(overlaps))) if overlaps else GREEN('0')}")
    print(f"  Clean / other    : {GREEN(str(n - len(conflicts) - len(overlaps)))}")
    print()

    for f in findings:
        risk = f["risk"]
        col  = RISK_COLOUR.get(risk, DIM)
        icon = RISK_ICON.get(risk, risk)

        print(col(f"  {icon}") + "  " + BOLD(f["ap_name"]) +
              DIM(f"  ({f['ap_mac']})  {f['ap_model']}"))
        print(DIM(f"                     Site  : {f['site_name']}"
                  f"  |  Status: {f['status']}"))

        if f["switch_port"]:
            access_str = (YELLOW("ACCESS") + f" (VLAN {f['access_vlan']})"
                          if f["on_access_port"] and f["access_vlan"]
                          else YELLOW("ACCESS") if f["on_access_port"]
                          else "TRUNK")
            print(DIM("                     Uplink: ") +
                  f"{f['switch_name']}/{f['switch_port']}"
                  f"  profile:{DIM(f['usage_profile'] or '?')}"
                  f"  type:{access_str}")
        else:
            print(DIM("                     Uplink: no LLDP data available"))

        if f["tunneled_ssids"]:
            print(DIM("                     Tunneled SSIDs:"))
            for s in f["tunneled_ssids"]:
                vl = ", ".join(str(v) for v in s["vlans"]) or "—"
                print(DIM(f"                       • {s['ssid']:<28}") +
                      f"VLANs: {CYAN(vl)}" + DIM(f"  → {s['tunnel']}"))

        if f["nontunneled_ssids"]:
            print(DIM("                     Non-tunneled SSIDs:"))
            for s in f["nontunneled_ssids"]:
                vl = ", ".join(str(v) for v in s["vlans"]) or "—"
                print(DIM(f"                       • {s['ssid']:<28}") +
                      f"VLANs: {CYAN(vl)}")

        if f["overlapping_vlans"]:
            ov = ", ".join(str(v) for v in f["overlapping_vlans"])
            print(col(f"                     ▶ Overlapping VLANs: {ov}"))

        print()

    if conflicts:
        print(BOLD(RED("━" * 72)))
        print(BOLD(RED("  CONFLICT SUMMARY")))
        print(BOLD(RED("━" * 72)))
        hdr = (f"  {'AP':<22} {'Site':<18} {'Switch / Port':<24} "
               f"{'Overlap VLANs':<16} {'Tunneled SSIDs'}")
        print(hdr)
        print("  " + "─" * 108)
        for f in conflicts:
            sw   = f"{f['switch_name']}/{f['switch_port']}"
            ov   = ", ".join(str(v) for v in f["overlapping_vlans"])
            ts   = ", ".join(s["ssid"] for s in f["tunneled_ssids"])
            print(f"  {f['ap_name']:<22} {f['site_name']:<18} {sw:<24} "
                  f"{RED(ov):<16} {ts}")
        print()


def emit_json(findings):
    print(json.dumps(findings, indent=2, default=str))


def emit_csv(findings):
    w = csv.writer(sys.stdout)
    w.writerow([
        "ap_name", "ap_mac", "ap_model", "site_name", "status",
        "switch_name", "switch_port", "usage_profile",
        "has_tunnel", "on_access_port", "access_vlan",
        "risk", "overlapping_vlans",
        "tunneled_ssids", "tunneled_vlans",
        "nontunneled_ssids", "nontunneled_vlans",
    ])
    for f in findings:
        w.writerow([
            f["ap_name"], f["ap_mac"], f["ap_model"],
            f["site_name"], f["status"],
            f["switch_name"], f["switch_port"], f["usage_profile"],
            f["has_tunnel"], f["on_access_port"], f.get("access_vlan", ""),
            f["risk"],
            ";".join(str(v) for v in f["overlapping_vlans"]),
            ";".join(s["ssid"]  for s in f["tunneled_ssids"]),
            ";".join(",".join(str(v) for v in s["vlans"]) for s in f["tunneled_ssids"]),
            ";".join(s["ssid"]  for s in f["nontunneled_ssids"]),
            ";".join(",".join(str(v) for v in s["vlans"]) for s in f["nontunneled_ssids"]),
        ])


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Audit Mist APs for tunnel/VLAN conflicts on access ports."
    )
    p.add_argument("--token",   default=os.environ.get("MIST_API_TOKEN"),
                   help="Mist API token  (or set MIST_API_TOKEN env var)")
    p.add_argument("--org-id",  default=None,
                   help="Org UUID  (auto-discovered from token if omitted)")
    p.add_argument("--site-id", default=None,
                   help="Limit audit to a single site UUID")
    p.add_argument("--json",    action="store_true", help="Output JSON to stdout")
    p.add_argument("--csv",     action="store_true", help="Output CSV to stdout")
    p.add_argument("--conflicts-only", action="store_true",
                   help="Only show APs with CONFLICT or OVERLAP_TRUNK risk")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.token:
        sys.exit("No API token.\n  Set MIST_API_TOKEN=<token>  or --token <token>")

    client = MistClient(args.token)
    print(BOLD("Connecting to Mist API..."))
    try:
        me = client.get_self()
    except requests.HTTPError as e:
        sys.exit(f"Authentication failed: {e}")

    privileges = me.get("privileges", [])
    if not privileges:
        sys.exit("No org privileges found for this token.")

    org_id   = args.org_id or privileges[0].get("org_id")
    org_name = next(
        (p.get("name", org_id) for p in privileges if p.get("org_id") == org_id),
        org_id,
    )
    print(f"  Org : {org_name} ({org_id})")
    if args.site_id:
        print(f"  Site: {args.site_id}")

    findings = run_audit(client, org_id, args.site_id)

    if args.conflicts_only:
        findings = [f for f in findings if f["risk"] in ("CONFLICT", "OVERLAP_TRUNK")]

    if args.json:
        emit_json(findings)
    elif args.csv:
        emit_csv(findings)
    else:
        print_report(findings)

    if any(f["risk"] == "CONFLICT" for f in findings):
        sys.exit(1)


if __name__ == "__main__":
    main()
