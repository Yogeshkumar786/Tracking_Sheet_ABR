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

    # built-in stopwatch: every phase laps itself, breakdown printed at the end
    import time as _clock
    _timing = []
    _tw = _clock.perf_counter()

    def _lap(label):
        nonlocal _tw
        t = _clock.perf_counter()
        _timing.append((label, t - _tw))
        _tw = t

    ss = sheetio.open_sheet()
    # ONE batched read for every tab this run needs — ten API calls become
    # one, which is what stops the 429 collisions with the hourly cloud job.
    # The Google read and the FMS dashboard call are independent, so they run
    # AT THE SAME TIME: startup costs the slower of the two, not the sum.
    from concurrent.futures import ThreadPoolExecutor as _TPE
    board_tabs = [f"{n} Tracker" for n, _ in hm.REPORTS]
    extra_tabs = [f"{n} Extra Trips" for n, _ in hm.REPORTS]
    via_tabs = [f"{n} Via Touching" for n, _ in hm.REPORTS]
    with _TPE(max_workers=2) as _ex:
        _fpre = _ex.submit(sheetio.read_many, ss,
                           [config.TAB_HUB_LIST, config.TAB_VEHICLES,
                            TAB_OLD, TAB_COMPLETED, VIA_TAB]
                           + board_tabs + extra_tabs + via_tabs)
        _flive = _ex.submit(fms.live_vehicles)
        pre = _fpre.result()
        _live_list = _flive.result()
    hubs = hublist.load(ss, rows=pre[config.TAB_HUB_LIST])
    index = tracker.build_index(hubs["names"])
    _lap("open + read all sheets / FMS dashboard (together)")

    # -- universe: Vehicles tab rows with a hub, deduped, stable order -------
    veh = pre[config.TAB_VEHICLES]
    _hub_canon = {k.strip().lower(): k for k in config.HUB_TRACKING_SHEETS}
    universe, types, seen, branch, hub_of = [], {}, set(), {}, {}
    for v in veh:
        vno = (v.get("Vehicle No") or "").strip().upper()
        hub = (v.get("Vehicle Hub") or "").strip()
        b = _branch_of(hub)
        if vno and hub and b and vno not in seen:
            seen.add(vno)
            universe.append(vno)
            types[vno] = (v.get("Vehicle Type") or "").strip()
            branch[vno] = b
            ch = _hub_canon.get(hub.lower())
            if ch:
                hub_of[vno] = ch
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
        o = []
        for r in pre[f"{name} Tracker"]:
            p = (r.get("Vehicle No") or "").strip().upper()
            if p:
                existing_rows[p] = r
                o.append(p)
        tab_order[name] = o
    for r in pre[TAB_OLD]:  # migrate from the superseded single board, once
        p = (r.get("Vehicle No") or "").strip().upper()
        if p and p not in existing_rows:
            existing_rows[p] = r
    # per-hub workbooks: ONE batched read each for all four tabs. The hub
    # team's Tracking edits OVERRIDE anything else for that hub's vehicles,
    # and the other three tabs are the persistent stores the upserts merge
    # into.
    hub_ss, hub_order, hub_pre = {}, {}, {}
    for hub in config.HUB_TRACKING_SHEETS:
        try:
            hss = sheetio.open_hub_sheet(hub)
            hub_ss[hub] = hss
            hp = sheetio.read_many(hss, [config.TAB_HUB_TRACKING,
                                         "Extra Trips", "Completed Trips",
                                         "Via Touching"])
            hub_pre[hub] = hp
            o = []
            for r in hp.get(config.TAB_HUB_TRACKING, []):
                p = (r.get("Vehicle No") or "").strip().upper()
                if p:
                    existing_rows[p] = r
                    o.append(p)
            hub_order[hub] = o
        except Exception as exc:
            print(f"  [Hub board] '{hub}' unreachable ({str(exc)[:50]}) — "
                  f"skipped this run", flush=True)
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
    # RPS history and the trip-sheet TATs are independent too — fetched at
    # the same time (the TAT side is usually a cache hit and returns at once)
    live_by = {fms.veh_no(v): v for v in _live_list}
    with _TPE(max_workers=2) as _ex:
        _ftrips = _ex.submit(routes.fetch_trips, universe, args.days, now)
        _ftats = _ex.submit(tatsheets.load_sheet_tats, hubs["cluster"])
        trips_by = _ftrips.result()
        try:
            sheet_tats = _ftats.result()
        except Exception as exc:
            print(f"  [TAT-sheet] unavailable ({str(exc)[:60]}) — using "
                  f"history", flush=True)
            sheet_tats = None
    _lap("45-day trip history / trip-sheet TATs (together)")
    # a human may have pasted coordinates into a PENDING hub row — finish it
    # now so THIS run can already time vias against the new pin
    if not args.dry_run and _complete_pending_hubs(ss, live_by, trips_by, now):
        hubs = hublist.load(ss)
        index = tracker.build_index(hubs["names"])
    tats = tracker.learn_tats(trips_by, hubs["cluster"])
    print(f"  [TAT] {len(tats)} lane timetable(s) learned from trip history",
          flush=True)
    _lap("pending-hub check + TAT learning")

    # -- build rows (only FMS-tracked vehicles) ------------------------------
    on_fms = [p for p in ordered if p in live_by]
    skipped = len(ordered) - len(on_fms)
    if skipped:
        print(f"  [Skip] {skipped} vehicle(s) not on the FMS dashboard — not shown",
              flush=True)

    # -- prefetch GPS trails IN PARALLEL (the single biggest time saver) -----
    # Sequential trail calls were ~5 min against a slow FMS; 8 workers overlap
    # the latency. PRUNING: a truck that is rolling, far from its destination,
    # with every via verdict already final, cannot change anything this run —
    # its trail is not fetched at all.
    viacur = pre[VIA_TAB] + [r for t in via_tabs for r in pre[t]] \
        + [r for hp in hub_pre.values() for r in hp.get("Via Touching", [])]
    via_by_vt = {}
    for r in viacur:
        k = ((r.get("Vehicle No") or "").strip().upper(),
             str(r.get("Trip ID") or "").strip().lstrip("0"))
        via_by_vt.setdefault(k, []).append(r)

    def _vias_final(vno, live):
        tid = (fms.current_rps(live) or "").strip().lstrip("0")
        rws = via_by_vt.get((vno, tid), [])
        return bool(rws) and all(
            (r.get("Result") or "").strip() in _FINAL_RESULTS for r in rws)

    need, pruned = [], 0
    for vno in on_fms:
        live = live_by[vno]
        on = fms.on_trip(live) or bool(
            (existing_rows.get(vno, {}).get("Trip ID") or "").strip())
        if not on and trips_by.get(vno):
            r0 = trips_by[vno][0]
            on = bool(r0.get("start_dt") and not r0.get("end_dt")
                      and (now - r0["start_dt"]).days < 7)
        if on:
            if fms.running(live) and (fms.remaining_km(live) or 0) > 100 \
                    and _vias_final(vno, live):
                pruned += 1
                continue
            need.append(vno)
        elif fms.running(live):
            need.append(vno)          # possible ghost trip: needs its trail
    trails = {}
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_one(v):
        try:
            return v, tracker.fetch_trail(live_by[v], now)
        except Exception:
            return v, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for v, t in ex.map(_fetch_one, need):
            trails[v] = t
    print(f"  [Trail] {len(need)} trail(s) prefetched in parallel"
          + (f", {pruned} pruned (nothing can change)" if pruned else ""),
          flush=True)
    _lap("GPS trails, 8 at a time")

    rows = []
    for i, vno in enumerate(on_fms, 1):
        live = live_by[vno]
        tid = (fms.current_rps(live) or "").strip().lstrip("0")
        row = tracker.build_row(vno, types.get(vno, ""), live,
                                trips_by.get(vno, []), hubs, index, tats,
                                existing_rows.get(vno, {}), now,
                                sheet_tats=sheet_tats,
                                pre_trail=trails.get(vno),
                                via_prior=via_by_vt.get((vno, tid)))
        rows.append(row)
        if i % 40 == 0:
            print(f"    [{i}/{len(on_fms)}]", flush=True)
    _lap("thinking: build all 201 rows")

    _report(rows)
    if args.dry_run:
        print("\n  DRY RUN — nothing written.\n", flush=True)
        return

    # ghost trips + real-trip ids
    all_ghosts, reals = [], {}
    for r in rows:
        if r.get("_ghost") is not None:
            all_ghosts.append((r["Vehicle No"], r["_ghost"]))
        real = r.get("_real")
        if real and real[1]:
            reals[r["Vehicle No"]] = real

    # ── EVERYTHING lives on the HUB workbooks (user decision 2026-08-29):
    # the master is READ-ONLY for the tracker — Hub List (+ pending-pin
    # rows) and the Vehicles list only. Each hub workbook is the persistent
    # home of its own Tracking board, Extra Trips, Completed Trips and Via
    # Touching, upserted with the same rules as before.
    unassigned = [r["Vehicle No"] for r in rows
                  if r["Vehicle No"] not in hub_of]
    if unassigned:
        print(f"  [Hub board] {len(unassigned)} vehicle(s) whose Vehicle Hub "
              f"matches no hub workbook — on no board: "
              + ", ".join(unassigned[:8])
              + ("…" if len(unassigned) > 8 else ""), flush=True)
    for hub, hss in hub_ss.items():
        hpre = hub_pre.get(hub, {})
        kept = [p for p in hub_order.get(hub, []) if hub_of.get(p) == hub]
        keptset = set(kept)
        hrows_by = {r["Vehicle No"]: r for r in rows
                    if hub_of.get(r["Vehicle No"]) == hub}
        ordered_h = [hrows_by[p] for p in kept if p in hrows_by] \
            + [r for p, r in hrows_by.items() if p not in keptset]
        hrows = list(hrows_by.values())
        hb = _Batch(hss)
        _write(hss, config.TAB_HUB_TRACKING, ordered_h, now, hb)
        _extra_write(hss, "Extra Trips",
                     [(v, g) for v, g in all_ghosts if hub_of.get(v) == hub],
                     reals, now, hb, cur=hpre.get("Extra Trips", []))
        _completed_write(hss, types, hubs, index, sheet_tats, hrows, now, hb,
                         cur=hpre.get("Completed Trips", []))
        _via_write(hss, "Via Touching", hrows, now, hb,
                   cur=hpre.get("Via Touching", []))
        hb.flush()
        print(f"  [Hub board] '{hub}': {len(ordered_h)} vehicle(s) — "
              f"tracking/extra/completed/via written", flush=True)
    _lap("write everything back (per hub)")
    _tot = sum(x for _, x in _timing)
    print(f"\n  [Timing] {_tot:.0f}s total — where the time went:", flush=True)
    for _lb, _sc in _timing:
        _bar = "#" * max(1, int(round(_sc / _tot * 34)))
        print(f"    {_lb:<48} {_sc:6.1f}s {_sc / _tot * 100:3.0f}%  {_bar}",
              flush=True)
    # the ONLY master write left: pending-hub rows appended to the Hub List
    # (the Hub List lives on the master by the user's rule)
    _request_pending_hubs(ss, hubs, rows, trips_by, now)
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


