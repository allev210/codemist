#!/usr/bin/env python3
"""
mist_dashboard.py
=================
Fetches live data from the Mist Cloud API and generates a self-contained
HTML dashboard with two tabs:
  1. Switch port overrides  (switch-level port_config)
  2. SSID inventory         (org + site WLANs, active/inactive)

Usage:
    export MIST_API_TOKEN="your_token_here"
    python3 mist_dashboard.py

    # Or pass the token directly:
    python3 mist_dashboard.py --token YOUR_TOKEN

    # Override org / site:
    python3 mist_dashboard.py --org-id <uuid> --site-id <uuid>

    # Custom output file:
    python3 mist_dashboard.py --output my_report.html

Requirements:
    pip install requests
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests is not installed. Run:  pip install requests")


# ── Mist API client ────────────────────────────────────────────────────────────

BASE_URL = "https://api.mist.com/api/v1"


class MistClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        })

    def get(self, path: str, params: dict = None) -> dict:
        url = f"{BASE_URL}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_self(self) -> dict:
        return self.get("/self")

    def get_org_devices(self, org_id: str) -> list:
        """Fetch all switches from org inventory."""
        devices, page = [], 1
        while True:
            data = self.get(f"/orgs/{org_id}/stats/devices", params={
                "type": "switch", "limit": 100, "page": page
            })
            batch = data if isinstance(data, list) else data.get("results", [])
            devices.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return devices

    def get_device_config(self, site_id: str, device_id: str) -> dict:
        """Fetch the switch-level port_config for a single device."""
        try:
            return self.get(f"/sites/{site_id}/devices/{device_id}")
        except requests.HTTPError:
            return {}

    def get_org_wlans(self, org_id: str) -> list:
        """Fetch all org-level WLANs (paginated)."""
        wlans, page = [], 1
        while True:
            data = self.get(f"/orgs/{org_id}/wlans", params={"limit": 100, "page": page})
            batch = data if isinstance(data, list) else data.get("results", [])
            wlans.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return wlans

    def get_site_wlans(self, site_id: str) -> list:
        """Fetch site-level WLANs."""
        try:
            data = self.get(f"/sites/{site_id}/wlans", params={"limit": 100})
            return data if isinstance(data, list) else data.get("results", [])
        except requests.HTTPError:
            return []


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_switches(client: MistClient, org_id: str, site_id: str) -> list:
    """Return list of switches with their port_config overrides."""
    print("  Fetching switch inventory...")
    devices = client.get_org_devices(org_id)
    switches = [d for d in devices if d.get("type") == "switch"]
    print(f"  Found {len(switches)} switch(es). Fetching port configs...")

    result = []
    for sw in switches:
        dev_site = sw.get("site_id", site_id)
        dev_id   = sw.get("id", "")
        config   = client.get_device_config(dev_site, dev_id) if dev_site and dev_id else {}
        port_cfg = config.get("port_config", {})

        ports = []
        for port_id, cfg in port_cfg.items():
            ports.append({
                "id":       port_id,
                "usage":    cfg.get("usage", ""),
                "ae":       f"ae{cfg['ae_idx']}" if cfg.get("aggregated") and cfg.get("ae_idx") is not None else None,
                "esilag":   bool(cfg.get("esilag", False)),
                "critical": bool(cfg.get("critical", False)),
                "desc":     cfg.get("description", ""),
            })

        result.append({
            "name":    sw.get("name", dev_id),
            "model":   sw.get("model", ""),
            "mac":     sw.get("mac", ""),
            "ip":      sw.get("ip", ""),
            "version": sw.get("version", ""),
            "serial":  sw.get("serial", ""),
            "status":  sw.get("status", ""),
            "ports":   ports,
        })

    return result


def fetch_ssids(client: MistClient, org_id: str, site_id: str) -> list:
    """Return normalised SSID list from org + site."""
    print("  Fetching org WLANs...")
    org_wlans  = client.get_org_wlans(org_id)
    print(f"  Found {len(org_wlans)} org-level WLAN(s).")

    print("  Fetching site WLANs...")
    site_wlans = client.get_site_wlans(site_id)
    print(f"  Found {len(site_wlans)} site-level WLAN(s).")

    def normalise(w, scope):
        auth      = w.get("auth", {})
        auth_type = auth.get("type", "open")
        bands_raw = w.get("bands", [])
        if bands_raw == "both":
            bands_raw = ["24", "5"]
        bands = []
        for b in bands_raw:
            if str(b) in ("24", "2.4"):   bands.append("2.4")
            elif str(b) == "5":           bands.append("5")
            elif str(b) == "6":           bands.append("6")
            else:                         bands.append(str(b))
        pairwise = auth.get("pairwise", [])
        return {
            "ssid":    w.get("ssid") or w.get("name", ""),
            "enabled": bool(w.get("enabled", False)),
            "auth":    auth_type,
            "bands":   bands,
            "mpsk":    bool(auth.get("multi_psk_only", False)),
            "wpa3":    "wpa3" in pairwise,
            "priv":    bool(auth.get("private_wlan", False)),
            "vlan":    w.get("vlan_id"),
            "scope":   scope,
            "macAuth": bool(auth.get("enable_mac_auth", False)),
        }

    ssids = [normalise(w, "org")  for w in org_wlans  if w.get("ssid") or w.get("name")]
    ssids += [normalise(w, "site") for w in site_wlans if w.get("ssid") or w.get("name")]
    return ssids


# ── HTML generation ────────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mist — Network Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0d0f12;--surface:#141720;--surface2:#1c2030;
    --border:#2a2f3e;--border2:#353c52;
    --text:#e8eaf2;--text2:#8890a8;--text3:#545c72;
    --accent:#00d4aa;--accent2:#0099ff;
    --warn:#f5a623;--green:#3ddc84;--red:#ff5a5a;--purple:#a78bfa;
    --radius:6px;--radius-lg:10px;
  }}
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6;min-height:100vh}}
  body::before{{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,212,170,0.03) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,170,0.03) 1px,transparent 1px);background-size:40px 40px;pointer-events:none;z-index:0}}
  .container{{max-width:1100px;margin:0 auto;padding:2rem 1.5rem;position:relative;z-index:1}}
  .site-header{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:2rem;gap:1rem}}
  .logo-area{{display:flex;align-items:center;gap:12px}}
  .logo-icon{{width:36px;height:36px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:8px;display:flex;align-items:center;justify-content:center}}
  .logo-icon svg{{width:20px;height:20px;fill:#0d0f12}}
  .site-title{{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:500;color:var(--text2);letter-spacing:.05em;text-transform:uppercase}}
  .site-title span{{color:var(--accent)}}
  .header-right{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
  .status-chip{{display:flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text2);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:5px 10px}}
  .pulse{{width:6px;height:6px;border-radius:50%;background:var(--accent);animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1;box-shadow:0 0 0 0 rgba(0,212,170,.4)}}50%{{opacity:.7;box-shadow:0 0 0 4px rgba(0,212,170,0)}}}}
  .tab-nav{{display:flex;border-bottom:1px solid var(--border);margin-bottom:2rem}}
  .tab-btn{{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:500;padding:10px 20px;color:var(--text3);background:transparent;border:none;border-bottom:2px solid transparent;cursor:pointer;transition:all .15s;letter-spacing:.04em;margin-bottom:-1px}}
  .tab-btn:hover{{color:var(--text2)}}
  .tab-btn.active{{color:var(--accent);border-bottom-color:var(--accent)}}
  .tab-panel{{display:none}}.tab-panel.active{{display:block}}
  .page-heading{{margin-bottom:2rem}}
  .page-heading h1{{font-size:26px;font-weight:300;color:var(--text);letter-spacing:-.02em;margin-bottom:4px}}
  .page-heading h1 em{{font-style:normal;color:var(--accent)}}
  .page-heading p{{font-size:13px;color:var(--text3);font-family:'IBM Plex Mono',monospace}}
  .metrics-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:2rem}}
  .metric-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px 18px;position:relative;overflow:hidden}}
  .metric-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px}}
  .metric-card.c-green::before{{background:var(--green)}}.metric-card.c-accent::before{{background:var(--accent)}}
  .metric-card.c-warn::before{{background:var(--warn)}}.metric-card.c-purple::before{{background:var(--purple)}}
  .metric-card.c-blue::before{{background:var(--accent2)}}.metric-card.c-red::before{{background:var(--red)}}
  .metric-label{{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;font-family:'IBM Plex Mono',monospace;margin-bottom:6px}}
  .metric-value{{font-size:28px;font-weight:300;color:var(--text);font-family:'IBM Plex Mono',monospace;line-height:1}}
  .metric-sub{{font-size:11px;color:var(--text3);margin-top:4px}}
  .filter-bar{{display:flex;align-items:center;gap:8px;margin-bottom:1.5rem;flex-wrap:wrap}}
  .filter-label{{font-size:11px;color:var(--text3);font-family:'IBM Plex Mono',monospace;text-transform:uppercase;letter-spacing:.06em;margin-right:4px}}
  .filter-btn{{font-family:'IBM Plex Mono',monospace;font-size:11px;padding:5px 12px;border-radius:var(--radius);border:1px solid var(--border);background:var(--surface);color:var(--text2);cursor:pointer;transition:all .15s}}
  .filter-btn:hover{{border-color:var(--border2);color:var(--text)}}
  .filter-btn.active{{background:rgba(0,212,170,.1);border-color:var(--accent);color:var(--accent)}}
  .search-input{{font-family:'IBM Plex Mono',monospace;font-size:12px;padding:5px 12px;border-radius:var(--radius);border:1px solid var(--border);background:var(--surface);color:var(--text);outline:none;transition:border-color .15s;margin-left:auto;width:200px}}
  .search-input::placeholder{{color:var(--text3)}}.search-input:focus{{border-color:var(--accent)}}
  .switch-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);margin-bottom:12px;overflow:hidden;transition:border-color .2s}}
  .switch-card:hover{{border-color:var(--border2)}}
  .switch-header{{display:flex;align-items:center;gap:14px;padding:16px 18px;cursor:pointer;user-select:none}}
  .switch-header:hover{{background:rgba(255,255,255,.02)}}
  .sw-indicator{{width:10px;height:10px;border-radius:50%;background:var(--green);flex-shrink:0;box-shadow:0 0 8px rgba(61,220,132,.5)}}
  .sw-info{{flex:1;min-width:0}}
  .sw-name{{font-size:15px;font-weight:500;color:var(--text);display:flex;align-items:center;gap:8px}}
  .model-tag{{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:2px 7px;border-radius:3px;background:var(--surface2);border:1px solid var(--border);color:var(--text3);font-weight:400;letter-spacing:.04em}}
  .sw-meta{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text3);margin-top:2px}}
  .sw-meta span{{color:var(--text2)}}
  .sw-badges{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
  .badge{{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:3px 8px;border-radius:3px;font-weight:500;letter-spacing:.04em}}
  .badge-count{{background:rgba(0,212,170,.1);color:var(--accent);border:1px solid rgba(0,212,170,.2)}}
  .badge-ae{{background:rgba(245,166,35,.1);color:var(--warn);border:1px solid rgba(245,166,35,.2)}}
  .badge-esi{{background:rgba(61,220,132,.1);color:var(--green);border:1px solid rgba(61,220,132,.2)}}
  .chevron{{font-size:10px;color:var(--text3);transition:transform .2s;flex-shrink:0}}
  .chevron.open{{transform:rotate(90deg)}}
  .port-body{{display:block}}.port-body.collapsed{{display:none}}
  .port-table-wrap{{border-top:1px solid var(--border);overflow-x:auto}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  thead th{{padding:8px 18px;text-align:left;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;background:var(--surface2);font-weight:400;white-space:nowrap;border-bottom:1px solid var(--border)}}
  tbody tr{{border-bottom:1px solid var(--border)}}tbody tr:last-child{{border-bottom:none}}
  tbody tr:hover td{{background:rgba(255,255,255,.02)}}
  tbody td{{padding:9px 18px;vertical-align:middle;color:var(--text2)}}
  .port-id{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--accent2);white-space:nowrap}}
  .usage-chip{{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:3px 8px;border-radius:3px;background:rgba(0,153,255,.1);border:1px solid rgba(0,153,255,.2);color:var(--accent2);white-space:nowrap}}
  .usage-chip.trunk{{background:rgba(167,139,250,.1);border-color:rgba(167,139,250,.2);color:var(--purple)}}
  .usage-chip.evpn{{background:rgba(0,212,170,.1);border-color:rgba(0,212,170,.2);color:var(--accent)}}
  .usage-chip.dot1x{{background:rgba(245,166,35,.1);border-color:rgba(245,166,35,.2);color:var(--warn)}}
  .usage-chip.vlan{{background:rgba(61,220,132,.1);border-color:rgba(61,220,132,.2);color:var(--green)}}
  .flags-cell{{display:flex;gap:5px;flex-wrap:wrap;align-items:center}}
  .flag{{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:2px 6px;border-radius:3px;white-space:nowrap}}
  .flag-ae{{background:rgba(245,166,35,.1);color:var(--warn);border:1px solid rgba(245,166,35,.2)}}
  .flag-esi{{background:rgba(61,220,132,.1);color:var(--green);border:1px solid rgba(61,220,132,.2)}}
  .flag-crit{{background:rgba(255,90,90,.1);color:var(--red);border:1px solid rgba(255,90,90,.2)}}
  .desc-cell{{color:var(--text3);font-size:11px;max-width:220px}}.desc-cell.has-desc{{color:var(--text2)}}
  .no-results{{text-align:center;padding:3rem;color:var(--text3);font-family:'IBM Plex Mono',monospace;font-size:12px}}
  .ssid-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}}
  .ssid-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px;transition:border-color .2s;position:relative;overflow:hidden}}
  .ssid-card::before{{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}}
  .ssid-card.enabled::before{{background:var(--green)}}.ssid-card.disabled::before{{background:var(--border2)}}
  .ssid-card:hover{{border-color:var(--border2)}}
  .ssid-top{{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:8px}}
  .ssid-name{{font-size:14px;font-weight:500;color:var(--text);word-break:break-all;line-height:1.3}}
  .ssid-status{{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:2px 8px;border-radius:3px;flex-shrink:0;font-weight:500;letter-spacing:.04em}}
  .ssid-status.on{{background:rgba(61,220,132,.12);color:var(--green);border:1px solid rgba(61,220,132,.25)}}
  .ssid-status.off{{background:rgba(255,255,255,.04);color:var(--text3);border:1px solid var(--border)}}
  .ssid-tags{{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}}
  .ssid-tag{{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:2px 6px;border-radius:3px}}
  .tag-auth-psk{{background:rgba(0,153,255,.1);color:var(--accent2);border:1px solid rgba(0,153,255,.2)}}
  .tag-auth-eap{{background:rgba(167,139,250,.1);color:var(--purple);border:1px solid rgba(167,139,250,.2)}}
  .tag-auth-open{{background:rgba(245,166,35,.1);color:var(--warn);border:1px solid rgba(245,166,35,.2)}}
  .tag-band{{background:rgba(0,212,170,.08);color:var(--accent);border:1px solid rgba(0,212,170,.15)}}
  .tag-mpsk{{background:rgba(61,220,132,.08);color:var(--green);border:1px solid rgba(61,220,132,.15)}}
  .tag-wpa3{{background:rgba(167,139,250,.08);color:var(--purple);border:1px solid rgba(167,139,250,.2)}}
  .tag-private{{background:rgba(255,90,90,.08);color:var(--red);border:1px solid rgba(255,90,90,.2)}}
  .tag-site{{background:rgba(245,166,35,.08);color:var(--warn);border:1px solid rgba(245,166,35,.15)}}
  .tag-mac-auth{{background:rgba(0,212,170,.08);color:var(--accent);border:1px solid rgba(0,212,170,.15)}}
  .ssid-meta{{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text3);margin-top:4px}}
  .ssid-meta span{{color:var(--text2)}}
  .footer{{margin-top:2.5rem;padding-top:1.5rem;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
  .footer-text{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text3)}}
  .footer-text a{{color:var(--text2);text-decoration:none}}.footer-text a:hover{{color:var(--accent)}}
  @media(max-width:640px){{
    .metrics-row{{grid-template-columns:repeat(2,1fr)}};
    .site-header{{flex-direction:column}};
    .search-input{{margin-left:0;width:100%}};
    .ssid-grid{{grid-template-columns:1fr}}
  }}
</style>
</head>
<body>
<div class="container">
  <header class="site-header">
    <div class="logo-area">
      <div class="logo-icon">
        <svg viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
          <rect x="2" y="2" width="7" height="7" rx="1"/>
          <rect x="11" y="2" width="7" height="7" rx="1"/>
          <rect x="2" y="11" width="7" height="7" rx="1"/>
          <rect x="11" y="11" width="7" height="7" rx="1"/>
        </svg>
      </div>
      <div><div class="site-title">Mist Cloud · <span>{org_name}</span></div></div>
    </div>
    <div class="header-right">
      <div class="status-chip"><div class="pulse"></div> live</div>
      <div class="status-chip">org: {org_id_short}</div>
      <div class="status-chip">generated: {generated_at}</div>
    </div>
  </header>

  <nav class="tab-nav">
    <button class="tab-btn active" data-tab="switches">switch port overrides</button>
    <button class="tab-btn" data-tab="ssids">SSIDs</button>
  </nav>

  <!-- TAB 1 -->
  <div class="tab-panel active" id="tab-switches">
    <div class="page-heading">
      <h1>Switch <em>port overrides</em></h1>
      <p>switch-level port_config · {sw_count} device(s) · {port_count} override(s) total</p>
    </div>
    <div class="metrics-row">
      <div class="metric-card c-green"><div class="metric-label">Switches</div><div class="metric-value">{sw_count}</div><div class="metric-sub">connected</div></div>
      <div class="metric-card c-accent"><div class="metric-label">Port overrides</div><div class="metric-value">{port_count}</div><div class="metric-sub">across all switches</div></div>
      <div class="metric-card c-warn"><div class="metric-label">AE bundles</div><div class="metric-value">{ae_count}</div><div class="metric-sub">aggregated ports</div></div>
      <div class="metric-card c-purple"><div class="metric-label">ESI-LAG ports</div><div class="metric-value">{esi_count}</div><div class="metric-sub">esi-lag enabled</div></div>
    </div>
    <div class="filter-bar">
      <span class="filter-label">filter:</span>
      <button class="sw-filter filter-btn active" data-filter="all">all</button>
      <button class="sw-filter filter-btn" data-filter="ae">ae bundles</button>
      <button class="sw-filter filter-btn" data-filter="esilag">esi-lag</button>
      <button class="sw-filter filter-btn" data-filter="evpn">evpn</button>
      <button class="sw-filter filter-btn" data-filter="dot1x">dot1x</button>
      <input class="search-input" type="text" placeholder="search port / usage..." id="sw-search">
    </div>
    <div id="cards-container"></div>
  </div>

  <!-- TAB 2 -->
  <div class="tab-panel" id="tab-ssids">
    <div class="page-heading">
      <h1>SSID <em>inventory</em></h1>
      <p>org-level + site-level wlans · {org_name}</p>
    </div>
    <div class="metrics-row">
      <div class="metric-card c-green"><div class="metric-label">Active SSIDs</div><div class="metric-value" id="ssid-active-count">—</div><div class="metric-sub">enabled</div></div>
      <div class="metric-card c-red"><div class="metric-label">Inactive SSIDs</div><div class="metric-value" id="ssid-inactive-count">—</div><div class="metric-sub">disabled</div></div>
      <div class="metric-card c-blue"><div class="metric-label">Total SSIDs</div><div class="metric-value" id="ssid-total-count">—</div><div class="metric-sub">org + site</div></div>
      <div class="metric-card c-purple"><div class="metric-label">Auth types</div><div class="metric-value">{auth_types}</div><div class="metric-sub">psk · eap · open</div></div>
    </div>
    <div class="filter-bar">
      <span class="filter-label">filter:</span>
      <button class="ssid-filter filter-btn active" data-filter="all">all</button>
      <button class="ssid-filter filter-btn" data-filter="enabled">active</button>
      <button class="ssid-filter filter-btn" data-filter="disabled">inactive</button>
      <button class="ssid-filter filter-btn" data-filter="psk">psk</button>
      <button class="ssid-filter filter-btn" data-filter="eap">eap / dot1x</button>
      <button class="ssid-filter filter-btn" data-filter="open">open</button>
      <input class="search-input" type="text" placeholder="search ssid..." id="ssid-search">
    </div>
    <div class="ssid-grid" id="ssid-grid"></div>
  </div>

  <footer class="footer">
    <span class="footer-text">Generated from Mist Cloud API · {generated_at}</span>
    <span class="footer-text"><a href="https://manage.mist.com" target="_blank">manage.mist.com ↗</a></span>
  </footer>
</div>

<script>
const SWITCHES = {switches_json};
const SSIDS    = {ssids_json};

document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  }});
}});

let swFilter='all', swSearch='';
function usageCls(u){{
  if(!u) return '';
  const l=u.toLowerCase();
  if(l.includes('evpn')) return 'evpn';
  if(l.includes('trunk')) return 'trunk';
  if(l.includes('dot1x')||l.includes('1x')) return 'dot1x';
  if(l.includes('vlan')) return 'vlan';
  return '';
}}
function portOk(p){{
  if(swFilter==='ae') return !!p.ae;
  if(swFilter==='esilag') return p.esilag;
  if(swFilter==='evpn') return p.usage&&p.usage.toLowerCase().includes('evpn');
  if(swFilter==='dot1x') return p.usage&&(p.usage.toLowerCase().includes('dot1x')||p.usage.toLowerCase().includes('1x'));
  return true;
}}
function portSearch(p){{
  if(!swSearch) return true;
  const q=swSearch.toLowerCase();
  return p.id.toLowerCase().includes(q)||(p.usage||'').toLowerCase().includes(q)||(p.desc||'').toLowerCase().includes(q);
}}
function buildCards(){{
  const c=document.getElementById('cards-container');
  c.innerHTML='';
  SWITCHES.forEach((sw,si)=>{{
    const vis=sw.ports.filter(p=>portOk(p)&&portSearch(p));
    if(!vis.length&&(swFilter!=='all'||swSearch)) return;
    const shown=(swFilter==='all'&&!swSearch)?sw.ports:vis;
    const ae=sw.ports.filter(p=>p.ae).length;
    const esi=sw.ports.filter(p=>p.esilag).length;
    const el=document.createElement('div');
    el.className='switch-card';
    el.innerHTML=`
      <div class="switch-header" onclick="toggleCard(${{si}})">
        <div class="sw-indicator"></div>
        <div class="sw-info">
          <div class="sw-name">${{sw.name}}<span class="model-tag">${{sw.model}}</span></div>
          <div class="sw-meta"><span>${{sw.ip}}</span> · <span>${{sw.mac}}</span> · v<span>${{sw.version}}</span> · sn: <span>${{sw.serial}}</span></div>
        </div>
        <div class="sw-badges">
          <span class="badge badge-count">${{sw.ports.length}} overrides</span>
          ${{ae?`<span class="badge badge-ae">${{ae}} ae</span>`:''}}
          ${{esi?`<span class="badge badge-esi">${{esi}} esi-lag</span>`:''}}
        </div>
        <div class="chevron open" id="chev-${{si}}">▶</div>
      </div>
      <div class="port-body" id="body-${{si}}">
        <div class="port-table-wrap"><table>
          <thead><tr><th>Port(s)</th><th>Usage profile</th><th>Flags</th><th>Description</th></tr></thead>
          <tbody>${{shown.length===0
            ?`<tr><td colspan="4"><div class="no-results">no ports match filter</div></td></tr>`
            :shown.map(p=>`<tr>
              <td class="port-id">${{p.id}}</td>
              <td><span class="usage-chip ${{usageCls(p.usage)}}">${{p.usage||'—'}}</span></td>
              <td><div class="flags-cell">
                ${{p.ae?`<span class="flag flag-ae">${{p.ae}}</span>`:''}}
                ${{p.esilag?`<span class="flag flag-esi">ESI-LAG</span>`:''}}
                ${{p.critical?`<span class="flag flag-crit">critical</span>`:''}}
                ${{!p.ae&&!p.esilag&&!p.critical?'<span style="color:var(--text3);font-size:11px">—</span>':''}}
              </div></td>
              <td class="desc-cell ${{p.desc?'has-desc':''}}">${{p.desc||'—'}}</td>
            </tr>`).join('')}}
          </tbody>
        </table></div>
      </div>`;
    c.appendChild(el);
  }});
  if(!c.innerHTML) c.innerHTML='<div class="no-results" style="padding:3rem;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface)">no switches match filter</div>';
}}
function toggleCard(idx){{
  const b=document.getElementById('body-'+idx),ch=document.getElementById('chev-'+idx);
  if(!b) return;
  b.classList.toggle('collapsed',!b.classList.contains('collapsed'));
  ch.classList.toggle('open',!b.classList.contains('collapsed'));
}}
document.querySelectorAll('.sw-filter').forEach(btn=>{{
  btn.addEventListener('click',()=>{{
    document.querySelectorAll('.sw-filter').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');swFilter=btn.dataset.filter;buildCards();
  }});
}});
document.getElementById('sw-search').addEventListener('input',e=>{{swSearch=e.target.value.trim();buildCards();}});

let ssidFilter='all',ssidSearch='';
function bandLabel(bands){{
  if(!bands||!bands.length) return null;
  return bands.map(b=>b==='2.4'?'2.4G':b==='5'?'5G':b==='6'?'6G':b+'G').join('+');
}}
function buildSSIDs(){{
  const q=ssidSearch.toLowerCase();
  const filtered=SSIDS.filter(s=>{{
    if(ssidFilter==='enabled'&&!s.enabled) return false;
    if(ssidFilter==='disabled'&&s.enabled) return false;
    if(ssidFilter==='psk'&&s.auth!=='psk') return false;
    if(ssidFilter==='eap'&&s.auth!=='eap') return false;
    if(ssidFilter==='open'&&s.auth!=='open') return false;
    if(q&&!s.ssid.toLowerCase().includes(q)) return false;
    return true;
  }}).sort((a,b)=>a.enabled===b.enabled?a.ssid.localeCompare(b.ssid):a.enabled?-1:1);
  document.getElementById('ssid-active-count').textContent=SSIDS.filter(s=>s.enabled).length;
  document.getElementById('ssid-inactive-count').textContent=SSIDS.filter(s=>!s.enabled).length;
  document.getElementById('ssid-total-count').textContent=SSIDS.length;
  const grid=document.getElementById('ssid-grid');
  if(!filtered.length){{
    grid.innerHTML='<div class="no-results" style="grid-column:1/-1;padding:3rem;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface)">no SSIDs match filter</div>';
    return;
  }}
  grid.innerHTML=filtered.map(s=>{{
    const bl=bandLabel(s.bands);
    const ac=s.auth==='psk'?'tag-auth-psk':s.auth==='eap'?'tag-auth-eap':'tag-auth-open';
    const al=s.auth==='psk'?'psk':s.auth==='eap'?'802.1x / eap':'open';
    return `<div class="ssid-card ${{s.enabled?'enabled':'disabled'}}">
      <div class="ssid-top">
        <div class="ssid-name">${{s.ssid}}</div>
        <span class="ssid-status ${{s.enabled?'on':'off'}}">${{s.enabled?'active':'inactive'}}</span>
      </div>
      <div class="ssid-tags">
        <span class="ssid-tag ${{ac}}">${{al}}</span>
        ${{bl?`<span class="ssid-tag tag-band">${{bl}}</span>`:''}}
        ${{s.mpsk?'<span class="ssid-tag tag-mpsk">multi-psk</span>':''}}
        ${{s.wpa3?'<span class="ssid-tag tag-wpa3">wpa3</span>':''}}
        ${{s.priv?'<span class="ssid-tag tag-private">private wlan</span>':''}}
        ${{s.macAuth?'<span class="ssid-tag tag-mac-auth">mac-auth</span>':''}}
        ${{s.scope==='site'?'<span class="ssid-tag tag-site">site-only</span>':''}}
      </div>
      <div class="ssid-meta">${{s.vlan?`vlan: <span>${{s.vlan}}</span> · `:''}}scope: <span>${{s.scope}}</span></div>
    </div>`;
  }}).join('');
}}
document.querySelectorAll('.ssid-filter').forEach(btn=>{{
  btn.addEventListener('click',()=>{{
    document.querySelectorAll('.ssid-filter').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');ssidFilter=btn.dataset.filter;buildSSIDs();
  }});
}});
document.getElementById('ssid-search').addEventListener('input',e=>{{ssidSearch=e.target.value.trim();buildSSIDs();}});

buildCards();
buildSSIDs();
</script>
</body>
</html>
"""


