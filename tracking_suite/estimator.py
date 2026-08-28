"""
Estimator — where is the truck DURING the silence, with real mathematics.
=========================================================================
The user's rule, formalised: two fixes give a time difference and a ROAD
distance, hence a speed; when the next fix is late, project from that speed,
consult what the whole fleet does at that spot, label everything an estimate,
and escalate to review when the silence outlives reason.

THE MATHEMATICS (a scalar Kalman predict step along the route arc, with a
Bayesian stop/move classifier):

1. Road speed — never straight-line, never the site's field.
   Path length between consecutive accepted fixes:
       s_i = haversine(p_i, p_{i+1}),   v_i = s_i / (t_{i+1} - t_i)
   summed over the ping chain, so a winding ghat road counts its real length.
   Segments implying > 110 km/h are rejected (teleports).

2. Robust speed state — exponentially weighted, outlier-proof:
       v_hat  = EW-median of the last K road speeds  (half-life ~20 min)
       sigma_v = 1.4826 * MAD(v_i)          (robust std of the speed history)

3. Dead-reckoning with growing uncertainty (Kalman predict, no update):
       s(tau)     = v_hat * tau                    (expected extra road-km)
       sigma_s(tau) = sqrt( (sigma_v * tau)^2 + sigma_gps^2 )
   Displayed as "est +62 +/- 18 km (unconfirmed)". The +/- widens with every
   silent minute — the honesty is in the mathematics, not a disclaimer.

4. Stop / move posterior — the fleet as a witness (log-odds fusion):
       prior      p0  = n / (n + n0)      n = distinct fleet vehicles that stop
                                          in this 300 m cell (from 3,978-truck
                                          sweep), n0 = 8 half-saturation
       likelihood     : own last road-speed  v_last < 8 km/h  -> slowing,
                        L = +1.2 log-odds;  v_last > 35 -> rolling, L = -1.2
       time-of-day    : 23:00-05:00 adds +0.6 (night halts are the norm)
       posterior  P(stopped) = sigmoid( logit(p0) + L + night )
   P > 0.6 -> "may have STOPPED here (fleet halt spot, P=0.74)" until the next
   fix confirms or denies; the arriving fix settles it (distance moved during
   the gap: < 1 km confirms the stop, else the movement estimate was right).

5. Escalation: silence beyond FRESH_HRS is no longer estimable — the row
   becomes REVIEW / ALERT. An estimate is a bridge, not a lifestyle.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from tracking_suite.presence import haversine_m

STOP_MAP_FILE = Path(__file__).resolve().parent / "stop_map.json"

MAX_SEG_KMH = 110.0     # faster than this between pings = teleport, rejected
HALF_LIFE_MIN = 20.0    # exponential weighting half-life for the speed state
SIGMA_GPS_KM = 0.2      # base position noise
SLOW_KMH, FAST_KMH = 8.0, 35.0
N0 = 8.0                # half-saturation for the fleet stop prior
AGING_MIN = 45.0        # a fix older than this (but still fresh) gets estimated

_stop_map = None


def _load_stop_map() -> dict:
    global _stop_map
    if _stop_map is None:
        try:
            d = json.loads(STOP_MAP_FILE.read_text(encoding="utf-8"))
            _stop_map = d
        except Exception:
            _stop_map = {"cell_m": 300.0, "cells": {}}
    return _stop_map


def fleet_stops_here(lat: float, lon: float) -> int:
    """Distinct fleet vehicles that halt in this cell (or its 8 neighbours)."""
    m = _load_stop_map()
    cell = m["cell_m"]
    mlat = cell / 111320.0
    mlon = cell / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    cx, cy = int(lat / mlat), int(lon / mlon)
    best = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            c = m["cells"].get(f"{cx + dx},{cy + dy}")
            if c:
                best = max(best, c["n"])
    return best


def road_speeds(trail: list) -> list:
    """[(t_mid, v_kmh, s_km), ...] per accepted segment — path speed, teleports
    dropped. trail = [(dt, lat, lon), ...] sorted."""
    out = []
    for (t1, la1, lo1), (t2, la2, lo2) in zip(trail, trail[1:]):
        dt_h = (t2 - t1).total_seconds() / 3600
        if dt_h <= 0:
            continue
        s_km = haversine_m(la1, lo1, la2, lo2) / 1000
        v = s_km / dt_h
        if v > MAX_SEG_KMH:
            continue                      # teleport — two-witness rule
        out.append((t1 + (t2 - t1) / 2, v, s_km))
    return out


def speed_state(trail: list, now: datetime) -> tuple:
    """(v_hat, sigma_v, v_last) — EW-robust speed and its spread, km/h.
    Returns (None, None, None) if the trail cannot support it."""
    segs = road_speeds(trail)
    if len(segs) < 2:
        return None, None, None
    lam = math.log(2) / HALF_LIFE_MIN
    weighted = []
    for t_mid, v, _ in segs[-40:]:
        age_min = max(0.0, (now - t_mid).total_seconds() / 60)
        weighted.append((math.exp(-lam * age_min), v))
    # EW median: sort by v, walk cumulative weight to half
    weighted.sort(key=lambda x: x[1])
    tot = sum(w for w, _ in weighted)
    if tot <= 0:
        return None, None, None
    acc, v_hat = 0.0, weighted[-1][1]
    for w, v in weighted:
        acc += w
        if acc >= tot / 2:
            v_hat = v
            break
    devs = sorted(abs(v - v_hat) for _, v in weighted)
    mad = devs[len(devs) // 2]
    sigma_v = 1.4826 * mad
    v_last = segs[-1][1]
    return v_hat, sigma_v, v_last


def stop_posterior(lat: float, lon: float, v_last: float | None,
                   when: datetime) -> tuple:
    """P(the truck is stopped at its last-known point), with the evidence.
    Log-odds fusion of the fleet prior, its own speed trend, and the hour."""
    n = fleet_stops_here(lat, lon)
    p0 = n / (n + N0)
    p0 = min(max(p0, 0.02), 0.95)
    logit = math.log(p0 / (1 - p0))
    if v_last is not None:
        if v_last < SLOW_KMH:
            logit += 1.2
        elif v_last > FAST_KMH:
            logit -= 1.2
    if when and (when.hour >= 23 or when.hour < 5):
        logit += 0.6
    p = 1 / (1 + math.exp(-logit))
    return p, n


def silent_estimate(trail: list, last_fix: datetime, lat: float, lon: float,
                    now: datetime) -> str | None:
    """One sentence for the Remark while a fix is overdue — estimate or
    stop-verdict, both explicitly unconfirmed. None if nothing useful."""
    tau_h = (now - last_fix).total_seconds() / 3600
    if tau_h * 60 < AGING_MIN:
        return None
    v_hat, sigma_v, v_last = speed_state(trail, now)
    p_stop, n = stop_posterior(lat, lon, v_last, last_fix)

    if p_stop > 0.6:
        spot = f"fleet halt spot, {n} trucks" if n >= 3 else "was slowing"
        return (f"no fix {tau_h:.1f}h — may have STOPPED here "
                f"({spot}, P={p_stop:.2f}) — awaiting next fix")
    if v_hat is not None and v_hat > 3:
        s = v_hat * tau_h
        sigma = math.sqrt((sigma_v * tau_h) ** 2 + SIGMA_GPS_KM ** 2)
        return (f"no fix {tau_h:.1f}h — est +{s:.0f}±{sigma:.0f} km along route "
                f"at {v_hat:.0f} km/h (unconfirmed)")
    return f"no fix {tau_h:.1f}h — last seen stationary (unconfirmed)"