class _Batch:
    """Collects every tab's values and every formatting request, then delivers
    the whole run's writes in TWO API calls: one values_batchUpdate, one
    spreadsheet batch_update. Fourteen write calls become two — faster, and
    almost no write-quota pressure against the hourly cloud jobs."""

    def __init__(self, ss):
        self.ss = ss
        self.values = []
        self.reqs = []

    def add_values(self, title, matrix):
        self.values.append({"range": f"'{title}'!A1", "values": matrix})

    def fmt(self, sid, r0, r1, c0, c1, cellfmt, fields):
        self.reqs.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                      "startColumnIndex": c0, "endColumnIndex": c1},
            "cell": {"userEnteredFormat": cellfmt} if cellfmt else {},
            "fields": fields}})

    def freeze(self, sid):
        self.reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}})

    def flush(self):
        from tracking_suite.sheetio import _with_quota_retry
        if self.values:
            _with_quota_retry(lambda: self.ss.values_batch_update(
                {"valueInputOption": "USER_ENTERED", "data": self.values}),
                "batched values write")
        if self.reqs:
            _with_quota_retry(
                lambda: self.ss.batch_update({"requests": self.reqs}),
                "batched formatting")
        print(f"  [Sheet] delivered {len(self.values)} tab(s) in 1 values "
              f"call + {len(self.reqs)} format op(s) in 1 batch", flush=True)
        self.values, self.reqs = [], []