def render_html(switches: list, ssids: list, org_name: str, org_id: str) -> str:
    port_count = sum(len(sw["ports"]) for sw in switches)
    ae_count   = sum(1 for sw in switches for p in sw["ports"] if p["ae"])
    esi_count  = sum(1 for sw in switches for p in sw["ports"] if p["esilag"])
    auth_set   = len({s["auth"] for s in ssids})

    return HTML_TEMPLATE.format(
        org_name      = org_name,
        org_id_short  = org_id[:8],
        generated_at  = datetime.now().strftime("%d %b %Y %H:%M"),
        sw_count      = len(switches),
        port_count    = port_count,
        ae_count      = ae_count,
        esi_count     = esi_count,
        auth_types    = auth_set,
        switches_json = json.dumps(switches, ensure_ascii=False),
        ssids_json    = json.dumps(ssids,    ensure_ascii=False),
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Generate a Mist Network Dashboard HTML file."
    )
    p.add_argument("--token",   default=os.environ.get("MIST_API_TOKEN"),
                   help="Mist API token (or set MIST_API_TOKEN env var)")
    p.add_argument("--org-id",  default=None,
                   help="Org UUID (auto-discovered if omitted)")
    p.add_argument("--site-id", default=None,
                   help="Primary site UUID for site-level WLANs (auto-discovered if omitted)")
    p.add_argument("--output",  default="mist_dashboard.html",
                   help="Output HTML file path (default: mist_dashboard.html)")
    return p.parse_args()


