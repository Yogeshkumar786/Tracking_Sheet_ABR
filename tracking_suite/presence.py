"""
Hub presence — which vehicle is at which hub.
=============================================
Step one of the holding report, and nothing more. It answers, for right now,
where each vehicle is standing. It does not say for how long; that comes later
and is built on top of this.

The method, in full:

    distance from the vehicle's position to every hub pin
    -> take the nearest
    -> is it inside the radius?

Every vehicle lands in exactly one of three answers, and no vehicle is ever
dropped from the list:

    AT HUB          the nearest hub, inside its radius, position fresh. This is
                    the EXACT hub — every hub is separate, none are merged. When
                    a sibling hub sits almost as close (co-located codes like
                    AML11 / AOT11), a note is added but the exact nearest hub is
                    still the answer.
    NOT AT A HUB    nearest hub is outside the radius — on the road, at a
                    workshop, somewhere that is not ours. A real answer.
    DON'T KNOW      the fix is too old, unparseable, or the vehicle is not on
                    the FMS dashboard at all. We say so rather than reporting a
                    stale position as if it were current.

Freshness is checked BEFORE distance, on purpose: there is no point measuring a
position we already know is stale.

Distances are straight-line (haversine), matching fms_to_sheets.py and the
DeparturePlanner so all three agree. A vehicle 1.4 km away across a canal reads
as at the hub.
"""
from __future__ import annotations

import math
from datetime import datetime

from tracking_suite import config, fms, hublist

HEADERS = [
    "Vehicle_No", "Vehicle_Type", "Home_Hub", "Home_Hub_Code",
    "Answer", "At_Hub_Code", "At_Hub_Name", "Distance_M",
    "Second_Hub_Code", "Second_Distance_M",
    "At_Home_Hub", "On_Trip", "Moving",
    "Last_GPS", "Position_Age", "Latitude", "Longitude", "Location_Text",
    "Flag", "Last_Updated",
]

AT_HUB = "AT HUB"
NOT_AT_HUB = "NOT AT A HUB"
UNKNOWN = "DON'T KNOW"

_ANSWER_COLOR = {
    AT_HUB: "green",
    NOT_AT_HUB: "grey",
    UNKNOWN: "red",
}

# A second hub within this many metres of the nearest one is a genuine near-tie:
# the truck could be in either yard. The nearest is still shown as the exact hub;
# this only adds a note. Kept well below the closest real hub separations (the
# tightest sibling pair is ~113 m), so it fires only when it truly matters.
NEAR_TIE_M = 120.0

EARTH_R = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line metres between two points. Same maths as fms_to_sheets.py."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def ranked_hubs(lat: float, lon: float, hub_by_code: dict) -> list:
    """[(hub_code, distance_m), ...] nearest first. Empty if there is no fix."""
    if not (lat and lon):
        return []
    out = [(code, haversine_m(lat, lon, hlat, hlon))
           for code, (hlat, hlon) in hub_by_code.items()]
    out.sort(key=lambda t: t[1])
    return out


def _hhmm(hours: float) -> str:
    if hours != hours or hours in (float("inf"), float("-inf")):
        return ""
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h}h {m:02d}m"