_DUR_FMT = {"numberFormat": {"type": "TIME", "pattern": "[h]:mm:ss"}}
_F_DUR = "userEnteredFormat.numberFormat"
_F_HDR = "userEnteredFormat(backgroundColor,textFormat)"


def _hdr_fmt():
    from tracking_suite.sheetio import HEADER_BG, HEADER_FG
    return {"backgroundColor": HEADER_BG,
            "textFormat": {"bold": True, "foregroundColor": HEADER_FG}}


def _mirror(hss, hb, title, headers, rows, dur_cols=()):
    """Read-only per-hub mirror tab: full overwrite each run, padded so a
    shrinking list never leaves stale rows."""
    ws = sheetio.get_or_create(hss, title, len(headers), len(rows) + 20)
    matrix = [list(headers)] + [[r.get(h, "") for h in headers] for r in rows]
    matrix += [[""] * len(headers) for _ in range(40)]
    hb.add_values(title, matrix)
    for col in dur_cols:
        c = headers.index(col)
        hb.fmt(ws.id, 1, len(rows) + 40, c, c + 1, _DUR_FMT, _F_DUR)
    hb.fmt(ws.id, 0, 1, 0, len(headers), _hdr_fmt(), _F_HDR)
    hb.freeze(ws.id)


def _write(ss, title, rows, now, batch):
    ws = sheetio.get_or_create(ss, title, len(tracker.HEADERS), len(rows) + 20)
    matrix = [tracker.HEADERS] + [[r.get(h, "") for h in tracker.HEADERS]
                                  for r in rows]
    # pad blank rows so a shrinking list never leaves stale rows behind
    # (we deliberately do NOT clear(), so user formatting/colours stay)
    matrix += [[""] * len(tracker.HEADERS) for _ in range(80)]
    batch.add_values(title, matrix)
    print(f"  [Sheet] '{title}': {len(rows)} row(s) queued", flush=True)

    sid = ws.id
    last = len(rows) + 80
    # TAT and Late Hrs are DURATIONS — force [h]:mm:ss so "9:12" can never
    # be mistaken for 9:12 AM by the sheet.
    for col in ("TAT", "Late Hrs"):
        c = tracker.HEADERS.index(col)
        batch.fmt(sid, 1, last, c, c + 1, _DUR_FMT, _F_DUR)
    batch.fmt(sid, 0, 1, 0, len(tracker.HEADERS), _hdr_fmt(), _F_HDR)
    batch.freeze(sid)

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
    batch.reqs += [
        dv(tracker.HEADERS.index("Status"), tracker.STATUS_VALUES),
        dv(tracker.HEADERS.index("Performance"),
           [tracker.P_ONTIME, tracker.P_DELAY]),
        dv(tracker.HEADERS.index("ARRIVAL STATUS"), tracker.ARRIVAL_VALUES),
    ]
    _paint(ws, rows, title, batch)


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