def main():
    args = parse_args()

    if not args.token:
        sys.exit(
            "No API token found.\n"
            "  Set the MIST_API_TOKEN environment variable, or pass --token YOUR_TOKEN"
        )

    client = MistClient(args.token)

    # ── Discover org / site ──
    print("Connecting to Mist API...")
    me = client.get_self()
    privileges = me.get("privileges", [])
    if not privileges:
        sys.exit("No org privileges found for this token.")

    # Pick org
    org_id   = args.org_id or privileges[0].get("org_id")
    org_name = privileges[0].get("name", org_id)
    for priv in privileges:
        if priv.get("org_id") == org_id:
            org_name = priv.get("name", org_name)
            break

    # Pick site (first site in org if not specified)
    site_id = args.site_id
    if not site_id:
        sites = client.get(f"/orgs/{org_id}/sites", params={"limit": 1})
        if isinstance(sites, list) and sites:
            site_id = sites[0]["id"]
        elif isinstance(sites, dict):
            items = sites.get("results", [])
            site_id = items[0]["id"] if items else None

    print(f"Org:  {org_name} ({org_id})")
    print(f"Site: {site_id or '(none)'}")

    # ── Fetch data ──
    print("\nFetching data...")
    switches = fetch_switches(client, org_id, site_id or "")
    ssids    = fetch_ssids(client, org_id, site_id or "")

    # ── Render ──
    print("\nRendering HTML...")
    html = render_html(switches, ssids, org_name, org_id)

    out = Path(args.output)
    out.write_text(html, encoding="utf-8")
    print(f"\nDashboard saved → {out.resolve()}")

    # Summary
    active   = sum(1 for s in ssids if s["enabled"])
    inactive = len(ssids) - active
    print(f"\nSummary:")
    print(f"  Switches : {len(switches)} ({sum(len(s['ports']) for s in switches)} port overrides)")
    print(f"  SSIDs    : {len(ssids)} total — {active} active, {inactive} inactive")


if __name__ == "__main__":
    main()
