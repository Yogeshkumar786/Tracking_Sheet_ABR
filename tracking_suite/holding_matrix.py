"""
Holding boards — hub-wise FORWARD / BACKWARD.
=============================================
Two reports, by administrative fleet:

    Ambala  = vehicles homed Ambala / Ambala Local
    Binola  = vehicles homed Binola / Binola Local / G.Noida

Inside each report the vehicles are organised HUB-WISE — by the hub they
actually start their journeys from, read from their trips, not by the typed
home hub. So the Binola report grows a section for every base its fleet really
operates out of (NCR, Delhi, Salem, Coimbatore, ...), which is what its
southern-circuit trucks need.

Each hub section:

    <BASE> Hub   (N held: F forward, B backward, O other)
    FORWARD
        <destination>  vehicle(s)      idle AT the base, ready to run out
    BACKWARD
        <destination>  vehicle(s)      idle AT the destination, due back
    OTHER
        <destination>  vehicle (at X)  idle at some third hub

A vehicle that is moving / on a running trip is NOT held, so it is left out.
A vehicle whose position or lane can't be determined is counted, not placed.

Base and destination come from the vehicle's own trips (routes.detect_lanes):
the base is the fleet-more-used endpoint of its lane, the destination the other.
Positions are compared by REGION (hub cluster), so any Ambala yard counts as
the Ambala base and any hub in the destination city counts as arrived.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime

from tracking_suite import fms

# The two reports and the Vehicles-tab home-hub names that feed each.
REPORTS = [
    ("Ambala", {"AMBALA", "AMBALA LOCAL"}),
    ("Binola", {"BINOLA", "BINOLA LOCAL", "BINOLA LOCAL ",
                "G.NOIDA", "GNOIDA", "G NOIDA", "GREATER NOIDA"}),
]

_LOCCODE = re.compile(r'\(([A-Za-z0-9]{2,8})\)')

FORWARD = "FORWARD"
BACKWARD = "BACKWARD"
OTHER = "OTHER"


def _athub_now(presence_row: dict, live_row: dict | None) -> str:
    code = (presence_row or {}).get("At_Hub_Code", "")
    if code:
        return code.upper()
    if live_row:
        m = _LOCCODE.search(fms.last_location(live_row) or "")
        if m:
            return m.group(1).upper()
    return ""


def _region(code: str, cluster: dict) -> str:
    return cluster.get(code, code) if code else ""


# ── Build ────────────────────────────────────────────────────────────────────

def build_report(report_name: str, vehicles: list, lanes: dict,
                 presence_by: dict, live_by: dict, names: dict,
                 cluster: dict) -> dict:
    """Group a report's vehicles into hub sections keyed by base region."""
    sections: dict = {}   # base_region -> section dict
    skipped = {"on_trip": 0, "no_lane": 0, "no_position": 0}

    for vno in vehicles:
        lane = lanes.get(vno, {})
        base, dest = lane.get("base", ""), lane.get("dest", "")
        if not (base and dest):
            skipped["no_lane"] += 1
            continue

        live = live_by.get(vno)
        if live and fms.running(live):     # moving / working -> not held
            skipped["on_trip"] += 1
            continue

        here = _athub_now(presence_by.get(vno, {}), live)
        if not here:
            skipped["no_position"] += 1
            continue

        breg, dreg, hreg = (_region(base, cluster), _region(dest, cluster),
                            _region(here, cluster))
        sec = sections.setdefault(breg, {
            "base_codes": Counter(), "fwd": defaultdict(list),
            "bwd": defaultdict(list), "other": defaultdict(list)})
        sec["base_codes"][base] += 1

        if hreg == breg:
            sec["fwd"][dest].append(vno)
        elif hreg == dreg:
            sec["bwd"][dest].append(vno)
        else:
            sec["other"][dest].append((vno, here))

    # finalise: label + counts, sort sections by size
    out = []
    for breg, sec in sections.items():
        base_label = sec["base_codes"].most_common(1)[0][0]
        nf = sum(len(v) for v in sec["fwd"].values())
        nb = sum(len(v) for v in sec["bwd"].values())
        no = sum(len(v) for v in sec["other"].values())
        out.append({
            "base_region": breg, "base_code": base_label,
            "fwd": sec["fwd"], "bwd": sec["bwd"], "other": sec["other"],
            "n_fwd": nf, "n_bwd": nb, "n_other": no, "total": nf + nb + no,
        })
    out.sort(key=lambda s: -s["total"])
    return {"report": report_name, "sections": out, "skipped": skipped,
            "names": names}


# ── Render to a 2-D cell grid ────────────────────────────────────────────────

def render(board: dict, now: datetime) -> list:
    """Side-by-side layout, matching the dispatch sheet:

        <HUB> FORWARD        <date>        <HUB> BACKWARD
        Destination Vehicles               Destination Vehicles
        <dest>      veh(s)                  <dest>      veh(s)   (same row)
    """
    names = board["names"]
    date = now.strftime("%d-%b-%Y")

    def dn(code):
        # Show the hub code only. Fall back to the hub name only when there is
        # no code at all.
        return code if code else names.get(code, "")

    def veh(lst):
        return ", ".join(lst) if lst else "Vehicle Not"

    rows = [[f"{board['report'].upper()} HOLDING", "", date, "", "", ""]]
    sk = board["skipped"]
    rows.append([f"excluded: {sk['on_trip']} on trip, {sk['no_lane']} no lane, "
                 f"{sk['no_position']} no position", "", "", "", "", ""])
    rows.append(["", "", "", "", "", ""])

    for sec in board["sections"]:
        base = sec["base_code"]
        # FORWARD | BACKWARD side by side, one row per destination (the union).
        rows.append([f"{base} FORWARD", "", f"{sec['total']} held", "",
                     f"{base} BACKWARD", ""])
        rows.append(["Destination", "Vehicles", "", "", "Destination", "Vehicles"])
        dests = sorted(set(sec["fwd"]) | set(sec["bwd"]))
        for d in dests:
            rows.append([dn(d), veh(sec["fwd"].get(d, [])), "", "",
                         dn(d), veh(sec["bwd"].get(d, []))])
        if not dests:
            rows.append(["Vehicle Not", "", "", "", "Vehicle Not", ""])

        # OTHER — held at a third hub, listed below the pair.
        if sec["n_other"]:
            rows.append(["HOLDING AT OTHER LOCATION", "", "", "", "", ""])
            rows.append(["Destination", "Vehicle (at hub)", "", "", "", ""])
            for d in sorted(sec["other"]):
                cells = ", ".join(f"{v} (at {h})" for v, h in sec["other"][d])
                rows.append([dn(d), cells, "", "", "", ""])
        rows.append(["", "", "", "", "", ""])

    if not board["sections"]:
        rows.append(["(no held vehicles with a detectable base hub)",
                     "", "", "", "", ""])
    return rows


def section_header_rows(matrix: list) -> list:
    """1-based row numbers of the FORWARD|BACKWARD title rows, for formatting."""
    out = []
    for i, r in enumerate(matrix):
        if len(r) >= 5 and str(r[0]).endswith("FORWARD") and str(r[4]).endswith("BACKWARD"):
            out.append(i + 1)
    return out