def _paint(ws, rows, title, batch):
    """Every semantic colour, appended to the run-wide write batch."""
    sid = ws.id
    n_cols = len(tracker.HEADERS)
    last = len(rows) + 80
    reqs = batch.reqs

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
        # a via waiting for its map pin: RED on the Via Point cell —
        # red = blocked on a human action, not an estimate
        if any(v.result in ("unknown hub", "pending hub")
               for v in (r.get("_vias") or [])):
            cell(rr, H("Via Point"), RED1_BG, RED1_FG)
        # predicted rows: the whole planned part goes italic
        if r.get("_trust") == "pred":
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": rr,
                          "endRowIndex": rr + 1, "startColumnIndex": 0,
                          "endColumnIndex": H("Actual Arrival Date&Time") + 1},
                "cell": {"userEnteredFormat": {"textFormat": {"italic": True}}},
                "fields": "userEnteredFormat.textFormat.italic"}})
    print(f"  [Sheet] '{title}': colours queued", flush=True)


# ── Extra Trips — permanent record of trips the website never filed ──────────
XHEADERS = ["Detected On", "Vehicle No", "From", "To", "Via", "DEP (hub exit)",
            "Confidence", "Evidence", "Site Trip ID", "Outcome"]
_OPEN = {"", "FORMING", "PREDICTED"}


