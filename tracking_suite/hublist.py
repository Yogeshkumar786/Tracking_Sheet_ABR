"""
Hub List loader — the report-facing hub master.
===============================================
The reports read their hubs from here, not from hubmaster.py. hubmaster held 21
hand-entered town-centre pins used to bootstrap discovery; this reads the Hub
List tab, which holds the 400-odd hubs whose pins were derived from real truck
positions AND independently verified against FMS's own coordinate.

So a report that loads hubs through this module is working off hubs that two
independent methods agree on, each with a radius sized to its own yard.

Everything is read from the one locked spreadsheet's Hub List tab. If that tab
is missing or empty, this raises with a clear instruction rather than silently
falling back to the 21 bootstrap pins — a report must never quietly run on the
wrong hub set.
"""
from __future__ import annotations

import math

from tracking_suite import config, sheetio

# Hubs within this of each other are the same BASE REGION — one city's yards.
# Used only to decide "is the truck at its home region" (a truck based at Ambala
# is at its base whether it sits at AML11, AOT11 or AMC11, ~7 km apart). The
# exact hub is still reported separately; this never merges what tracking shows.
# Kept well below the gap to the nearest distinct city (Ambala->Chandigarh ~45 km)
# so two different cities are never grouped.
CLUSTER_KM = 12.0


def _haversine_km(a, b, c, d) -> float:
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(x)))


def _cluster(by_code: dict) -> dict:
    """Union-find hubs within CLUSTER_KM. Returns {code: cluster_id} where the
    cluster_id is the alphabetically-first code in the group (a stable label)."""
    codes = list(by_code)
    parent = {c: c for c in codes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)  # keep the smaller code as root

    for i in range(len(codes)):
        ai, (la1, lo1) = codes[i], by_code[codes[i]]
        for j in range(i + 1, len(codes)):
            la2, lo2 = by_code[codes[j]]
            if _haversine_km(la1, lo1, la2, lo2) <= CLUSTER_KM:
                union(ai, codes[j])
    return {c: find(c) for c in codes}


def load(ss, rows: list | None = None) -> dict:
    """Read the Hub List tab. Returns a dict of lookups the reports use:

        by_code    {CODE: (lat, lon)}
        names      {CODE: hub_name}
        radius     {CODE: radius_m}   per-hub, from the list
        rows       the raw records, for anything else

    Pass `rows` (records already read via a batched call) to skip the read.
    """
    if rows is None:
        try:
            rows = sheetio.read_records(ss, config.TAB_HUB_LIST)
        except RuntimeError:
            rows = []
    if not rows:
        raise RuntimeError(
            f"The '{config.TAB_HUB_LIST}' tab is empty or missing.\n"
            f"  Build it first:  python tracking_suite/run_build_hublist.py\n"
            f"  (it needs a completed hub discovery run behind it)."
        )

    by_code, names, radius, vstatus, pending = {}, {}, {}, {}, set()
    for r in rows:
        code = (r.get("Hub_Code") or "").strip().upper()
        if not code:
            continue
        try:
            lat = float(r.get("Latitude"))
            lon = float(r.get("Longitude"))
        except (TypeError, ValueError):
            # a placeholder row waiting for the human's Google-Maps pin —
            # remember it so the via engine can say "pending" not "unknown"
            pending.add(code)
            nm = (r.get("Hub_Name") or "").strip().upper()
            if nm:
                pending.add(nm)
            continue
        by_code[code] = (lat, lon)
        names[code] = (r.get("Hub_Name") or "").strip()
        vstatus[code] = (r.get("Verify_Status") or "").strip().upper()
        try:
            radius[code] = float(r.get("Radius_M"))
        except (TypeError, ValueError):
            radius[code] = config.HUB_RADIUS_M

    cluster = _cluster(by_code)
    n_clusters = len(set(cluster.values()))
    print(f"  [Hub List] {len(by_code)} verified hub(s) loaded, "
          f"{n_clusters} base region(s)"
          + (f", {len(pending)//2 or len(pending)} pending pin(s)"
             if pending else ""), flush=True)
    return {"by_code": by_code, "names": names, "radius": radius,
            "cluster": cluster, "rows": rows, "vstatus": vstatus,
            "pending": pending}


def home_hub_code(home_hub_name: str, names: dict) -> str:
    """A vehicle's Vehicles-tab home hub -> a hub code in the list.

    The Vehicles tab uses short town names ("Ambala", "Binola"); the list uses
    FMS codes and full names. Match on the name, tolerantly. The Local variants
    share a yard with their parent, so both resolve to the same hub.
    """
    if not home_hub_name:
        return ""
    want = home_hub_name.strip().upper().replace(" LOCAL", "")
    for code, name in names.items():
        n = (name or "").upper()
        if want == code or want in n or n.endswith(want):
            return code
    return ""


def radius_for(code: str, radius: dict) -> float:
    return radius.get(code, config.HUB_RADIUS_M)
