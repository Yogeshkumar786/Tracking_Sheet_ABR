"""
Holding Report — the simple flat table.
=======================================
One row per held vehicle, longest-standing first, in the dispatch-sheet layout:

    S no | Branch | Vehicle Number | From | To | From | To | Standing Hrs |
         Safe Location | Remark

    From / To (hubs)   the vehicle's last trip: where it ran from and to
    From / To (dates)  when it started standing / the report time (now)
    Standing Hrs       how long it has been idle at its current location
    Safe Location      the hub it is standing at now
    Branch             Ambala or Binola (its fleet)

Held = not moving. A vehicle running a trip is working, so it is left out.
Everything is read from live GPS + the vehicle's own trips — nothing typed.

Reads  : Hub List + Vehicles tabs + FMS dashboard + RPS report
Writes : the "Holding Report" tab on the locked spreadsheet, nothing else.

Usage:
    python tracking_suite/run_holding_simple.py --dry-run
    python tracking_suite/run_holding_simple.py
    python tracking_suite/run_holding_simple.py --days 45
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tracking_suite import (config, fms, holding_matrix as hm, hublist,
                            presence, routes, sheetio)

HEADERS = ["S no", "Branch", "Vehicle Number", "From", "To",
           "From", "To", "Standing Hrs", "Safe Location", "Remark"]

RED_AFTER_HRS = 10.0     # rows standing longer than this print in red (as in the sheet)
# Clean the hub name for display: drop "Safexpress", the word "Hub", and the
# trailing number codes. Keep distinguishing words like Outbound / City / West.
_STRIP = re.compile(r'\b(SAFEXPRESS|HUB|-?\d+)\b')


def _report_of(hub_value):
    v = (hub_value or "").strip().upper()
    for name, aliases in hm.REPORTS:
        if v in {a.strip().upper() for a in aliases}:
            return name
    return None


def clean_name(text: str) -> str:
    """Clean a raw hub name for display: drop any '(CODE)', 'Safexpress', 'Hub'
    and trailing numbers. 'SAFEXPRESS DHULE HUB-11' -> 'DHULE'."""
    if not text:
        return ""
    t = re.sub(r'\([A-Za-z0-9]{2,8}\)', '', text)
    t = _STRIP.sub("", t.upper()).replace(",", " ")
    return " ".join(t.split())


def disp(code: str, names: dict) -> str:
    """Hub code -> plain hub name (no 'Safexpress'), from the Hub List name.
    Falls back to the code when there is no name."""
    if not code:
        return ""
    nm = names.get(code, "")
    return clean_name(nm) or code if nm else code


_NAME_CODE = re.compile(r'([A-Za-z][A-Za-z0-9 .&/\-]*?)\s*\(([A-Za-z0-9]{2,8})\)')


def names_from_trips(trips: dict) -> dict:
    """{CODE: name} pulled from the RPS route strings, which spell out every hub
    as 'NAME(CODE)'. This fills in names for hubs that aren't in the verified Hub
    List (DLO11, BBY11, ...), so From/To read as names, not bare codes."""
    out = {}
    for tl in trips.values():
        for t in tl:
            for m in _NAME_CODE.finditer(t.get("route", "") or ""):
                nm, code = m.group(1).strip(), m.group(2).upper()
                if nm and code and code not in out:
                    out[code] = nm
    return out


def _hhmm(minutes: float) -> str:
    m = int(round(max(0.0, minutes)))
    h, mm = divmod(m, 60)
    return f"{h:02d}:{mm:02d} HRS"


# A vehicle on an active trip counts as "reached" when it is at the destination
# hub, or within this many km of it, and has stopped.
REACHED_KM = 5.0


def build_rows(universe: list, branch_of: dict, lanes: dict, presence_by: dict,
               live_by: dict, names: dict, cluster: dict, now: datetime):
    rows = []
    skipped = {"on_trip": 0, "moving": 0, "no_trip": 0, "not_tracked": 0}

    def _region(code):
        return cluster.get(code, code) if code else ""

    for vno in universe:
        live = live_by.get(vno)
        if not live:
            skipped["not_tracked"] += 1
            continue

        at = presence_by.get(vno, {}).get("At_Hub_Code", "")

        # -- Active RPS (on trip) -------------------------------------------
        if fms.on_trip(live):
            dest = fms.current_dest_code(live)
            reached = (dest and _region(at) == _region(dest)) \
                or (0 < fms.remaining_km(live) <= REACHED_KM)
            stopped = not fms.running(live)
            if not (reached and stopped):
                skipped["on_trip"] += 1     # still running its trip -> not held
                continue
            # reached the destination and stopped -> unloading.
            # The live trip carries origin/dest as NAMES (sometimes without a
            # code), so prefer those; fall back to the code lookup.
            lane = lanes.get(vno, {})
            last = lane.get("last") or {}
            from_disp = (clean_name(fms.consigner_name(live))
                         or disp(fms.origin_code(live) or last.get("origin")
                                 or lane.get("base", ""), names))
            to_disp = clean_name(fms.consignee_name(live)) or disp(dest, names)
            safe_disp = disp(at, names) or clean_name(fms.consignee_name(live))
            since = fms.stopped_since(live) or fms.last_gps_dt(live) or now
            stand_min = max(0.0, (now - since).total_seconds() / 60)
            rows.append(_row(branch_of, vno, from_disp, to_disp, since,
                             safe_disp, "Unloading", stand_min, now))
            continue

        # -- Not on a trip: idle between trips ------------------------------
        if fms.running(live):
            skipped["moving"] += 1
            continue
        lane = lanes.get(vno, {})
        last = lane.get("last") or {}
        idle_since = lane.get("idle_since")
        if not (last.get("origin") and last.get("dest") and idle_since):
            skipped["no_trip"] += 1
            continue
        stand_min = max(0.0, (now - idle_since).total_seconds() / 60)
        rows.append(_row(branch_of, vno, disp(last["origin"], names),
                         disp(last["dest"], names), idle_since,
                         disp(at or last["dest"], names), "Empty", stand_min, now))

    rows.sort(key=lambda r: -r["_min"])
    return rows, skipped


def _row(branch_of, vno, from_disp, to_disp, since, safe_disp, remark,
         stand_min, now):
    return {
        "Branch": branch_of.get(vno, ""),
        "Vehicle Number": vno,
        "From": from_disp, "To": to_disp,
        "From_date": since.strftime("%d-%b-%Y %I:%M%p"),
        "To_date": now.strftime("%d-%b-%Y %I:%M%p"),
        "Standing Hrs": _hhmm(stand_min),
        "Safe Location": safe_disp, "Remark": remark,
        "_min": stand_min,
    }


def main():
    ap = argparse.ArgumentParser(description="Simple flat holding report")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    now = datetime.now()

    print(f"\n{'=' * 74}\n  Holding Report (simple)  |  {now:%Y-%m-%d %H:%M:%S}\n"
          f"{'=' * 74}", flush=True)
    print(config.describe(), flush=True)
    print(f"  {'-' * 70}", flush=True)

    ss = sheetio.open_sheet()
    hubs = hublist.load(ss)
    names = hubs["names"]

    veh = sheetio.read_records(ss, config.TAB_VEHICLES)
    universe, branch_of, types = [], {}, {}
    for v in veh:
        vno = (v.get("Vehicle No") or "").strip().upper()
        rep = _report_of(v.get("Vehicle Hub"))
        if vno and rep:
            universe.append(vno)
            branch_of[vno] = rep.upper()
            types[vno] = (v.get("Vehicle Type") or "").strip()
    print(f"  [Universe] {len(universe)} vehicle(s)", flush=True)

    live = fms.live_vehicles()
    live_by = {fms.veh_no(v): v for v in live}
    presence_by = {
        vno: presence.measure(
            {"Vehicle No": vno, "Vehicle Type": types.get(vno, ""), "Vehicle Hub": ""},
            live_by.get(vno), hubs, now)
        for vno in universe
    }
    trips = routes.fetch_trips(universe, args.days, now)
    lanes = routes.detect_lanes(trips)

    # Names: Hub List first, then fill gaps from the trip route strings so hubs
    # outside the verified list still show a name instead of a bare code.
    names = {**names_from_trips(trips), **{k: v for k, v in names.items() if v}}

    rows, sk = build_rows(universe, branch_of, lanes, presence_by, live_by,
                          names, hubs['cluster'], now)
    unload = sum(1 for r in rows if r["Remark"] == "Unloading")
    print(f"\n  HELD: {len(rows)} ({unload} unloading)  (excluded: "
          f"{sk['on_trip']} on active trip, {sk['moving']} moving, "
          f"{sk['no_trip']} no recent trip, {sk['not_tracked']} not FMS-tracked)\n",
          flush=True)
    for r in rows[:12]:
        print(f"    {r['Branch']:<7} {r['Vehicle Number']:<12} "
              f"{r['From']:<16} -> {r['To']:<18} {r['Standing Hrs']:<10} "
              f"@ {r['Safe Location']}", flush=True)

    if args.dry_run:
        print("\n  DRY RUN — nothing written.\n", flush=True)
        return

    # One tab per branch — Ambala and Binola separately.
    for name, _ in hm.REPORTS:
        brows = [r for r in rows if r["Branch"] == name.upper()]
        _write(ss, f"{name} Holding", brows, now)
    print("\n  Done.\n", flush=True)


def _write(ss, title, rows, now):
    matrix = [[f"{title} Report {now:%d-%b-%Y} Time - {now:%I:%M %p}"] + [""] * 9]
    matrix.append(HEADERS)
    for i, r in enumerate(rows, 1):
        matrix.append([i, r["Branch"], r["Vehicle Number"], r["From"], r["To"],
                       r["From_date"], r["To_date"], r["Standing Hrs"],
                       r["Safe Location"], r["Remark"]])

    ws = sheetio.get_or_create(ss, title, len(HEADERS), len(matrix) + 5)
    ws.clear()
    ws.update(values=matrix, range_name="A1", value_input_option="USER_ENTERED")
    _format(ws, rows)
    print(f"  [Sheet] '{title}': {len(rows)} row(s)", flush=True)


FONT_PT = 12


def _format(ws, rows):
    green_bg = {"red": 0.72, "green": 0.88, "blue": 0.60}
    last = len(rows) + 2
    try:
        # base: font size 12 across the whole table
        ws.format(f"A1:J{last}", {"textFormat": {"fontSize": FONT_PT}})
        ws.merge_cells("A1:J1")
        ws.format("A1:J1", {"backgroundColor": green_bg,
                            "horizontalAlignment": "CENTER",
                            "textFormat": {"bold": True, "fontSize": FONT_PT}})
        ws.format("A2:J2", {"backgroundColor": green_bg,
                            "textFormat": {"bold": True, "fontSize": FONT_PT}})
        ws.freeze(rows=2)
        # red text for the long-standing rows (matches the sheet)
        red = {"textFormat": {"fontSize": FONT_PT,
                              "foregroundColor": {"red": 0.80, "green": 0.0, "blue": 0.0}}}
        specs = [{"range": f"A{i + 3}:J{i + 3}", "format": red}
                 for i, r in enumerate(rows) if r["_min"] >= RED_AFTER_HRS * 60]
        for k in range(0, len(specs), 100):
            ws.batch_format(specs[k:k + 100])
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted.\n", flush=True)
        sys.exit(130)
    except (RuntimeError, config.SheetLockError) as exc:
        print(f"\n  [STOP] {exc}\n", flush=True)
        sys.exit(2)