def _extra_write(ss, title, ghosts, reals, now, batch, cur=None):
    """Append-and-update: one row per detected ghost trip, never deleted.
    Open rows are reconciled with the real Trip ID once the site files it."""
    from tracking_suite.sheetio import _with_quota_retry

    def dep_dt(s):
        try:
            return datetime.strptime(str(s).strip(), tracker._DT_OUT)
        except (ValueError, TypeError):
            return None

    if cur is None:
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
    batch.add_values(title, matrix)
    batch.fmt(ws.id, 0, 1, 0, len(XHEADERS), _hdr_fmt(), _F_HDR)
    batch.freeze(ws.id)
    print(f"  [Sheet] '{title}': {len(rows)} ghost trip(s) queued",
          flush=True)
    return rows


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


def _completed_write(ss, types, hubs, index, sheet_tats, rows, now,
                     batch, cur=None):
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

    if cur is None:
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

    def make_rec(rps, vno, o, vias, d, start, end, status, late_reason="",
                 touch_h=0.0):
        tat_h, _ = tracker.lane_tat({}, atlas, o, d, sheet=sheet_tats)
        transit_h = (end - start).total_seconds() / 3600 \
            if (start and end) else None
        # the user's model: actual transit = total elapsed minus the time
        # spent touching via hubs; delay is judged on the actual transit
        actual_h = (max(0.0, transit_h - touch_h)
                    if transit_h is not None else None)
        delay = (max(0.0, actual_h - tat_h)
                 if (actual_h is not None and tat_h) else None)
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
            "Extra_Touching_Time": _dur(touch_h) if touch_h else "",
            "Actual_Transit_Time": _dur(actual_h),
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
        vias = tracker.via_codes(str(r.get("Via Point", "")))
        # measured via dwell time for this trip (the touching engine)
        touch_h = sum((v.dwell_h or 0.0) for v in (r.get("_vias") or [])
                      if v.result in ("stopped", "stopped (in GPS gap)"))
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
                               "COMPLETED (PREDICTED)", journal, touch_h))
        else:
            pk = find_pred(vno, start)
            if pk:
                del byid[pk]        # the site filed our prediction: one row
            upsert(("rps", tid.lstrip("0")),
                   make_rec(tid, vno, o, vias, d, start, end, "COMPLETED",
                            journal, touch_h))

    out = list(byid.values())
    out.sort(key=lambda r: parse_dt(r.get("End_Time")) or datetime.min,
             reverse=True)
    ws = sheetio.get_or_create(ss, TAB_COMPLETED, len(CHEADERS), len(out) + 40)
    matrix = [CHEADERS] + [[r.get(h, "") for h in CHEADERS] for r in out]
    matrix += [[""] * len(CHEADERS) for _ in range(20)]
    batch.add_values(TAB_COMPLETED, matrix)
    last = len(out) + 20
    for col in ("Route_TAT", "Transit_Time", "Extra_Touching_Time",
                "Actual_Transit_Time", "Delay_Hours"):
        c = CHEADERS.index(col)
        batch.fmt(ws.id, 1, last, c, c + 1, _DUR_FMT, _F_DUR)
    batch.fmt(ws.id, 0, 1, 0, len(CHEADERS), _hdr_fmt(), _F_HDR)
    batch.freeze(ws.id)
    print(f"  [Sheet] '{TAB_COMPLETED}': {len(out)} tracker-completed trip(s) "
          f"queued", flush=True)
    return out


