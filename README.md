Information on how to use scripts in this folder. Tools for Mist AI dashboard:

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

**copy_asset_filters.py**
─────────────────────
Copy asset filters from a source Mist site to one or more destination sites.

Mist API endpoints used
  GET  /api/v1/sites/{site_id}/assetfilters          – list all asset filters on a site
  GET  /api/v1/sites/{site_id}/assetfilters/{id}     – get a single asset filter
  POST /api/v1/sites/{site_id}/assetfilters          – create an asset filter
  PUT  /api/v1/sites/{site_id}/assetfilters/{id}     – update an existing asset filter

Usage examples
  # Interactive mode – prompts for source site, destination sites and filter selection
  python copy_asset_filters.py

  # Copy ALL filters from site A to sites B and C (non-interactive)
  python copy_asset_filters.py \\
      --token  <YOUR_API_TOKEN> \\
      --src    <SOURCE_SITE_ID> \\
      --dst    <SITE_B_ID> <SITE_C_ID>

  # Copy only specific filters (by name) to every other site in the org
  python copy_asset_filters.py \\
      --token  <YOUR_API_TOKEN> \\
      --src    <SOURCE_SITE_ID> \\
      --dst    all \\
      --filter-names "BLE Beacon" "HR Badge"

  # Dry-run: show what would happen without making any changes
  python copy_asset_filters.py \\
      --token  <YOUR_API_TOKEN> \\
      --src    <SOURCE_SITE_ID> \\
      --dst    all \\
      --dry-run

  # Update existing filters with the same name (instead of skipping them)
  python copy_asset_filters.py \\
      --token  <YOUR_API_TOKEN> \\
      --src    <SOURCE_SITE_ID> \\
      --dst    all \\
      --overwrite

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
