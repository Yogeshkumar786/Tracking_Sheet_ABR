"""
Tracker — evidence kernel + board renderer.
===========================================
The board's format is unchanged (the dispatch-sheet columns). What is rigid is
HOW every cell is derived. The bug pattern this kills: the same question being
answered differently in different code paths, so fixing one path leaves the
others wrong.

THE KERNEL — one authority per question:

  Fix        the vehicle's position+time, VALIDATED ONCE. If the last fix is
             stale, or older than the trip's own departure, the Fix is unusable
             and no downstream code can read coordinates off it. There is no
             second way to get a position.
  site_dt()  the only gate for website timestamps. Future-dated or unparseable
             -> None, with the reason recorded. No site time reaches any maths
             without passing here.
  place_of() the only function that answers "at which hub is this point".
             Pin distance, then location-text code, then clean-name resolution
             — in that order, once. Current Location, arrival, loading and via
             checks all call this one function.
  stopped()  the only motion authority: derived from the trail (distance over
             the last hour); the website's speed/isRunning are used only when
             the trail is too thin AND the fix is fresh, and that fallback is
             recorded in the evidence log.

INVARIANTS — enforced centrally in check(), not scattered as ifs:

  I1  every event time lies in [departure, now]
  I2  no event may be derived from a stale Fix
  I3  arrival requires: fresh Fix + at destination region + stopped
  I4  a claim that fails a check is not clamped or guessed — it is dropped and
      the reason appears in Remark ("unverified: ...")

Human edits (manual Trip ID, manual Status, Remark) are merged at the very end,
in exactly one place.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

from tracking_suite import fms
from tracking_suite import estimator
from tracking_suite import ghost as ghostmod
from tracking_suite import viatouch
from tracking_suite.presence import haversine_m

# ── board vocabulary (unchanged) ─────────────────────────────────────────────

HEADERS = ["Trip ID", "Vehicle No", "Vehicle Type", "From", "To", "Via Point",
           "TAT", "DEP Date&Time", "SCH Arrival Date&Time",
           "Actual Arrival Date&Time", "Status", "Performance",
           "Current Location", "Late Hrs", "Remark", "ARRIVAL STATUS"]

S_RUNNING = "RUNNING"
S_LOADING = "WATING FOR LOADING"
S_UNLOADING = "WATING FOR UNLOADING"
S_AT_VIA = "AT VIA"
S_REVIEW = "REVIEW / ALERT"
MANUAL_STATUSES = {"BREAKDOWN", "ACCIDENT", "MAINTENANCE", "DOCUMENT ISSUE"}
STATUS_VALUES = [S_RUNNING, S_LOADING, S_UNLOADING, S_AT_VIA, S_REVIEW,
                 "BREAKDOWN", "ACCIDENT", "MAINTENANCE", "DOCUMENT ISSUE"]

P_ONTIME = "*ON TIME"
P_DELAY = "DELAY"

A_TRANSIT = "IN TRANSIT"
A_COMPLETED = "COMPLETED"
A_NOT_ON_TRIP = "NOT ON TRIP"
ARRIVAL_VALUES = [A_TRANSIT, A_COMPLETED, A_NOT_ON_TRIP]

# ── tunables ─────────────────────────────────────────────────────────────────

FRESH_HRS = 6.0        # a fix older than this is stale: no positional claims
UNLOAD_HRS = 2.0       # unloading window after arrival, then COMPLETED
HUB_MIN_RADIUS = 1500.0
TRAIL_HOURS = 36
TRAIL_EXT_HOURS = 24 * 7
VIA_NEAR_KM = 60.0
MIN_TAT_RUNS = 2
MOVE_KM_PER_HR = 2.0   # trail movement below this over an hour = stopped

_DT_OUT = "%d/%m/%Y %H:%M:%S"

# Phrases this tracker itself writes into Remark. On re-read, a remark made
# only of these is OURS (stale) and must be regenerated — only text a human
# actually typed is preserved. Without this, our own old notes masquerade as
# human edits and stick forever.
_MACHINE_REMARK = re.compile(
    r"^(unverified |no learned TAT|no Route_TAT|no TAT |trip id manual|"
    r"motion from site|stopped en route|at via |via [A-Z0-9]|no fix |"
    r"GPS silent|est \+|may have STOPPED|resting |arrival clamped|TAT |"
    r"trip completed|not on FMS|site claims|last seen|trip details|"
    r"predicted trip|destination forming|left [A-Z0-9]|arrival ~)", re.I)


def _is_machine_remark(text: str) -> bool:
    parts = [p.strip() for p in (text or "").split(";") if p.strip()]
    return bool(parts) and all(_MACHINE_REMARK.match(p) for p in parts)


def _same_trip_id(a: str, b: str) -> bool:
    """Sheets strips leading zeros off numeric ids (0008203547 -> 8203547);
    compare with zeros normalised so that is never read as a manual edit."""
    na = (a or "").strip().lstrip("0")
    nb = (b or "").strip().lstrip("0")
    return na == nb and na != ""
_CODE_RE = re.compile(r'\(([A-Za-z0-9]{2,8})\)')


def via_codes(cell: str) -> list:
    """Via hub codes out of a Via Point cell — plain ('TAU11, JPR11') or
    annotated with timings ('TAU11 09:30→11:15 ✓ · JPR11 pending')."""
    out = []
    for part in re.split(r'[·,]', cell or ""):
        tok = part.strip().split()
        if tok and re.fullmatch(r'[A-Z0-9]{3,10}', tok[0]):
            out.append(tok[0])
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  KERNEL
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Evidence:
    """Two kinds of truth about a trip:
    notes  — VOLATILE state, rewritten every run ("GPS silent 3h", "trip id
             manual"). They live in the Remark's single "now — ..." line.
    events — the trip's JOURNAL: dated entries that persist for the life of
             the trip (via passages, long stops, cleared breakdowns). On
             completion the whole journal becomes the Late Reason."""
    notes: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def refuse(self, what: str, why: str):
        self.notes.append(f"unverified {what}: {why}")

    def note(self, text: str):
        self.notes.append(text)

    def log(self, text: str):
        self.events.append(text)

    def text(self) -> str:
        return "; ".join(dict.fromkeys(self.notes))


# ── the Remark journal ───────────────────────────────────────────────────────
# One cell, three kinds of lines, told apart mechanically:
#   "27/08 14:30 — ..."  machine journal entry (ours to update or expire)
#   "now — ..."          machine volatile state (ours, rewritten every run)
#   anything else        HUMAN — never touched, always kept, always first
_JSTAMP = re.compile(r'^\d{2}/\d{2} \d{2}:\d{2} — ')
_MAX_JOURNAL = 12


def _jkey(text: str) -> str:
    """Numeric-insensitive identity of a journal line, so 'stopped 1.2h near
    JAIPUR' UPDATES to 'stopped 2.4h near JAIPUR' instead of duplicating."""
    return re.sub(r'[\d.,:%]+', '#', text)


def assemble_remark(prev: str, wiped: bool, ev: Evidence,
                    now: datetime) -> str:
    human, events = [], []
    if not wiped:
        for ln in (prev or "").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("now — "):
                continue
            if _JSTAMP.match(ln):
                events.append(ln)
            elif _is_machine_remark(ln):
                continue            # legacy machine remark — regenerated
            else:
                human.append(ln)
    for t in dict.fromkeys(ev.events):
        k = _jkey(t)
        for i, e in enumerate(events):
            body = _JSTAMP.sub("", e)
            if _jkey(body) == k:
                events[i] = e[:len(e) - len(body)] + t   # keep original stamp
                break
        else:
            events.append(f"{now:%d/%m %H:%M} — {t}")
    events = events[-_MAX_JOURNAL:]
    vol = ev.text()
    return "\n".join(human + events + ([f"now — {vol}"] if vol else []))


@dataclass
class Fix:
    """The vehicle's validated position. `fresh` is the single usability gate:
    stale or pre-trip fixes expose NO claim coordinates. raw_lat/raw_lon are the
    last-known point for the ESTIMATOR ONLY — estimates and review text, never
    arrivals, vias or locations-as-fact."""
    lat: float = 0.0
    lon: float = 0.0
    t: datetime | None = None
    age_h: float = 999.0
    fresh: bool = False
    raw_lat: float = 0.0
    raw_lon: float = 0.0


def read_fix(live: dict, now: datetime, dep: datetime | None,
             ev: Evidence) -> Fix:
    lat, lon = fms.position(live)
    t = fms.last_gps_dt(live)
    if not (lat and lon and t):
        ev.refuse("position", "no usable GPS fix")
        return Fix()
    age = (now - t).total_seconds() / 3600
    if age > FRESH_HRS:
        ev.refuse("position", f"GPS silent {age:.0f}h")
        return Fix(t=t, age_h=age, fresh=False, raw_lat=lat, raw_lon=lon)
    if dep and t < dep:
        ev.refuse("position", "last fix predates this trip's departure")
        return Fix(t=t, age_h=age, fresh=False, raw_lat=lat, raw_lon=lon)
    return Fix(lat=lat, lon=lon, t=t, age_h=age, fresh=True,
               raw_lat=lat, raw_lon=lon)


def site_dt(raw, now: datetime, what: str, ev: Evidence) -> datetime | None:
    """THE gate for website timestamps. Future or unparseable -> None."""
    s = str(raw or "").strip()
    if not s or s.upper() == "NA":
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%b-%Y %H:%M:%S",
                "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            t = datetime.strptime(s, fmt)
            if t > now + timedelta(minutes=5):
                ev.refuse(what, "site timestamp is in the future")
                return None
            return t
        except ValueError:
            continue
    ev.refuse(what, f"unreadable site timestamp '{s[:16]}'")
    return None


@dataclass
class Place:
    code: str = ""
    region: str = ""
    label: str = ""
    how: str = ""       # pin | loc-code | name | none


class Atlas:
    """Wraps the Hub List. place_of() is the ONLY hub-resolution authority."""

    def __init__(self, hubs: dict, index: list):
        self.by_code = hubs["by_code"]
        self.names = hubs["names"]
        self.cluster = hubs["cluster"]
        self.radius = hubs["radius"]
        self.index = index
        self.pending = hubs.get("pending", set())
        self.vstatus = hubs.get("vstatus", {})
        # spatial grid (~2.2 km cells): "nearest hub to this point" becomes a
        # 9-cell lookup instead of a 477-hub scan — the ghost detector walks
        # thousands of trail points through this
        self._cell = 0.02
        self._grid: dict = {}
        for code, (la, lo) in self.by_code.items():
            k = (int(la / self._cell), int(lo / self._cell))
            self._grid.setdefault(k, []).append((code, la, lo))

    def nearest(self, lat: float, lon: float):
        """(code, dist_m) of the nearest hub within ~2 km, else ("", inf)."""
        cx, cy = int(lat / self._cell), int(lon / self._cell)
        best, bd = "", float("inf")
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for code, la, lo in self._grid.get((cx + dx, cy + dy), ()):
                    d = haversine_m(lat, lon, la, lo)
                    if d < bd:
                        best, bd = code, d
        return best, bd

    def region(self, code: str) -> str:
        return self.cluster.get(code, code) if code else ""

    def rad(self, code: str) -> float:
        return max(self.radius.get(code, HUB_MIN_RADIUS), HUB_MIN_RADIUS)

    def resolve_ident(self, ident: str) -> str:
        """Code or messy name -> Hub List code (the holding-report rule:
        search each clean hub name INTO the text, longest first)."""
        if not ident:
            return ""
        if ident in self.by_code:
            return ident
        n = _norm(ident)
        for key, code in self.index:
            if key and key in n:
                return code
        return ""

    def place_of(self, fix: Fix, loc_text: str) -> Place:
        """Where is this vehicle? One answer, one method chain."""
        if fix.fresh:
            best, bd = self.nearest(fix.lat, fix.lon)
            if best and bd <= self.rad(best):
                return Place(best, self.region(best), best, "pin")
        m = _CODE_RE.search(loc_text or "")
        if m and m.group(1).upper() in self.by_code:
            c = m.group(1).upper()
            return Place(c, self.region(c), c, "loc-code")
        c = self.resolve_ident(loc_text)
        if c:
            return Place(c, self.region(c), c, "name")
        t = re.sub(r'\([^)]*\)', '', loc_text or '').strip()
        return Place("", "", t[:28], "none")


def _norm(s: str) -> str:
    s = re.sub(r'\([^)]*\)', '', s or '')
    s = s.upper().replace("SAFEXPRESS", "").replace(" HUB", "")
    s = re.sub(r'[^A-Z0-9]+', ' ', s)
    return " ".join(s.split())


def build_index(names: dict) -> list:
    idx = [(_norm(nm), c) for c, nm in names.items() if _norm(nm)]
    idx.sort(key=lambda t: -len(t[0]))
    return idx


# ── trail (position history) ────────────────────────────────────────────────

def _ping_dt(p):
    for f in ("datetimestamp", "datetime", "gpsdatetime", "dateTime"):
        raw = p.get(f)
        if raw:
            for fmt in fms._DT_FORMATS:
                try:
                    return datetime.strptime(str(raw).strip(), fmt)
                except ValueError:
                    continue
    return None


_TRAIL_CACHE = Path(__file__).resolve().parent / ".cache" / "trails"
_TRAIL_KEEP_H = 24 * 7.0


def _fetch_range(vid: int, frm: datetime, to: datetime) -> list:
    pts = []
    for p in fms.tracking_report(vid, frm, to):
        try:
            la, lo = float(p.get("latitude") or 0), float(p.get("longitude") or 0)
        except (TypeError, ValueError):
            continue
        dt = _ping_dt(p)
        if la and lo and dt and dt <= to + timedelta(minutes=5):
            pts.append((dt, la, lo))
    return pts


def fetch_trail(live: dict, now: datetime, hours: float = TRAIL_HOURS) -> list:
    """Position history for the window — INCREMENTALLY. Consecutive runs
    overlap almost entirely, so pings are cached on disk per vehicle and only
    the missing edges (newer than the cache, or older when the window grows)
    are fetched from FMS. Cold cache = one full fetch, same as before."""
    try:
        vid = int(live.get("vehicleId"))
    except (TypeError, ValueError):
        return []
    win_start = now - timedelta(hours=hours)
    cachef = _TRAIL_CACHE / f"{vid}.json"
    cached: list = []
    try:
        import json as _json
        raw = _json.loads(cachef.read_text(encoding="utf-8"))
        cached = [(datetime.fromisoformat(t), la, lo) for t, la, lo in raw]
    except Exception:
        cached = []
    keep_from = now - timedelta(hours=_TRAIL_KEEP_H)
    cached = [p for p in cached if p[0] >= keep_from]

    # the dashboard already says when this truck LAST sent GPS — if our cache
    # already ends there, the server has nothing new: skip the call entirely.
    # Guards: only trusted when the timestamp exists and is under 6h old
    # (silent/REVIEW trucks always get a real fetch to double-check).
    hint = fms.last_gps_dt(live)
    up_to_date = bool(
        cached and hint and (now - hint) < timedelta(hours=FRESH_HRS)
        and cached[-1][0] >= hint - timedelta(seconds=90))

    pts = list(cached)
    if cached:
        # newer edge: everything since the last cached ping
        if not up_to_date and now - cached[-1][0] > timedelta(minutes=2):
            pts += _fetch_range(vid, cached[-1][0], now)
        # older edge: the window reaches further back than the cache does
        if cached[0][0] - win_start > timedelta(minutes=30):
            pts += _fetch_range(vid, win_start, cached[0][0])
    else:
        pts = _fetch_range(vid, win_start, now)

    pts.sort()
    out = []
    for p in pts:
        if not out or p[0] > out[-1][0]:
            out.append(p)
    try:
        import json as _json
        _TRAIL_CACHE.mkdir(parents=True, exist_ok=True)
        cachef.write_text(_json.dumps(
            [[p[0].isoformat(), p[1], p[2]] for p in out]), encoding="utf-8")
    except Exception:
        pass
    return [p for p in out if p[0] >= win_start]


def stay_at(trail: list, hlat: float, hlon: float, radius_m: float):
    """(entered, exited|None) of the LATEST stay at a point. <=2 stray
    out-of-radius pings are absorbed as drift."""
    if not trail:
        return None, None
    inr = [haversine_m(la, lo, hlat, hlon) <= radius_m for _, la, lo in trail]
    i = len(trail) - 1
    while i >= 0 and not inr[i]:
        i -= 1
    if i < 0:
        return None, None
    end = i
    start, out = i, 0
    while i >= 0:
        if inr[i]:
            start, out = i, 0
        else:
            out += 1
            if out > 2:
                break
        i -= 1
    entered = trail[start][0]
    exited = None if end == len(trail) - 1 else trail[end + 1][0]
    return entered, exited


def interp_arrival(trail: list, entered: datetime, fix: Fix,
                   ev: Evidence) -> datetime:
    """The blackout case: last fix OUTSIDE the hub at T1, GPS gap, first fix
    AT the hub at T2. The truck reached somewhere inside (T1, T2] — estimate
    where with its own road speed, clamped inside the gap. Gap <= 15 min or
    speed unknown -> keep T2 (the confirmed-by time)."""
    prev = None
    for p in trail:
        if p[0] < entered:
            prev = p
        else:
            break
    if prev is None:
        return entered
    gap_h = (entered - prev[0]).total_seconds() / 3600
    if gap_h * 60 <= 15:
        return entered
    v_hat, _, _ = estimator.speed_state(
        [p for p in trail if p[0] <= prev[0]], prev[0])
    if not v_hat or v_hat < 5:
        return entered
    d_km = haversine_m(prev[1], prev[2], fix.lat, fix.lon) / 1000
    est = prev[0] + timedelta(hours=d_km / v_hat)
    if not (prev[0] < est < entered):
        return entered              # physics says it needed the whole gap
    ev.log(f"arrival ~{est:%H:%M} (est, interpolated across "
           f"{gap_h:.1f}h GPS gap)")
    return est


def stopped(trail: list, fix: Fix, live: dict, ev: Evidence) -> bool:
    """THE motion authority. Trail first; site hint only as a recorded fallback."""
    recent = [p for p in trail if fix.t and (fix.t - p[0]).total_seconds() <= 3600]
    if len(recent) >= 2:
        d = haversine_m(recent[0][1], recent[0][2], recent[-1][1], recent[-1][2])
        return d < MOVE_KM_PER_HR * 1000
    if fix.fresh:
        ev.note("motion from site flag (trail thin)")
        return not fms.running(live)
    return True   # stale fix: cannot be shown moving


# ── invariants ───────────────────────────────────────────────────────────────

def check_event(t: datetime | None, dep: datetime | None, now: datetime,
                fix: Fix, what: str, ev: Evidence) -> datetime | None:
    """I1 + I2 in one place: event within [dep, now], from a fresh fix."""
    if t is None:
        return None
    if not fix.fresh:
        ev.refuse(what, "no fresh position to support it")
        return None
    if t > now + timedelta(minutes=5):
        ev.refuse(what, "in the future")
        return None
    if dep and t < dep:
        ev.refuse(what, "before this trip's departure")
        return None
    return t


# ═════════════════════════════════════════════════════════════════════════════
#  TRIP CONTEXT
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class TripCtx:
    trip_id: str = ""
    origin: str = ""       # resolved codes ('' if unresolvable)
    dest: str = ""
    vias: list = field(default_factory=list)
    dep: datetime | None = None
    source: str = ""       # site | rps | none

    @property
    def active(self) -> bool:
        return bool(self.trip_id or self.dest)


def read_trip(live: dict, trips: list, atlas: Atlas, now: datetime,
              ev: Evidence) -> TripCtx:
    t = TripCtx()
    if fms.on_trip(live):
        t.trip_id = fms.current_rps(live)
        codes = [c.strip().upper() for c in
                 str(live.get("consigneeCode") or "").split(";")
                 if c.strip() and c.strip().upper() != "NA"]
        raw_dest = codes[-1] if codes else fms.consignee_name(live)
        raw_vias = codes[:-1]
        t.dest = atlas.resolve_ident(raw_dest) or _norm(raw_dest)
        t.vias = [atlas.resolve_ident(v) or v for v in raw_vias]
        raw_org = fms.origin_code(live) or fms.consigner_name(live)
        t.origin = atlas.resolve_ident(raw_org) or _norm(raw_org)
        t.dep = site_dt(live.get("dispatchDate"), now, "departure", ev)
        t.source = "site"
        return t
    if trips:
        r = trips[0]
        if r.get("start_dt") and not r.get("end_dt") \
                and (now - r["start_dt"]).days < 7:
            segs = r.get("segs") or []
            t.trip_id = r.get("rps", "")
            if segs:
                t.origin = atlas.resolve_ident(segs[0]) or segs[0]
                t.dest = atlas.resolve_ident(segs[-1]) or segs[-1]
                t.vias = [atlas.resolve_ident(x) or x for x in segs[1:-1]]
            t.dep = r["start_dt"] if r["start_dt"] <= now else None
            t.source = "rps"
    return t


# ═════════════════════════════════════════════════════════════════════════════
#  TAT (learned timetables)
# ═════════════════════════════════════════════════════════════════════════════

def learn_tats(trips_by_veh: dict, cluster: dict) -> dict:
    """{(o_region, d_region): (median_hours, n_runs)} from every closed trip."""
    runs: dict = {}
    for trips in trips_by_veh.values():
        for t in trips:
            if not (t.get("start_dt") and t.get("end_dt")):
                continue
            h = (t["end_dt"] - t["start_dt"]).total_seconds() / 3600
            o, d = t.get("origin", ""), t.get("dest", "")
            if not (o and d) or o == d or not (1.0 < h < 200):
                continue
            key = (cluster.get(o, o), cluster.get(d, d))
            runs.setdefault(key, []).append(h)
    return {k: (median(v), len(v)) for k, v in runs.items()}


def distance_tat(atlas: Atlas, origin: str, dest: str) -> float | None:
    """Fallback TAT when no run history exists for the lane: straight-line
    distance x 1.45 road factor at 32 km/h effective (incl. rests). Coarse by
    design — it exists so SCH Arrival is never blank for a plannable trip, and
    it is always labelled as estimated."""
    if origin in atlas.by_code and dest in atlas.by_code:
        o, d = atlas.by_code[origin], atlas.by_code[dest]
        km = haversine_m(o[0], o[1], d[0], d[1]) / 1000 * 1.45
        if km >= 20:
            return km / 32.0
    return None


def lane_tat(tats: dict, atlas: Atlas, origin: str, dest: str,
             sheet: dict | None = None) -> tuple:
    """(hours, basis) — the FIXED Route_TAT from the trip sheets, and nothing
    else. The user's rule: TAT is an operational standard, never a calculation.
    A lane the sheets don't know gets a blank TAT and says so."""
    if not (origin and dest) or not sheet:
        return None, ""
    h = sheet.get("exact", {}).get((origin, dest))
    if h:
        return h, "sheet"
    rk = (atlas.region(origin), atlas.region(dest))
    h = sheet.get("region", {}).get(rk)
    if h:
        return h, "sheet"
    return None, ""


def tat_hhmmss(hours: float | None) -> str:
    if not hours:
        return ""
    m = int(round(hours * 60))
    return f"{m // 60:02d}:{m % 60:02d}:00"


def _f(v):
    try:
        x = float(v)
        return x if x >= 0 else None
    except (TypeError, ValueError):
        return None


def _hhmm(minutes: float) -> str:
    """Duration as H:MM:SS — with the duration number-format on the column this
    reads as elapsed hours, never as a clock time (9:12 must mean 9h12m)."""
    m = int(round(max(0.0, minutes)))
    return f"{m // 60:02d}:{m % 60:02d}:00"


# ═════════════════════════════════════════════════════════════════════════════
#  ASSESSMENT — one pipeline, evidence-ranked
# ═════════════════════════════════════════════════════════════════════════════

def build_row(vno: str, vtype: str, live: dict | None, trips: list,
              hubs: dict, index: list, tats: dict, existing: dict,
              now: datetime, sheet_tats: dict | None = None,
              pre_trail: list | None = None,
              via_prior: list | None = None) -> dict:
    atlas = Atlas(hubs, index)
    ev = Evidence()
    row = {h: "" for h in HEADERS}
    row["Vehicle No"] = vno
    row["Vehicle Type"] = vtype

    prev_status = (existing.get("Status") or "").strip().upper()
    manual_status = prev_status if prev_status in MANUAL_STATUSES else ""
    manual_trip = (existing.get("Trip ID") or "").strip()
    if manual_trip.upper() == "PREDICTED":
        manual_trip = ""          # our own ghost-trip label, not a human edit
    prev_remark = (existing.get("Remark") or "").strip()
    # a row that reached COMPLETED is a closed book: its journal moved to the
    # ledger's Late Reason, so trip id + journal are wiped for the next trip
    prev_completed = (existing.get("ARRIVAL STATUS") or "").strip() \
        == A_COMPLETED
    if prev_completed:
        manual_trip = ""

    if not live:
        row["Status"] = manual_status
        row["Trip ID"] = manual_trip
        ev.note("not on FMS")
        row["Remark"] = assemble_remark(prev_remark, prev_completed, ev, now)
        return row

    # 1 · trip context (site timestamps pass the gate exactly once)
    trip = read_trip(live, trips, atlas, now, ev)
    if manual_trip and not _same_trip_id(manual_trip, trip.trip_id):
        ev.note("trip id manual" + (f" (site: {trip.trip_id})" if trip.trip_id else ""))
        trip.trip_id = manual_trip
    # FALLBACK: a trip id whose details the dashboard doesn't carry (manual id,
    # or site gave the id but nothing else) — look the id up in the vehicle's
    # own RPS history and take route/departure from there.
    if trip.trip_id and (not trip.dest or not trip.origin or not trip.dep):
        for t_ in trips:
            if _same_trip_id(t_.get("rps", ""), trip.trip_id):
                segs = t_.get("segs") or []
                if segs:
                    trip.origin = trip.origin or (atlas.resolve_ident(segs[0])
                                                  or segs[0])
                    trip.dest = trip.dest or (atlas.resolve_ident(segs[-1])
                                              or segs[-1])
                    if not trip.vias:
                        trip.vias = [atlas.resolve_ident(x) or x
                                     for x in segs[1:-1]]
                if not trip.dep and t_.get("start_dt")                         and t_["start_dt"] <= now:
                    trip.dep = t_["start_dt"]
                ev.note("trip details from RPS history")
                break

    # 1b · carry an open predicted trip across runs — and accept a HUMAN
    # putting the real RPS on it. A ghost trip is detected while MOVING; once
    # the truck arrives it stops, so without this the prediction would vanish
    # as NOT ON TRIP one run before its own arrival could ever be verified.
    # The previous board row is the carrier. If the human replaced PREDICTED
    # with the actual RPS number, the id becomes theirs and the predicted
    # route rides along until the site or the RPS report can confirm it.
    # A row that already reached COMPLETED is not resurrected.
    prev_id = (existing.get("Trip ID") or "").strip()
    if not trip.dest \
            and (existing.get("To") or "").strip() \
            and (existing.get("ARRIVAL STATUS") or "").strip() != A_COMPLETED \
            and (prev_id.upper() == "PREDICTED"
                 or (trip.trip_id and _same_trip_id(prev_id, trip.trip_id))):
        pdep = None
        try:
            pdep = datetime.strptime(
                (existing.get("DEP Date&Time") or "").strip(), _DT_OUT)
        except ValueError:
            pass
        trip.origin = trip.origin or (existing.get("From") or "").strip()
        trip.dest = (existing.get("To") or "").strip()
        trip.vias = trip.vias or via_codes(existing.get("Via Point") or "")
        trip.dep = trip.dep or pdep
        trip.trip_id = trip.trip_id or "PREDICTED"
        trip.source = trip.source or "ghost"
        ev.note("predicted trip (carried)"
                + (" — RPS from human" if trip.trip_id.upper() != "PREDICTED"
                   else ""))

    # 2 · validated position (unusable if stale or pre-trip — everywhere)
    fix = read_fix(live, now, trip.dep, ev)
    loc_text = fms.last_location(live)
    here = atlas.place_of(fix, loc_text)
    row["Current Location"] = here.label

    # 3 · trail + motion. The runner usually hands the trail in (prefetched
    # in parallel); the lazy fetches below remain as fallback for the rare
    # vehicle the prefetch pass didn't anticipate.
    trail = list(pre_trail) if pre_trail else []
    if not trail and pre_trail is None and trip.active and fix.fresh:
        near_pts = [c for c in ([trip.dest] + trip.vias) if c in atlas.by_code]
        near = any(haversine_m(fix.lat, fix.lon, *atlas.by_code[c])
                   <= VIA_NEAR_KM * 1000 for c in near_pts)
        if near or not fms.running(live):
            trail = fetch_trail(live, now)
    is_stopped = stopped(trail, fix, live, ev)

    # 3b · ghost trips — no filed trip, no manual id, but the truck is out on
    # the road: run the prediction ladder (see ghost.py). Locked -> the row is
    # filled like a real trip, labelled PREDICTED. Forming -> origin + honest
    # candidate list only. The runner records every ghost in the Extra Trips
    # tabs and reconciles them when the site finally files the trip.
    if not trip.active and not manual_trip and fix.fresh \
            and here.how != "pin":
        if not trail and not is_stopped:
            trail = fetch_trail(live, now)
            is_stopped = stopped(trail, fix, live, ev)
        if trail and not is_stopped:
            g = ghostmod.detect(trail, trips, atlas, fix, now)
            if g:
                row["_ghost"] = g
                trip = TripCtx("PREDICTED", g.origin, "", [], g.dep, "ghost")
                if g.locked:
                    trip.dest = g.dest
                    trip.vias = list(g.vias)
                    ev.note(f"predicted trip ({g.confidence:.0%}): {g.evidence}")
                else:
                    names = " or ".join(d for d, _ in g.candidates[:2])
                    ev.note("destination forming"
                            + (f" — {names} ({g.confidence:.0%})" if names
                               else f" — {g.evidence}"))

    # 3c · a manual BREAKDOWN/etc. dies the moment GPS shows real movement —
    # the user's rule: remove the human work, the truck itself says it's fixed
    if manual_status and fix.fresh and not is_stopped:
        ev.log(f"{manual_status} cleared — vehicle moving again")
        manual_status = ""

    # 4 · schedule from learned history
    tat_h, tat_basis = lane_tat(tats, atlas, trip.origin, trip.dest,
                                sheet=sheet_tats)
    sch = trip.dep + timedelta(hours=tat_h) if (trip.dep and tat_h) else None
    if trip.active and trip.dest and not tat_h:
        ev.note("no Route_TAT in trip sheets for this lane")
    row["Trip ID"] = trip.trip_id
    row["From"] = trip.origin
    row["To"] = trip.dest
    row["Via Point"] = ", ".join(v for v in trip.vias if v)
    row["TAT"] = tat_hhmmss(tat_h)
    row["DEP Date&Time"] = trip.dep.strftime(_DT_OUT) if trip.dep else ""
    row["SCH Arrival Date&Time"] = sch.strftime(_DT_OUT) if sch else ""
    if trip.active and not tat_h and trip.dest:
        ev.note("no TAT derivable (unknown lane geometry)")

    # 5 · arrival — I3 gated, time passes check_event (I1+I2)
    arrived = None
    if trip.active and trip.dest and is_stopped and fix.fresh \
            and here.region and here.region == atlas.region(trip.dest):
        entered, _ = stay_at(trail, fix.lat, fix.lon, HUB_MIN_RADIUS)
        used = trail
        if entered and trail and entered <= trail[0][0] + timedelta(minutes=5):
            longer = fetch_trail(live, now, hours=TRAIL_EXT_HOURS)
            if longer:
                e2, _ = stay_at(longer, fix.lat, fix.lon, HUB_MIN_RADIUS)
                entered = e2 or entered
                used = longer
        if entered:
            entered = interp_arrival(used, entered, fix, ev)
        arrived = check_event(entered or fms.stopped_since(live) or fix.t,
                              trip.dep, now, fix, "arrival", ev)
    if arrived:
        row["Actual Arrival Date&Time"] = arrived.strftime(_DT_OUT)

    # 6 · vias — the touching engine (viatouch.py). One verdict per via, in
    # route order, hiccup-tolerant: clean stays CONFIRMED, gap-edged stays
    # INTERPOLATED, blackout passages INFERRED from geometry + time budget.
    at_via = ""
    if trip.active and trip.vias and fix.fresh:
        if not trail:
            hrs = TRAIL_HOURS
            if trip.dep and (now - trip.dep).total_seconds() \
                    > TRAIL_HOURS * 3600:
                hrs = TRAIL_EXT_HOURS
            trail = fetch_trail(live, now, hours=hrs)
        if not trail and via_prior:
            # pruned run: no fresh trail, but the tab already holds final
            # verdicts — display them instead of pretending "no data"
            visits = viatouch.from_rows(via_prior, trip.vias)
        else:
            visits = viatouch.assess(trip.vias, trail, atlas, trip.dep, now)
        row["_vias"] = visits
        parts = []
        for v in visits:
            if v.result == "at via now":
                at_via = v.via
                parts.append(f"{v.via} {v.entry:%H:%M}→…")
                ev.note(f"at via {v.via} since {v.entry:%H:%M} "
                        f"({v.confidence.lower()})")
            elif v.result in ("stopped", "stopped (in GPS gap)") \
                    and v.entry and v.exit:
                mark = "✓" if v.confidence == "CONFIRMED" else "est"
                parts.append(f"{v.via} {v.entry:%H:%M}→{v.exit:%H:%M} {mark}")
                ev.log(f"via {v.via} {v.entry:%d/%m %H:%M}->{v.exit:%H:%M} "
                       f"dwell {v.dwell_h:.1f}h ({v.confidence.lower()})")
            elif v.result == "passed through" and v.entry:
                parts.append(f"{v.via} ~{v.entry:%H:%M} pass")
            elif v.result == "not reached":
                parts.append(f"{v.via} pending")
            elif v.result in ("pending hub", "unknown hub"):
                parts.append(f"{v.via} ⚠ add pin")
            else:
                parts.append(f"{v.via} ?")
        if parts:
            row["Via Point"] = " · ".join(parts)

    # 7 · state machine — one decision, monotonic
    if arrived:
        done = now >= arrived + timedelta(hours=UNLOAD_HRS)
        row["ARRIVAL STATUS"] = A_COMPLETED if done else A_TRANSIT
        row["Status"] = manual_status or ("" if done else S_UNLOADING)
        if done:
            ev.log("trip completed")
    elif trip.active:
        row["ARRIVAL STATUS"] = A_TRANSIT
        if manual_status:
            row["Status"] = manual_status
        elif at_via:
            row["Status"] = S_AT_VIA
        elif not is_stopped:
            row["Status"] = S_RUNNING
        elif trip.origin and here.region \
                and here.region == atlas.region(trip.origin):
            row["Status"] = S_LOADING
        else:
            row["Status"] = ""
            if fix.fresh:
                dur_h = 0.0
                if trail:
                    ent, _ = stay_at(trail, fix.lat, fix.lon, 800.0)
                    if ent:
                        dur_h = (now - ent).total_seconds() / 3600
                if dur_h >= 0.5:
                    # a real halt is a journal event — the line updates in
                    # place as the stop grows, and survives into Late Reason
                    ev.log(f"stopped {dur_h:.1f}h near "
                           f"{here.label or 'en route'}")
                else:
                    ev.note("stopped en route")
    else:
        row["ARRIVAL STATUS"] = A_NOT_ON_TRIP
        row["Status"] = manual_status

    # 7b · the silence layer — estimate while a fix is overdue, escalate when
    # the silence outlives reason. Estimates read RAW last-known coordinates
    # (never claim coordinates) and only ever write Remark / REVIEW status.
    if trip.active and not arrived:
        if not fix.fresh and fix.t is not None:
            if not manual_status:
                row["Status"] = S_REVIEW
            if fix.age_h >= 2:
                ev.log(f"GPS silent {fix.age_h:.0f}h")
        elif fix.fresh and fix.t is not None and fix.age_h * 60 >= estimator.AGING_MIN:
            if not trail:
                trail = fetch_trail(live, now)
            est = estimator.silent_estimate(trail, fix.t, fix.raw_lat,
                                            fix.raw_lon, now)
            if est:
                ev.note(est)
    if not fix.fresh and fix.t is not None and here.label:
        row["Current Location"] = f"{here.label} (last {fix.age_h:.0f}h ago)"

    # 8 · performance — the user's progress rule. Time the covered distance
    # SHOULD have taken = TAT x (covered / total road distance); compare with
    # the time actually taken. A truck behind pace shows DELAY mid-trip, not
    # only after SCH has passed. On arrival, lateness freezes vs DEP+TAT.
    if trip.active and trip.dep and tat_h:
        if arrived:
            late_min = (arrived - (trip.dep + timedelta(hours=tat_h))
                        ).total_seconds() / 60
            row["Performance"] = P_ONTIME if late_min <= 0 else P_DELAY
            if late_min > 0:
                row["Late Hrs"] = _hhmm(late_min)
        else:
            covered = _f(live.get("coveredDistance"))
            total = _f(live.get("plannedDistance"))                 or ((covered or 0) + (_f(live.get("remainingDistance")) or 0))
            elapsed_h = (now - trip.dep).total_seconds() / 3600
            if covered is not None and total and total > 0:
                progress = min(1.0, max(0.0, covered / total))
                expected_h = tat_h * progress
                behind_h = elapsed_h - expected_h
                row["Performance"] = P_ONTIME if behind_h <= 1.0 else P_DELAY
                if behind_h > 1.0:
                    row["Late Hrs"] = _hhmm(behind_h * 60)
            else:
                # no distance telemetry — fall back to schedule comparison
                row["Performance"] = P_ONTIME if now <= sch else P_DELAY                     if sch else ""
                if sch and now > sch:
                    row["Late Hrs"] = _hhmm((now - sch).total_seconds() / 60)

    # 9 · the Remark journal: human lines untouched and first, machine events
    # dated and self-managed, volatile state on one "now — " line. A completed
    # trip's journal was moved to the ledger, so it starts blank here.
    row["Remark"] = assemble_remark(prev_remark, prev_completed, ev, now)

    # 10 · trust lamp (colour metadata, never written as a cell value):
    #   pred — the whole trip is predicted (ghost)
    #   est  — the row leans on estimates / refused claims / silence
    #   ok   — everything confirmed
    mach = ev.text() + " " + " ".join(ev.events)
    if trip.source == "ghost":
        row["_trust"] = "pred"
    elif row["Status"] == S_REVIEW or "unverified" in mach \
            or "(est" in mach or "est +" in mach or "no fix" in mach \
            or "may have STOPPED" in mach \
            or any(v.confidence in ("INTERPOLATED", "INFERRED")
                   for v in (row.get("_vias") or [])):
        row["_trust"] = "est"
    else:
        row["_trust"] = "ok"
    if trip.source in ("site", "rps") and trip.trip_id:
        row["_real"] = (trip.trip_id, trip.dep)   # for Extra Trips reconcile
    return row