# ── Pending hubs — an unknown via becomes a Hub List row the human finishes ──
# The contract (user design): unknown via -> RED + a placeholder Hub List row
# with Latitude/Longitude "-"; the human pastes the pin from Google Maps; the
# next run completes Radius/Verify/Gap/Confidence/Spread from the fleet's own
# FMS sightings and the hub goes live for via timing.
import re as _re
_NAMECODE_RE = _re.compile(r'([A-Z][A-Z0-9 .&-]{2,40}?)\s*\(([A-Z0-9]{3,8})\)')
_PENDING_MARK = "PENDING — add Latitude/Longitude from Google Maps"


def _request_pending_hubs(ss, hubs, rows, trips_by, now):
    """Append one placeholder Hub List row per never-seen via identifier."""
    from tracking_suite.sheetio import _with_quota_retry
    unknowns = []
    for r in rows:
        for v in (r.get("_vias") or []):
            if v.result == "unknown hub" and v.via not in unknowns:
                unknowns.append(v.via)
    if not unknowns:
        return
    known = {tracker._norm(c) for c in hubs["by_code"]}
    known |= {tracker._norm(n) for n in hubs["names"].values()}
    known |= {tracker._norm(p) for p in hubs.get("pending", set())}
    # route strings often carry the FMS code for the name: "BHAGWANPUR(BGW01)"
    code_by_name = {}
    for trips in trips_by.values():
        for t in trips:
            for nm, code in _NAMECODE_RE.findall(
                    (t.get("route") or "").upper()):
                code_by_name.setdefault(tracker._norm(nm), code)
    ws = sheetio.find_tab(ss, config.TAB_HUB_LIST)
    hdr = _with_quota_retry(lambda: ws.row_values(1), "Hub List header")
    new = []
    for ident in unknowns:
        idn = tracker._norm(ident)
        if not idn or idn in known:
            continue
        known.add(idn)
        code = ident if _re.fullmatch(r'[A-Z]{2,4}\d{2,3}', ident.upper()) \
            else code_by_name.get(idn, idn.replace(" ", "")[:12])
        vals = {"Hub_Code": code.upper(), "Hub_Name": ident.title(),
                "Latitude": "-", "Longitude": "-",
                "Verify_Status": _PENDING_MARK,
                "Last_Updated": now.strftime(tracker._DT_OUT)}
        new.append([vals.get(h, "") for h in hdr])
    if new:
        _with_quota_retry(lambda: ws.append_rows(
            new, value_input_option="USER_ENTERED"), "append pending hubs")
        print(f"  [Hub List] {len(new)} pending hub row(s) added — waiting "
              f"for coordinates: "
              + ", ".join(r[0] for r in new), flush=True)


