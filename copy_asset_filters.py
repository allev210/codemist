#!/usr/bin/env python3
"""
copy_asset_filters.py
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
"""

import argparse
import json
import os
import sys
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://api.mist.com"

# Fields that are server-generated and must be stripped before POSTing / PUTting
_READONLY_FIELDS = {"id", "site_id", "org_id", "created_time", "modified_time"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, token: str, payload: Optional[dict] = None) -> dict | list:
    """Thin wrapper around urllib so the script has zero external dependencies."""
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=_headers(token), method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} {exc.reason} – {method} {url}\n{body}"
        ) from exc


def get_self(token: str) -> dict:
    return _request("GET", f"{BASE_URL}/api/v1/self", token)


def list_org_sites(token: str, org_id: str) -> list[dict]:
    url = f"{BASE_URL}/api/v1/orgs/{org_id}/sites?limit=200"
    return _request("GET", url, token)


def list_asset_filters(token: str, site_id: str) -> list[dict]:
    url = f"{BASE_URL}/api/v1/sites/{site_id}/assetfilters?limit=200"
    return _request("GET", url, token)


def create_asset_filter(token: str, site_id: str, payload: dict) -> dict:
    url = f"{BASE_URL}/api/v1/sites/{site_id}/assetfilters"
    return _request("POST", url, token, payload)


def update_asset_filter(token: str, site_id: str, filter_id: str, payload: dict) -> dict:
    url = f"{BASE_URL}/api/v1/sites/{site_id}/assetfilters/{filter_id}"
    return _request("PUT", url, token, payload)


def _clean(f: dict) -> dict:
    """Remove read-only fields so the payload is safe to POST / PUT."""
    return {k: v for k, v in f.items() if k not in _READONLY_FIELDS}


def _pick_interactively(items: list[dict], label: str, multi: bool = False) -> list[dict]:
    """CLI selection helper."""
    print(f"\nAvailable {label}:")
    for i, item in enumerate(items, 1):
        print(f"  [{i:>3}] {item.get('name', item.get('id'))}")

    prompt = "Enter numbers separated by spaces (or 'all'): " if multi else "Enter number: "
    while True:
        raw = input(prompt).strip()
        if multi and raw.lower() == "all":
            return items
        try:
            indices = [int(x) - 1 for x in raw.split()]
            chosen = [items[i] for i in indices]
            if not chosen:
                raise ValueError
            return chosen
        except (ValueError, IndexError):
            print("  ⚠  Invalid selection, try again.")


# ── Core logic ────────────────────────────────────────────────────────────────

def copy_filters(
    token: str,
    src_site_id: str,
    dst_site_ids: list[str],
    filter_names: Optional[list[str]],
    overwrite: bool,
    dry_run: bool,
) -> None:
    # 1. Fetch source filters
    print(f"\n🔍  Fetching asset filters from source site {src_site_id} …")
    all_filters = list_asset_filters(token, src_site_id)

    if not all_filters:
        print("  No asset filters found on the source site. Nothing to copy.")
        return

    # 2. Optionally narrow down by name
    if filter_names:
        name_set = {n.lower() for n in filter_names}
        src_filters = [f for f in all_filters if f.get("name", "").lower() in name_set]
        if not src_filters:
            print(f"  ⚠  None of the requested filter names were found: {filter_names}")
            return
    else:
        src_filters = all_filters

    print(f"  ✔  {len(src_filters)} filter(s) to copy: {[f['name'] for f in src_filters]}")

    # 3. Iterate over destination sites
    results: dict[str, dict] = {}
    for dst_id in dst_site_ids:
        if dst_id == src_site_id:
            print(f"\n  ⏭  Skipping source site {dst_id}")
            continue

        print(f"\n📋  Processing destination site {dst_id} …")

        # Fetch existing filters on the destination to detect name conflicts
        existing = {f["name"]: f for f in list_asset_filters(token, dst_id)}
        site_results = {"created": [], "updated": [], "skipped": [], "errors": []}

        for src_f in src_filters:
            name = src_f["name"]
            payload = _clean(src_f)

            if name in existing:
                if overwrite:
                    existing_id = existing[name]["id"]
                    action = f"UPDATE  '{name}' (id={existing_id})"
                    if dry_run:
                        print(f"    [DRY-RUN] {action}")
                        site_results["updated"].append(name)
                    else:
                        try:
                            update_asset_filter(token, dst_id, existing_id, payload)
                            print(f"    ✔  {action}")
                            site_results["updated"].append(name)
                        except RuntimeError as exc:
                            print(f"    ✖  Failed to update '{name}': {exc}")
                            site_results["errors"].append(name)
                else:
                    print(f"    ⏭  SKIP    '{name}' (already exists – use --overwrite to update)")
                    site_results["skipped"].append(name)
            else:
                action = f"CREATE  '{name}'"
                if dry_run:
                    print(f"    [DRY-RUN] {action}")
                    site_results["created"].append(name)
                else:
                    try:
                        create_asset_filter(token, dst_id, payload)
                        print(f"    ✔  {action}")
                        site_results["created"].append(name)
                    except RuntimeError as exc:
                        print(f"    ✖  Failed to create '{name}': {exc}")
                        site_results["errors"].append(name)

        results[dst_id] = site_results

    # 4. Summary
    print("\n" + "─" * 60)
    print("Summary" + (" (DRY-RUN – no changes were made)" if dry_run else ""))
    print("─" * 60)
    for site_id, r in results.items():
        print(
            f"  Site {site_id}: "
            f"{len(r['created'])} created, "
            f"{len(r['updated'])} updated, "
            f"{len(r['skipped'])} skipped, "
            f"{len(r['errors'])} error(s)"
        )
    print()


