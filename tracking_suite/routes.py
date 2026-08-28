"""
Route / lane detection — from actual trips, not a typed column.
==============================================================
A vehicle's lane is drawn from what it actually did: its RPS trip history over
the last N days. Nobody types it, so nobody can forget to update it — reassign
a truck and its next run redraws the lane on its own.

    fetch_trips(plates, days)   -> {plate: [trip, ...]} newest first
    detect_lanes(trips_by_veh)  -> {plate: lane}  with base/destination assigned

Each trip's route text carries hub codes: "HYDERABAD(HYD11) / ... / AMBALA(AML11)".
The first code is the origin, the last is the destination. Trips whose origin and
destination are the same (positioning moves, data quirks) are ignored for lane
detection.

WHICH END IS THE BASE?  A shuttle A<->B touches both ends equally, so frequency
alone can't say which is home. But across the WHOLE fleet, the bases (Ambala,
Binola, ...) are the endpoints of dozens of vehicles' lanes, while a destination
like Jammu is the far end for a few. So the base is the fleet-popular endpoint of
the lane; the destination is the other. That is fully automatic — no hub is
hard-coded as "home".

IDLE SINCE comes free with the trips: the most recent trip's END_TIME (POD close)
is when the vehicle last became free. If it has not started a new trip since, it
has been idle since then.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import requests

from tracking_suite.rps import (RPS_REPORT_URL, RPS_REPORT_HEADERS,
                                  RPS_REQUEST_TIMEOUT, RPS_BATCH_SIZE,
                                  _parse_rps_response,
                                  F_RPS, F_VEH, F_ROUTE, F_START, F_END)

requests.packages.urllib3.disable_warnings()

_CODE = re.compile(r'\(([A-Za-z0-9]{2,8})\)')


def _first(rec: dict, fields: tuple) -> str:
    for f in fields:
        v = rec.get(f)
        if v not in (None, "", "null", "None"):
            return str(v).strip()
    return ""


def _parse_dt(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %I:%M:%S %p", "%d-%b-%Y %H:%M:%S",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S",
                "%d-%m-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _seg_id(segment: str) -> str:
    """A route segment -> a hub identifier: the bracket code if present, else the
    cleaned segment name (many routes carry a code on only some segments)."""
    m = _CODE.search(segment or "")
    if m:
        return m.group(1).upper()
    t = re.sub(r'\([^)]*\)', '', segment or '')
    t = re.sub(r'\bSAFEXPRESS\b|\bHUB\b|-?\d+', '', t.upper())
    return " ".join(t.split())


def route_segs(route: str) -> list:
    """Ordered hub identifiers for a route string: [origin, via..., dest]."""
    parts = [p.strip() for p in re.split(r'\s*[/;:]\s*|\s*,\s*', route or "") if p.strip()]
    return [x for x in (_seg_id(p) for p in parts) if x]


def _endpoints(route: str) -> tuple[str, str]:
    segs = route_segs(route)
    if not segs:
        return ("", "")
    if len(segs) == 1:
        return (segs[0], segs[0])
    return segs[0], segs[-1]


# ── Fetch ────────────────────────────────────────────────────────────────────

def fetch_trips(plates: list[str], days: int, now: datetime) -> dict:
    """{plate: [trip, ...]} newest first. trip = {rps, route, origin, dest,
    start_dt, end_dt}. Batched RPS calls (the report takes a list of plates)."""
    from_time = (now - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    to_time = now.strftime("%Y-%m-%d 23:59:59")
    by_veh: dict = defaultdict(list)

    plates = [p for p in plates if p]
    for i in range(0, len(plates), RPS_BATCH_SIZE):
        batch = plates[i:i + RPS_BATCH_SIZE]
        try:
            r = requests.post(RPS_REPORT_URL, headers=RPS_REPORT_HEADERS,
                              json={"from_time": from_time, "to_time": to_time,
                                    "vehicleno": batch},
                              timeout=RPS_REQUEST_TIMEOUT, verify=False)
            r.raise_for_status()
            recs = _parse_rps_response(r.json())
        except Exception as exc:
            print(f"    [RPS] batch {i // RPS_BATCH_SIZE + 1} failed: {str(exc)[:60]}",
                  flush=True)
            continue
        for rec in recs:
            vno = _first(rec, F_VEH).upper()
            if not vno:
                continue
            route = _first(rec, F_ROUTE)
            o, d = _endpoints(route)
            by_veh[vno].append({
                "rps": _first(rec, F_RPS),
                "route": route, "origin": o, "dest": d,
                "segs": route_segs(route),
                "start_dt": _parse_dt(_first(rec, F_START)),
                "end_dt": _parse_dt(_first(rec, F_END)),
            })

    for vno, trips in by_veh.items():
        trips.sort(key=lambda t: t["start_dt"] or datetime.min, reverse=True)
    print(f"  [RPS] {sum(len(v) for v in by_veh.values())} trip(s) for "
          f"{len(by_veh)} vehicle(s) over {days} day(s)", flush=True)
    return by_veh


# ── Detect ───────────────────────────────────────────────────────────────────

def detect_lanes(trips_by_veh: dict) -> dict:
    """{plate: lane}. Two passes: per-vehicle most-run pair, then fleet-wide
    endpoint popularity to decide which end of each lane is the base."""
    # Pass 1: per-vehicle most-run unordered endpoint pair.
    raw: dict = {}
    endpoint_pop: Counter = Counter()
    for vno, trips in trips_by_veh.items():
        pairs = Counter()
        valid = 0
        for t in trips:
            o, d = t["origin"], t["dest"]
            if o and d and o != d:
                pairs[frozenset((o, d))] += 1
                valid += 1
        last = trips[0] if trips else None
        # idle-since = most recent CLOSED trip's end
        idle_since = None
        for t in trips:
            if t["end_dt"]:
                idle_since = t["end_dt"]
                break
        if not pairs:
            raw[vno] = {"endpoints": (), "runs": 0, "trips": len(trips),
                        "last": last, "idle_since": idle_since, "varies": False}
            continue
        top_pair, runs = pairs.most_common(1)[0]
        a, b = tuple(top_pair)
        endpoint_pop[a] += 1
        endpoint_pop[b] += 1
        raw[vno] = {"endpoints": (a, b), "runs": runs, "trips": len(trips),
                    "last": last, "idle_since": idle_since,
                    "varies": len(pairs) > 1,
                    "all_dests": sorted({d for t in trips
                                         for d in [t["dest"]] if d})}

    # Pass 2: base = the fleet-more-popular endpoint of the pair.
    out: dict = {}
    for vno, info in raw.items():
        eps = info["endpoints"]
        if not eps:
            out[vno] = {**info, "base": "", "dest": "", "lane": ""}
            continue
        a, b = eps
        base, dest = (a, b) if endpoint_pop[a] >= endpoint_pop[b] else (b, a)
        out[vno] = {**info, "base": base, "dest": dest,
                    "lane": f"{base}<->{dest}"}
    return out


def hub_of_name(names: dict) -> dict:
    """Convenience: {code: short_name} for display, from the Hub List names."""
    return names
