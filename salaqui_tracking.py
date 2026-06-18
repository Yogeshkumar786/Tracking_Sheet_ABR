"""
salaqui_tracking.py — Live route tracker: Shambhu → Salaqui

Reads watched vehicles from the manifest sheet's Routes tab,
finds active Shambhu→Salaqui trips, gets current location for each,
and writes a live status report to a hardcoded Google Sheet.

Secrets required (same repo secrets, no extras needed):
    FLEETX_USERNAME, FLEETX_PASSWORD, GOOGLE_CREDENTIALS
"""
from __future__ import annotations

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

# ── CONFIG ────────────────────────────────────────────────────────────────────
TRACKING_SHEET_ID = '13fo7UntbmwSZCOidVUA-kQoZmyA8fKvDS5s6PPh9qE0'
MANIFEST_SHEET_ID = '10IOgyNyxBE2kcMJ1GwywKEXL4mc4hmTVIItjEtsFn2o'  # manifest source
TAB_NAME          = 'Salaqui Tracking'

SHAMBHU_KEYWORDS = ['shambhu', 'sambhu']
SALAQUI_KEYWORDS = ['salaqui', 'selaqui', 'selaque']

# Both directions: Shambhu→Salaqui and Salaqui→Shambhu
FROM_KEYWORDS = SHAMBHU_KEYWORDS + SALAQUI_KEYWORDS
TO_KEYWORDS   = SHAMBHU_KEYWORDS + SALAQUI_KEYWORDS

HEADERS = [
    'S.No', 'Vehicle No', 'From', 'TO',
    'DEP Date&Time', 'Actual arrival Date&Time', 'Status', 'REMARK',
]
# ─────────────────────────────────────────────────────────────────────────────

IST         = timezone(timedelta(hours=5, minutes=30))
HERE        = Path(__file__).parent
FLEETX_API  = "https://api.fleetx.io"
TOKEN_CACHE = HERE / ".token_cache.json"

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

# ── Fleetx client (same as manifest.py) ──────────────────────────────────────
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
            pass

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
            sys.exit(f"Fleetx login OK but no token: {list(body)[:10]}")
        expires_in = int(body.get("expires_in") or body.get("expiresIn") or 3600)
        self._set_token(token, expires_in)
        self._save_cached_token(token, expires_in)
        print("[login] OK")
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

# ── Geo helpers ───────────────────────────────────────────────────────────────
def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R    = 6371000.0
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl   = math.radians(lon2 - lon1)
    a    = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def nearest_poi(pois: list, lat: float, lon: float, max_m: float = 5000) -> dict | None:
    best, best_d = None, max_m
    for p in pois:
        try:
            d = haversine_m(p["latitude"], p["longitude"], lat, lon)
        except (TypeError, KeyError):
            continue
        if d < best_d:
            best, best_d = p, d
    return best

# ── Helpers ───────────────────────────────────────────────────────────────────
def kw_match(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(k in t for k in keywords)

def _is_shambhu_salaqui_route(frm: str, to: str) -> bool:
    """True for both Shambhu→Salaqui and Salaqui→Shambhu, but not same-to-same."""
    frm_is_s = kw_match(frm, SHAMBHU_KEYWORDS)
    frm_is_q = kw_match(frm, SALAQUI_KEYWORDS)
    to_is_s  = kw_match(to,  SHAMBHU_KEYWORDS)
    to_is_q  = kw_match(to,  SALAQUI_KEYWORDS)
    return (frm_is_s and to_is_q) or (frm_is_q and to_is_s)

def fmt_ts(v) -> str:
    if not v:
        return ''
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=IST).strftime('%Y-%m-%d %H:%M')
    s = str(v).strip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S.%f'):
        try:
            return datetime.strptime(s[:26], fmt).strftime('%Y-%m-%d %H:%M')
        except ValueError:
            continue
    return s[:16]   # fallback: return as-is trimmed

def open_sheet(sheet_id: str):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
        gc = gspread.authorize(creds)
    else:
        gc = gspread.service_account(
            filename=str((HERE.parent / "credentials.json").resolve())
        )
    return gc.open_by_key(sheet_id)