def measure(vehicle: dict, live: dict | None, hubs: dict,
            now: datetime) -> dict:
    """One vehicle -> one row. Never returns None; every vehicle is reported.

    `hubs` is the loaded Hub List: {by_code, names, radius}.
    """
    hub_by_code = hubs["by_code"]
    hub_names = hubs["names"]
    hub_radius = hubs["radius"]
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    vno = (vehicle.get("Vehicle No") or "").strip().upper()
    vtype = (vehicle.get("Vehicle Type") or "").strip()
    home = (vehicle.get("Vehicle Hub") or "").strip()
    home_code = hublist.home_hub_code(home, hub_names)

    row = {
        "Vehicle_No": vno, "Vehicle_Type": vtype,
        "Home_Hub": home, "Home_Hub_Code": home_code or "",
        "At_Hub_Code": "", "At_Hub_Name": "", "Distance_M": "",
        "Second_Hub_Code": "", "Second_Distance_M": "",
        "At_Home_Hub": "", "On_Trip": "", "Moving": "",
        "Last_GPS": "", "Position_Age": "", "Latitude": "", "Longitude": "",
        "Location_Text": "", "Flag": "", "Last_Updated": now_str,
        "_colors": {}, "_dist_m": None, "_parked": False,
    }
    flags = []
    if home and not home_code:
        flags.append(f"home hub '{home}' has no pin in the hub master")

    # -- Not on the dashboard at all ----------------------------------------
    if not live:
        row["Answer"] = UNKNOWN
        flags.append("not on the FMS dashboard")
        row["Flag"] = "; ".join(flags)
        row["_colors"] = {"Answer": _ANSWER_COLOR[UNKNOWN], "Flag": "grey"}
        return row

    lat, lon = fms.position(live)
    gps_dt = fms.last_gps_dt(live)
    is_on_trip = fms.on_trip(live)
    is_moving = fms.running(live)

    row["Latitude"] = f"{lat:.6f}" if lat else ""
    row["Longitude"] = f"{lon:.6f}" if lon else ""
    row["Location_Text"] = fms.last_location(live)
    row["On_Trip"] = "yes" if is_on_trip else "no"
    row["Moving"] = "yes" if is_moving else "no"
    row["Last_GPS"] = gps_dt.strftime("%d-%b-%Y %H:%M:%S") if gps_dt else ""
    row["_parked"] = not is_moving

    # -- Freshness, before distance -----------------------------------------
    if gps_dt is None:
        row["Answer"] = UNKNOWN
        flags.append("no usable GPS timestamp")
        row["Flag"] = "; ".join(flags)
        row["_colors"] = {"Answer": _ANSWER_COLOR[UNKNOWN], "Last_GPS": "grey"}
        return row

    age_h = (now - gps_dt).total_seconds() / 3600
    row["Position_Age"] = _hhmm(age_h)

    if age_h > config.GPS_STALE_HRS:
        row["Answer"] = UNKNOWN
        flags.append(f"fix is {age_h:.0f} h old (stale after "
                     f"{config.GPS_STALE_HRS:.0f} h)")
        row["Flag"] = "; ".join(flags)
        row["_colors"] = {"Answer": _ANSWER_COLOR[UNKNOWN], "Position_Age": "grey"}
        return row

    if not (lat and lon):
        row["Answer"] = UNKNOWN
        flags.append("no coordinates in the record")
        row["Flag"] = "; ".join(flags)
        row["_colors"] = {"Answer": _ANSWER_COLOR[UNKNOWN], "Flag": "grey"}
        return row

    # -- Distance ------------------------------------------------------------
    ranked = ranked_hubs(lat, lon, hub_by_code)
    if not ranked:
        row["Answer"] = UNKNOWN
        flags.append("hub master is empty")
        row["Flag"] = "; ".join(flags)
        row["_colors"] = {"Answer": _ANSWER_COLOR[UNKNOWN]}
        return row

    code, dist = ranked[0]
    row["_dist_m"] = dist
    row["At_Hub_Code"] = code
    row["At_Hub_Name"] = hub_names.get(code, "")
    row["Distance_M"] = round(dist)

    if len(ranked) > 1:
        code2, dist2 = ranked[1]
        row["Second_Hub_Code"] = code2
        row["Second_Distance_M"] = round(dist2)

    # Each hub carries its own radius (a small depot is tighter than a big
    # compound), taken from the Hub List. Fall back to the flat default only
    # when the list has no radius for it.
    radius = hub_radius.get(code, config.HUB_RADIUS_M)
    if dist > radius:
        row["Answer"] = NOT_AT_HUB
        # Nearest hub is still reported, as context — but it is not a location.
        row["At_Hub_Code"] = ""
        row["At_Hub_Name"] = f"nearest: {code} at {dist / 1000:.1f} km"
        row["At_Home_Hub"] = "no"
        row["Flag"] = "; ".join(flags)
        row["_colors"] = {"Answer": _ANSWER_COLOR[NOT_AT_HUB]}
        return row

    # The nearest hub IS the exact hub — tracking always shows it, and every
    # hub stays a separate answer (no merging of co-located hubs). Only when a
    # sibling hub is a genuine near-tie — nearly the same distance away, so the
    # true yard could be either — is a note added, without changing the answer.
    row["Answer"] = AT_HUB
    if len(ranked) > 1 and (ranked[1][1] - dist) <= NEAR_TIE_M:
        flags.append(f"sibling {ranked[1][0]} nearly as close "
                     f"({round(ranked[1][1])} m) — same vicinity")
        row["_colors"]["Flag"] = "yellow"
    row["At_Home_Hub"] = "yes" if (home_code and home_code == code) else "no"
    if home_code and home_code != code:
        flags.append(f"away from its home hub ({home_code})")

    row["Flag"] = "; ".join(flags)
    row["_colors"] = {"Answer": _ANSWER_COLOR[row["Answer"]]}
    if flags:
        row["_colors"]["Flag"] = "yellow"
    return row


