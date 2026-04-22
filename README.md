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