# ── Sheet readers ─────────────────────────────────────────────────────────────
def read_pois_from_sheet(ss) -> list[dict]:
    try:
        ws = ss.worksheet("POIs")
    except gspread.WorksheetNotFound:
        return []
    records = ws.get_all_records()
    pois = []
    for r in records:
        try:
            pois.append({
                "name":      r.get("name") or r.get("Name", ""),
                "latitude":  float(r.get("latitude") or r.get("lat") or r.get("Latitude", 0)),
                "longitude": float(r.get("longitude") or r.get("lon") or r.get("Longitude", 0)),
                "radius":    float(r.get("radius") or r.get("Radius") or 500),
            })
        except (TypeError, ValueError):
            continue
    return [p for p in pois if p["latitude"] and p["longitude"]]

def read_routes_from_sheet(ss) -> list[dict]:
    try:
        ws = ss.worksheet("Routes")
    except gspread.WorksheetNotFound:
        return []
    return ws.get_all_records()

# ── Fleetx data ───────────────────────────────────────────────────────────────
def get_all_vehicles(client: FleetxClient) -> dict:
    data = client.get("/api/v2/vehicles/autocomplete/",
                      page=0, size=40000,
                      showHorseAndTrailer="false",
                      showCurrentTransporter="false")
    veh_list = data if isinstance(data, list) else data.get("content", data.get("data", []))
    by_plate: dict = {}
    for v in veh_list:
        plate = (v.get("licensePlate") or "").upper().strip()
        if plate:
            by_plate[plate] = v
    return by_plate

def get_trips_for_vehicle(client: FleetxClient, vehicle_id: int, days_back: int = 2) -> list:
    now_ist  = datetime.now(IST)
    start    = now_ist.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back - 1)
    end      = now_ist
    data = client.get(
        "/api/v1/trips/",
        vehicleId=vehicle_id,
        **{"from": int(start.timestamp() * 1000),
           "to":   int(end.timestamp() * 1000)},
        skipList="false",
        ignoreExtraRunning="false",
    )
    return data.get("trips", [])

