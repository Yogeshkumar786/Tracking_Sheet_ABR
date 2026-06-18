"""
Fleetx -> Google Sheets  (manifest scraper)

Secrets required (GitHub repo Settings -> Secrets -> Actions):
    FLEETX_USERNAME      Fleetx login email
    FLEETX_PASSWORD      Fleetx login password
    GOOGLE_CREDENTIALS   Full JSON of the service-account key file
    SHEET_ID             Google Sheet ID (long string in the URL)

Optional env vars:
    FLEETX_LOOKBACK_DAYS   Days to look back (default 7)
    FLEETX_BACKFILL_FROM   YYYY-MM-DD — fetch from this date instead of rolling window

Routes tab columns (either schema, rows can be mixed):
    vehicle | from | to | enabled
    vehicle | watch_pois | enabled   (comma-separated POI list)

Sheet tabs (auto-created):
    Routes        — you fill this in
    POIs          — all Fleetx address-book entries with lat/lon/radius
    Live Tracking — current position of every watched vehicle
    _meta         — hidden; stores manual column values across sync runs
    June_AML SAFE — one manifest tab per month+destination (auto-named)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import gspread
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
HERE = Path(__file__).parent

FLEETX_API      = "https://api.fleetx.io"
TOKEN_CACHE     = HERE / ".token_cache.json"

LOOKBACK_DAYS   = int(os.environ.get("FLEETX_LOOKBACK_DAYS", "7"))
BACKFILL_FROM   = os.environ.get("FLEETX_BACKFILL_FROM")
ROUTES_TAB      = os.environ.get("ROUTES_TAB", "Routes")
POIS_TAB        = os.environ.get("POIS_TAB", "POIs")
TRACKING_TAB    = os.environ.get("TRACKING_TAB", "Live Tracking")
SHEET_ID        = os.environ.get("SHEET_ID")

MANIFEST_HEADERS = [
    "S.NO", "VEHICLE NO.", "VEHICLE TYPE", "MANIFEST NO",
    "FROM", "TO", "START DATE", "END DATE",
    "DR. NAME", "DR. CODE", "Advance(In Cash)",
    "Round Trip(HSD)", "HSD Rate/Ltr", "Total Cost",
]
# Manifest column indices that users fill in manually (never overwritten from Fleetx)
_MANIFEST_MANUAL_COLS = {2, 3, 8, 9, 10, 11, 12, 13}

META_TAB = "_meta"
META_HEADERS = [
    "tab", "key",
    "vehicle_type", "manifest_no", "dr_name", "dr_code",
    "advance", "round_trip_hsd", "hsd_rate", "total_cost",
]
# manifest col → meta col (meta cols 2+ hold the manual values)
_MANIFEST_TO_META = {2: 2, 3: 3, 8: 4, 9: 5, 10: 6, 11: 7, 12: 8, 13: 9}
_META_TO_MANIFEST = {v: k for k, v in _MANIFEST_TO_META.items()}

_FLEETX_HEADERS = {
    "accept":          "application/json, text/plain, */*",
    "accept-language": "en",
    "clientid":        "fleetxweb",
    "origin":          "https://app.fleetx.io",
    "referer":         "https://app.fleetx.io/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# Fleetx client
# ---------------------------------------------------------------------------
class FleetxClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(_FLEETX_HEADERS)
        self.token: str | None = None
        self.token_expires_at: float = 0.0

    def _set_token(self, token: str, expires_in: int) -> None:
        self.token = token
        self.token_expires_at = time.time() + expires_in - 60
        self.session.headers["authorization"] = f"Bearer {token}"

    def _load_cached_token(self) -> bool:
        if not TOKEN_CACHE.exists():
            return False
        try:
            data = json.loads(TOKEN_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        if data.get("expires_at", 0) <= time.time() + 60:
            return False
        self._set_token(data["access_token"], int(data["expires_at"] - time.time()))
        return True

    def _save_cached_token(self, token: str, expires_in: int) -> None:
        try:
            TOKEN_CACHE.write_text(json.dumps({
                "access_token": token,
                "expires_at":   time.time() + expires_in,
            }, indent=2))
        except OSError:
            pass  # read-only FS on CI — token re-fetched next run

    def login(self, force: bool = False) -> str:
        if not force and self._load_cached_token():
            return self.token  # type: ignore[return-value]

        username = os.environ.get("FLEETX_USERNAME")
        password = os.environ.get("FLEETX_PASSWORD")
        if not username or not password:
            creds_file = HERE / "creds.json"
            if not creds_file.exists():
                sys.exit("Set FLEETX_USERNAME/FLEETX_PASSWORD env vars or create creds.json")
            creds = json.loads(creds_file.read_text())
            username, password = creds["username"], creds["password"]

        resp = self.session.post(
            f"{FLEETX_API}/api/v1/login",
            files={
                "username":   (None, username.strip()),
                "password":   (None, password),
                "grant_type": (None, "password"),
            },
            headers={"clientId": "fleetxweb"},
            timeout=30,
        )
        if resp.status_code != 200:
            sys.exit(f"Fleetx login failed: HTTP {resp.status_code} {resp.text[:300]}")
        body  = resp.json()
        token = body.get("access_token") or body.get("accessToken") or body.get("token")
        if not token:
            sys.exit(f"Fleetx login OK but no token in response: {list(body)[:10]}")
        expires_in = int(body.get("expires_in") or body.get("expiresIn") or 3600)
        self._set_token(token, expires_in)
        self._save_cached_token(token, expires_in)
        print(f"[login] OK")
        return token

    def get(self, path: str, **params: Any) -> Any:
        if not self.token or time.time() >= self.token_expires_at:
            self.login()
        url  = path if path.startswith("http") else f"{FLEETX_API}{path}"
        resp = self.session.get(url, params=params or None, timeout=60)
        if resp.status_code == 401:
            self.login(force=True)
            resp = self.session.get(url, params=params or None, timeout=60)
        resp.raise_for_status()
        return resp.json()

# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------
def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R    = 6371000.0
    phi1 = math.radians(lat1);  phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl   = math.radians(lon2 - lon1)
    a    = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def inside(poi, lat, lon, slack_m: float = 100.0) -> bool:
    if lat is None or lon is None:
        return False
    return haversine_m(poi["latitude"], poi["longitude"], lat, lon) <= (poi.get("radius") or 0) + slack_m

# ---------------------------------------------------------------------------
# Fleetx data fetchers
# ---------------------------------------------------------------------------
def build_lookup_caches(client: FleetxClient) -> tuple[dict, list, dict]:
    vehicles     = client.get("/api/v2/vehicles/autocomplete/",
                              page=0, size=40000,
                              showHorseAndTrailer="false",
                              showCurrentTransporter="false")
    address_book = client.get("/api/v1/address_book/")
    veh_by_plate = {(v.get("licensePlate") or "").upper().strip(): v
                    for v in vehicles if v.get("licensePlate")}
    pois         = [a for a in address_book if a.get("name")]
    poi_by_name  = {(a.get("name") or "").lower().strip(): a for a in pois}
    return veh_by_plate, pois, poi_by_name


def find_vehicle(veh_by_plate: dict, query: str) -> dict | None:
    q = query.upper().strip()
    if q in veh_by_plate:
        return veh_by_plate[q]
    for plate, v in veh_by_plate.items():
        if q in plate:
            return v
    return None


def find_poi(pois: list, poi_by_name: dict, query: str) -> dict | None:
    q = query.lower().strip()
    if not q:
        return None
    if q in poi_by_name:
        return poi_by_name[q]
    hits = [a for n, a in poi_by_name.items() if q in n]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print(f"[poi] {query!r} matches {len(hits)} names: "
              f"{[a.get('name') for a in hits[:5]]} — use a more specific name")
    return None


def fetch_trips(client: FleetxClient, vehicle_id: int, frm_dt: datetime, to_dt: datetime) -> list:
    data = client.get(
        "/api/v1/trips/",
        vehicleId=vehicle_id,
        **{"from": int(frm_dt.timestamp() * 1000),
           "to":   int(to_dt.timestamp() * 1000)},
        skipList="false",
        ignoreExtraRunning="false",
    )
    return data.get("trips", [])


def fetch_vehicle_location(client: FleetxClient, vehicle_id: int) -> dict | None:
    now_ms = int(time.time() * 1000)
    try:
        return client.get("/api/v1/vehicles/history/location",
                          vehicleId=vehicle_id, time=now_ms)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Journey detection
# ---------------------------------------------------------------------------
def find_vehicle_journeys(trips: list, watched_pois: list) -> list:
    """Walk trip legs chronologically, emit one journey per POI-to-POI transition."""
    trips = sorted((t for t in trips if t.get("sDate")), key=lambda t: t["sDate"])

    def overlaps(p1, p2):
        if p1 is None or p2 is None:
            return False
        if p1["id"] == p2["id"]:
            return True
        return haversine_m(p1["latitude"], p1["longitude"],
                           p2["latitude"], p2["longitude"]) <= (
               (p1.get("radius") or 0) + (p2.get("radius") or 0))

    def matches_at(lat, lon):
        return [p for p in watched_pois if inside(p, lat, lon)]

    def pick(matches, prefer):
        if not matches:
            return None
        if prefer is not None:
            for p in matches:
                if p["id"] == prefer["id"] or overlaps(p, prefer):
                    return prefer
        return min(matches, key=lambda p: p.get("radius") or 1e9)

    out: list = []
    current       = None
    current_since = None
    src           = None
    departed_at   = None
    legs_buf: list = []
    prev_e_p      = None
    prev_e_date   = None

    def set_current(poi, when):
        nonlocal current, current_since
        if poi is not None and (current is None or current["id"] != poi["id"]):
            current_since = when
        current = poi

    def emit(arr_poi, arr_time):
        nonlocal src, departed_at, legs_buf
        if src is not None and arr_poi["id"] != src["id"]:
            out.append({
                "src": src, "dst": arr_poi,
                "departed_at": departed_at,
                "arrived_at":  arr_time,
                "legs":        legs_buf,
            })
        src, departed_at, legs_buf = None, None, []
        set_current(arr_poi, arr_time)

    for t in trips:
        is_move = not t.get("haltTrip")

        s_p = pick(matches_at(t.get("sLat"), t.get("sLon")), prefer=current)
        e_p = pick(matches_at(t.get("eLat"), t.get("eLon")), prefer=current)

        if not is_move:
            halt_p = e_p or s_p
            if halt_p:
                if src is not None:
                    emit(halt_p, t["sDate"])
                else:
                    set_current(halt_p, t.get("sDate"))
            prev_e_p, prev_e_date = e_p or s_p, t.get("eDate")
            continue

        if src is not None and s_p is not None and s_p["id"] != src["id"]:
            arr_time = (prev_e_date
                        if prev_e_p is not None and prev_e_p["id"] == s_p["id"]
                        else t["sDate"])
            emit(s_p, arr_time)

        confirmed_arrival = bool(e_p)

        if src is None:
            base = current or s_p
            if not base:
                prev_e_p, prev_e_date = e_p, t.get("eDate")
                continue
            src         = base
            departed_at = current_since if current is not None else t["sDate"]
            saved_since = current_since
            legs_buf    = [t]
            current     = None
            if confirmed_arrival:
                if e_p["id"] != src["id"]:
                    emit(e_p, t.get("eDate"))
                else:
                    src, departed_at, legs_buf = None, None, []
                    set_current(e_p, t.get("eDate"))
                    current_since = saved_since
        else:
            legs_buf.append(t)
            if confirmed_arrival:
                if e_p["id"] != src["id"]:
                    emit(e_p, t.get("eDate"))
                else:
                    src, departed_at, legs_buf = None, None, []
                    set_current(e_p, t.get("eDate"))

        prev_e_p, prev_e_date = e_p, t.get("eDate")

    if src is not None and legs_buf:
        out.append({"src": src, "dst": None,
                    "departed_at": departed_at, "arrived_at": None, "legs": legs_buf})
    elif current is not None:
        out.append({"src": current, "dst": None,
                    "departed_at": current_since, "arrived_at": None, "legs": []})
    return out


def journey_to_row(j: dict, vehicle_plate: str) -> dict:
    return {
        "vehicle":    vehicle_plate,
        "from":       j["src"]["name"] if j["src"] else "",
        "to":         j["dst"]["name"] if j["dst"] else "",
        "start_date": j["departed_at"] or "",
        "end_date":   j["arrived_at"]  or "",
    }


def matches_direction(j: dict, allowed_directed: set[tuple[int, int]]) -> bool:
    src, dst = j["src"], j["dst"]
    if dst is None:
        return any(src["id"] == a for (a, _) in allowed_directed)
    return (src["id"], dst["id"]) in allowed_directed

# ---------------------------------------------------------------------------
# Google Sheet helpers
# ---------------------------------------------------------------------------
def open_sheet():
    if not SHEET_ID:
        sys.exit("SHEET_ID env var is required.")

    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        creds_dict = json.loads(creds_json)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
    else:
        creds_path = os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS",
            str((HERE.parent / "credentials.json").resolve()),
        )
        gc = gspread.service_account(filename=creds_path)

    return gc.open_by_key(SHEET_ID)


def read_routes(sheet) -> list[dict]:
    try:
        ws = sheet.worksheet(ROUTES_TAB)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=ROUTES_TAB, rows=100, cols=5)
        ws.update(values=[["vehicle", "from", "to", "watch_pois", "enabled"]], range_name="A1:E1")
        print(f"[setup] Created '{ROUTES_TAB}' tab.")
        return []

    records = ws.get_all_records()
    tasks: list[dict] = []
    seen: set[tuple] = set()

    def add(vehicle: str, a: str, b: str):
        if not vehicle or not a or not b or a.lower() == b.lower():
            return
        key = (vehicle.upper(), a.lower(), b.lower())
        if key in seen:
            return
        seen.add(key)
        tasks.append({"vehicle": vehicle, "a": a, "b": b})

    for r in records:
        vehicle = str(r.get("vehicle") or "").strip()
        if not vehicle:
            continue
        enabled = str(r.get("enabled", "Y")).strip().upper()
        if enabled in ("N", "NO", "FALSE", "0"):
            continue

        frm       = str(r.get("from")       or "").strip()
        to        = str(r.get("to")         or "").strip()
        watch_raw = str(r.get("watch_pois") or "").strip()

        if frm and to:
            add(vehicle, frm, to)

        if watch_raw:
            poi_names = [p.strip() for p in watch_raw.replace(";", ",").split(",") if p.strip()]
            for i in range(len(poi_names)):
                for j in range(len(poi_names)):
                    if i != j:
                        add(vehicle, poi_names[i], poi_names[j])

    return tasks


def sync_pois_tab(sheet, poi_by_name: dict):
    try:
        ws = sheet.worksheet(POIS_TAB)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=POIS_TAB,
                                 rows=max(200, len(poi_by_name) + 50), cols=5)
        ws.freeze(rows=1)
    headers = ["name", "latitude", "longitude", "radius_m", "id"]
    rows    = sorted(poi_by_name.values(), key=lambda a: (a.get("name") or "").lower())
    values  = [headers] + [
        [a.get("name"), a.get("latitude"), a.get("longitude"),
         a.get("radius"), a.get("id")]
        for a in rows
    ]
    ws.clear()
    ws.update(values=values, range_name="A1")


TRACKING_HEADERS = [
    "vehicle", "driver", "status", "speed_kmh",
    "current_location", "maps_link", "last_seen", "latitude", "longitude",
]

_STATUS_LABEL = {
    "RUNNING":  "Moving",
    "IDLE":     "Idle (engine on)",
    "STOPPED":  "Stopped",
    "INACTIVE": "Inactive",
    "HALT":     "Halted",
}


def resolve_location(lat, lon, pois: list) -> str:
    if lat is None or lon is None:
        return "Unknown"
    for poi in pois:
        if inside(poi, lat, lon, slack_m=200.0):
            return poi["name"]
    return f"{lat:.5f}, {lon:.5f}"


def maps_hyperlink(lat, lon, label: str) -> str:
    if lat is None or lon is None:
        return ""
    url = f"https://maps.google.com/?q={lat},{lon}"
    safe_label = label.replace('"', "'")
    return f'=HYPERLINK("{url}","{safe_label}")'


def sync_live_tracking_tab(sheet, tracking_rows: list[dict]) -> None:
    try:
        ws = sheet.worksheet(TRACKING_TAB)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=TRACKING_TAB, rows=200,
                                 cols=len(TRACKING_HEADERS))
        ws.freeze(rows=1)

    values = [TRACKING_HEADERS] + [
        [r.get(h, "") for h in TRACKING_HEADERS]
        for r in sorted(tracking_rows, key=lambda r: r.get("vehicle", ""))
    ]
    ws.clear()
    ws.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    print(f"[tracking] {len(tracking_rows)} vehicle(s) written to '{TRACKING_TAB}'")


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
)


def normalize_date(s) -> str:
    if s is None:
        return ""
    s = str(s).strip()
    if not s:
        return ""
    head = s[:26] if "." in s else s[:19]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return s


# ---------------------------------------------------------------------------
# Manifest tabs
# ---------------------------------------------------------------------------
def _manifest_tab_name(start_date_str: str, to_name: str) -> str | None:
    try:
        dt = datetime.strptime(str(start_date_str)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    safe_to = (to_name
               .replace("/", "-").replace("\\", "-")
               .replace("?", "").replace("*", "")
               .replace("[", "").replace("]", "").replace(":", "-"))
    return f"{dt.strftime('%B')}_{safe_to}"[:100]


def _hide_sheet(spreadsheet, ws) -> None:
    spreadsheet.batch_update({"requests": [{"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "hidden": True},
        "fields": "hidden",
    }}]})


def load_meta(sheet) -> tuple:
    """Load manual column values from the hidden _meta tab."""
    try:
        ws = sheet.worksheet(META_TAB)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=META_TAB, rows=5000, cols=len(META_HEADERS))
        ws.update(values=[META_HEADERS], range_name="A1")
        _hide_sheet(sheet, ws)
        return ws, {}

    meta: dict[tuple, dict[int, str]] = {}
    for row in ws.get_all_values()[1:]:
        if not any(row):
            continue
        tab = row[0] if len(row) > 0 else ""
        key = row[1] if len(row) > 1 else ""
        if not tab or not key:
            continue
        manual = {manifest_col: (row[meta_col] if meta_col < len(row) else "")
                  for meta_col, manifest_col in _META_TO_MANIFEST.items()}
        meta[(tab, key)] = manual
    return ws, meta


def save_meta(ws, meta: dict) -> None:
    values = [META_HEADERS]
    for (tab, key), manual in sorted(meta.items()):
        row = [tab, key] + [""] * (len(META_HEADERS) - 2)
        for meta_col, manifest_col in _META_TO_MANIFEST.items():
            row[meta_col] = manual.get(manifest_col, "")
        values.append(row)
    ws.clear()
    if len(values) > 1:
        ws.update(values=values, range_name="A1", value_input_option="RAW")


def upsert_manifest_tabs(sheet, all_rows: list[dict]) -> None:
    by_tab: dict[str, list[dict]] = {}
    for r in all_rows:
        if not r.get("start_date") or not r.get("to") or not r.get("end_date"):
            continue
        tab = _manifest_tab_name(r["start_date"], r["to"])
        if tab:
            by_tab.setdefault(tab, []).append(r)

    meta_ws, meta = load_meta(sheet)
    for tab_name, rows in by_tab.items():
        _upsert_one_manifest_tab(sheet, tab_name, rows, meta)
    save_meta(meta_ws, meta)


def _row_key(vehicle: str, start_date: str) -> str:
    return f"{vehicle}|{normalize_date(start_date)}"


def _upsert_one_manifest_tab(sheet, tab_name: str, new_rows: list[dict],
                              meta: dict) -> None:
    try:
        ws           = sheet.worksheet(tab_name)
        existing_raw = ws.get_all_values()
    except gspread.WorksheetNotFound:
        ws           = sheet.add_worksheet(title=tab_name, rows=1000,
                                           cols=len(MANIFEST_HEADERS))
        existing_raw = []

    if not existing_raw:
        existing_raw = [MANIFEST_HEADERS]

    # Step 1: scan existing rows — separate keyed rows (vehicle+start_date present)
    # from orphan rows (missing either field — user-added, script never touches them)
    orphan_rows:     list[list]       = []
    existing_by_key: dict[str, list]  = {}
    vehicle_order:   list[str]        = []
    seen_vehicles:   set[str]         = set()

    in_orphan_block = False
    for raw in existing_raw[1:]:
        if not any(raw):
            in_orphan_block = True
            continue
        vehicle    = raw[1] if len(raw) > 1 else ""
        start_date = raw[6] if len(raw) > 6 else ""
        has_key    = bool(vehicle and start_date)

        if in_orphan_block or not has_key:
            orphan_rows.append((list(raw) + [""] * len(MANIFEST_HEADERS))[:len(MANIFEST_HEADERS)])
            continue

        # Old schema stored _key in col 14 — use it if present
        key = (raw[14] if len(raw) > 14 and raw[14]
               else _row_key(vehicle, start_date))

        existing_by_key[key] = (list(raw) + [""] * len(MANIFEST_HEADERS))[:len(MANIFEST_HEADERS)]

        manual = {col: (raw[col] if col < len(raw) else "") for col in _MANIFEST_MANUAL_COLS}
        if any(v for v in manual.values()):
            meta[(tab_name, key)] = manual
        elif (tab_name, key) not in meta:
            meta[(tab_name, key)] = manual

        if vehicle not in seen_vehicles:
            seen_vehicles.add(vehicle)
            vehicle_order.append(vehicle)

    # Step 2: build incoming rows from Fleetx data
    incoming:               dict[str, list] = {}
    incoming_vehicle_order: list[str]       = []
    incoming_seen:          set[str]        = set()

    for r in new_rows:
        vehicle = r["vehicle"]
        key     = _row_key(vehicle, r.get("start_date", ""))
        manual  = meta.get((tab_name, key), {})
        row = [
            "",                       # S.NO
            vehicle,                  # VEHICLE NO.
            manual.get(2,  ""),       # VEHICLE TYPE
            manual.get(3,  ""),       # MANIFEST NO
            r.get("from", ""),        # FROM
            r.get("to",   ""),        # TO
            r.get("start_date", ""),  # START DATE
            r.get("end_date",   ""),  # END DATE
            manual.get(8,  ""),       # DR. NAME
            manual.get(9,  ""),       # DR. CODE
            manual.get(10, ""),       # Advance(In Cash)
            manual.get(11, ""),       # Round Trip(HSD)
            manual.get(12, ""),       # HSD Rate/Ltr
            manual.get(13, ""),       # Total Cost
        ]
        incoming[key] = row
        if (tab_name, key) not in meta:
            meta[(tab_name, key)] = {col: "" for col in _MANIFEST_MANUAL_COLS}
        if vehicle not in incoming_seen:
            incoming_seen.add(vehicle)
            incoming_vehicle_order.append(vehicle)

    # Step 3: merge keyed rows; orphan rows are untouched
    merged: dict[str, list] = {}
    for key, ex_row in existing_by_key.items():
        merged[key] = incoming.get(key, ex_row)
    for key, row in incoming.items():
        if key not in merged:
            merged[key] = row

    for v in incoming_vehicle_order:
        if v not in seen_vehicles:
            vehicle_order.append(v)

    # Group by vehicle, sort by start_date, renumber S.NO
    by_vehicle: dict[str, list[list]] = {}
    for row in merged.values():
        by_vehicle.setdefault(row[1], []).append(row)
    for vehicle in by_vehicle:
        by_vehicle[vehicle].sort(key=lambda r: r[6] or "")
        for i, row in enumerate(by_vehicle[vehicle], start=1):
            row[0] = i

    # Build final sheet: synced blocks, then orphan block at the bottom
    sheet_rows: list[list] = [MANIFEST_HEADERS]
    first_block = True
    for vehicle in vehicle_order:
        if vehicle not in by_vehicle:
            continue
        if not first_block:
            sheet_rows.append([""] * len(MANIFEST_HEADERS))
        first_block = False
        sheet_rows.extend(by_vehicle[vehicle])

    if orphan_rows:
        sheet_rows.append([""] * len(MANIFEST_HEADERS))
        sheet_rows.extend(orphan_rows)

    ws.clear()
    if len(sheet_rows) > 1:
        ws.update(values=sheet_rows, range_name="A1", value_input_option="RAW")
    ws.freeze(rows=1)

    total = sum(len(b) for b in by_vehicle.values())
    print(f"[manifest] {tab_name}: {total} synced, {len(orphan_rows)} manual row(s) preserved")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description="Fleetx -> Google Sheets manifest scraper")
    ap.add_argument("--backfill-from", metavar="YYYY-MM-DD",
                    help="Fetch from this date until now (overrides --days)")
    ap.add_argument("--days", type=int,
                    help=f"Rolling lookback in days (default {LOOKBACK_DAYS})")
    ap.add_argument("--sheet-id",
                    help="Google Sheet ID (overrides SHEET_ID env var)")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    global SHEET_ID
    if args.sheet_id:
        SHEET_ID = args.sheet_id

    print(f"[run] {datetime.now(IST):%Y-%m-%d %H:%M:%S IST}")
    sheet  = open_sheet()
    routes = read_routes(sheet)
    print(f"[routes] {len(routes)} enabled route(s)")
    if not routes:
        return 0

    client = FleetxClient()
    client.login()
    veh_by_plate, pois, poi_by_name = build_lookup_caches(client)
    print(f"[fleetx] {len(veh_by_plate)} vehicles, {len(pois)} POIs")
    sync_pois_tab(sheet, poi_by_name)

    to_dt    = datetime.now(IST)
    backfill = args.backfill_from or BACKFILL_FROM
    lookback = args.days if args.days else LOOKBACK_DAYS

    if backfill:
        try:
            frm_dt = datetime.strptime(backfill, "%Y-%m-%d").replace(tzinfo=IST)
        except ValueError:
            sys.exit(f"--backfill-from must be YYYY-MM-DD, got {backfill!r}")
        print(f"[window] BACKFILL: {frm_dt:%Y-%m-%d} -> now")
    else:
        frm_dt = to_dt - timedelta(days=lookback)
        print(f"[window] rolling {lookback}d: {frm_dt:%Y-%m-%d %H:%M} -> now")

    # Group routes per vehicle
    by_vehicle: dict[str, dict] = {}
    for r in routes:
        v_key = r["vehicle"].upper()
        slot  = by_vehicle.setdefault(v_key, {
            "vehicle_query": r["vehicle"],
            "poi_inputs":    set(),
        })
        slot["poi_inputs"].add(r["a"])
        slot["poi_inputs"].add(r["b"])

    all_rows:      list[dict] = []
    tracking_rows: list[dict] = []

    for v_key, slot in by_vehicle.items():
        veh = find_vehicle(veh_by_plate, slot["vehicle_query"])
        if not veh:
            print(f"[skip] vehicle '{slot['vehicle_query']}' not found")
            continue

        resolved: dict[int, dict] = {}
        for name in slot["poi_inputs"]:
            p = find_poi(pois, poi_by_name, name)
            if p is None:
                print(f"[skip] POI '{name}' not found for {v_key}")
                continue
            resolved[p["id"]] = p
        if len(resolved) < 2:
            print(f"[skip] {v_key} has fewer than 2 resolvable POIs")
            continue

        allowed_pairs:    set[tuple[int, int]] = set()
        allowed_directed: set[tuple[int, int]] = set()
        for r in routes:
            if r["vehicle"].upper() != v_key:
                continue
            pa = find_poi(pois, poi_by_name, r["a"])
            pb = find_poi(pois, poi_by_name, r["b"])
            if pa and pb and pa["id"] != pb["id"]:
                allowed_pairs.add(tuple(sorted([pa["id"], pb["id"]])))
                allowed_directed.add((pa["id"], pb["id"]))

        # Live location
        loc = fetch_vehicle_location(client, veh["id"])
        if loc:
            lat      = loc.get("latitude")
            lon      = loc.get("longitude")
            spd      = loc.get("speed")
            location = resolve_location(lat, lon, pois)
            tracking_rows.append({
                "vehicle":          veh.get("licensePlate") or v_key,
                "driver":           loc.get("driverName") or "",
                "status":           _STATUS_LABEL.get(loc.get("status", ""), loc.get("status", "")),
                "speed_kmh":        round(spd, 1) if spd is not None else "",
                "current_location": location,
                "maps_link":        maps_hyperlink(lat, lon, "Open Map"),
                "last_seen":        loc.get("timeStamp") or "",
                "latitude":         lat if lat is not None else "",
                "longitude":        lon if lon is not None else "",
            })

        try:
            trips = fetch_trips(client, veh["id"], frm_dt, to_dt)
        except Exception as exc:
            print(f"[error] trips fetch for {v_key}: {exc}")
            continue

        watched  = list(resolved.values())
        journeys = find_vehicle_journeys(trips, watched)

        # Anchor model: reattribute src when intermediate clusters exist
        kept: list[dict] = []
        anchor = None
        for j in journeys:
            src = j["src"]
            dst = j["dst"]
            if dst is None:
                eff_src = anchor or src
                if eff_src and any(eff_src["id"] in pair for pair in allowed_pairs):
                    new_j = dict(j); new_j["src"] = eff_src
                    kept.append(new_j)
                continue

            eff_src = src
            if anchor and anchor["id"] != src["id"]:
                anchor_src_pair = tuple(sorted([anchor["id"], src["id"]]))
                if anchor_src_pair not in allowed_pairs:
                    eff_src = anchor

            if eff_src["id"] == dst["id"]:
                continue

            key = tuple(sorted([eff_src["id"], dst["id"]]))
            if key in allowed_pairs:
                new_j = dict(j); new_j["src"] = eff_src
                kept.append(new_j)
                anchor = dst

        kept_undirected = len(kept)
        kept = [j for j in kept if matches_direction(j, allowed_directed)]
        wrong_dir = kept_undirected - len(kept)

        rows = [journey_to_row(j, veh.get("licensePlate")) for j in kept]
        all_rows.extend(rows)
        intra = len(journeys) - kept_undirected
        print(f"[vehicle] {v_key:14}  {len(trips)} legs -> {len(journeys)} transitions, "
              f"{len(kept)} kept ({intra} intra-cluster, {wrong_dir} wrong-direction)")

    upsert_manifest_tabs(sheet, all_rows)

    if tracking_rows:
        sync_live_tracking_tab(sheet, tracking_rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
