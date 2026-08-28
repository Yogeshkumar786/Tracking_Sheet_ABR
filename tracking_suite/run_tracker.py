"""
Tracker runner — writes the live board to the TESTING sheet.
============================================================
Reads  : Vehicles + Hub List tabs (locked testing sheet) + FMS + RPS report
Writes : the "Tracker" tab, one stable row per vehicle

Human edits survive every refresh:
  Trip ID typed by hand    kept until the trip is seen to complete
  Status set by hand       BREAKDOWN / ACCIDENT / MAINTENANCE / DOCUMENT ISSUE
  Remark typed by hand     never overwritten
  row order + colouring    rows are locked to Vehicle No; only values change

Usage:
    python tracking_suite/run_tracker.py --dry-run
    python tracking_suite/run_tracker.py
    python tracking_suite/run_tracker.py --limit 30
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tracking_suite import config, fms, holding_matrix as hm, hublist, routes, sheetio, tatsheets, tracker

TAB_OLD = "Tracker"          # the single board this split supersedes


def _branch_of(hub_value):
    v = (hub_value or "").strip().upper()
    for name, aliases in hm.REPORTS:
        if v in {a.strip().upper() for a in aliases}:
            return name
    return None


def main():
    ap = argparse.ArgumentParser(description="Live trip tracker board")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=45,
                    help="trip-history window for TAT learning (default 45)")
    ap.add_argument("--limit", type=int, help="first N vehicles only")
    args = ap.parse_args()
    # IST wall-clock regardless of host timezone — GitHub Actions runners are
    # UTC, and FMS/RPS datetimes are naive IST (same fix as the holding job)
    from datetime import timezone
    now = datetime.now(timezone(timedelta(hours=5, minutes=30))) \
        .replace(tzinfo=None)

    print(f"\n{'=' * 74}\n  Tracker  |  {now:%Y-%m-%d %H:%M:%S}\n{'=' * 74}", flush=True)
    print(config.describe(), flush=True)
    print(f"  {'-' * 70}", flush=True)

    ss = sheetio.open_sheet()
    hubs = hublist.load(ss)
    index = tracker.build_index(hubs["names"])

    # -- universe: Vehicles tab rows with a hub, deduped, stable order -------
    veh = sheetio.read_records(ss, config.TAB_VEHICLES)
    universe, types, seen, branch = [], {}, set(), {}
    for v in veh:
        vno = (v.get("Vehicle No") or "").strip().upper()
        hub = (v.get("Vehicle Hub") or "").strip()
        b = _branch_of(hub)
        if vno and hub and b and vno not in seen:
            seen.add(vno)
            universe.append(vno)
            types[vno] = (v.get("Vehicle Type") or "").strip()
            branch[vno] = b
    dupes = len(veh) - len(seen) - sum(1 for v in veh
                                       if not (v.get("Vehicle No") or "").strip()
                                       or not (v.get("Vehicle Hub") or "").strip())
    print(f"  [Universe] {len(universe)} vehicle(s)"
          + (f"  ({dupes} duplicate row(s) ignored)" if dupes > 0 else ""), flush=True)
    if args.limit:
        universe = universe[:args.limit]

    # -- existing boards: preserve human edits + per-tab row order -----------
    existing_rows, tab_order = {}, {}
    for name, _ in hm.REPORTS:
        try:
            cur = sheetio.read_records(ss, f"{name} Tracker")
            o = []
            for r in cur:
                p = (r.get("Vehicle No") or "").strip().upper()
                if p:
                    existing_rows[p] = r
                    o.append(p)
            tab_order[name] = o
        except RuntimeError:
            tab_order[name] = []
    try:  # migrate manual edits from the superseded single board, once
        for r in sheetio.read_records(ss, TAB_OLD):
            p = (r.get("Vehicle No") or "").strip().upper()
            if p and p not in existing_rows:
                existing_rows[p] = r
    except RuntimeError:
        pass
    if existing_rows:
        print(f"  [Board] {len(existing_rows)} existing row(s) — manual edits "
              f"preserved", flush=True)
    # stable order per tab: existing first, new vehicles appended
    uni = set(universe)
    ordered = []
    for name, _ in hm.REPORTS:
        kept = [p for p in tab_order[name] if p in uni and branch.get(p) == name]
        new = [p for p in universe
               if branch.get(p) == name and p not in set(kept)]
        ordered += kept + new

    # -- data ----------------------------------------------------------------
    live_by = {fms.veh_no(v): v for v in fms.live_vehicles()}
    trips_by = routes.fetch_trips(universe, args.days, now)
    tats = tracker.learn_tats(trips_by, hubs["cluster"])
    print(f"  [TAT] {len(tats)} lane timetable(s) learned from trip history",
          flush=True)
    try:
        sheet_tats = tatsheets.load_sheet_tats(hubs["cluster"])
    except Exception as exc:
        print(f"  [TAT-sheet] unavailable ({str(exc)[:60]}) — using history",
              flush=True)
        sheet_tats = None

    # -- build rows (only FMS-tracked vehicles; trail calls where needed) ----
    on_fms = [p for p in ordered if p in live_by]
    skipped = len(ordered) - len(on_fms)
    if skipped:
        print(f"  [Skip] {skipped} vehicle(s) not on the FMS dashboard — not shown",
              flush=True)
    rows = []
    for i, vno in enumerate(on_fms, 1):
        row = tracker.build_row(vno, types.get(vno, ""), live_by[vno],
                                trips_by.get(vno, []), hubs, index, tats,
                                existing_rows.get(vno, {}), now,
                                sheet_tats=sheet_tats)
        rows.append(row)
        if i % 40 == 0:
            print(f"    [{i}/{len(on_fms)}]", flush=True)

    _report(rows)
    if args.dry_run:
        print("\n  DRY RUN — nothing written.\n", flush=True)
        return

    # ghost trips + real-trip ids, for the Extra Trips tabs
    ghosts_by, reals = {}, {}
    for r in rows:
        b = branch.get(r["Vehicle No"])
        if r.get("_ghost") is not None and b:
            ghosts_by.setdefault(b, []).append((r["Vehicle No"], r["_ghost"]))
        real = r.get("_real")
        if real and real[1]:
            reals[r["Vehicle No"]] = real

    for name, _ in hm.REPORTS:
        brows = [r for r in rows if branch.get(r["Vehicle No"]) == name]
        _write(ss, f"{name} Tracker", brows, now)
    for name, _ in hm.REPORTS:
        _extra_write(ss, f"{name} Extra Trips", ghosts_by.get(name, []),
                     reals, now)
    _completed_write(ss, types, hubs, index, sheet_tats, rows, now)
    # retire the superseded single board so a stale copy can't mislead
    try:
        ss.del_worksheet(sheetio.find_tab(ss, TAB_OLD))
        print(f"  [Sheet] removed superseded '{TAB_OLD}' tab", flush=True)
    except Exception:
        pass
    print("\n  Done.\n", flush=True)


def _report(rows):
    from collections import Counter
    st = Counter(r["Status"] for r in rows if r["Status"])
    ar = Counter(r["ARRIVAL STATUS"] for r in rows if r["ARRIVAL STATUS"])
    pf = Counter(r["Performance"] for r in rows if r["Performance"])
    print(f"\n  {'-' * 70}", flush=True)
    print(f"  Status : {dict(st)}", flush=True)
    print(f"  Arrival: {dict(ar)}", flush=True)
    print(f"  Perf   : {dict(pf)}", flush=True)
    print(f"\n  sample in-transit rows:", flush=True)
    shown = 0
    for r in rows:
        if r["ARRIVAL STATUS"] == tracker.A_TRANSIT and shown < 10:
            shown += 1
            print(f"    {r['Vehicle No']:<12} {r['From']:>7}->{r['To']:<7} "
                  f"via[{r['Via Point'][:12]:<12}] TAT {r['TAT'] or '-':<9} "
                  f"dep {r['DEP Date&Time'] or '-':<15} {r['Status']:<20} "
                  f"{r['Performance']:<8} late {r['Late Hrs'] or '-'}", flush=True)
    print(f"  {'-' * 70}", flush=True)


def _write(ss, title, rows, now):
    ws = sheetio.get_or_create(ss, title, len(tracker.HEADERS), len(rows) + 20)
    matrix = [tracker.HEADERS] + [[r.get(h, "") for h in tracker.HEADERS]
                                  for r in rows]
    # pad blank rows so a shrinking list never leaves stale rows behind
    # (we deliberately do NOT clear(), so user formatting/colours stay)
    matrix += [[""] * len(tracker.HEADERS) for _ in range(80)]
    from tracking_suite.sheetio import _with_quota_retry
    _with_quota_retry(lambda: ws.update(values=matrix, range_name="A1",
                                        value_input_option="USER_ENTERED"),
                      f"write '{title}'")
    print(f"  [Sheet] '{title}': {len(rows)} row(s)", flush=True)

    # header + dropdown validations
    try:
        from tracking_suite.sheetio import HEADER_BG, HEADER_FG
        # TAT and Late Hrs are DURATIONS — force [h]:mm:ss so "9:12" can never
        # be mistaken for 9:12 AM by the sheet.
        dur = {"numberFormat": {"type": "TIME", "pattern": "[h]:mm:ss"}}
        gcol = sheetio.col_a1(tracker.HEADERS.index("TAT"))
        ncol = sheetio.col_a1(tracker.HEADERS.index("Late Hrs"))
        last = len(rows) + 80
        ws.format(f"{gcol}2:{gcol}{last}", dur)
        ws.format(f"{ncol}2:{ncol}{last}", dur)
        ws.format("A1:P1", {"backgroundColor": HEADER_BG,
                            "textFormat": {"bold": True,
                                           "foregroundColor": HEADER_FG}})
        ws.freeze(rows=1)
        sid = ws.id
        def dv(col_idx, values):
            return {"setDataValidation": {
                "range": {"sheetId": sid, "startRowIndex": 1,
                          "endRowIndex": len(rows) + 60,
                          "startColumnIndex": col_idx,
                          "endColumnIndex": col_idx + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": v}
                                                  for v in values]},
                         "showCustomUi": True, "strict": False}}}
        ss.batch_update({"requests": [
            dv(tracker.HEADERS.index("Status"), tracker.STATUS_VALUES),
            dv(tracker.HEADERS.index("Performance"),
               [tracker.P_ONTIME, tracker.P_DELAY]),
            dv(tracker.HEADERS.index("ARRIVAL STATUS"), tracker.ARRIVAL_VALUES),
        ]})
        print("  [Sheet] dropdowns set for Status / Performance / Arrival",
              flush=True)
        _paint(ss, ws, rows, title)
    except Exception as exc:
        print(f"  [WARN] formatting/validation: {str(exc)[:80]}", flush=True)


# ── the colour language (agreed on the Ghost Trips page) ─────────────────────
# One rule: the colour says what it means, the shade says how sure / how bad.
def _c(r, g, b):
    return {"red": r / 255, "green": g / 255, "blue": b / 255}

OK_BG, OK_FG = _c(220, 242, 228), _c(23, 105, 62)
AMBER_BG, AMBER_FG = _c(251, 239, 212), _c(150, 100, 8)
ORANGE_BG, ORANGE_FG = _c(251, 228, 206), _c(150, 76, 14)
BLUE_BG, BLUE_FG = _c(221, 234, 248), _c(30, 82, 143)
REVIEW_BG, REVIEW_FG = _c(245, 222, 234), _c(115, 30, 75)
GREY_BG, GREY_FG = _c(238, 239, 241), _c(90, 95, 105)
RED1_BG, RED1_FG = _c(242, 199, 199), _c(115, 20, 20)   # DELAY < 6 h
RED2_BG, RED2_FG = _c(214, 98, 98), _c(255, 255, 255)   # DELAY 6-24 h
RED3_BG, RED3_FG = _c(120, 22, 22), _c(255, 255, 255)   # DELAY > 24 h
TRUST_BG = {"ok": _c(233, 246, 238), "est": _c(251, 239, 212),
            "pred": _c(245, 222, 234)}


def _late_h(s):
    m = None
    try:
        hh, mm, _sec = str(s).strip().split(":")
        m = int(hh) + int(mm) / 60
    except (ValueError, AttributeError):
        pass
    return m


def _delay_shade(late_hours):
    if late_hours is None or late_hours < 6:
        return RED1_BG, RED1_FG
    if late_hours < 24:
        return RED2_BG, RED2_FG
    return RED3_BG, RED3_FG


def _paint(ss, ws, rows, title):
    """Every semantic colour in ONE batch_update — no extra reads, quota-safe."""
    sid = ws.id
    n_cols = len(tracker.HEADERS)
    last = len(rows) + 80
    reqs = []

    def cell(r0, c0, bg, fg, r1=None, c1=None):
        f = {"backgroundColor": bg}
        fields = "userEnteredFormat.backgroundColor"
        if fg is not None:
            f["textFormat"] = {"foregroundColor": fg}
            fields = "userEnteredFormat(backgroundColor,textFormat.foregroundColor)"
        reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r0,
                      "endRowIndex": (r1 or r0 + 1),
                      "startColumnIndex": c0, "endColumnIndex": (c1 or c0 + 1)},
            "cell": {"userEnteredFormat": f}, "fields": fields}})

    # reset everything we paint (bg, text colour, italics) across the board
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": last,
                  "startColumnIndex": 0, "endColumnIndex": n_cols},
        "cell": {},
        "fields": "userEnteredFormat(backgroundColor,"
                  "textFormat.foregroundColor,textFormat.italic)"}})

    H = tracker.HEADERS.index
    STATUS_COLOR = {
        tracker.S_RUNNING: (OK_BG, OK_FG),
        tracker.S_LOADING: (AMBER_BG, AMBER_FG),
        tracker.S_UNLOADING: (ORANGE_BG, ORANGE_FG),
        tracker.S_AT_VIA: (BLUE_BG, BLUE_FG),
        tracker.S_REVIEW: (REVIEW_BG, REVIEW_FG),
    }
    ARR_COLOR = {
        tracker.A_TRANSIT: (BLUE_BG, BLUE_FG),
        tracker.A_COMPLETED: (OK_BG, OK_FG),
        tracker.A_NOT_ON_TRIP: (GREY_BG, GREY_FG),
    }
    for i, r in enumerate(rows):
        rr = i + 1
        # trust lamp on Vehicle No
        cell(rr, H("Vehicle No"), TRUST_BG.get(r.get("_trust", "ok"),
                                               TRUST_BG["ok"]), None)
        st = (r.get("Status") or "").strip()
        if st in STATUS_COLOR:
            cell(rr, H("Status"), *STATUS_COLOR[st])
        elif st.upper() in tracker.MANUAL_STATUSES:
            cell(rr, H("Status"), GREY_BG, GREY_FG)
        arr = (r.get("ARRIVAL STATUS") or "").strip()
        if arr in ARR_COLOR:
            cell(rr, H("ARRIVAL STATUS"), *ARR_COLOR[arr])
        perf = (r.get("Performance") or "").strip()
        if perf == tracker.P_ONTIME:
            cell(rr, H("Performance"), OK_BG, OK_FG)
        elif perf == tracker.P_DELAY:
            bg, fg = _delay_shade(_late_h(r.get("Late Hrs")))
            cell(rr, H("Performance"), bg, fg)
            if (r.get("Late Hrs") or "").strip():
                cell(rr, H("Late Hrs"), bg, fg)
        # predicted rows: the whole planned part goes italic
        if r.get("_trust") == "pred":
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": rr,
                          "endRowIndex": rr + 1, "startColumnIndex": 0,
                          "endColumnIndex": H("Actual Arrival Date&Time") + 1},
                "cell": {"userEnteredFormat": {"textFormat": {"italic": True}}},
                "fields": "userEnteredFormat.textFormat.italic"}})
    from tracking_suite.sheetio import _with_quota_retry
    _with_quota_retry(lambda: ss.batch_update({"requests": reqs}),
                      f"paint '{title}'")
    print(f"  [Sheet] '{title}': colours painted ({len(reqs)} format op(s))",
          flush=True)


# ── Extra Trips — permanent record of trips the website never filed ──────────
XHEADERS = ["Detected On", "Vehicle No", "From", "To", "Via", "DEP (hub exit)",
            "Confidence", "Evidence", "Site Trip ID", "Outcome"]
_OPEN = {"", "FORMING", "PREDICTED"}


def _extra_write(ss, title, ghosts, reals, now):
    """Append-and-update: one row per detected ghost trip, never deleted.
    Open rows are reconciled with the real Trip ID once the site files it."""
    from tracking_suite.sheetio import _with_quota_retry

    def dep_dt(s):
        try:
            return datetime.strptime(str(s).strip(), tracker._DT_OUT)
        except (ValueError, TypeError):
            return None

    try:
        cur = sheetio.read_records(ss, title)
    except RuntimeError:
        cur = []
    rows = [dict(r) for r in cur if (r.get("Vehicle No") or "").strip()]

    # 1 · reconcile: the site finally filed a trip for an open prediction.
    # Back-to-back predicted trips (out leg + return leg) can both sit within
    # the window, so the filed trip claims only the CLOSEST departure.
    best = {}
    for i, r in enumerate(rows):
        if str(r.get("Outcome", "")).strip().upper() not in _OPEN:
            continue
        v = (r.get("Vehicle No") or "").strip().upper()
        real = reals.get(v)
        if not real:
            continue
        rid, rdep = real
        pd = dep_dt(r.get("DEP (hub exit)"))
        if pd and rdep:
            d = abs((rdep - pd).total_seconds())
            if d <= 12 * 3600 and (v not in best or d < best[v][0]):
                best[v] = (d, i, rid)
    for v, (_, i, rid) in best.items():
        rows[i]["Site Trip ID"] = rid
        rows[i]["Outcome"] = "CONFIRMED by site (late)"

    # 2 · upsert this run's ghosts (same vehicle + same departure = same trip)
    for vno, g in ghosts:
        hit = None
        for r in rows:
            if (r.get("Vehicle No") or "").strip().upper() == vno \
                    and str(r.get("Outcome", "")).strip().upper() in _OPEN:
                od = dep_dt(r.get("DEP (hub exit)"))
                if od and abs((od - g.dep).total_seconds()) <= 6 * 3600:
                    hit = r
                    break
        human_id = ((hit or {}).get("Site Trip ID") or "").strip()
        rec = {
            "Detected On": (hit or {}).get("Detected On")
                           or now.strftime(tracker._DT_OUT),
            "Vehicle No": vno,
            "From": g.origin,
            "To": g.dest or " / ".join(d for d, _ in g.candidates[:2]),
            "Via": ", ".join(g.vias),
            "DEP (hub exit)": g.dep.strftime(tracker._DT_OUT),
            "Confidence": f"{g.confidence:.0%}",
            "Evidence": g.evidence,
            "Site Trip ID": human_id,
            "Outcome": ("CONFIRMED by human" if human_id
                        else ("PREDICTED" if g.locked else "FORMING")),
        }
        if hit:
            hit.update(rec)
        else:
            rows.append(rec)

    ws = sheetio.get_or_create(ss, title, len(XHEADERS), len(rows) + 40)
    matrix = [XHEADERS] + [[r.get(h, "") for h in XHEADERS] for r in rows]
    matrix += [[""] * len(XHEADERS) for _ in range(20)]
    _with_quota_retry(lambda: ws.update(values=matrix, range_name="A1",
                                        value_input_option="USER_ENTERED"),
                      f"write '{title}'")
    try:
        from tracking_suite.sheetio import HEADER_BG, HEADER_FG
        ws.format("A1:J1", {"backgroundColor": HEADER_BG,
                            "textFormat": {"bold": True,
                                           "foregroundColor": HEADER_FG}})
        ws.freeze(rows=1)
    except Exception:
        pass
    print(f"  [Sheet] '{title}': {len(rows)} ghost trip(s) on record",
          flush=True)


# ── Completed Trips — every finished trip, filed or predicted, no duplicates ─
CHEADERS = ["RPS_Number", "Vehicle_Number", "Vehicle_Size", "Route",
            "Route_Code", "Route_TAT", "Start_Time", "End_Time",
            "Transit_Time", "Extra_Touching_Time", "Actual_Transit_Time",
            "Delay_Hours", "Late Reason", "Status"]
TAB_COMPLETED = "Completed Trips"


def _dur(hours) -> str:
    if hours is None or hours == "":
        return ""
    m = int(round(float(hours) * 60))
    return f"{m // 60:02d}:{m % 60:02d}:00"


def _completed_write(ss, types, hubs, index, sheet_tats, rows, now):
    """One row per trip THE TRACKER ITSELF verified as completed (arrival
    proven by GPS + the 2h unloading window elapsed) — in the trip-sheet
    format. NOT a history dump: a trip enters this tab only when the live
    board shows it COMPLETED, predicted and confirmed alike. Dedup: RPS
    number is the key; predicted trips key on vehicle + departure, and are
    REPLACED (not duplicated) when the site later files the same trip.
    Late Reason is a human column — always preserved."""
    from tracking_suite.sheetio import _with_quota_retry
    atlas = tracker.Atlas(hubs, index)
    names = hubs["names"]

    def nm(code):
        return names.get(code, code)

    def parse_dt(s):
        try:
            return datetime.strptime(str(s).strip(), tracker._DT_OUT)
        except (ValueError, TypeError):
            return None

    try:
        cur = sheetio.read_records(ss, TAB_COMPLETED)
    except RuntimeError:
        cur = []
    byid: dict = {}
    for r in cur:
        v = (r.get("Vehicle_Number") or "").strip().upper()
        if not v:
            continue
        rid = (r.get("RPS_Number") or "").strip()
        if rid and rid.upper() != "PREDICTED":
            k = ("rps", rid.lstrip("0"))
        else:
            k = ("pred", v, (r.get("Start_Time") or "").strip())
        byid.setdefault(k, dict(r))

    def find_pred(vno, start):
        # closest departure wins — two predicted legs can both be in window
        cand = None
        for k, r in byid.items():
            if k[0] == "pred" and k[1] == vno:
                sd = parse_dt(r.get("Start_Time"))
                if sd and start:
                    d = abs((sd - start).total_seconds())
                    if d <= 12 * 3600 and (cand is None or d < cand[0]):
                        cand = (d, k)
        return cand[1] if cand else None

    def make_rec(rps, vno, o, vias, d, start, end, status, late_reason=""):
        tat_h, _ = tracker.lane_tat({}, atlas, o, d, sheet=sheet_tats)
        transit_h = (end - start).total_seconds() / 3600 \
            if (start and end) else None
        delay = (max(0.0, transit_h - tat_h)
                 if (transit_h is not None and tat_h) else None)
        return {
            "RPS_Number": rps or "PREDICTED",
            "Vehicle_Number": vno,
            "Vehicle_Size": types.get(vno, ""),
            "Route": " - ".join(nm(x) for x in [o] + vias + [d] if x),
            "Route_Code": (f"{o}-" + ";".join(vias + [d])) if (o and d) else "",
            "Route_TAT": _dur(tat_h),
            "Start_Time": start.strftime(tracker._DT_OUT) if start else "",
            "End_Time": end.strftime(tracker._DT_OUT) if end else "",
            "Transit_Time": _dur(transit_h),
            "Extra_Touching_Time": "",
            "Actual_Transit_Time": _dur(transit_h),
            "Delay_Hours": _dur(delay) if delay else "",
            "Late Reason": late_reason,
            "Status": status,
        }

    def upsert(k, rec):
        old = byid.get(k)
        if old:
            keep = (old.get("Late Reason") or "").strip()
            touch = (old.get("Extra_Touching_Time") or "").strip()
            old.update(rec)
            if keep:
                old["Late Reason"] = keep
            if touch:
                old["Extra_Touching_Time"] = touch
        else:
            byid[k] = rec

    # every trip the LIVE board just verified as completed
    for r in rows:
        if r.get("ARRIVAL STATUS") != tracker.A_COMPLETED:
            continue
        vno = r["Vehicle No"]
        tid = str(r.get("Trip ID", "")).strip()
        start = parse_dt(r.get("DEP Date&Time"))
        end = parse_dt(r.get("Actual Arrival Date&Time"))
        if not (tid and start and end and end > start):
            continue
        o, d = r.get("From", ""), r.get("To", "")
        vias = [v.strip() for v in str(r.get("Via Point", "")).split(",")
                if v.strip()]
        # the trip's journal (human notes + dated machine events, minus the
        # volatile "now" line) becomes the Late Reason — the why behind Delay
        journal = "; ".join(
            ln.strip() for ln in str(r.get("Remark", "")).splitlines()
            if ln.strip() and not ln.strip().startswith("now — ")
            and ln.strip() != "trip completed"
            and not ln.strip().endswith("— trip completed"))
        if tid.upper() == "PREDICTED":
            k = find_pred(vno, start) or ("pred", vno,
                                          start.strftime(tracker._DT_OUT))
            upsert(k, make_rec("", vno, o, vias, d, start, end,
                               "COMPLETED (PREDICTED)", journal))
        else:
            pk = find_pred(vno, start)
            if pk:
                del byid[pk]        # the site filed our prediction: one row
            upsert(("rps", tid.lstrip("0")),
                   make_rec(tid, vno, o, vias, d, start, end, "COMPLETED",
                            journal))

    out = list(byid.values())
    out.sort(key=lambda r: parse_dt(r.get("End_Time")) or datetime.min,
             reverse=True)
    ws = sheetio.get_or_create(ss, TAB_COMPLETED, len(CHEADERS), len(out) + 40)
    matrix = [CHEADERS] + [[r.get(h, "") for h in CHEADERS] for r in out]
    matrix += [[""] * len(CHEADERS) for _ in range(20)]
    _with_quota_retry(lambda: ws.update(values=matrix, range_name="A1",
                                        value_input_option="USER_ENTERED"),
                      f"write '{TAB_COMPLETED}'")
    try:
        from tracking_suite.sheetio import HEADER_BG, HEADER_FG
        dur = {"numberFormat": {"type": "TIME", "pattern": "[h]:mm:ss"}}
        last = len(out) + 20
        for col in ("Route_TAT", "Transit_Time", "Extra_Touching_Time",
                    "Actual_Transit_Time", "Delay_Hours"):
            a = sheetio.col_a1(CHEADERS.index(col))
            ws.format(f"{a}2:{a}{last}", dur)
        ws.format("A1:N1", {"backgroundColor": HEADER_BG,
                            "textFormat": {"bold": True,
                                           "foregroundColor": HEADER_FG}})
        ws.freeze(rows=1)
    except Exception:
        pass
    print(f"  [Sheet] '{TAB_COMPLETED}': {len(out)} tracker-completed trip(s)",
          flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted.\n", flush=True)
        sys.exit(130)
    except (RuntimeError, config.SheetLockError) as exc:
        print(f"\n  [STOP] {exc}\n", flush=True)
        sys.exit(2)
