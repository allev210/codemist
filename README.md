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
