"""
Ghost trips — trips the website never filed, detected from GPS alone.
=====================================================================
The agreed ladder (see the "Ghost Trips & the Colour Code" page):

  1. departure trigger : the trail shows a real stay (>=45 min) inside a hub
                         circle, then the truck leaves and is now >=20 km out
                         and moving. DEP = the minute it exited the circle.
  2. habit prior       : candidate lanes = this vehicle's own RPS history
                         starting from that origin's region, weighted by count.
  3. the road votes    : candidates the current position isn't "between"
                         origin and dest for are eliminated (ellipse gate +
                         must have made progress); passing a via hub that a
                         lane uses multiplies its odds.
  4. confidence        : best / total. >= 80% locks the lane; below that the
                         destination is "forming" and the board says so.

Everything returned is labelled predicted — reconciliation with the real Trip
ID (when the site finally files it) happens in the runner's Extra Trips tabs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tracking_suite.presence import haversine_m

MIN_AWAY_KM = 20.0      # closer than this to the origin hub = not a trip yet
MIN_DWELL_MIN = 45.0    # a shorter hub stay is a pass-through, not a loading
MAX_DEP_AGE_H = 48.0    # a hub exit older than this is another story entirely
LOCK_CONF = 0.80        # at/above this the lane is locked onto the board
DETOUR_MAX = 1.30       # (d(o,p)+d(p,d))/d(o,d) beyond this = not on that lane
MIN_PROGRESS = 0.03     # must have closed >=3% of the origin->dest distance
VIA_BOOST = 3.0         # trail passed a lane's via hub -> that lane's odds x3
VIA_PASS_M = 8000.0     # "passed a via" = any post-departure ping this close


@dataclass
class Ghost:
    origin: str
    dep: datetime
    dest: str = ""
    vias: list = field(default_factory=list)
    confidence: float = 0.0
    locked: bool = False
    candidates: list = field(default_factory=list)   # [(dest, share)] best first
    evidence: str = ""


def _hub_at(atlas, lat: float, lon: float) -> str:
    best, bd = atlas.nearest(lat, lon)   # grid lookup, not a 477-hub scan
    return best if best and bd <= atlas.rad(best) else ""


def _last_loading_stay(trail: list, atlas) -> tuple:
    """(hub_code, exit_time) of the latest >=45-min stay inside a hub circle.
    Shorter contacts (pass-throughs) are skipped and the scan continues back."""
    i = len(trail) - 1
    while i >= 0:
        t, la, lo = trail[i]
        code = _hub_at(atlas, la, lo)
        if not code:
            i -= 1
            continue
        # extend this stay backwards (<=2 stray pings absorbed as drift)
        j, start, stray = i - 1, i, 0
        while j >= 0:
            if _hub_at(atlas, trail[j][1], trail[j][2]) == code:
                start, stray = j, 0
            else:
                stray += 1
                if stray > 2:
                    break
            j -= 1
        dwell_min = (trail[i][0] - trail[start][0]).total_seconds() / 60
        if dwell_min >= MIN_DWELL_MIN:
            exit_t = trail[i + 1][0] if i + 1 < len(trail) else trail[i][0]
            return code, exit_t
        i = start - 1          # pass-through: keep looking further back
    return "", None


def _own_lanes(trips: list, atlas, origin_region: str) -> dict:
    """{(dest_code, vias_tuple): weight} from this vehicle's own history.
    Forward lanes (starting in the origin's region) count in full. Every lane
    ALSO predicts its reverse at half weight — the site rarely files return
    legs, so a truck leaving its delivery hub is almost always heading back
    the way it came. Without this, every backhaul is an unnameable ghost."""
    lanes: dict = {}
    for t in trips:
        segs = t.get("segs") or []
        if len(segs) < 2:
            continue
        o = atlas.resolve_ident(segs[0]) or segs[0]
        d = atlas.resolve_ident(segs[-1]) or segs[-1]
        if not (o and d) or o == d:
            continue
        vias = tuple(v for v in (atlas.resolve_ident(x) or x
                                 for x in segs[1:-1]) if v)
        if atlas.region(o) == origin_region:
            lanes[(d, vias)] = lanes.get((d, vias), 0) + 1.0
        if atlas.region(d) == origin_region:        # return leg of this lane
            rk = (o, tuple(reversed(vias)))
            lanes[rk] = lanes.get(rk, 0) + 0.5
    return lanes


def detect(trail: list, trips: list, atlas, fix, now: datetime) -> Ghost | None:
    """The whole ladder. None = no believable ghost trip."""
    if not trail or fix is None or not fix.fresh:
        return None

    # 1 · departure trigger
    origin, dep = _last_loading_stay(trail, atlas)
    if not origin or not dep:
        return None
    if (now - dep).total_seconds() / 3600 > MAX_DEP_AGE_H:
        return None
    olat, olon = atlas.by_code[origin]
    away_km = haversine_m(fix.lat, fix.lon, olat, olon) / 1000
    if away_km < MIN_AWAY_KM:
        return None

    # 2 · habit prior
    lanes = _own_lanes(trips, atlas, atlas.region(origin))
    g = Ghost(origin=origin, dep=dep)
    if not lanes:
        g.evidence = f"left {origin}, {away_km:.0f} km out, no lane history"
        return g

    # 3 · the road votes
    after_dep = [p for p in trail if p[0] >= dep]
    scored = []
    for (dest, vias), n in lanes.items():
        if dest not in atlas.by_code:
            continue
        dlat, dlon = atlas.by_code[dest]
        d_od = haversine_m(olat, olon, dlat, dlon)
        d_pd = haversine_m(fix.lat, fix.lon, dlat, dlon)
        d_op = away_km * 1000
        if d_od <= 1000:
            continue
        progress = (d_od - d_pd) / d_od
        if progress < MIN_PROGRESS:
            continue                       # not moving toward this dest
        if (d_op + d_pd) / d_od > DETOUR_MAX:
            continue                       # position is off this lane's ellipse
        boost = 1.0
        for vc in vias:
            if vc in atlas.by_code:
                vla, vlo = atlas.by_code[vc]
                if any(haversine_m(la, lo, vla, vlo) <= VIA_PASS_M
                       for _, la, lo in after_dep):
                    boost = VIA_BOOST
                    break
        scored.append((n * boost, n, dest, list(vias)))

    if not scored:
        g.evidence = f"left {origin}, no historical lane fits the heading"
        return g

    scored.sort(reverse=True)
    total = sum(s for s, *_ in scored)
    # merge same-dest lanes for the share the human sees
    by_dest: dict = {}
    for s, n, dest, vias in scored:
        by_dest[dest] = by_dest.get(dest, 0.0) + s
    ranked = sorted(by_dest.items(), key=lambda kv: -kv[1])
    best_s, best_n, best_dest, best_vias = scored[0]
    g.confidence = by_dest[best_dest] / total
    g.candidates = [(d, s / total) for d, s in ranked]
    g.dest = best_dest
    g.vias = best_vias
    g.locked = g.confidence >= LOCK_CONF and best_n >= 2
    g.evidence = (f"{best_n} prior run(s) {origin}->{best_dest}"
                  + (", via confirmed" if best_s > best_n else "")
                  + f", {away_km:.0f} km out")
    return g
