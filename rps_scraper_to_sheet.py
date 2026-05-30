"""
RPS Scraper → Per-Hub Trip Sheets (direct API version)

For a specified date range, pulls all completed trips from the FMS
RPS Report API and distributes each to its hub's monthly MIS sheet,
dedup'd by RPS_Number.

Replaces the old Playwright + Excel download approach with one direct
HTTP POST. Works for any date range; reuses the same fetcher, hub-
distribution logic, and sheet writers as the live tracker, so the
output format is identical and the two scripts never collide.

USAGE
─────
  python rps_scraper_to_sheet.py
      → last 10 days  (default)

  python rps_scraper_to_sheet.py --days 30
      → last 30 days

  python rps_scraper_to_sheet.py --from 2026-04-01 --to 2026-04-30
      → explicit window  (date or datetime, inclusive)

  python rps_scraper_to_sheet.py --dry-run
      → fetch + count, but do not write

The script needs the master spreadsheet to resolve `vehicle → hub` and
to pick up `Route SLA` rows for Route_TAT. It does NOT touch the master
Tracking tab.
"""
from __future__ import annotations
import argparse
import sys
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Reuse everything from the main tracker. Source of truth lives there.
from fms_to_sheets import (
    connect,
    fetch_all_completed_trips,
    get_or_create_tab,
    HUB_LOCATIONS_HEADERS, HUB_LOCATIONS_TAB,
    load_hub_coords_tab,
    load_lookup_tab,
    load_route_sla_tab, ROUTE_SLA_HEADERS, ROUTE_SLA_TAB,
    load_vehicles_tab, VEHICLES_HEADERS, VEHICLES_TAB,
    ROUTE_CODES_TAB,
    safety_net_completed_trips,
)


def _parse_dt_arg(s: str, name: str) -> datetime:
    """Parse a CLI date/datetime arg. Accepts YYYY-MM-DD or YYYY-MM-DD HH:MM:SS."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(
        f"--{name}: '{s}' is not YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'"
    )


def main():
    ap = argparse.ArgumentParser(
        description=("Backfill completed RPS trips into per-hub MIS sheets "
                     "for a given date range."),
    )
    ap.add_argument("--days", type=int, default=10,
                    help="Days back from now (default 10). Ignored if --from/--to set.")
    ap.add_argument("--from", dest="from_dt", type=lambda s: _parse_dt_arg(s, "from"),
                    default=None, help="Start date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)")
    ap.add_argument("--to",   dest="to_dt",   type=lambda s: _parse_dt_arg(s, "to"),
                    default=None, help="End date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and print summary, do NOT write to sheets.")
    args = ap.parse_args()

    # Resolve window
    if args.from_dt and args.to_dt:
        from_dt, to_dt = args.from_dt, args.to_dt
        if from_dt >= to_dt:
            ap.error("--from must be earlier than --to")
        days_label = (f"{from_dt.strftime('%Y-%m-%d')}  →  "
                      f"{to_dt.strftime('%Y-%m-%d')}")
    elif args.from_dt or args.to_dt:
        ap.error("--from and --to must be used together (or use --days)")
    else:
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=args.days)
        days_label = f"last {args.days} day(s)"

    print(f"[rps_scraper] window: {days_label}", flush=True)
    print(f"[rps_scraper] mode:   {'DRY RUN' if args.dry_run else 'WRITE'}",
          flush=True)

    # ── Load master sheet side tabs (vehicle_hub, vt_map, sla_map, hub_map) ──
    print("\n[rps_scraper] Loading master sheet lookups…", flush=True)
    ss, _ = connect()                                        # master file
    veh_ws  = get_or_create_tab(ss, VEHICLES_TAB,    VEHICLES_HEADERS)
    sla_ws  = get_or_create_tab(ss, ROUTE_SLA_TAB,   ROUTE_SLA_HEADERS)
    rc_ws   = get_or_create_tab(ss, ROUTE_CODES_TAB, ["Hub_Name", "Hub_Code"])

    vt_map, vehicle_hub, _vehicle_route, _present = load_vehicles_tab(veh_ws)
    sla_map, _present_routes = load_route_sla_tab(sla_ws)
    # Route Codes → {Hub_Name: Hub_Code}. Required so "INDORE-11" (no parens)
    # resolves to "IDR11" just like the live tracker does in build_route_code().
    rc_dict = load_lookup_tab(rc_ws)
    hub_map = {k.upper(): v for k, v in rc_dict.items()}

    print(f"  Vehicles tab:  {len(vehicle_hub)} vehicle→hub mappings", flush=True)
    print(f"  Route SLA:     {len(sla_map)} route(s) with TAT hours", flush=True)
    print(f"  Route Codes:   {len(hub_map)} hub-name→code mappings", flush=True)

    # ── Dispatch to the shared safety-net writer ─────────────────────────────
    print("\n[rps_scraper] Calling RPS Report API…", flush=True)
    added = safety_net_completed_trips(
        vehicle_hub = vehicle_hub,
        vt_map      = vt_map,
        sla_map     = sla_map,
        hub_map     = hub_map,
        days_back   = args.days,
        from_dt     = from_dt,
        to_dt       = to_dt,
        dry_run     = args.dry_run,
    )

    print(f"\n[rps_scraper] Done. {added} trip row(s) inserted or updated.", flush=True)


if __name__ == "__main__":
    main()