def _complete_pending_hubs(ss, live_by, trips_by, now) -> int:
    """A PENDING row where the human has now written numeric coordinates gets
    finished the tracker way: sightings of that name in the fleet's own FMS
    location texts -> Gap/Spread/Radius/Confidence + a verdict on the pin."""
    from statistics import median
    from tracking_suite.presence import haversine_m
    from tracking_suite.sheetio import _with_quota_retry
    ws = sheetio.find_tab(ss, config.TAB_HUB_LIST)
    grid = _with_quota_retry(ws.get_all_values, "Hub List for completion")
    if not grid:
        return 0
    hdr = grid[0]

    def col(name):
        return hdr.index(name) if name in hdr else None

    ic, inm = col("Hub_Code"), col("Hub_Name")
    ila, ilo = col("Latitude"), col("Longitude")
    ist = col("Verify_Status")
    if None in (ic, ila, ilo, ist):
        return 0
    done = 0
    for rix, row in enumerate(grid[1:], start=2):
        st = row[ist] if ist < len(row) else ""
        if "PENDING" not in st.upper():
            continue
        try:
            plat = float(row[ila])
            plon = float(row[ilo])
        except (TypeError, ValueError, IndexError):
            continue                      # still waiting for the human
        code = (row[ic] or "").strip().upper()
        name = (row[inm] or "").strip() if inm is not None else ""
        idn = tracker._norm(name) or tracker._norm(code)
        # vehicles whose trips touch this hub are the witnesses
        cands = []
        for vno, trips in trips_by.items():
            if vno in live_by and any(
                    tracker._norm(s) == idn or s.upper() == code
                    for t in trips for s in (t.get("segs") or [])):
                cands.append(vno)
        sight = []
        for vno in cands[:5]:
            try:
                vid = int(live_by[vno].get("vehicleId"))
            except (TypeError, ValueError):
                continue
            for p in fms.tracking_report(vid, now - timedelta(days=3), now):
                loc = str(p.get("location") or p.get("Location") or "")
                if idn and idn in tracker._norm(loc) \
                        or (code and code in loc.upper()):
                    try:
                        la = float(p.get("latitude") or 0)
                        lo = float(p.get("longitude") or 0)
                    except (TypeError, ValueError):
                        continue
                    if la and lo:
                        sight.append((la, lo))
            if len(sight) >= 200:
                break
        upd = dict(zip(hdr, row + [""] * (len(hdr) - len(row))))
        if len(sight) >= 5:
            cla = median(s[0] for s in sight)
            clo = median(s[1] for s in sight)
            gap = haversine_m(cla, clo, plat, plon)
            dists = sorted(haversine_m(cla, clo, s[0], s[1]) for s in sight)
            spread = dists[int(0.85 * (len(dists) - 1))]
            upd["Radius_M"] = str(int(min(max(spread * 2, 800), 2000)))
            upd["Gap_M"] = str(int(gap))
            upd["Spread_M"] = str(int(spread))
            upd["Verify_Status"] = ("CONFIRMED" if gap <= 400 else
                                    "CLOSE" if gap <= 800 else "INVESTIGATE")
            upd["Confidence"] = ("HIGH" if len(sight) >= 20 else
                                 "MED" if len(sight) >= 8 else "LOW")
        else:
            upd["Radius_M"] = "1500"
            upd["Verify_Status"] = "UNVERIFIED (no FMS sightings yet)"
            upd["Confidence"] = "LOW"
        if "Last_Updated" in hdr:
            upd["Last_Updated"] = now.strftime(tracker._DT_OUT)
        _with_quota_retry(lambda rix=rix, u=upd: ws.update(
            values=[[u.get(h, "") for h in hdr]],
            range_name=f"A{rix}", value_input_option="USER_ENTERED"),
            f"complete hub '{code}'")
        print(f"  [Hub List] '{code}' completed from {len(sight)} FMS "
              f"sighting(s) — {upd['Verify_Status']}", flush=True)
        done += 1
    return done


# ── Via Touching — one row per via visit, the third report of the trio ───────
VIA_TAB = "Via Touching"
VIA_HEADERS = ["Trip ID", "Vehicle No", "From", "To", "DEP Date&Time",
               "Via Hub", "Entry Time", "Exit Time", "Touching Hours",
               "Confidence", "Result", "Updated"]
_CONF_RANK = {"": 0, "INFERRED": 1, "INTERPOLATED": 2, "CONFIRMED": 3}
_FINAL_RESULTS = {"stopped", "passed through", "stopped (in GPS gap)"}


