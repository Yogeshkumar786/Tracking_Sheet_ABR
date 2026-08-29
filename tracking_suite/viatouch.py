"""
Via touching engine — entry/exit per via hub, hiccup-tolerant.
==============================================================
The agreed ladder (one via at a time, road order, each judged on its own
evidence):

  CONFIRMED     pings inside the via circle, tight ping gaps at both edges.
  INTERPOLATED  a stay is visible but one edge sits inside a GPS gap — that
                edge is estimated from the last known point + road distance /
                the truck's own speed, clamped inside the gap.
  INFERRED      the whole via vanished inside a blackout. Geometry proves the
                passage (the road between the bracketing fixes runs through
                the via); the TIME BUDGET splits the gap: driving those km at
                the truck's own speed needs X hours, the gap lasted Y — the
                excess (Y - X) is the dwell, charged to the via, because via
                hubs are exactly where the fleet halts.

  >= 20 min inside the circle = "stopped" (touched); less = "passed through".

Results per via: stopped | passed through | stopped (in GPS gap) |
at via now | not reached | no data | unknown hub.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from tracking_suite import estimator
from tracking_suite.presence import haversine_m

TOUCH_MIN_H = 20 / 60      # >= 20 min inside = stopped / touched
EDGE_TIGHT_MIN = 15.0      # ping gap <= this at an edge = edge is confirmed
BRACKET_MAX = 1.15         # (d1v+dv2)/d12 <= this = the road passes the via
NEAR_MISS_KM = 25.0        # got this close without evidence -> "no data"
FALLBACK_KMH = 30.0        # speed floor when the trail can't support an estimate


@dataclass
class ViaVisit:
    via: str
    entry: datetime | None = None
    exit: datetime | None = None
    dwell_h: float | None = None
    confidence: str = ""       # CONFIRMED | INTERPOLATED | INFERRED | ""
    result: str = ""


def _speed(trail: list, at: datetime) -> float:
    if len(trail) >= 3:
        v, _, _ = estimator.speed_state(trail, at)
        if v and v > 5:
            return v
    return FALLBACK_KMH


def _assess_one(vc: str, vlat: float, vlon: float, radius: float,
                pts: list, dep: datetime | None, now: datetime) -> ViaVisit:
    vis = ViaVisit(via=vc)
    if not pts:
        vis.result = "no data"
        return vis
    inr = [haversine_m(la, lo, vlat, vlon) <= radius for _, la, lo in pts]

    # ── a visible stay: pings inside the circle ─────────────────────────────
    if any(inr):
        i = len(pts) - 1
        while i >= 0 and not inr[i]:
            i -= 1
        end = i
        j, start, stray = i - 1, i, 0
        while j >= 0:
            if inr[j]:
                start, stray = j, 0
            else:
                stray += 1
                if stray > 2:
                    break
            j -= 1
        conf = "CONFIRMED"
        entry = pts[start][0]
        if start > 0:
            gap_min = (pts[start][0] - pts[start - 1][0]).total_seconds() / 60
            if gap_min > EDGE_TIGHT_MIN:        # entry edge inside a hiccup
                v = _speed(pts[:start], pts[start - 1][0])
                d_km = haversine_m(pts[start - 1][1], pts[start - 1][2],
                                   vlat, vlon) / 1000
                est = pts[start - 1][0] + timedelta(hours=d_km / v)
                if pts[start - 1][0] < est < pts[start][0]:
                    entry = est
                conf = "INTERPOLATED"
        elif dep and (pts[0][0] - dep).total_seconds() > 3600:
            conf = "INTERPOLATED"               # trail starts too late to know

        if end == len(pts) - 1:                 # still inside right now
            vis.entry, vis.exit = entry, None
            vis.dwell_h = max(0.0, (now - entry).total_seconds() / 3600)
            vis.confidence, vis.result = conf, "at via now"
            return vis

        exit_t = pts[end + 1][0]
        gap_min = (pts[end + 1][0] - pts[end][0]).total_seconds() / 60
        if gap_min > EDGE_TIGHT_MIN:            # exit edge inside a hiccup
            v = _speed(pts[:end + 1], pts[end][0])
            d_km = haversine_m(pts[end + 1][1], pts[end + 1][2],
                               vlat, vlon) / 1000
            est = pts[end + 1][0] - timedelta(hours=d_km / v)
            if pts[end][0] < est < pts[end + 1][0]:
                exit_t = est
            conf = "INTERPOLATED"
        vis.entry, vis.exit = entry, exit_t
        vis.dwell_h = max(0.0, (exit_t - entry).total_seconds() / 3600)
        vis.confidence = conf
        vis.result = "stopped" if vis.dwell_h >= TOUCH_MIN_H else "passed through"
        return vis

    # ── no ping inside: does the road between two fixes run through it? ─────
    best = None
    for (t1, la1, lo1), (t2, la2, lo2) in zip(pts, pts[1:]):
        d12 = haversine_m(la1, lo1, la2, lo2)
        if d12 < 1000:
            continue
        d1v = haversine_m(la1, lo1, vlat, vlon)
        dv2 = haversine_m(vlat, vlon, la2, lo2)
        ratio = (d1v + dv2) / d12
        if ratio <= BRACKET_MAX and (best is None or ratio < best[0]):
            best = (ratio, t1, la1, lo1, t2, dv2, d1v)
    if best:
        _, t1, la1, lo1, t2, dv2, d1v = best
        gap_h = (t2 - t1).total_seconds() / 3600
        v = _speed([p for p in pts if p[0] <= t1] or pts, t1)
        drive_h = (d1v + dv2) / 1000 / v
        excess = gap_h - drive_h
        entry = t1 + timedelta(hours=(d1v / 1000) / v)
        if entry > t2:
            entry = t2
        if gap_h * 60 <= EDGE_TIGHT_MIN * 2:
            # dense pings around it and none inside — it drove straight past
            vis.entry = vis.exit = entry
            vis.dwell_h = 0.0
            vis.confidence, vis.result = "CONFIRMED", "passed through"
        elif excess < TOUCH_MIN_H:
            vis.entry = vis.exit = entry
            vis.dwell_h = 0.0
            vis.confidence, vis.result = "INFERRED", "passed through"
        else:
            exit_t = entry + timedelta(hours=excess)
            vis.entry, vis.exit = entry, min(exit_t, t2)
            vis.dwell_h = max(0.0,
                              (vis.exit - entry).total_seconds() / 3600)
            vis.confidence, vis.result = "INFERRED", "stopped (in GPS gap)"
        return vis

    # ── never bracketed: pending, or a near miss with no evidence ───────────
    dmin = min(haversine_m(la, lo, vlat, vlon) for _, la, lo in pts)
    vis.result = "no data" if dmin <= NEAR_MISS_KM * 1000 else "not reached"
    return vis


def from_rows(rows: list, vias: list) -> list:
    """Rebuild ViaVisits from previously-written Via Touching tab rows — used
    on pruned runs (no fresh trail) so the board still displays the recorded
    verdicts instead of 'no data'. Vias with no recorded row stay pending."""
    def _dt(s):
        try:
            return datetime.strptime(str(s).strip(), "%d/%m/%Y %H:%M:%S")
        except (ValueError, TypeError):
            return None
    by_via = {}
    for r in rows:
        by_via[(r.get("Via Hub") or "").strip()] = r
    out = []
    for vc in vias:
        r = by_via.get(vc)
        if not r:
            out.append(ViaVisit(via=vc, result="not reached"))
            continue
        v = ViaVisit(via=vc,
                     entry=_dt(r.get("Entry Time")),
                     exit=_dt(r.get("Exit Time")),
                     confidence=(r.get("Confidence") or "").strip(),
                     result=(r.get("Result") or "").strip())
        if v.entry and v.exit:
            v.dwell_h = max(0.0, (v.exit - v.entry).total_seconds() / 3600)
        out.append(v)
    return out


def assess(vias: list, trail: list, atlas, dep: datetime | None,
           now: datetime) -> list:
    """One ViaVisit per via, in route order. Vias the Hub List doesn't know
    get an honest row instead of silence: 'pending hub' when a placeholder is
    already waiting for the human's Google-Maps pin, else 'unknown hub'.
    Timings against a pin the fleet hasn't verified yet (Verify_Status not
    CONFIRMED/CLOSE) are capped at INTERPOLATED — an uncertain pin can never
    produce a green timing."""
    pts = [p for p in trail if dep is None or p[0] >= dep]
    out = []
    for vc in vias:
        if vc in atlas.by_code:
            vlat, vlon = atlas.by_code[vc]
            vis = _assess_one(vc, vlat, vlon, atlas.rad(vc), pts, dep, now)
            if vis.confidence == "CONFIRMED" \
                    and atlas.vstatus.get(vc, "CONFIRMED") \
                    not in ("CONFIRMED", "CLOSE"):
                vis.confidence = "INTERPOLATED"
            out.append(vis)
        elif vc.upper() in atlas.pending:
            out.append(ViaVisit(via=vc, result="pending hub"))
        else:
            out.append(ViaVisit(via=vc, result="unknown hub"))
    return out