def build(vehicles: list[dict], live_rows: list[dict], hubs: dict,
          now: datetime) -> list[dict]:
    """Every vehicle in the Vehicles tab -> one row, at-hub first.

    `hubs` is the loaded Hub List (hublist.load).
    """
    live_by_vno = {fms.veh_no(v): v for v in live_rows}

    rows = [measure(v, live_by_vno.get((v.get("Vehicle No") or "").strip().upper()),
                    hubs, now)
            for v in vehicles if (v.get("Vehicle No") or "").strip()]

    order = {AT_HUB: 0, NOT_AT_HUB: 1, UNKNOWN: 2}
    rows.sort(key=lambda r: (order.get(r["Answer"], 9),
                             r.get("At_Hub_Code") or "~",
                             r.get("_dist_m") if r.get("_dist_m") is not None else 9e9))
    return rows


# ── Radius evidence ─────────────────────────────────────────────────────────

AUDIT_HEADERS = [
    "Hub_Code", "Hub_Name", "Parked_Nearest", "Under_500m", "500m_1km",
    "1_2km", "2_5km", "5_10km", "Over_10km",
    "Closest_M", "Median_M", "Largest_Gap_Starts_M", "Largest_Gap_Ends_M",
    "Suggested_Radius_M", "Note",
]

# Bands used for the audit histogram, in metres.
_BANDS = [(500, "Under_500m"), (1000, "500m_1km"), (2000, "1_2km"),
          (5000, "2_5km"), (10000, "5_10km")]


def audit(rows: list[dict], hubs: dict, now: datetime) -> list[dict]:
    """Where do PARKED vehicles actually sit relative to each pin?

    This is the evidence for choosing a radius instead of guessing one. Only
    stationary vehicles count — a moving truck 400 m from a hub is passing it,
    not standing in it.

    IMPORTANT: this is one snapshot. It shows where the fleet happens to be
    right now, not where it habitually parks. Treat a suggestion drawn from
    three vehicles as a hint, not a measurement.
    """
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    per_hub: dict = {}
    for r in rows:
        if not r.get("_parked") or r.get("_dist_m") is None:
            continue
        # Nearest hub, whether or not it fell inside the radius — the point of
        # the audit is to see the distances the radius is being drawn through.
        code = _nearest_code(r)
        if not code:
            continue
        per_hub.setdefault(code, []).append(r["_dist_m"])

    names = hubs["names"]
    out = []
    for code in hubs["by_code"]:
        dists = sorted(per_hub.get(code, []))
        if not dists:
            continue
        row = {"Hub_Code": code, "Hub_Name": names.get(code, ""),
               "Parked_Nearest": len(dists), "Last_Updated": now_str, "_colors": {}}
        prev = 0
        for limit, key in _BANDS:
            row[key] = sum(1 for d in dists if prev <= d < limit)
            prev = limit
        row["Over_10km"] = sum(1 for d in dists if d >= 10000)
        row["Closest_M"] = round(dists[0])
        row["Median_M"] = round(dists[len(dists) // 2])

        gap_lo, gap_hi, suggested, note = _suggest(dists)
        row["Largest_Gap_Starts_M"] = round(gap_lo) if gap_lo is not None else ""
        row["Largest_Gap_Ends_M"] = round(gap_hi) if gap_hi is not None else ""
        row["Suggested_Radius_M"] = round(suggested) if suggested else ""
        row["Note"] = note
        if len(dists) < 3:
            row["_colors"]["Suggested_Radius_M"] = "grey"
        if dists[0] > 1000:
            row["_colors"]["Closest_M"] = "orange"
            row["Note"] = (note + "; " if note else "") + \
                "nothing parks near this pin — check it points at the yard"
        out.append(row)
    return out


def _nearest_code(row: dict) -> str:
    """The nearest hub code for a row, including rows judged NOT AT A HUB
    (whose At_Hub_Code was blanked so it could not read as a location)."""
    name = row.get("At_Hub_Name") or ""
    if name.startswith("nearest: "):
        return name.split()[1]
    return row.get("At_Hub_Code") or ""


def _suggest(dists: list[float]):
    """Find the largest gap below 10 km and put the radius in the middle of it.

    The reasoning is the one thing worth remembering here: a yard produces a
    cluster of parked trucks, then a stretch where nothing parks, then whatever
    happens to be further away. The empty stretch is where the boundary belongs.
    """
    near = [d for d in dists if d <= 10000]
    if len(near) < 2:
        only = near[0] if near else None
        if only is None:
            return None, None, None, "no parked vehicle within 10 km"
        return None, None, max(500.0, only * 1.5), "only one parked vehicle — a hint, not a measurement"

    best_lo, best_hi, best_gap = None, None, 0.0
    for a, b in zip(near, near[1:]):
        if (b - a) > best_gap:
            best_lo, best_hi, best_gap = a, b, b - a

    if best_gap < 300:
        return None, None, None, "no clear gap — needs parking history, not one snapshot"
    return best_lo, best_hi, (best_lo + best_hi) / 2, f"gap of {best_gap / 1000:.1f} km"