def _via_write(ss, title, rows, now, batch, cur=None):
    """Upsert one row per (trip, via). A finalised row (exit known) is only
    replaced by evidence of equal or better confidence — so a via recorded
    CONFIRMED yesterday can never be downgraded by today's truncated trail."""
    from tracking_suite.sheetio import _with_quota_retry
    if cur is None:
        try:
            cur = sheetio.read_records(ss, title)
        except RuntimeError:
            cur = []

    def key(r):
        veh = (r.get("Vehicle No") or "").strip().upper()
        tid = str(r.get("Trip ID") or "").strip().lstrip("0")
        if not tid or tid.upper() == "PREDICTED":
            tid = "P|" + (r.get("DEP Date&Time") or "").strip()
        return (veh, tid, (r.get("Via Hub") or "").strip())

    byk, order = {}, []
    for r in cur:
        if (r.get("Vehicle No") or "").strip() and (r.get("Via Hub") or "").strip():
            k = key(r)
            if k not in byk:
                byk[k] = dict(r)
                order.append(k)

    n_new = 0
    for r in rows:
        visits = r.get("_vias") or []
        if not visits:
            continue
        for v in visits:
            rec = {
                "Trip ID": r.get("Trip ID", ""),
                "Vehicle No": r["Vehicle No"],
                "From": r.get("From", ""),
                "To": r.get("To", ""),
                "DEP Date&Time": r.get("DEP Date&Time", ""),
                "Via Hub": v.via,
                "Entry Time": v.entry.strftime(tracker._DT_OUT)
                              if v.entry else "",
                "Exit Time": v.exit.strftime(tracker._DT_OUT)
                             if v.exit else "",
                "Touching Hours": _dur(v.dwell_h)
                                  if v.dwell_h is not None else "",
                "Confidence": v.confidence,
                "Result": v.result,
                "Updated": now.strftime(tracker._DT_OUT),
            }
            k = key(rec)
            old = byk.get(k)
            if old:
                old_final = bool((old.get("Exit Time") or "").strip()) \
                    and (old.get("Result") or "").strip() in _FINAL_RESULTS
                new_final = v.result in _FINAL_RESULTS
                if old_final and not new_final:
                    continue            # never downgrade a finished verdict
                if old_final and new_final \
                        and _CONF_RANK.get(v.confidence, 0) \
                        < _CONF_RANK.get((old.get("Confidence") or "").strip(), 0):
                    continue
                byk[k] = rec
            else:
                byk[k] = rec
                order.append(k)
                n_new += 1

    out = [byk[k] for k in order]
    ws = sheetio.get_or_create(ss, title, len(VIA_HEADERS), len(out) + 40)
    matrix = [VIA_HEADERS] + [[r.get(h, "") for h in VIA_HEADERS]
                              for r in out]
    matrix += [[""] * len(VIA_HEADERS) for _ in range(20)]
    batch.add_values(title, matrix)
    sid = ws.id
    c = VIA_HEADERS.index("Touching Hours")
    batch.fmt(sid, 1, len(out) + 20, c, c + 1, _DUR_FMT, _F_DUR)
    batch.fmt(sid, 0, 1, 0, len(VIA_HEADERS), _hdr_fmt(), _F_HDR)
    batch.freeze(sid)
    # RED rows = via hub waiting for its map pin (human action pending)
    batch.fmt(sid, 1, len(out) + 20, 0, len(VIA_HEADERS), None,
              "userEnteredFormat.backgroundColor")
    for i, r in enumerate(out):
        if (r.get("Result") or "").strip() in ("unknown hub", "pending hub"):
            batch.fmt(sid, i + 1, i + 2, 0, len(VIA_HEADERS),
                      {"backgroundColor": RED1_BG},
                      "userEnteredFormat.backgroundColor")
    print(f"  [Sheet] '{title}': {len(out)} via visit(s) queued "
          f"({n_new} new)", flush=True)
    return out


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted.\n", flush=True)
        sys.exit(130)
    except (RuntimeError, config.SheetLockError) as exc:
        print(f"\n  [STOP] {exc}\n", flush=True)
        sys.exit(2)
