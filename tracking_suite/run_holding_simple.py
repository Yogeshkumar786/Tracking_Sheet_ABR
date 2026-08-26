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
from datetime import datetime, timedelta, timezone

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


def _norm(s: str) -> str:
    """Normalise for matching: drop '(code)', 'Safexpress', 'Hub', punctuation;
    keep the distinguishing words (SDS/OUTBOUND/CITY) and the -11 number.
    'SAFEXPRESS BENGALURU-11' -> 'BENGALURU 11'."""
    s = re.sub(r'\([^)]*\)', '', s or '')
    s = s.upper().replace("SAFEXPRESS", "").replace(" HUB", "")
    s = re.sub(r'[^A-Z0-9]+', ' ', s)
    return " ".join(s.split())


def build_index(names: dict) -> list:
    """[(normalised_hub_name, code), ...] longest first. The route text can be
    anything, but the Hub List names are clean — so to identify a messy segment
    we search each CLEAN hub name INTO the segment and take the longest that
    fits ('BENGALURU SDS 11' beats 'BENGALURU 11' beats 'BENGALURU')."""
    idx = [(_norm(nm), code) for code, nm in names.items() if _norm(nm)]
    idx.sort(key=lambda t: -len(t[0]))
    return idx


def disp(ident: str, names: dict, index: list | None = None) -> str:
    """A hub identifier -> clean display name. A code is looked up directly; a
    messy route-segment name is identified by searching the clean hub-list names
    into it (longest match wins), so From/To read as consistent hub names."""
    if not ident:
        return ""
    if ident in names:                       # already a code
        return clean_name(names[ident])
    if index:
        nseg = _norm(ident)
        for nkey, code in index:             # longest clean hub name that fits
            if nkey and nkey in nseg:
                return clean_name(names[code])
    return clean_name(ident)                 # nothing matched -> cleaned text


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
    index = build_index(names)
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
            # "Reached" the destination if: presence matched it, it is within a
            # few km, OR the live location text is AT/Near a hub whose code is the
            # destination (or its region). The last case catches destinations that
            # are not in the verified Hub List (e.g. Baghpat) and code variants in
            # the same city (MOH11 vs MOH400) — where the region check alone fails.
            loc = (fms.last_location(live) or "")
            m = re.search(r'\(([A-Za-z0-9]{2,8})\)', loc)
            loccode = m.group(1).upper() if m else ""
            near_hub = loc.strip().upper().startswith(("AT ", "NEAR"))
            # city-name match: destination and location share a real place word
            # (>=4 letters, not 'Safexpress'/'Hub' which _norm already drops).
            # Catches code variants in one city (MOH11 vs MOH400 = Mohali).
            dest_words = {w for w in _norm(fms.consignee_name(live)).split()
                          if len(w) >= 4 and not w.isdigit()}
            loc_words = {w for w in _norm(loc).split()
                         if len(w) >= 4 and not w.isdigit()}
            reached = (dest and _region(at) == _region(dest)) \
                or (0 < fms.remaining_km(live) <= REACHED_KM) \
                or (near_hub and dest and loccode
                    and (loccode == dest or _region(loccode) == _region(dest))) \
                or (near_hub and bool(dest_words & loc_words))
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
                                 or lane.get("base", ""), names, index))
            to_disp = clean_name(fms.consignee_name(live)) or disp(dest, names, index)
            safe_disp = disp(at, names, index) or clean_name(fms.consignee_name(live))
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
        rows.append(_row(branch_of, vno, disp(last["origin"], names, index),
                         disp(last["dest"], names, index), idle_since,
                         disp(at or last["dest"], names, index), "Empty", stand_min, now))

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
    # FMS trip times are IST; GitHub runners are UTC. Use IST wall-clock
    # so the timestamps AND the standing-hours maths are correct in the cloud.
    now = datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)

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