# ── Interactive mode ───────────────────────────────────────────────────────────

def interactive_mode(token: str, overwrite: bool, dry_run: bool) -> None:
    # Discover org
    me = get_self(token)
    privileges = me.get("privileges", [])
    orgs = [p for p in privileges if p.get("scope") == "org"]

    if not orgs:
        print("No org-level privileges found for this token. Exiting.")
        sys.exit(1)

    if len(orgs) == 1:
        org = orgs[0]
    else:
        org = _pick_interactively(orgs, "organisations")[0]

    org_id = org["org_id"]
    print(f"\n✔  Using org: {org.get('name', org_id)} ({org_id})")

    # List sites
    sites = list_org_sites(token, org_id)
    if not sites:
        print("No sites found in this org.")
        sys.exit(1)

    # Source site
    print("\nSelect the SOURCE site:")
    src_site = _pick_interactively(sites, "sites")[0]
    src_site_id = src_site["id"]

    # Destination sites
    remaining = [s for s in sites if s["id"] != src_site_id]
    if not remaining:
        print("No other sites to copy to.")
        sys.exit(0)

    print("\nSelect DESTINATION site(s):")
    dst_sites = _pick_interactively(remaining, "sites", multi=True)
    dst_site_ids = [s["id"] for s in dst_sites]

    # Filter selection
    src_filters = list_asset_filters(token, src_site_id)
    if not src_filters:
        print(f"\nNo asset filters found on site '{src_site['name']}'. Exiting.")
        sys.exit(0)

    print(f"\nSelect ASSET FILTER(S) to copy (from '{src_site['name']}'):")
    chosen_filters = _pick_interactively(src_filters, "asset filters", multi=True)
    filter_names = [f["name"] for f in chosen_filters]

    # Confirm
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}About to copy {len(chosen_filters)} filter(s) "
          f"to {len(dst_site_ids)} site(s).")
    if input("Continue? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        sys.exit(0)

    copy_filters(token, src_site_id, dst_site_ids, filter_names, overwrite, dry_run)


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Copy asset filters from one Mist site to other sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--token",
        default=os.environ.get("MIST_API_TOKEN"),
        help="Mist API token (or set MIST_API_TOKEN env var)",
    )
    p.add_argument("--src", metavar="SITE_ID", help="Source site UUID")
    p.add_argument(
        "--dst",
        metavar="SITE_ID",
        nargs="+",
        help="Destination site UUID(s), or 'all' to target every site in the org",
    )
    p.add_argument(
        "--filter-names",
        metavar="NAME",
        nargs="+",
        help="Names of filters to copy (default: all filters)",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Update existing filters with the same name (default: skip them)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making any API changes",
    )
    p.add_argument(
        "--org-id",
        metavar="ORG_ID",
        default=os.environ.get("MIST_ORG_ID"),
        help="Org UUID – required only when --dst all is used (or set MIST_ORG_ID)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Token is always required
    token = args.token
    if not token:
        print("Error: --token / MIST_API_TOKEN is required.")
        sys.exit(1)

    # If no --src / --dst supplied → interactive mode
    if not args.src:
        interactive_mode(token, args.overwrite, args.dry_run)
        return

    src_site_id = args.src

    # Resolve destination sites
    if not args.dst:
        print("Error: --dst is required when --src is supplied.")
        sys.exit(1)

    if args.dst == ["all"]:
        org_id = args.org_id
        if not org_id:
            # Try to discover from self
            me = get_self(token)
            orgs = [p for p in me.get("privileges", []) if p.get("scope") == "org"]
            if len(orgs) == 1:
                org_id = orgs[0]["org_id"]
            else:
                print(
                    "Error: --org-id (or MIST_ORG_ID) is required when using --dst all "
                    "and your token has access to multiple orgs."
                )
                sys.exit(1)
        sites = list_org_sites(token, org_id)
        dst_site_ids = [s["id"] for s in sites if s["id"] != src_site_id]
    else:
        dst_site_ids = args.dst

    copy_filters(
        token=token,
        src_site_id=src_site_id,
        dst_site_ids=dst_site_ids,
        filter_names=args.filter_names,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