def get_live_location(client: FleetxClient, vehicle_id: int) -> dict | None:
    try:
        loc = client.get("/api/v1/vehicles/history/location",
                         vehicleId=vehicle_id,
                         time=int(time.time() * 1000))
        if isinstance(loc, dict):
            return loc
    except Exception:
        pass
    return None

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    client = FleetxClient()
    client.login()

    print("[sheets] Opening manifest source sheet...")
    ss_src = open_sheet(MANIFEST_SHEET_ID)

    print("[pois] Reading POIs...")
    pois = read_pois_from_sheet(ss_src)
    print(f"  {len(pois)} POIs loaded")

    print("[routes] Reading Routes tab for vehicle list...")
    routes = read_routes_from_sheet(ss_src)
    enabled_plates = {
        str(r.get("vehicle", "")).upper().strip()
        for r in routes
        if str(r.get("enabled", "yes")).lower() not in ("no", "false", "0")
        and str(r.get("vehicle", "")).strip()
    }
    print(f"  {len(enabled_plates)} enabled vehicle(s) in Routes tab")

    print("[vehicles] Fetching vehicle list from Fleetx...")
    by_plate = get_all_vehicles(client)

    # Match Routes vehicles to Fleetx IDs
    vehicles_to_track: list[tuple[str, int]] = []
    for plate in sorted(enabled_plates):
        veh = by_plate.get(plate)
        if not veh:
            for p, v in by_plate.items():
                if plate in p:
                    veh = v
                    break
        if not veh:
            print(f"  [warn] '{plate}' not found in Fleetx — skipping")
            continue
        vehicles_to_track.append((plate, int(veh["id"])))

    print(f"  {len(vehicles_to_track)} vehicle(s) to scan for Shambhu↔Salaqui trips")

    # Build report rows
    rows: list[list] = []
    for plate, vid in vehicles_to_track:
        print(f"  [{plate}] fetching trips + live location...")

        trips     = get_trips_for_vehicle(client, vid, days_back=2)
        live      = get_live_location(client, vid)

        dep_ts    = ''
        arr_ts    = ''
        status    = ''
        src_name  = 'SAMBHU'
        dst_name  = 'SALAQUI'

        # Find the most recent trip leg on either Shambhu↔Salaqui direction
        active_trip    = None
        dest_keywords  = []
        for t in sorted(trips, key=lambda x: x.get("sDate", 0), reverse=True):
            s_name = (t.get("startAddress") or t.get("sName") or "").lower()
            s_lat  = t.get("sLat") or t.get("startLat")
            s_lon  = t.get("sLon") or t.get("startLon")

            src_kws = None
            dst_kws = None
            if kw_match(s_name, SHAMBHU_KEYWORDS):
                src_kws, dst_kws = SHAMBHU_KEYWORDS, SALAQUI_KEYWORDS
            elif kw_match(s_name, SALAQUI_KEYWORDS):
                src_kws, dst_kws = SALAQUI_KEYWORDS, SHAMBHU_KEYWORDS
            elif s_lat and s_lon:
                # fallback: match start coords against POIs
                for p in pois:
                    try:
                        if haversine_m(p["latitude"], p["longitude"], float(s_lat), float(s_lon)) < (p["radius"] + 500):
                            if kw_match(p["name"], SHAMBHU_KEYWORDS):
                                src_kws, dst_kws = SHAMBHU_KEYWORDS, SALAQUI_KEYWORDS
                            elif kw_match(p["name"], SALAQUI_KEYWORDS):
                                src_kws, dst_kws = SALAQUI_KEYWORDS, SHAMBHU_KEYWORDS
                            break
                    except (TypeError, ValueError):
                        continue

            if not src_kws:
                continue

            active_trip   = t
            dest_keywords = dst_kws
            dep_ts  = fmt_ts(t.get("sDate"))
            arr_ts  = fmt_ts(t.get("eDate")) if not t.get("haltTrip") and t.get("eDate") else ''
            s_n = t.get("startAddress") or t.get("sName") or ("SAMBHU" if src_kws == SHAMBHU_KEYWORDS else "SALAQUI")
            e_n = t.get("endAddress")   or t.get("eName") or ("SALAQUI" if dst_kws == SALAQUI_KEYWORDS else "SAMBHU")
            src_name = s_n.upper()
            dst_name = e_n.upper()
            break

        # Determine current status from live location
        if live:
            lat = live.get("lat") or live.get("latitude")
            lon = live.get("lng") or live.get("longitude")
            if lat and lon:
                lat, lon = float(lat), float(lon)
                poi = nearest_poi(pois, lat, lon, max_m=5000)
                if poi:
                    pname = poi["name"].upper()
                    arrived = dest_keywords and kw_match(pname, dest_keywords)
                    if arrived:
                        status = f'ARRIVED - {pname}'
                        if not arr_ts:
                            ts = live.get("time") or live.get("timestamp")
                            arr_ts = fmt_ts(int(ts)) if ts else ''
                    else:
                        status = pname
                else:
                    status = f"{lat:.4f}, {lon:.4f}"
            elif active_trip:
                status = 'IN TRANSIT'
        elif active_trip:
            status = 'IN TRANSIT'

        # Only include vehicles that have (or had) a Shambhu↔Salaqui trip
        if not active_trip and not status:
            continue

        rows.append([
            '',            # S.No — filled below
            plate,
            src_name,
            dst_name,
            dep_ts,
            arr_ts,
            status or 'IN TRANSIT',
            '',            # REMARK — manual
        ])

    # Sort by departure time
    rows.sort(key=lambda r: r[4] or '')

    # Auto-number S.No
    for i, r in enumerate(rows, 1):
        r[0] = i

    # Write to tracking sheet
    print(f"[sheet] Writing {len(rows)} row(s) to '{TAB_NAME}'...")
    ss_dst = open_sheet(TRACKING_SHEET_ID)
    try:
        ws = ss_dst.worksheet(TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = ss_dst.add_worksheet(TAB_NAME, rows=200, cols=len(HEADERS))

    ws.clear()
    ws.update([HEADERS] + rows, value_input_option='USER_ENTERED')

    # Header formatting
    ws.format(f'A1:H1', {
        'textFormat':          {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
        'backgroundColor':     {'red': 0.067, 'green': 0.333, 'blue': 0.8},
        'horizontalAlignment': 'CENTER',
    })

    # Freeze header
    ss_dst.batch_update({'requests': [{
        'updateSheetProperties': {
            'properties': {'sheetId': ws.id, 'gridProperties': {'frozenRowCount': 1}},
            'fields': 'gridProperties.frozenRowCount',
        }
    }]})

    now_ist = datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')
    print(f"Done — {len(rows)} vehicle(s) written. Last updated: {now_ist}")


if __name__ == '__main__':
    main()
