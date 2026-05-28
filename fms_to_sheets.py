"""
FMS Smart -> Google Sheets Live Tracker
- Row positions locked by Vehicle_No forever. Never reorders.
- Never touches MANUAL columns (Driver, Financials, Reason).
- Route_Reaching_Date_Time written once at arrival, never overwritten.
- Current_Stage column color-coded for instant visual scanning.

Run once   : python fms_to_sheets.py
Dry run    : python fms_to_sheets.py --dry-run
Auto loop  : python fms_to_sheets.py --loop --interval 600
"""
import sys, re, time, argparse, json, traceback, math, warnings
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

import requests
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
SHEET_ID            = "1-tEwE7YwZFNhfGjvgZPHYMKXJqSr_TOmrAodfuadGf0"   # MASTER
SHEET_NAME          = "Tracking"
VEHICLES_TAB        = "Vehicles"          # Vehicle No | Vehicle Type | Vehicle Hub | Vehicle Route
VEHICLES_HEADERS    = ["Vehicle No", "Vehicle Type", "Vehicle Hub", "Vehicle Route"]
ROUTE_CODES_TAB     = "Route Codes"
STOPPAGE_LOG_TAB      = "Stoppage Log"
HUB_LOCATIONS_TAB     = "Hub Locations"
HUB_LOCATIONS_HEADERS = ["Hub_Name", "Hub_Code", "Latitude", "Longitude"]
ROUTE_SLA_TAB         = "Route SLA"        # Route_Code | Expected_Hours (dispatch→final)
ROUTE_SLA_HEADERS     = ["Route_Code", "Expected_Hours"]
SHADOW_TAB            = "_shadow"          # hidden mirror of last script-written values

# Per-hub Tracking mirrors. Each gets ONLY the Tracking tab, filtered to the
# vehicles whose "Vehicle Hub" (in the master Vehicles tab) matches the key.
# The master file above keeps everything (all vehicles + side tabs + stoppage).
HUB_TRACKING_SHEETS = {
    "Ambala":       ("1xHxlccSE3z4cE-HqI8bh9Lwja7I_VkbkkTStWCcLvpE", "Tracking"),
    "Ambala Local": ("1C9BePLnuPL1DfnNtuKheZ1uWu5j1ob_zoMXsXo0REgQ", "Tracking"),
}

# Per-hub trip (MIS) sheets — completed trip log, tab per month
HUB_TRIP_SHEETS = {
    "Ambala": "1_unl3WrQZngLUdS1-jA95UZpkjoa1ZqZIIiu3G11DBo",
}

# RPS Report API — plain HTTP, requires X-Requested-With header
RPS_REPORT_URL = (
    "http://smart.dsmsoft.com/FMSSmartApp/"
    "Safex_RPS_Reports/WebService.asmx/getRpsReportData"
)
RPS_REPORT_HEADERS = {
    "Accept":           "*/*",
    "Content-Type":     "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           "http://smart.dsmsoft.com",
    "Referer":          (
        "http://smart.dsmsoft.com/FMSSmartApp/"
        "Safex_RPS_Reports/RPS_Reports.aspx?usergroup=NRM.101"
    ),
    "User-Agent":       (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
}

TRIP_HEADERS = [
    "RPS_Number", "Vehicle_Number", "Vehicle_Size",
    "Driver_Name", "Driver_Code",
    "Route", "Route_Code", "Route_TAT",
    "Start_Time", "End_Time",
    "Transit_Time", "Extra_Touching_Time", "Actual_Transit_Time",
    "Delay_Hours", "Status",
    "Given_Advance", "Given_Diesel", "Diesel_Amount",
    "Given_Toll", "Given_Challan", "Extra_Diesel", "Maintainance",
    "Close_Status",
]
TRIP_NCOLS = len(TRIP_HEADERS)
CREDS_FILE          = Path(__file__).parent / "credentials.json"
HUB_CODES_FILE      = Path(__file__).parent / "hub_codes.json"
BASE_API            = "https://fmssmart.dsmsoft.com/FMSSmart"
USER_ID             = 3435
FREE_WINDOW_HRS     = 2.0
ROAD_HALT_LOG_HRS   = 2.0    # log "Halted on Road" stops only if ≥ this many hours
ALMOST_REACHED_KM   = 1.0    # GPS distance to final destination hub → "Almost Reached"
SAME_HUB_KM         = 1.0    # GPS distance to a planned hub → treat as that hub
                             # (handles side-by-side twins, e.g. IDR11 ↔ IDO11)
ONTIME_BUFFER_HRS   = 0.0    # delay beyond this → "Delayed"; arrived before → "Early"
DEFAULT_INTERVAL    = 600

API_HEADERS = {
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer":      "https://fmssmart.dsmsoft.com/FMSSmartApp/",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "Chrome/148.0.0.0 Safari/537.36",
}

# API retry settings
API_MAX_RETRIES = 3
API_RETRY_DELAY = 2.0   # seconds; doubles each retry
API_TIMEOUT     = 30    # seconds per request

# ── Stage colors ───────────────────────────────────────────────────────────────
COLORS = {
    "In Transit":        {"red": 0.714, "green": 0.933, "blue": 0.714},
    "At Loading":        {"red": 1.000, "green": 0.851, "blue": 0.600},
    "Loading Overdue":   {"red": 0.850, "green": 0.330, "blue": 0.100},
    "At Via Stop":       {"red": 1.000, "green": 0.953, "blue": 0.714},   # yellow
    "Almost Reached":    {"red": 0.745, "green": 0.850, "blue": 0.980},   # periwinkle
    "Reached-Unloading": {"red": 0.678, "green": 0.847, "blue": 0.980},   # blue
    "Halted on Road":    {"red": 1.000, "green": 0.800, "blue": 0.800},   # red-ish
    "NRD":               {"red": 0.900, "green": 0.700, "blue": 0.700},
    "Untracked":         {"red": 0.850, "green": 0.850, "blue": 0.850},
    "Unknown":           {"red": 0.930, "green": 0.930, "blue": 0.930},
}
LOADING_OVERDUE_HRS = 24
HEADER_COLOR = {"red": 0.180, "green": 0.286, "blue": 0.490}
WHITE        = {"red": 1.0,   "green": 1.0,   "blue": 1.0}

# ── Per-column independent color maps ─────────────────────────────────────────
STATUS_COLORS = {
    "Moving":            {"red": 0.714, "green": 0.933, "blue": 0.714},  # green
    "On Trip - Stopped": {"red": 1.000, "green": 0.953, "blue": 0.714},  # yellow
    "Stopped":           {"red": 1.000, "green": 0.851, "blue": 0.600},  # orange
    "Reached":           {"red": 0.678, "green": 0.847, "blue": 0.980},  # blue
    "NRD":               {"red": 0.900, "green": 0.700, "blue": 0.700},  # pink
    "Untracked":         {"red": 0.850, "green": 0.850, "blue": 0.850},  # gray
}

ONTIME_COLORS = {
    "On Time":     {"red": 0.714, "green": 0.933, "blue": 0.714},  # green
    "Early":       {"red": 0.714, "green": 0.933, "blue": 0.714},  # teal
    "Delayed":     {"red": 0.850, "green": 0.330, "blue": 0.100},  # red
    "Not On Trip": {"red": 0.850, "green": 0.850, "blue": 0.850},  # gray
}

LOG_COLOR_EXCESS = {"red": 1.000, "green": 0.878, "blue": 0.706}
LOG_COLOR_OK     = {"red": 0.851, "green": 0.918, "blue": 0.827}

STOPLOG_HEADERS = [
    "Date", "Vehicle_No", "RPS_No", "Route_Code",
    "Via_Location", "Arrived_At", "Departed_At",
    "Stop_Duration", "Free_Window", "Excess_Detention", "Remarks",
]

# ── Column layout ──────────────────────────────────────────────────────────────
# Types: auto | key | lock | static | manual
COLUMNS = [
    (0,  "S.No",                              "auto"),
    (1,  "Vehicle_Route",                     "auto"),     # from Vehicles tab (master)
    (2,  "RPS_No",                            "auto"),
    (3,  "Vehicle_No",                        "key"),      # anchor — row locked forever
    (4,  "Vehicle_Type",                      "auto"),
    (5,  "Route",                             "auto"),
    (6,  "Route_Code",                        "auto"),
    (7,  "Route_Start_Date_Time",             "auto"),
    (8,  "Route_Reaching_Date_Time",          "lock"),     # written once at arrival
    (9,  "Status",                            "auto"),
    (10, "Current_Stage",                     "auto"),     # color-coded
    (11, "Ontime_Delay",                      "auto"),
    (12, "Current_Location",                  "auto"),
    (13, "Delay_Hrs",                         "auto"),
    (14, "Reason",                            "manual"),
    (15, "Driver_Name",                       "manual"),
    (16, "Driver_Code",                       "manual"),
    (17, "Given_Advance",                     "manual"),
    (18, "Given_Diesel_Litre",                "manual"),
    (19, "Given_Diesel_Amount",               "manual"),
    (20, "Given_Toll",                        "manual"),
    (21, "Given_Challan",                     "manual"),
    (22, "Extra_Diesel",                      "manual"),
    (23, "In_Route_Mainenance",               "manual"),
    (24, "Current_Stop_Since",                "auto"),
    (25, "Current_Stop_Duration",             "auto"),
    (26, "Last_GPS_Update",                   "auto"),
    (27, "Last_Refreshed",                    "auto"),
]

KEY_COL    = 3    # Vehicle_No
LOCK_COL   = 8    # Route_Reaching_Date_Time
STAGE_COL  = 10   # Current_Stage (colored)

# Columns removed/renamed — auto-deleted from sheets on first run.
_STALE_COLUMNS = [
    "Route_Schedule_Reaching_Date_Time",
    "Fix_Advance",
    "Advance",
    "DSL_LTR",
    "DSL_Amount",
    "Toll",
    "Challan_MH_Border",
    "In_Route_Extra_DSL",
    "In_Route_Maintenance_Exp",
]
TOTAL_COLS = len(COLUMNS)
HEADER_ROW = 1
DATA_START = 2

# Pre-computed bad vehicle-type strings (upper-cased once at import)
_BAD_VTYPE       = {"TARGET TIME LOGISTICS PRIVATE LIMITED",
                    "ABR ROADLINES PRIVATE LIMITED", "NA", ""}
_BAD_VTYPE_UPPER = {v.upper() for v in _BAD_VTYPE}

# ── Via-stop detection ─────────────────────────────────────────────────────────
# Words that identify a logistics hub in the API's lastLocation text.
HUB_KEYWORDS = frozenset([
    "safexpress", "hub", "terminal", "depot", "warehouse",
    "abr binola", "abr", "logistics park", "freight station",
])

VIA_RADIUS_KM  = 2.0    # GPS fallback: within this distance of a hub = via stop
NEAR_HUB_MAX_M = 500    # "Near (Xm) [hub]" — treat as AT hub if within this many metres
                        # Handles GPS glitches where vehicle IS at the hub but
                        # position is reported slightly offset, e.g.:
                        #   "Near (137.41Meters) safexpress salem hub-11 (slm11)"

# Pre-compiled regex for the "Near (Xm)" / "Near (X Kms)" pattern
# Matches: Near (137.41Meters) | Near (0.14Kms) | Near(50m) | near ( 200 meters )
_NEAR_RE = re.compile(
    r'^near\s*\(\s*([\d.]+)\s*(meters?|kms?|km|m)\s*\)',
    re.IGNORECASE
)


# ── Utilities ──────────────────────────────────────────────────────────────────

def col_letter(idx: int) -> str:
    result, n = "", idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result

COL_LETTERS = [col_letter(i) for i in range(TOTAL_COLS)]


def extract_list(resp: dict) -> list:
    inner = resp.get("data", [])
    if isinstance(inner, list):  return inner
    if isinstance(inner, dict):  return inner.get("data", []) or []
    return []


def parse_stoppage_hrs(s: str) -> float:
    if not s or s in ("NA", ""):  return 0.0
    d  = int(m.group(1)) if (m := re.search(r"(\d+)D", s)) else 0
    h  = int(m.group(1)) if (m := re.search(r"(\d+)H", s)) else 0
    mn = int(m.group(1)) if (m := re.search(r"(\d+)M", s)) else 0
    sc = int(m.group(1)) if (m := re.search(r"(\d+)S", s)) else 0
    return d * 24 + h + mn / 60 + sc / 3600


def parse_delay_hrs(s: str) -> float:
    if not s or s in ("NA", "", "00:00:00"):  return 0.0
    try:
        parts = str(s).split(":")
        return int(parts[0]) + int(parts[1]) / 60 + int(parts[2]) / 3600
    except Exception:
        return 0.0


def hrs_to_hms(h: float) -> str:
    """Convert decimal hours → 'HH:MM:SS'. Returns '' for zero/negative."""
    if h <= 0:
        return ""
    total_sec = int(round(h * 3600))
    hh = total_sec // 3600
    mm = (total_sec % 3600) // 60
    ss = total_sec % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _td_to_dhms(td: timedelta) -> str:
    """Convert timedelta → '00D 08H 33M 17S' (API stop-duration format)."""
    total_s = max(0, int(td.total_seconds()))
    d, rem  = divmod(total_s, 86400)
    h, rem  = divmod(rem, 3600)
    m, s    = divmod(rem, 60)
    return f"{d:02d}D {h:02d}H {m:02d}M {s:02d}S"


def _parse_since_dt(s: str) -> datetime | None:
    """Parse a sheet or API datetime string into a datetime object."""
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None


# IGNORECASE: the API's lastLocation reports codes in lowercase — "(jha11)",
# "(rmp01)" — while consigneeName/consigneeCode use uppercase. Match both.
_HUB_CODE_RE = re.compile(r'\(([A-Z0-9]+)\)\s*$', re.IGNORECASE)


def _extract_hub_code_from_location(loc: str) -> str:
    """
    Extract the parenthetical hub code at the end of a location string.
      "AT safexpress nagpur (NGP11)"  → "NGP11"
      "AT ABR BINOLA"                 → ""
    Returns upper-cased code, or "" if not found.
    """
    m = _HUB_CODE_RE.search((loc or "").strip())
    return m.group(1).upper() if m else ""


# Generic words that carry no hub identity — dropped before name matching so a
# location like "AT safexpress greater mumbai" matches the consignee
# "SAFEXPRESS GREATER MUMBAI (GBB11)" on the distinctive token "mumbai".
_HUB_NAME_STOPWORDS = frozenset({
    "safexpress", "hub", "outbound", "inbound", "sds", "abr", "roadlines",
    "private", "limited", "near", "meter", "meters", "metre", "metres",
    "kms", "from", "town", "toll", "plaza", "road", "city", "the", "and", "of",
})


def _hub_name_tokens(text: str) -> set[str]:
    """
    Distinctive lowercase word tokens from a hub name or location string —
    used to match a location against the planned route when the location text
    has no parenthetical hub code (e.g. "AT abr binola").

      "AT abr binola"                         → {"binola"}
      "Near 110 Meters SAFEXPRESS TIRUVALLUR" → {"tiruvallur"}
      "SAFEXPRESS GREATER MUMBAI (GBB11)"     → {"greater", "mumbai"}

    Strips the "(CODE)" suffix, punctuation, standalone numbers, generic
    logistics words (safexpress/hub/outbound…) and short tokens (<4 chars).
    """
    s = _HUB_CODE_RE.sub("", text or "")        # drop "(GBB11)" code suffix
    s = _DIST_RE.sub(" ", s)                      # drop "300 Meters" / "0.78 Kms"
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())    # punctuation → space
    return {t for t in s.split()
            if len(t) >= 4 and not t.isdigit() and t not in _HUB_NAME_STOPWORDS}


# Leading distance + unit, covers every lastLocation form the API emits:
#   "Near (308.69Meters) …"  "Near 300 Meters …"  "0.78 Kms From …"  "50m …"
_DIST_RE = re.compile(r'([\d.]+)\s*(meters?|metres?|kms?|km|m)\b', re.IGNORECASE)


def _location_distance_m(loc: str) -> float | None:
    """
    Distance (in metres) from the hub named in a location string.

      "AT safexpress delhi west (dlw400)"            → 0.0     (at the hub)
      "Near 300 Meters SAFEXPRESS DELHI WEST"        → 300.0
      "Near (308.69Meters) safexpress delhi west"    → 308.69
      "0.78 Kms From safexpress jhansi (jha11)"      → 780.0
      "safexpress delhi west (dlw400)"               → None    (no distance given)

    Returns None when no distance can be read (and it isn't an "AT" location).
    """
    s = (loc or "").strip().lower()
    if not s:
        return None
    if s.startswith("at "):
        return 0.0
    m = _DIST_RE.search(s)
    if not m:
        return None
    val  = float(m.group(1))
    unit = m.group(2).lower()
    return val * 1000 if unit in ("km", "kms") else val


def build_hub_code_map(vehicles: list, sheet_map: dict) -> tuple[dict, dict]:
    """
    Build the internal {UPPER_NAME: code} hub map from:
      1. sheet_map  — Route Codes tab (highest priority / manually editable)
      2. Live API   — consigneeName[i]+consigneeCode[i] pairs & parentheses

    Returns (hub_map, new_entries) where new_entries are discoveries not yet
    in the sheet (original-cased keys, ready to append to Route Codes tab).
    """
    hub_map: dict = dict(sheet_map)
    new_entries: dict = {}

    def _add(orig_name: str, code: str):
        name, code = orig_name.strip(), code.strip()
        if not name or not code or code.upper() in ("NA", ""):
            return
        key  = name.upper()
        norm = _HUB_CODE_RE.sub("", name).strip().upper()
        if key not in hub_map:
            hub_map[key] = code
            new_entries[name] = code
        if norm and norm not in hub_map:
            hub_map[norm] = code

    for v in vehicles:
        cnames = [x.strip() for x in (v.get("consigneeName") or "").split(";")]
        ccodes = [x.strip() for x in (v.get("consigneeCode")  or "").split(";")]
        for name, code in zip(cnames, ccodes):
            if name and code:
                _add(name, code)
            m = _HUB_CODE_RE.search(name)
            if m:
                _add(name, m.group(1))
        cname = (v.get("consignerName") or "").strip()
        m = _HUB_CODE_RE.search(cname)
        if m:
            _add(cname, m.group(1))

    return hub_map, new_entries


def hub_code(name: str, hub_map: dict) -> str:
    name = name.strip()
    m = _HUB_CODE_RE.search(name)
    if m:
        return m.group(1)
    key  = name.upper()
    norm = _HUB_CODE_RE.sub("", name).strip().upper()
    return hub_map.get(key) or hub_map.get(norm) or ""


def vehicle_type(v: dict) -> str:
    """Derive vehicle type from driverId, e.g. 'ABR ROADLINES - 18 Ton 32 Ft'."""
    raw   = (v.get("driverId") or "").strip()
    vtype = raw.split(" - ", 1)[1].strip() if " - " in raw else raw
    return "" if vtype.upper() in _BAD_VTYPE_UPPER else vtype   # pre-computed set


def build_route_code(v: dict, hub_map: dict) -> str:
    rname = fmt(v.get("routeName"))
    if rname and not rname.upper().startswith("DR_"):
        return rname
    origin_code = hub_code(v.get("consignerName") or "", hub_map)
    dest_code   = fmt(v.get("consigneeCode"))
    if origin_code and dest_code:
        return f"{origin_code}-{dest_code}"
    if dest_code:
        return dest_code
    if origin_code:
        return origin_code
    return rname


def fmt(val) -> str:
    if val is None:        return ""
    s = str(val).strip()
    if s.upper() == "NA": return ""
    return s

NOT_ASSIGNED = "Not Assigned"


# ── Via-stop helpers ───────────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in metres between two GPS coordinates."""
    R  = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _at_hub_by_text(last_location: str) -> bool:
    """
    Signal 1 — text analysis (no coordinates needed).

    Handles two location formats the FMS API produces:

    Pattern A  "AT [hub]"
      Vehicle is exactly at a fixed point.
        "AT safexpress nagpur (ngp11)"    → True   (hub keyword present)
        "AT ABR BINOLA"                   → True
        "AT some village dhaba"           → False  (no hub keyword)

    Pattern B  "Near (Xm / XKms) [hub]"
      GPS glitch — vehicle IS at the hub but position is slightly offset.
        "Near (137.41Meters) safexpress salem hub-11 (slm11)" → True
                                             (≤ 500 m + hub keyword)
        "Near (0.08Kms) abr depot"        → True   (80 m + hub keyword)
        "Near (1.5Kms) safexpress hub"    → False  (too far for GPS glitch)
        "Near (300Meters) some dhaba"     → False  (no hub keyword)

    "X Kms From ..." strings always mean on-road → False.
    """
    loc = (last_location or "").strip().lower()

    # ── Pattern A: "AT [hub]" ────────────────────────────────────────────────
    if loc.startswith("at "):
        return any(kw in loc[3:] for kw in HUB_KEYWORDS)

    # ── Pattern B: "Near (Xm/XKms) [hub]" ───────────────────────────────────
    m = _NEAR_RE.match(loc)
    if m:
        distance = float(m.group(1))
        unit     = m.group(2).lower()
        dist_m   = distance * 1000 if unit in ("km", "kms") else distance
        if dist_m <= NEAR_HUB_MAX_M:
            loc_after = loc[m.end():]   # text after the "Near (Xm)" prefix
            return any(kw in loc_after for kw in HUB_KEYWORDS)

    return False


def _at_hub_by_gps(v_lat: float, v_lon: float,
                   hub_coords: dict[str, tuple[float, float]]) -> bool:
    """
    Signal 2 — GPS proximity fallback.

    Checks if the vehicle is within VIA_RADIUS_KM of any known hub.
    Only runs when Signal 1 returned False AND hub_coords is populated
    (fetched from the FMS API at startup).
    """
    if not v_lat or not v_lon or not hub_coords:
        return False
    for h_lat, h_lon in hub_coords.values():
        if haversine_m(v_lat, v_lon, h_lat, h_lon) / 1000 <= VIA_RADIUS_KM:
            return True
    return False


# ── Stage logic ────────────────────────────────────────────────────────────────

def derive_stage(v: dict,
                 hub_coords:         dict[str, tuple[float, float]] | None = None,
                 consignee_codes:    list[str] | None = None,
                 hub_coords_by_code: dict[str, tuple[float, float]] | None = None,
                 prev_snap:          dict | None = None,
                 route_hub_names:    list[str] | None = None,
                 ) -> tuple[str, dict]:
    """
    Returns (stage_label, bg_color).

    Stage detection order (first match wins):
      1.  NRD / Untracked
      2.  At Loading / Loading Overdue  — not yet on trip, stopped at origin
      3.  Almost Reached               — on trip, within ALMOST_REACHED_KM of
                                         final-destination hub GPS
      4.  Reached-Unloading            — isOnTrip flipped False while prev was
                                         "Almost Reached" or "Reached-Unloading"
      5.  In Transit                   — moving
      6.  At Via Stop                  — stopped at a PLANNED via hub
      7.  Halted on Road               — stopped anywhere else (incl. an
                                         unplanned hub)
      8.  Unknown

    consignee_codes : per-vehicle list parsed from v["consigneeCode"] split by ";".
                      Last element = final destination; all others = planned via hubs.
    hub_coords_by_code : {HUB_CODE_UPPER: (lat, lon)} — from Hub Locations column B.
    prev_snap        : previous cycle's snapshot entry for this vehicle.
    """
    stop_hrs   = parse_stoppage_hrs(v.get("stoppageDuration") or "")
    is_on_trip = bool(v.get("isOnTrip"))
    is_stopped = bool(v.get("isStopped"))
    is_running = bool(v.get("isRunning"))

    # 1. NRD / Untracked
    if v.get("isNRD"):       return "NRD",       COLORS["NRD"]
    if v.get("isUntracked"): return "Untracked",  COLORS["Untracked"]

    # 2. Not yet on trip → loading point
    if not is_on_trip and is_stopped:
        if stop_hrs >= LOADING_OVERDUE_HRS:
            return "Loading Overdue", COLORS["Loading Overdue"]
        return "At Loading", COLORS["At Loading"]

    # 3. At / within ALMOST_REACHED_KM of the FINAL destination hub.
    #    Distance is measured by GPS to the final hub's OWN coordinates, so two
    #    side-by-side buildings with different codes (e.g. safexpress delhi DLI11
    #    and safexpress delhi outbound DLO11, ~200 m apart) both resolve to the
    #    final correctly — without the fragile city-name token matching that gave
    #    false positives (e.g. "delhi" matching "delhi west").
    #      stopped → Reached-Unloading ;  moving → Almost Reached
    if is_on_trip and consignee_codes:
        final_code = consignee_codes[-1].upper()
        v_lat      = float(v.get("latitude")  or 0)
        v_lon      = float(v.get("longitude") or 0)
        near_final = False

        # (a) GPS distance: vehicle ↔ final-hub coordinates (primary, robust).
        if hub_coords_by_code and v_lat and v_lon and final_code in hub_coords_by_code:
            f_lat, f_lon = hub_coords_by_code[final_code]
            if haversine_m(v_lat, v_lon, f_lat, f_lon) / 1000 <= ALMOST_REACHED_KM:
                near_final = True

        # (b) Text fallback when the final hub has no stored coordinates: the
        #     location's OWN code equals the final code + parsed distance ≤ 1 km.
        if not near_final:
            last_loc = v.get("lastLocation", "")
            if _extract_hub_code_from_location(last_loc) == final_code:
                d = _location_distance_m(last_loc)
                if d is not None and d <= ALMOST_REACHED_KM * 1000:
                    near_final = True

        if near_final:
            if is_stopped:
                return "Reached-Unloading", COLORS["Reached-Unloading"]
            return "Almost Reached", COLORS["Almost Reached"]

    # 4. Reached-Unloading — isOnTrip just flipped False AND prev was Almost Reached,
    #    OR vehicle is still sitting at destination (prev = Reached-Unloading)
    if not is_on_trip:
        prev_stage = (prev_snap or {}).get("stage", "")
        if prev_stage in ("Almost Reached", "Reached-Unloading"):
            return "Reached-Unloading", COLORS["Reached-Unloading"]

    # 5. In Transit
    if is_running:
        return "In Transit", COLORS["In Transit"]

    # 6 & 7. On-trip stop — is the vehicle at a hub?
    if is_on_trip and is_stopped:
        last_loc    = v.get("lastLocation", "")
        at_hub_text = _at_hub_by_text(last_loc)
        at_hub_gps  = False
        if not at_hub_text and stop_hrs >= 0.5:
            v_lat = float(v.get("latitude")  or 0)
            v_lon = float(v.get("longitude") or 0)
            at_hub_gps = _at_hub_by_gps(v_lat, v_lon, hub_coords or {})

        if at_hub_text or at_hub_gps:
            # Is this hub one of the route's planned hubs (origin + vias + final)?
            # Decided by NAME and GPS together, in priority order.
            hub_code_loc = _extract_hub_code_from_location(last_loc)
            loc_core     = _hub_name_tokens(last_loc)
            v_lat        = float(v.get("latitude")  or 0)
            v_lon        = float(v.get("longitude") or 0)

            # Build planned hubs: (code, core-name tokens, coords-or-None).
            planned_codes = {c.upper() for c in (consignee_codes or [])}
            planned: list[tuple[str, set, tuple | None]] = []
            for nm in (route_hub_names or []):
                m    = _HUB_CODE_RE.search(nm or "")
                code = m.group(1).upper() if m else ""
                if code:
                    planned_codes.add(code)
                planned.append((code, _hub_name_tokens(nm),
                                (hub_coords_by_code or {}).get(code) if code else None))
            # Codes from consigneeCode with no matching name entry still count.
            for c in planned_codes:
                if not any(p[0] == c for p in planned):
                    planned.append((c, set(), (hub_coords_by_code or {}).get(c)))

            # 1) Exact code match → planned via.
            if hub_code_loc and hub_code_loc in planned_codes:
                return "At Via Stop", COLORS["At Via Stop"]

            # 2) GPS (precise): within SAME_HUB_KM of a planned hub's coordinates.
            #    Resolves same-hub-different-code twins (IDR11↔IDO11) when coords
            #    exist, and keeps genuinely distant hubs (delhi vs delhi-west) apart.
            if v_lat and v_lon:
                for _code, _core, coords in planned:
                    if coords and haversine_m(v_lat, v_lon, *coords) / 1000 <= SAME_HUB_KM:
                        return "At Via Stop", COLORS["At Via Stop"]

            # 3) Name (fallback when the planned hub has no stored coords): the
            #    location's core name EQUALS a planned hub's core name. Generic
            #    words (outbound/inbound/hub/safexpress) are stripped, so
            #    "indore"=="indore" matches but "delhi"≠"delhi west".
            if loc_core:
                for _code, core, _coords in planned:
                    if core and loc_core == core:
                        return "At Via Stop", COLORS["At Via Stop"]

            # 4) Determinable (we have a code or a name) but nothing matched →
            #    an unplanned hub is treated the same as a road stop.
            if hub_code_loc or loc_core:
                return "Halted on Road", COLORS["Halted on Road"]

            # 5) Truly indeterminate (no code, no name tokens) → safe default.
            return "At Via Stop", COLORS["At Via Stop"]

        return "Halted on Road", COLORS["Halted on Road"]

    return "Unknown", COLORS["Unknown"]


def derive_status(v: dict, stage: str = "") -> str:
    """
    Derive the Status column value.
    Pass the already-computed stage string so "Reached" is driven by the
    isOnTrip flip (via "Reached-Unloading") rather than a raw km threshold.
    """
    if v.get("isNRD"):       return "NRD"
    if v.get("isUntracked"): return "Untracked"
    if stage == "Reached-Unloading": return "Reached"
    if v.get("isRunning"):   return "Moving"
    if v.get("isOnTrip") and v.get("isStopped"): return "On Trip - Stopped"
    if v.get("isStopped"):   return "Stopped"
    return "Unknown"


# ── API layer with exponential-backoff retry ────────────────────────────────────

def _api_post(url: str, payload: dict,
              max_retries: int = API_MAX_RETRIES,
              base_delay:  float = API_RETRY_DELAY):
    """
    POST to FMS API with automatic retry on transient errors.
    Raises RuntimeError after all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=API_HEADERS,
                              json=payload, timeout=API_TIMEOUT)
            r.raise_for_status()   # raises on 4xx/5xx before trying .json()
            return r.json()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"    [WARN] API error (attempt {attempt+1}/{max_retries}), "
                      f"retrying in {delay:.0f}s: {exc}", flush=True)
                time.sleep(delay)
    raise RuntimeError(
        f"API failed after {max_retries} attempts: {last_exc}") from last_exc


# ── FMS API ────────────────────────────────────────────────────────────────────

def fetch_vehicles() -> list[dict]:
    url  = f"{BASE_API}/dashboard/post/json/getTransporterStatusDashbaordDetails"
    body = _api_post(url, {"userId": USER_ID})
    lst  = extract_list(body) if isinstance(body, dict) else (body or [])
    if not lst:
        raise RuntimeError("No vehicles in API response")
    print(f"  [API] {len(lst)} vehicles", flush=True)
    return lst


def fetch_vehicle_id_map() -> dict[str, int]:
    """
    Return {VEHICLE_NUMBER_UPPER: vehicleId} for all vehicles in the account.
    The tracking-report API needs the numeric vehicleId, not the plate number.
    Tries two generic-data type values; falls back to an empty dict on failure.
    """
    url = f"{BASE_API}/masters/post/json/getUserGenericData"
    base_payload = {
        "text": "", "by": "itemName", "exact": False,
        "listOfParam": ["id", "itemName"],
        "numberOfRecords": 9999,
        "userId": USER_ID,
    }
    for type_val in ("vehicles", "vehicle"):
        try:
            body = _api_post(url, {**base_payload, "type": type_val})
            data = extract_list(body) if isinstance(body, dict) else (body or [])
            result: dict[str, int] = {}
            for item in data:
                name = str(item.get("itemName") or "").strip().upper()
                vid  = item.get("id")
                if name and vid:
                    try:
                        result[name] = int(vid)
                    except (ValueError, TypeError):
                        pass
            if result:
                print(f"  [API] {len(result)} vehicle IDs loaded "
                      f"(type='{type_val}')", flush=True)
                return result
        except Exception:
            continue
    print("  [API] Could not load vehicle ID map — arrival-time lookup disabled",
          flush=True)
    return {}


def fetch_hub_locations() -> tuple[dict[str, tuple[float, float]], list[list]]:
    """
    Try to load hub GPS coordinates from the FMS master-data API.

    Returns:
        coords    {HUB_NAME_UPPER: (lat, lon)}  — used at runtime for GPS checks
        raw_rows  [[Hub_Name, "", lat, lon], …]  — used to self-populate the
                  Hub Locations sheet tab on first run

    Both return values are empty if the API has no coordinate data — Signal 1
    (text check) still works without GPS coordinates.

    Tries several type values because the FMS API field name is not documented.
    """
    url = f"{BASE_API}/masters/post/json/getUserGenericData"
    base_payload = {
        "text": "", "by": "itemName", "exact": False,
        "listOfParam": ["id", "itemName", "latitude", "longitude"],
        "numberOfRecords": 9999,
        "userId": USER_ID,
    }
    for type_val in ("consignees", "hubs", "locations", "consignee"):
        try:
            body = _api_post(url, {**base_payload, "type": type_val})
            data = extract_list(body) if isinstance(body, dict) else (body or [])
            coords:   dict[str, tuple[float, float]] = {}
            raw_rows: list[list] = []
            for item in data:
                name = str(item.get("itemName") or "").strip()
                lat  = item.get("latitude")
                lon  = item.get("longitude")
                if name and lat and lon:
                    try:
                        lat_f, lon_f = float(lat), float(lon)
                        coords[name.upper()] = (lat_f, lon_f)
                        raw_rows.append([name, "", lat_f, lon_f])
                    except (ValueError, TypeError):
                        pass
            if coords:
                print(f"  [API] {len(coords)} hub coordinates loaded "
                      f"(type='{type_val}')", flush=True)
                return coords, raw_rows
        except Exception:
            continue   # try next type value

    # No coordinates found — text-only detection will still work
    print("  [API] No hub coordinates from API — GPS via-check disabled",
          flush=True)
    return {}, []


def fetch_actual_arrival_time(
        vehicle_no:         str,
        vehicle_id_map:     dict[str, int],
        final_hub_code:     str,
        final_hub_coords:   tuple[float, float] | None,
        dispatch_dt:        datetime,
        hub_coords_by_code: dict[str, tuple[float, float]],
) -> str:
    """
    Query the FMS Tracking Report for `vehicle_no` and find the **earliest**
    GPS point that is AT the final destination hub, after the dispatch time.

    Strategy (first match wins for each point):
      1. If final hub code is in hub_coords_by_code → GPS distance ≤ 2 km
      2. Point's location text contains the final hub code (case-insensitive)
      3. Point's location text starts with 'AT' and contains final hub name tokens

    Returns a formatted string "DD/MM/YYYY HH:MM:SS", or "" if not found.
    """
    vid = vehicle_id_map.get(vehicle_no.upper())
    if not vid:
        return ""   # unknown vehicle — fall back to datetime.now()

    url = f"{BASE_API}/report/post/json/getTrackingReport"
    # Search from dispatch time up to 10 days ahead (capped at now)
    to_dt  = min(dispatch_dt + timedelta(days=10), datetime.now())
    payload = {
        "userId":    USER_ID,
        "vehicleId": vid,
        "fromDate":  dispatch_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate":    to_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "interval":  5,   # 5-minute resolution
    }
    try:
        body  = _api_post(url, payload, max_retries=2, base_delay=1.0)
        points = extract_list(body) if isinstance(body, dict) else (body or [])
    except Exception as exc:
        print(f"  [TrackRpt] {vehicle_no}: API error — {exc}", flush=True)
        return ""

    if not points:
        return ""

    # Hub name tokens for text-fallback matching
    hub_code_upper = final_hub_code.upper() if final_hub_code else ""
    hub_coords     = final_hub_coords or (hub_coords_by_code.get(hub_code_upper)
                                          if hub_code_upper else None)

    # Field names tried for timestamp and location (vary across API versions)
    _TIME_FIELDS = ("dateTime", "datetime", "gpsTime", "gps_time",
                    "timestamp", "locationTime", "time")
    _LOC_FIELDS  = ("location", "lastLocation", "address", "landmark",
                    "locationName", "place")
    _LAT_FIELDS  = ("latitude",  "lat")
    _LON_FIELDS  = ("longitude", "lon", "lng")

    def _get(point: dict, fields: tuple) -> str:
        for f in fields:
            v = point.get(f)
            if v is not None and str(v).strip() not in ("", "null", "None"):
                return str(v).strip()
        return ""

    earliest: datetime | None = None

    for pt in points:
        ts_raw = _get(pt, _TIME_FIELDS)
        if not ts_raw:
            continue
        pt_dt = _parse_since_dt(ts_raw)
        if not pt_dt or pt_dt <= dispatch_dt:
            continue
        # Already found an earlier match — no need to check further
        if earliest and pt_dt >= earliest:
            continue

        at_dest = False

        # Check 1: GPS distance to final hub
        if hub_coords:
            try:
                p_lat = float(_get(pt, _LAT_FIELDS) or 0)
                p_lon = float(_get(pt, _LON_FIELDS) or 0)
                if p_lat and p_lon:
                    dist_km = haversine_m(p_lat, p_lon, *hub_coords) / 1000
                    if dist_km <= 2.0:
                        at_dest = True
            except (ValueError, TypeError):
                pass

        # Check 2: location text contains the hub code
        if not at_dest and hub_code_upper:
            loc_text = _get(pt, _LOC_FIELDS).upper()
            if hub_code_upper in loc_text:
                at_dest = True

        # Check 3: location starts with "AT" and hub name tokens match
        if not at_dest and hub_code_upper:
            loc_text = _get(pt, _LOC_FIELDS)
            if loc_text.strip().upper().startswith("AT "):
                # Extract code from parentheses e.g. "AT safexpress vijayawada (VWR11)"
                code_in_loc = _extract_hub_code_from_location(loc_text)
                if code_in_loc and code_in_loc.upper() == hub_code_upper:
                    at_dest = True

        if at_dest:
            earliest = pt_dt

    if earliest:
        result = earliest.strftime("%d/%m/%Y %H:%M:%S")
        print(f"  [TrackRpt] {vehicle_no}: actual arrival at "
              f"{hub_code_upper or 'dest'} = {result}", flush=True)
        return result

    return ""


# ── Google Sheets ──────────────────────────────────────────────────────────────

def _gspread_client():
    creds = Credentials.from_service_account_file(
        str(CREDS_FILE),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)


def connect(sheet_id: str = SHEET_ID, tab: str = SHEET_NAME):
    """
    Open a spreadsheet by id and return (spreadsheet, tracking_worksheet),
    auto-creating the Tracking tab if missing. Defaults to the master file.
    """
    ss = _gspread_client().open_by_key(sheet_id)
    try:
        ws = ss.worksheet(tab)
    except gspread.WorksheetNotFound:
        print(f"  [Sheet] '{tab}' tab not found in {sheet_id[:12]}… — creating it.",
              flush=True)
        ws = ss.add_worksheet(title=tab, rows=500, cols=TOTAL_COLS)
    return ss, ws


def get_or_create_tab(ss, name: str, headers: list) -> gspread.Worksheet:
    """Return existing worksheet or create it with a formatted header row."""
    try:
        return ss.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=name, rows=1000, cols=len(headers))
        ws.update(values=[headers], range_name="A1", value_input_option="RAW")
        ss.batch_update({"requests": [{"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": len(headers)},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HEADER_COLOR,
                "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 10},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }}]})
        return ws


def _ensure_rows(ws: gspread.Worksheet, needed: int):
    """Grow the worksheet if it has fewer than `needed` rows."""
    if ws.row_count < needed:
        ws.resize(rows=needed + 50)


def get_or_create_shadow(ss) -> gspread.Worksheet:
    """
    Return the hidden '_shadow' tab (mirror of the script's last-written values,
    used to detect user edits), creating + hiding it on first use.
    """
    try:
        return ss.worksheet(SHADOW_TAB)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=SHADOW_TAB, rows=1000, cols=TOTAL_COLS)
        ws.update(values=[[c[1] for c in COLUMNS]], range_name="A1",
                  value_input_option="RAW")
        ss.batch_update({"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "hidden": True},
            "fields": "hidden",
        }}]})
        return ws


def load_lookup_tab(ws: gspread.Worksheet) -> dict:
    """Load a 2-column (key, value) sheet into a plain dict. Skips header row."""
    rows = ws.get_all_values()
    return {r[0].strip(): r[1].strip()
            for r in rows[1:]
            if len(r) >= 2 and r[0].strip() and r[1].strip()}


def load_route_sla_tab(ws: gspread.Worksheet) -> tuple[dict, set]:
    """
    Read the 'Route SLA' tab (Route_Code | Expected_Hours).

    Returns:
        sla_map   {ROUTE_CODE_UPPER: expected_hours_float}  — only numeric rows
        present   {ROUTE_CODE_UPPER, …}                     — every code listed
                  (incl. blank-hours rows, so we don't re-add them)
    """
    rows = ws.get_all_values()
    sla_map: dict = {}
    present: set = set()
    for r in rows[1:]:
        code = r[0].strip() if len(r) > 0 else ""
        if not code:
            continue
        present.add(code.upper())
        hrs_s = r[1].strip() if len(r) > 1 else ""
        try:
            if hrs_s:
                sla_map[code.upper()] = float(hrs_s)
        except (ValueError, TypeError):
            pass
    return sla_map, present


def compute_sla_delay(route_code: str, sla_map: dict, dispatch_str: str,
                      arrival_str: str, reached: bool,
                      is_on_trip: bool) -> tuple[str, str] | tuple[None, None]:
    """
    Self-computed delay from the Route-SLA table (FMS delay is unreliable).

      scheduled = dispatch + Expected_Hours
      delay     = (actual arrival if reached, else now) − scheduled

    Returns (delay_hms, ontime_label) where:
        delay_hms     "HH:MM:SS" of lateness ("00:00:00" if on-time/early)
        ontime_label  "Delayed" | "Early" | "On Time"
    Returns (None, None) when the route has no usable SLA → caller falls back
    to the FMS values.
    """
    hrs = sla_map.get(route_code.upper()) if route_code else None
    if not hrs:
        return None, None
    disp = _parse_since_dt(dispatch_str or "")
    if not disp:
        return None, None
    scheduled = disp + timedelta(hours=hrs)

    ref = None
    if reached and arrival_str:
        ref = _parse_since_dt(arrival_str)
    if ref is None:
        ref = datetime.now()

    delay_h = (ref - scheduled).total_seconds() / 3600
    if delay_h > ONTIME_BUFFER_HRS:
        return hrs_to_hms(delay_h), "Delayed"
    if reached and delay_h < -ONTIME_BUFFER_HRS:
        return "00:00:00", "Early"
    return "00:00:00", "On Time"


def load_vehicles_tab(ws: gspread.Worksheet) -> tuple[dict, dict, dict, set]:
    """
    Read the master 'Vehicles' tab:
        Vehicle No | Vehicle Type | Vehicle Hub | Vehicle Route

    Returns:
        vt_map     {vehicle_no: vehicle_type}   — operator-maintained types
        hub_map    {vehicle_no: hub}            — routing key (e.g. "Ambala")
        route_map  {vehicle_no: vehicle_route}  — fills Vehicle_Route column
        present    {vehicle_no, …}              — every vehicle already listed
    """
    rows = ws.get_all_values()
    vt_map:    dict = {}
    hub_map:   dict = {}
    route_map: dict = {}
    present:   set  = set()
    for r in rows[1:]:   # skip header
        vno = r[0].strip() if len(r) > 0 else ""
        if not vno:
            continue
        present.add(vno)
        vtype = r[1].strip() if len(r) > 1 else ""
        hub   = r[2].strip() if len(r) > 2 else ""
        route = r[3].strip() if len(r) > 3 else ""
        if vtype: vt_map[vno]    = vtype
        if hub:   hub_map[vno]   = hub
        if route: route_map[vno] = route
    return vt_map, hub_map, route_map, present


def load_hub_coords_tab(ws: gspread.Worksheet) -> dict[str, tuple[float, float]]:
    """
    Read the Hub Locations tab (Hub_Name | Hub_Code | Latitude | Longitude)
    and return {HUB_NAME_UPPER: (lat, lon)}.

    This is the highest-priority coordinate source — values here override
    anything fetched from the FMS API, so operators can manually correct bad
    coordinates or add hubs the API doesn't know about.

    Rows with missing or non-numeric lat/lon are silently skipped.
    """
    rows = ws.get_all_values()
    result: dict[str, tuple[float, float]] = {}
    for row in rows[1:]:   # skip header row
        name  = row[0].strip() if len(row) > 0 else ""
        lat_s = row[2].strip() if len(row) > 2 else ""
        lon_s = row[3].strip() if len(row) > 3 else ""
        if not name or not lat_s or not lon_s:
            continue
        try:
            result[name.upper()] = (float(lat_s), float(lon_s))
        except (ValueError, TypeError):
            pass
    return result


def load_hub_coords_by_code(ws: gspread.Worksheet) -> dict[str, tuple[float, float]]:
    """
    Read the Hub Locations tab and return {HUB_CODE_UPPER: (lat, lon)}.

    Used for "Almost Reached" detection — looks up the GPS of the final
    destination hub by its code (column B), so the vehicle's position can
    be compared against it.

    Rows without a hub code (column B empty) are silently skipped.
    """
    rows = ws.get_all_values()
    result: dict[str, tuple[float, float]] = {}
    for row in rows[1:]:   # skip header row
        code  = row[1].strip() if len(row) > 1 else ""
        lat_s = row[2].strip() if len(row) > 2 else ""
        lon_s = row[3].strip() if len(row) > 3 else ""
        if not code or not lat_s or not lon_s:
            continue
        try:
            result[code.upper()] = (float(lat_s), float(lon_s))
        except (ValueError, TypeError):
            pass
    return result


def append_lookup_rows(ws: gspread.Worksheet, new_entries: dict):
    """Append new rows to a 2-column lookup tab, sorted by key."""
    if not new_entries:
        return
    ws.append_rows([[k, v] for k, v in sorted(new_entries.items())],
                   value_input_option="RAW")


def write_headers(ws: gspread.Worksheet, existing_header_row: list):
    """Write header row only when it has actually changed (uses pre-loaded data)."""
    # One-time migration: delete any stale columns no longer in the layout.
    for stale in _STALE_COLUMNS:
        if stale in existing_header_row:
            col_idx = existing_header_row.index(stale)
            ws.spreadsheet.batch_update({"requests": [{"deleteDimension": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": col_idx, "endIndex": col_idx + 1},
            }}]})
            existing_header_row.pop(col_idx)
            print(f"  [Migration] Deleted stale column '{stale}' from "
                  f"{ws.spreadsheet.title}/{ws.title}", flush=True)

    headers = [c[1] for c in COLUMNS]
    if existing_header_row[:len(headers)] == headers:
        return
    print("  [Sheet] Writing headers…", flush=True)
    ws.update(values=[headers], range_name=f"A{HEADER_ROW}", value_input_option="RAW")
    ws.spreadsheet.batch_update({"requests": [{"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": TOTAL_COLS},
        "cell": {"userEnteredFormat": {
            "backgroundColor": HEADER_COLOR,
            "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
    }}]})


def _parse_sheet(all_rows: list[list]) -> tuple[dict, dict, dict]:
    """
    Single pass over ws.get_all_values() to derive:
        stage_snapshot  {vno: {stage, stop_since, ...}}
        row_map         {vno: sheet_row_number_1based}
        lock_vals       {vno: Route_Reaching_Date_Time value}

    Replaces three separate API calls (get_all_values + 2x col_values).
    """
    if not all_rows:
        return {}, {}, {}

    headers = all_rows[0]

    def _ci(name: str) -> int:
        try:    return headers.index(name)
        except ValueError: return -1

    ci_vno      = _ci("Vehicle_No")
    ci_stage    = _ci("Current_Stage")
    ci_since    = _ci("Current_Stop_Since")
    ci_duration = _ci("Current_Stop_Duration")
    ci_location = _ci("Current_Location")
    ci_rps      = _ci("RPS_No")
    ci_route    = _ci("Route_Code")
    ci_lock     = _ci("Route_Reaching_Date_Time")

    def _cell(row: list, ci: int) -> str:
        return row[ci].strip() if 0 <= ci < len(row) else ""

    stage_snapshot: dict = {}
    row_map:        dict = {}
    lock_vals:      dict = {}

    for i, row in enumerate(all_rows[1:], start=DATA_START):
        vno = _cell(row, ci_vno)
        if not vno:
            continue
        row_map[vno]  = i
        lock_vals[vno] = _cell(row, ci_lock)
        stage_snapshot[vno] = {
            "stage":    _cell(row, ci_stage),
            "since":    _cell(row, ci_since),
            "duration": _cell(row, ci_duration),
            "location": _cell(row, ci_location),
            "rps":      _cell(row, ci_rps),
            "route":    _cell(row, ci_route),
        }

    return stage_snapshot, row_map, lock_vals


# ── Build one vehicle's cell values ───────────────────────────────────────────

def build_row(v: dict, sno: int, existing_arrival: str,
              hub_map: dict, vt_sheet: dict,
              hub_coords:         dict | None = None,
              prev_snap:          dict | None = None,
              consignee_codes:    list[str] | None = None,
              hub_coords_by_code: dict | None = None,
              route_hub_names:    list[str] | None = None,
              sla_map:            dict | None = None,
              vehicle_route_map:  dict | None = None,
              vehicle_id_map:     dict | None = None) -> list:
    stage, _  = derive_stage(v, hub_coords, consignee_codes, hub_coords_by_code,
                             prev_snap, route_hub_names)
    status    = derive_status(v, stage)
    route_code = build_route_code(v, hub_map)

    # Delay & on-time: prefer our Route-SLA calc (FMS delay is unreliable);
    # fall back to FMS delayTime/ontime only when the route has no SLA entry.
    sla_delay, sla_label = compute_sla_delay(
        route_code, sla_map or {}, v.get("dispatchDate") or "",
        existing_arrival, stage == "Reached-Unloading", bool(v.get("isOnTrip")))

    # ── Hub-stop start time — preserved across GPS glitches ──────────────────
    # GPS can briefly show a vehicle as "moving" at a hub, resetting stoppedStartDate
    # and losing the true arrival time. When the vehicle is already recorded as
    # "At Via Stop" in the sheet, we keep whichever start time is earlier: the
    # sheet's recorded value or the API's current value.
    api_since = fmt(v.get("stoppedStartDate"))
    hub_stop_stages = {"At Via Stop"}
    if stage in hub_stop_stages and prev_snap and prev_snap.get("stage") == stage:
        prev_since = prev_snap.get("since", "")
        if prev_since and api_since:
            prev_dt = _parse_since_dt(prev_since)
            api_dt  = _parse_since_dt(api_since)
            via_since = prev_since if (prev_dt and api_dt and prev_dt < api_dt) else api_since
        else:
            via_since = prev_since or api_since
    else:
        via_since = api_since

    # Arrival timestamp rules:
    #   • "Reached-Unloading" + no timestamp yet      → query FMS Tracking Report
    #     for the ACTUAL first-arrival time at the final hub; fall back to now()
    #   • "Reached-Unloading" + already written       → None (keep; don't overwrite)
    #   • "Almost Reached" + existing timestamp       → None (preserve; not yet reached)
    #   • Any other stage  + existing timestamp       → "" (stale; vehicle on new trip)
    #   • Any other stage  + no timestamp             → None (nothing to do)
    if stage == "Reached-Unloading" and not existing_arrival:
        # Find the actual arrival time: earliest GPS point AT the final hub
        # that occurred AFTER the route start (dispatch) time.
        actual_arrival = ""
        vno_str        = fmt(v.get("vehicleNumber"))
        dispatch_str   = fmt(v.get("dispatchDate"))
        dispatch_dt    = _parse_since_dt(dispatch_str) if dispatch_str else None
        final_code     = (consignee_codes[-1].upper()
                          if consignee_codes else "")
        final_coords   = ((hub_coords_by_code or {}).get(final_code)
                          if final_code else None)
        if dispatch_dt and vno_str and vehicle_id_map:
            actual_arrival = fetch_actual_arrival_time(
                vno_str,
                vehicle_id_map,
                final_code,
                final_coords,
                dispatch_dt,
                hub_coords_by_code or {},
            )
        # Fall back to detection time only when the Tracking Report had no data
        arrival = actual_arrival or datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    elif stage not in ("Reached-Unloading", "Almost Reached") and existing_arrival:
        arrival = ""     # stale arrival from a previous trip — wipe it
    else:
        arrival = None   # no change needed

    eta_raw = v.get("eta") or ""
    eta     = eta_raw.split(";")[0].strip() if ";" in eta_raw else eta_raw
    rps     = fmt(v.get("tripId")) if v.get("isOnTrip") else ""

    row = [None] * TOTAL_COLS
    for idx, _, ctype in COLUMNS:
        if ctype == "manual":      row[idx] = None;    continue
        if ctype == "static":      row[idx] = None;    continue   # user-filled
        if ctype == "lock":        row[idx] = arrival; continue

        if   idx == 0:  row[idx] = sno
        elif idx == 1:
            vno = fmt(v.get("vehicleNumber"))
            row[idx] = (vehicle_route_map or {}).get(vno, "")
        elif idx == 2:  row[idx] = fmt(rps)
        elif idx == 3:  row[idx] = fmt(v.get("vehicleNumber"))
        elif idx == 4:
            vno = fmt(v.get("vehicleNumber"))
            row[idx] = vt_sheet.get(vno) or vehicle_type(v)
        elif idx == 5:
            origin = fmt(v.get("consignerName"))
            dest   = fmt(v.get("consigneeName"))
            if origin or dest:
                row[idx] = f"{origin};{dest}".strip(";")
            else:
                row[idx] = NOT_ASSIGNED if not v.get("isOnTrip") else ""
        elif idx == 6:  row[idx] = route_code or (NOT_ASSIGNED if not v.get("isOnTrip") else "")
        elif idx == 7:  row[idx] = fmt(v.get("dispatchDate"))   or (NOT_ASSIGNED if not v.get("isOnTrip") else "")
        elif idx == 9:  row[idx] = status
        elif idx == 10: row[idx] = stage
        elif idx == 11:
            if sla_label is not None:                 # our SLA calc
                row[idx] = sla_label
            else:                                     # FMS fallback (no SLA yet)
                ontime = fmt(v.get("ontime"))
                row[idx] = ontime if ontime else ("Not On Trip" if not v.get("isOnTrip") else "")
        elif idx == 12:
            loc = fmt(v.get("lastLocation"))
            # Normalise double prefix: "AT at safexpress …" → "AT safexpress …"
            loc = re.sub(r'^(at\s+){2,}', 'AT ', loc, flags=re.IGNORECASE)
            row[idx] = loc
        elif idx == 13:
            if sla_delay is not None:                 # our SLA calc
                row[idx] = sla_delay
            elif not v.get("isOnTrip"):               # FMS fallback
                row[idx] = ""
            else:
                delay_hrs = parse_delay_hrs(v.get("delayTime") or "")
                row[idx] = hrs_to_hms(delay_hrs) if delay_hrs > 0 else "00:00:00"
        elif idx == 24:
            row[idx] = via_since   # earliest start; GPS-glitch-resistant for via stops
        elif idx == 25:
            if stage in hub_stop_stages and via_since:
                # Recalculate from preserved start → immune to GPS glitch resets
                since_dt = _parse_since_dt(via_since)
                row[idx] = _td_to_dhms(datetime.now() - since_dt) if since_dt \
                           else fmt(v.get("stoppageDuration"))
            else:
                row[idx] = fmt(v.get("stoppageDuration"))
        elif idx == 26:
            raw = fmt(v.get("lastLocationDatetime"))
            if raw:
                try:   # API returns "YYYY-MM-DD HH:MM:SS" — reformat to match other cols
                    row[idx] = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")\
                                       .strftime("%d/%m/%Y %H:%M:%S")
                except ValueError:
                    row[idx] = raw   # unknown format — keep as-is
            else:
                row[idx] = ""
        elif idx == 27: row[idx] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        else:           row[idx] = ""
    return row


# ── Trip sheet helpers ─────────────────────────────────────────────────────────

def _parse_rps_response(body) -> list[dict]:
    """
    Unwrap the RPS Report API response.
    Format: {"d": "22*104663*[{...}]"} — count*id* prefix before JSON array.
    """
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    val = body.get("d")
    if not val:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        json_str = val.strip()
        if json_str and json_str[0].isdigit():
            bracket = json_str.find("[")
            brace   = json_str.find("{")
            start   = -1
            if bracket != -1 and (brace == -1 or bracket < brace):
                start = bracket
            elif brace != -1:
                start = brace
            if start != -1:
                json_str = json_str[start:]
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


def fetch_rps_closure(rps_no: str, vehicle_no: str,
                      dispatch_date_str: str) -> str:
    """
    Call getRpsReportData for a 10-day window around dispatch_date.
    Returns the POD_DATE (closure date) string, or "" if not found.
    """
    dispatch_dt = _parse_since_dt(dispatch_date_str) or datetime.now()
    from_dt = dispatch_dt - timedelta(days=5)
    to_dt   = dispatch_dt + timedelta(days=5)
    payload = {
        "from_time": from_dt.strftime("%Y-%m-%d 00:00:00"),
        "to_time":   to_dt.strftime("%Y-%m-%d 00:00:00"),
        "vehicleno": [vehicle_no],
    }
    try:
        resp = requests.post(
            RPS_REPORT_URL, headers=RPS_REPORT_HEADERS,
            json=payload, timeout=30, verify=False,
        )
        resp.raise_for_status()
        records = _parse_rps_response(resp.json())
    except Exception as exc:
        print(f"  [RPS API] Error for {vehicle_no}/{rps_no}: {exc}", flush=True)
        return ""

    for rec in records:
        rec_rps = ""
        for f in ("RPS_Number", "lrNumber", "rpsNumber", "tripId"):
            v = str(rec.get(f) or "").strip()
            if v:
                rec_rps = v
                break
        if rec_rps and rec_rps != rps_no:
            continue
        for f in ("POD_DATE", "pod_date", "closureDate", "deliveryDate", "endDate"):
            v = str(rec.get(f) or "").strip()
            if v and v not in ("null", "None", "0", ""):
                return v
    return ""


def _get_or_create_trip_tab(ss, tab_name: str) -> gspread.Worksheet:
    try:
        ws = ss.worksheet(tab_name)
        if ws.row_values(1) != TRIP_HEADERS:
            ws.update(values=[TRIP_HEADERS], range_name="A1",
                      value_input_option="RAW")
        return ws
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=2000, cols=TRIP_NCOLS)
        ws.update(values=[TRIP_HEADERS], range_name="A1", value_input_option="RAW")
        ss.batch_update({"requests": [
            {"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": TRIP_NCOLS},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": HEADER_COLOR,
                    "textFormat": {"foregroundColor": WHITE,
                                   "bold": True, "fontSize": 10},
                    "horizontalAlignment": "CENTER",
                }},
                "fields": ("userEnteredFormat("
                           "backgroundColor,textFormat,horizontalAlignment)"),
            }},
            {"setDataValidation": {
                "range": {"sheetId": ws.id, "startRowIndex": 1,
                          "endRowIndex": 2000,
                          "startColumnIndex": TRIP_NCOLS - 1,
                          "endColumnIndex": TRIP_NCOLS},
                "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
            }},
        ]})
        return ws


def _write_trip_row(ss, ws: gspread.Worksheet, r: int, data: dict):
    """Write one completed-trip row with formulas. r is 1-based row number."""
    row_values = [
        data.get("RPS_Number",     ""),
        data.get("Vehicle_Number", ""),
        data.get("Vehicle_Size",   ""),
        data.get("Driver_Name",    ""),
        data.get("Driver_Code",    ""),
        data.get("Route",          ""),
        data.get("Route_Code",     ""),
        data.get("Route_TAT",      ""),   # hours/24 day-fraction; formatted [h]:mm
        data.get("Start_Time",     ""),
        data.get("End_Time",       ""),
        f"=J{r}-I{r}",                                # K  Transit_Time
        "",                                            # L  Extra_Touching_Time (manual)
        f"=IF(L{r}=\"\",K{r},K{r}-L{r})",            # M  Actual_Transit_Time
        f"=IF(M{r}>H{r},M{r}-H{r},0)",               # N  Delay_Hours (H already day-frac)
        f'=IF(N{r}=0,"On Time","Delayed")',           # O  Status
        data.get("Given_Advance",  ""),
        data.get("Given_Diesel",   ""),
        data.get("Diesel_Amount",  ""),
        data.get("Given_Toll",     ""),
        data.get("Given_Challan",  ""),
        data.get("Extra_Diesel",   ""),
        data.get("Maintainance",   ""),
        False,                                         # W  Close_Status checkbox
    ]
    ws.update(values=[row_values], range_name=f"A{r}",
              value_input_option="USER_ENTERED")
    # Format H (Route_TAT), K (Transit_Time), M (Actual_Transit_Time),
    # N (Delay_Hours) as [h]:mm
    fmt_reqs = [{"repeatCell": {
        "range": {"sheetId": ws.id,
                  "startRowIndex": r - 1, "endRowIndex": r,
                  "startColumnIndex": ci, "endColumnIndex": ci + 1},
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "TIME", "pattern": "[h]:mm"},
        }},
        "fields": "userEnteredFormat.numberFormat",
    }} for ci in (7, 10, 12, 13)]
    ss.batch_update({"requests": fmt_reqs})


def _write_completed_trip(trip_ss, tab_name: str, trip_data: dict):
    """
    Append a completed trip to the MIS tab (one row per RPS). Silently skips
    duplicates. End_Time must already be set in trip_data by the caller
    (it comes from the locked Route_Reaching_Date_Time in the Tracking sheet).
    """
    rps_no = trip_data.get("RPS_Number", "")
    if not rps_no:
        return
    ws = _get_or_create_trip_tab(trip_ss, tab_name)
    if rps_no in ws.col_values(1):
        return   # already logged — dedup
    # End_Time is the GPS-confirmed Route_Reaching_Date_Time passed by caller.
    # No need to re-query the RPS API; that time is already accurate.
    row_num = len(ws.col_values(1)) + 1
    _write_trip_row(trip_ss, ws, row_num, trip_data)
    end_str = trip_data.get("End_Time", "") or "(pending)"
    print(f"  [✓ Trip→{tab_name}] RPS {rps_no} | {trip_data.get('Vehicle_Number','')} "
          f"| Arrival: {end_str}", flush=True)


def sync_reached_to_trip_sheet(hub_ws, trip_sheet_id,
                                sla_map, vt_sheet, dry_run=False):
    """
    Read hub Tracking sheet directly. Copy every row where
      Current_Stage == "Reached-Unloading" AND
      Route_Reaching_Date_Time is filled AND
      RPS_No is filled
    to the correct monthly MIS tab in the trip sheet.
    Skips RPS numbers already present (dedup via col-A check).
    Returns count of rows newly written.
    """
    if not trip_sheet_id or dry_run:
        return 0

    all_rows = hub_ws.get_all_values()
    if len(all_rows) < 2:
        return 0

    header = [h.strip() for h in all_rows[0]]

    def ci(name, default):
        try:
            return header.index(name)
        except ValueError:
            return default

    ci_stage   = ci("Current_Stage",            10)
    ci_arrival = ci("Route_Reaching_Date_Time",   8)
    ci_rps     = ci("RPS_No",                     2)
    ci_vno     = ci("Vehicle_No",                 3)
    ci_route   = ci("Route",                      5)
    ci_rcode   = ci("Route_Code",                 6)
    ci_start   = ci("Route_Start_Date_Time",       7)
    ci_driver  = ci("Driver_Name",               15)
    ci_dcode   = ci("Driver_Code",               16)
    ci_adv     = ci("Given_Advance",             17)
    ci_diesel  = ci("Given_Diesel",              18)
    ci_damt    = ci("Diesel_Amount",             19)
    ci_toll    = ci("Given_Toll",                20)
    ci_challan = ci("Given_Challan",             21)
    ci_xdsl    = ci("Extra_Diesel",              22)
    ci_maint   = ci("Maintainance",              23)

    def cell(row, idx):
        return row[idx].strip() if idx < len(row) else ""

    trip_ss = _gspread_client().open_by_key(trip_sheet_id)
    copied  = 0

    for row in all_rows[1:]:
        stage   = cell(row, ci_stage)
        arrival = cell(row, ci_arrival)
        rps     = cell(row, ci_rps)

        if stage != "Reached-Unloading" or not arrival or not rps:
            continue

        vno     = cell(row, ci_vno)
        start   = cell(row, ci_start)
        rcode   = cell(row, ci_rcode)
        tat_hrs = sla_map.get(rcode.upper()) if rcode else None

        start_dt = _parse_since_dt(start)
        tab_name = (start_dt.strftime("%B_%Y_MIS") if start_dt
                    else datetime.now().strftime("%B_%Y_MIS"))

        trip_data = {
            "RPS_Number":     rps,
            "Vehicle_Number": vno,
            "Vehicle_Size":   vt_sheet.get(vno, ""),
            "Driver_Name":    cell(row, ci_driver),
            "Driver_Code":    cell(row, ci_dcode),
            "Route":          cell(row, ci_route),
            "Route_Code":     rcode,
            "Route_TAT":      tat_hrs / 24 if tat_hrs else "",
            "Start_Time":     start,
            "End_Time":       arrival,
            "Given_Advance":  cell(row, ci_adv),
            "Given_Diesel":   cell(row, ci_diesel),
            "Diesel_Amount":  cell(row, ci_damt),
            "Given_Toll":     cell(row, ci_toll),
            "Given_Challan":  cell(row, ci_challan),
            "Extra_Diesel":   cell(row, ci_xdsl),
            "Maintainance":   cell(row, ci_maint),
        }
        _write_completed_trip(trip_ss, tab_name, trip_data)
        copied += 1

    return copied


# ── Main update ────────────────────────────────────────────────────────────────

def _collect_via_departures(snapshot: dict, stage_map: dict) -> list[dict]:
    """
    Return the via stops that ENDED this cycle (was "At Via Stop", now isn't).
    Non-via stoppages are intentionally excluded.
    """
    departed = []
    for vno, prev in snapshot.items():
        if prev.get("stage", "") != "At Via Stop":
            continue
        if (stage_map or {}).get(vno, "") == "At Via Stop":
            continue   # still at the via — log when it leaves
        stop_hrs = parse_stoppage_hrs(prev["duration"])
        excess   = max(0.0, stop_hrs - FREE_WINDOW_HRS)
        departed.append({
            "vno":      vno,
            "rps":      prev["rps"],
            "route":    prev["route"],
            "location": prev["location"],
            "since":    prev["since"],
            "duration": prev["duration"],
            "excess":   excess,
        })
    return departed


def load_shared(ss, vehicles: list[dict]) -> dict:
    """
    Load every lookup that's shared across the master and all hub sheets,
    and perform the master-only side-tab housekeeping (hub-coord discovery,
    route-code discovery, Vehicles-tab auto-add). Runs ONCE per cycle on the
    master spreadsheet. Returns a dict consumed by write_tracking().
    """
    # ── Hub coordinates (Hub Locations sheet + FMS API) ───────────────────────
    hl_ws            = get_or_create_tab(ss, HUB_LOCATIONS_TAB, HUB_LOCATIONS_HEADERS)
    sheet_hub_coords = load_hub_coords_tab(hl_ws)
    api_hub_coords, api_raw_rows = fetch_hub_locations()
    hub_coords = {**api_hub_coords, **sheet_hub_coords}   # sheet wins on collision

    existing_upper = set(sheet_hub_coords.keys())
    new_coord_rows = [r for r in api_raw_rows if r[0].upper() not in existing_upper]
    if new_coord_rows:
        hl_ws.append_rows(new_coord_rows, value_input_option="RAW")
        print(f"  [Hub Locations] +{len(new_coord_rows)} coordinate(s) added from API",
              flush=True)

    # Recover code→coords from "(CODE)" embedded in hub NAME keys (col B is blank).
    hub_coords_by_code = load_hub_coords_by_code(hl_ws)
    for name_key, coord in hub_coords.items():
        m = _HUB_CODE_RE.search(name_key)
        if m:
            hub_coords_by_code.setdefault(m.group(1).upper(), coord)

    _hl_rows           = hl_ws.get_all_values()
    existing_hub_codes = {r[1].strip().upper() for r in _hl_rows[1:]
                          if len(r) > 1 and r[1].strip()}
    existing_hub_names = {r[0].strip().upper() for r in _hl_rows[1:]
                          if r and r[0].strip()}

    if hub_coords:
        print(f"  [Hub Locations] {len(hub_coords)} hub(s) by name, "
              f"{len(hub_coords_by_code)} by code  "
              f"({len(sheet_hub_coords)} from sheet, "
              f"{len(api_hub_coords)} from API) — GPS via-check active", flush=True)
    else:
        print("  [Hub Locations] No coordinates — using text-only via detection",
              flush=True)

    # ── Route Codes (hub name → code) ─────────────────────────────────────────
    rc_ws    = get_or_create_tab(ss, ROUTE_CODES_TAB, ["Hub_Name", "Hub_Code"])
    rc_sheet = load_lookup_tab(rc_ws)
    if HUB_CODES_FILE.exists():   # one-time migration of legacy json
        try:
            json_hubs = json.loads(HUB_CODES_FILE.read_text(encoding="utf-8"))
            migrate   = {k: v for k, v in json_hubs.items()
                         if k.upper() not in {x.upper() for x in rc_sheet}}
            if migrate:
                append_lookup_rows(rc_ws, migrate)
                rc_sheet.update(migrate)
                print(f"  [Route Codes] Migrated {len(migrate)} entries from hub_codes.json",
                      flush=True)
            HUB_CODES_FILE.unlink()
        except Exception as exc:
            print(f"  [WARN] hub_codes.json migration failed: {exc}", flush=True)

    sheet_hub_upper = {k.upper(): v for k, v in rc_sheet.items()}
    hub_map, new_hubs = build_hub_code_map(vehicles, sheet_hub_upper)
    if new_hubs:
        append_lookup_rows(rc_ws, new_hubs)
        print(f"  [Route Codes] +{len(new_hubs)} new hub(s) discovered", flush=True)

    # ── Vehicles tab (Vehicle No | Vehicle Type | Vehicle Hub | Vehicle Route) ─
    veh_ws = get_or_create_tab(ss, VEHICLES_TAB, VEHICLES_HEADERS)
    vt_map, vehicle_hub, vehicle_route, present = load_vehicles_tab(veh_ws)

    new_veh = []
    for v in vehicles:
        vno = (v.get("vehicleNumber") or "").strip()
        if not vno or vno in present:
            continue
        vtype = vt_map.get(vno) or vehicle_type(v)
        if vtype:
            vt_map[vno] = vtype
        new_veh.append([vno, vtype, "", ""])    # blank hub + blank route
        present.add(vno)
    if new_veh:
        veh_ws.append_rows(new_veh, value_input_option="RAW")
        print(f"  [Vehicles] +{len(new_veh)} new vehicle(s) added (blank hub — "
              f"assign Ambala / Ambala Local in the Vehicles tab)", flush=True)

    # Warn about vehicles with no/unknown hub — they stay master-only.
    unknown = sorted(
        (v.get("vehicleNumber") or "").strip() for v in vehicles
        if (v.get("vehicleNumber") or "").strip()
        and vehicle_hub.get((v.get("vehicleNumber") or "").strip(), "")
        not in HUB_TRACKING_SHEETS)
    if unknown:
        shown = ", ".join(unknown[:15]) + (" …" if len(unknown) > 15 else "")
        print(f"  [Vehicles] {len(unknown)} vehicle(s) without a known hub "
              f"(master only): {shown}", flush=True)

    # ── Route SLA (Route_Code | Expected_Hours) — drives self-computed delay ───
    sla_ws = get_or_create_tab(ss, ROUTE_SLA_TAB, ROUTE_SLA_HEADERS)
    sla_map, existing_sla_codes = load_route_sla_tab(sla_ws)
    if sla_map:
        print(f"  [Route SLA] {len(sla_map)} route(s) with hours "
              f"({len(existing_sla_codes)} listed) — self-computed delay active",
              flush=True)
    else:
        print("  [Route SLA] No SLA hours yet — using FMS delay until filled",
              flush=True)

    # ── Vehicle ID map (plate → numeric id) — for Tracking Report lookups ───
    vehicle_id_map = fetch_vehicle_id_map()

    return {
        "hub_coords":         hub_coords,
        "hub_coords_by_code": hub_coords_by_code,
        "hl_ws":              hl_ws,
        "existing_hub_codes": existing_hub_codes,
        "existing_hub_names": existing_hub_names,
        "hub_map":            hub_map,
        "vt_map":             vt_map,
        "vehicle_hub":        vehicle_hub,
        "vehicle_route":      vehicle_route,
        "sla_map":            sla_map,
        "existing_sla_codes": existing_sla_codes,
        "sla_ws":             sla_ws,
        "vehicle_id_map":     vehicle_id_map,
    }


def write_tracking(ss, ws: gspread.Worksheet, vehicles: list[dict], shared: dict,
                   dry_run: bool = False, do_side_effects: bool = False,
                   remove_strangers: bool = False,
                   trip_sheet_id: str | None = None):
    """
    Write the Tracking tab for one spreadsheet from `vehicles` (already filtered
    to the right subset) using the `shared` lookups.

    do_side_effects   : master only — write missing-coord hubs + stoppage log.
    remove_strangers  : hub sheets — blank rows for vehicles not in this subset
                        (handles vehicles whose hub changed / were removed).
    """
    hub_coords         = shared["hub_coords"]
    hub_coords_by_code = shared["hub_coords_by_code"]
    hub_map            = shared["hub_map"]
    vt_sheet           = shared["vt_map"]
    vehicle_route_map  = shared["vehicle_route"]
    existing_hub_codes = shared["existing_hub_codes"]
    existing_hub_names = shared["existing_hub_names"]
    hl_ws              = shared["hl_ws"]
    sla_map            = shared["sla_map"]
    existing_sla_codes = shared["existing_sla_codes"]
    sla_ws             = shared["sla_ws"]
    vehicle_id_map     = shared.get("vehicle_id_map", {})

    # ── Tracking: read once. Indexed by Vehicle_No so user-sort doesn't matter.
    all_rows = ws.get_all_values()
    existing_header = all_rows[0] if all_rows else []
    write_headers(ws, existing_header)
    stage_snapshot, row_map, lock_vals = _parse_sheet(all_rows)
    live_by_vno: dict[str, list] = {
        vno: all_rows[rnum - 1]
        for vno, rnum in row_map.items()
        if 0 <= rnum - 1 < len(all_rows)
    }

    # ── Shadow: independent layout, ALSO keyed by Vehicle_No. Whatever order
    #     the user sorts Tracking into, the shadow keeps its own positions
    #     and the per-cell freeze still finds the right baseline by vno.
    shadow_ws  = get_or_create_shadow(ss)
    shadow_all = shadow_ws.get_all_values()
    shadow_row_of: dict[str, int] = {}
    shadow_by_vno: dict[str, list] = {}
    for i, row in enumerate(shadow_all[1:], start=DATA_START):
        svno = row[KEY_COL].strip() if len(row) > KEY_COL else ""
        if svno:
            shadow_row_of[svno] = i
            shadow_by_vno[svno] = row

    # Assign a Tracking row for each vehicle (existing keep theirs; new appended).
    next_trk_row = max(row_map.values(), default=DATA_START - 1) + 1
    assignments: dict[str, int] = {}
    valid_vnos: set = set()
    for v in vehicles:
        vno = (v.get("vehicleNumber") or "").strip()
        if not vno:
            continue
        valid_vnos.add(vno)
        if vno in row_map:
            assignments[vno] = row_map[vno]
        else:
            assignments[vno] = next_trk_row
            row_map[vno]     = next_trk_row
            next_trk_row    += 1

    # Assign a shadow row (independent of Tracking). Existing vno keeps its
    # shadow row; new ones are appended to the shadow.
    next_shd_row = max(shadow_row_of.values(), default=DATA_START - 1) + 1
    for vno in valid_vnos:
        if vno not in shadow_row_of:
            shadow_row_of[vno] = next_shd_row
            next_shd_row      += 1

    tracking_updates: list = []
    shadow_updates:   list = []
    color_requests:   list = []
    stage_map:        dict = {}
    missing_coord_hubs: dict[str, str] = {}
    missing_sla:    dict = {}    # route_code → True, pending operator SLA hours
    edits_kept = 0
    completed_trips: list = []   # trips to log to MIS sheet this cycle

    def _cell_color(sheet_id: int, row_num: int, col_idx: int, bg: dict) -> dict:
        return {"repeatCell": {
            "range": {"sheetId": sheet_id,
                      "startRowIndex":    row_num - 1, "endRowIndex": row_num,
                      "startColumnIndex": col_idx,     "endColumnIndex": col_idx + 1},
            "cell": {"userEnteredFormat": {"backgroundColor": bg}},
            "fields": "userEnteredFormat.backgroundColor",
        }}

    skipped = 0
    for v in vehicles:
        try:
            vno = (v.get("vehicleNumber") or "").strip()
            if not vno or vno not in assignments:
                continue
            codes_str       = (v.get("consigneeCode") or "").strip()
            consignee_codes = [c.strip() for c in codes_str.split(";") if c.strip()] \
                              if codes_str else []

            consigner_name  = (v.get("consignerName") or "").strip()
            consignee_names = [n.strip() for n in (v.get("consigneeName") or "").split(";")
                               if n.strip()]
            route_hub_names = consignee_names  # origin is not a via stop

            row_num   = assignments[vno]
            sno       = row_num - DATA_START + 1
            prev_snap = stage_snapshot.get(vno, {})

            stage, stage_color = derive_stage(v, hub_coords, consignee_codes,
                                              hub_coords_by_code, prev_snap,
                                              route_hub_names)
            status             = derive_status(v, stage)
            stage_map[vno]     = stage

            # ── Per-cell freeze (vno-indexed; survives any sort of Tracking) ─
            trk_row    = row_num
            shd_row    = shadow_row_of[vno]
            live_row   = live_by_vno.get(vno)        # None if new in Tracking
            shadow_row = shadow_by_vno.get(vno)      # None if new in shadow
            cur_rps    = fmt(v.get("tripId")) if v.get("isOnTrip") else ""
            shadow_rps = (shadow_row[2].strip() if shadow_row and len(shadow_row) > 2 else "")
            new_trip   = (shadow_row is None) or (bool(cur_rps) and cur_rps != shadow_rps)

            # ── Completed trip detection ──────────────────────────────────────
            # confirmed_arrival = Route_Reaching_Date_Time already in the sheet
            # (lock_vals holds what was there before this cycle's writes).
            # rps_no_snap = the RPS from the snapshot (prev cycle).
            confirmed_arrival = lock_vals.get(vno, "")
            rps_no_snap       = prev_snap.get("rps", "")

            # Console: log first-time arrival (stage just flipped to Reached-Unloading)
            prev_stage = prev_snap.get("stage", "")
            if stage == "Reached-Unloading" and prev_stage != "Reached-Unloading":
                arrival_label = confirmed_arrival or "(arrival time → next cycle)"
                print(f"  [▶ REACHED] {vno} | RPS {rps_no_snap or 'unknown'} "
                      f"| Arrival: {arrival_label}", flush=True)

            # Copy to trip sheet when:
            #   A) Stage is still Reached-Unloading AND arrival confirmed, OR
            #   B) Stage just left Reached-Unloading (LAST CHANCE before arrival
            #      gets wiped this cycle) — catches the FMS trip-close cycle.
            # Dedup inside _write_completed_trip prevents double rows per RPS.
            should_copy = (
                trip_sheet_id and not dry_run
                and confirmed_arrival
                and rps_no_snap
                and (stage == "Reached-Unloading"          # still at dest
                     or prev_stage == "Reached-Unloading") # just left dest
            )
            if should_copy:
                def _lv(col: int, _row=live_row) -> str:
                    return (_row[col].strip()
                            if _row and col < len(_row) else "")
                live_rcode = _lv(6)
                if live_rcode in ("Not Assigned", NOT_ASSIGNED, ""):
                    live_rcode = prev_snap.get("route", "")
                if live_rcode in ("Not Assigned", NOT_ASSIGNED):
                    live_rcode = ""
                tat_hrs = sla_map.get(live_rcode.upper()) if live_rcode else None
                completed_trips.append({
                    "RPS_Number":     rps_no_snap,
                    "Vehicle_Number": vno,
                    "Vehicle_Size":   vt_sheet.get(vno, ""),
                    "Driver_Name":    _lv(15),
                    "Driver_Code":    _lv(16),
                    "Route":          _lv(5),
                    "Route_Code":     live_rcode,
                    "Route_TAT":      tat_hrs / 24 if tat_hrs else "",
                    "Start_Time":     _lv(7),
                    "End_Time":       confirmed_arrival,   # GPS-confirmed arrival
                    "Given_Advance":  _lv(17),
                    "Given_Diesel":   _lv(18),
                    "Diesel_Amount":  _lv(19),
                    "Given_Toll":     _lv(20),
                    "Given_Challan":  _lv(21),
                    "Extra_Diesel":   _lv(22),
                    "Maintainance":   _lv(23),
                })

            # Queue planned hubs missing coords for manual entry (master only).
            if do_side_effects and v.get("isOnTrip"):
                for nm in route_hub_names:
                    mm = _HUB_CODE_RE.search(nm or "")
                    if not mm:
                        continue
                    hcode = mm.group(1).upper()
                    if (hcode in hub_coords_by_code or hcode in existing_hub_codes
                            or nm.strip().upper() in existing_hub_names):
                        continue
                    missing_coord_hubs.setdefault(hcode, nm.strip())

            # Queue routes with no SLA entry yet (master only) → operator fills hours.
            route_code = build_route_code(v, hub_map)
            if (do_side_effects and v.get("isOnTrip") and route_code
                    and route_code.upper() not in existing_sla_codes):
                missing_sla.setdefault(route_code, True)

            row_data = build_row(v, sno, lock_vals.get(vno, ""), hub_map, vt_sheet,
                                 hub_coords, prev_snap, consignee_codes,
                                 hub_coords_by_code, route_hub_names, sla_map,
                                 vehicle_route_map, vehicle_id_map)

            # New trip (new RPS) → overwrite everything. Same trip → for each
            # cell, if the Tracking value differs from what we last wrote
            # (shadow), the user edited it → preserve it; otherwise refresh.

            for col_idx, value in enumerate(row_data):
                if value is None:
                    continue   # None = manual / static / unset lock — never touch
                if not new_trip:
                    live_val   = (live_row[col_idx].strip()
                                  if live_row and col_idx < len(live_row) else "")
                    shadow_val = (shadow_row[col_idx].strip()
                                  if shadow_row and col_idx < len(shadow_row) else "")
                    if live_val != shadow_val:
                        edits_kept += 1
                        continue   # user-edited → preserve until next trip
                tracking_updates.append({
                    "range":  f"{COL_LETTERS[col_idx]}{trk_row}",
                    "values": [[value]],
                })
                shadow_updates.append({
                    "range":  f"{COL_LETTERS[col_idx]}{shd_row}",
                    "values": [[value]],
                })

            # Colors follow the value actually shown in each indicator column.
            status_color = STATUS_COLORS.get(status, WHITE)
            ontime_color = ONTIME_COLORS.get(row_data[11], WHITE)

            color_requests.extend([
                _cell_color(ws.id, trk_row, 9,  status_color),   # Status
                _cell_color(ws.id, trk_row, 10, stage_color),    # Current_Stage
                _cell_color(ws.id, trk_row, 11, ontime_color),   # Ontime_Delay
            ])

        except Exception as exc:
            skipped += 1
            print(f"  [WARN] Skipped vehicle {v.get('vehicleNumber', '?')}: {exc}",
                  flush=True)

    if skipped:
        print(f"  [WARN] {skipped} vehicle(s) skipped due to errors above", flush=True)

    # Blank rows for vehicles that no longer belong in this (hub) sheet.
    # Blank BOTH Tracking and shadow rows so the freeze resets if the vehicle
    # returns to this hub later.
    removed = 0
    if remove_strangers:
        blank_row = [""] * TOTAL_COLS
        last_col  = COL_LETTERS[-1]
        for vno, rnum in row_map.items():
            if vno not in valid_vnos:
                # ── Last-chance trip copy ─────────────────────────────────────
                # Vehicle disappeared from FMS API (trip closed / removed from
                # dashboard) before we could copy it. If it was Reached-Unloading
                # with a confirmed arrival, copy now before blanking the row.
                prev_gone   = stage_snapshot.get(vno, {})
                conf_arr    = lock_vals.get(vno, "")
                rps_gone    = prev_gone.get("rps", "")
                if (trip_sheet_id and not dry_run
                        and prev_gone.get("stage") == "Reached-Unloading"
                        and conf_arr and rps_gone):
                    live_r = live_by_vno.get(vno)
                    def _lv_g(col: int, _row=live_r) -> str:
                        return (_row[col].strip() if _row and col < len(_row) else "")
                    rc_gone = _lv_g(6)
                    if rc_gone in ("Not Assigned", NOT_ASSIGNED, ""):
                        rc_gone = prev_gone.get("route", "")
                    if rc_gone in ("Not Assigned", NOT_ASSIGNED):
                        rc_gone = ""
                    tat_g = sla_map.get(rc_gone.upper()) if rc_gone else None
                    print(f"  [↑ LAST-CHANCE] {vno} | RPS {rps_gone} | "
                          f"copying to trip sheet before row is blanked", flush=True)
                    completed_trips.append({
                        "RPS_Number":     rps_gone,
                        "Vehicle_Number": vno,
                        "Vehicle_Size":   vt_sheet.get(vno, ""),
                        "Driver_Name":    _lv_g(15),
                        "Driver_Code":    _lv_g(16),
                        "Route":          _lv_g(5),
                        "Route_Code":     rc_gone,
                        "Route_TAT":      tat_g / 24 if tat_g else "",
                        "Start_Time":     _lv_g(7),
                        "End_Time":       conf_arr,
                        "Given_Advance":  _lv_g(17),
                        "Given_Diesel":   _lv_g(18),
                        "Diesel_Amount":  _lv_g(19),
                        "Given_Toll":     _lv_g(20),
                        "Given_Challan":  _lv_g(21),
                        "Extra_Diesel":   _lv_g(22),
                        "Maintainance":   _lv_g(23),
                    })
                # Now blank the row
                tracking_updates.append({"range":  f"A{rnum}:{last_col}{rnum}",
                                         "values": [blank_row]})
                removed += 1
                shd_rnum = shadow_row_of.get(vno)
                if shd_rnum:
                    shadow_updates.append({"range":  f"A{shd_rnum}:{last_col}{shd_rnum}",
                                           "values": [blank_row]})

    # Surface planned hubs that need coordinates (master only).
    if do_side_effects and missing_coord_hubs:
        miss_rows = [[name, code, "", ""]
                     for code, name in sorted(missing_coord_hubs.items())]
        if dry_run:
            print(f"  [DRY RUN] {len(miss_rows)} planned hub(s) without coords would be "
                  f"added to '{HUB_LOCATIONS_TAB}' for manual entry.", flush=True)
        else:
            hl_ws.append_rows(miss_rows, value_input_option="RAW")
            print(f"  [Hub Locations] +{len(miss_rows)} planned hub(s) need coordinates "
                  f"— added with blank lat/lon for manual entry.", flush=True)

    # Surface routes with no SLA hours yet (master only) → operator fills hours.
    if do_side_effects and missing_sla:
        sla_rows = [[rc, ""] for rc in sorted(missing_sla)]
        if dry_run:
            print(f"  [DRY RUN] {len(sla_rows)} route(s) without SLA would be added to "
                  f"'{ROUTE_SLA_TAB}' for manual entry.", flush=True)
        else:
            sla_ws.append_rows(sla_rows, value_input_option="RAW")
            print(f"  [Route SLA] +{len(sla_rows)} route(s) need hours — added with "
                  f"blank Expected_Hours (FMS delay used until filled).", flush=True)

    if dry_run:
        print(f"  [DRY RUN] {ws.spreadsheet.title}/{ws.title}: "
              f"{len(assignments)} rows, {len(tracking_updates)} cell updates, "
              f"{removed} stale row(s), {edits_kept} user edit(s) kept "
              f"— write skipped.", flush=True)
        return stage_snapshot, stage_map

    if tracking_updates:
        _ensure_rows(ws, max(assignments.values(), default=DATA_START))
        ws.batch_update(tracking_updates, value_input_option="RAW")
    if shadow_updates:
        # Shadow has its own row positions (vno-keyed). Keeps the per-cell
        # freeze working even if the user sorts the Tracking tab.
        _ensure_rows(shadow_ws, max(shadow_row_of.values(), default=DATA_START))
        shadow_ws.batch_update(shadow_updates, value_input_option="RAW")
    if color_requests:
        ws.spreadsheet.batch_update({"requests": color_requests})

    print(f"  [{ws.spreadsheet.title}/{ws.title}] {len(assignments)} rows | "
          f"{len(tracking_updates)} cells | {len(color_requests)} colors"
          f"{f' | {removed} removed' if removed else ''}"
          f"{f' | {edits_kept} edits kept' if edits_kept else ''}", flush=True)

    # ── Write completed trips to MIS sheet (hub sheets only) ─────────────────
    if completed_trips and trip_sheet_id:
        try:
            trip_ss = _gspread_client().open_by_key(trip_sheet_id)
            for trip in completed_trips:
                start_dt = _parse_since_dt(trip.get("Start_Time", ""))
                tab_name = (start_dt.strftime("%B_%Y_MIS") if start_dt
                            else datetime.now().strftime("%B_%Y_MIS"))
                _write_completed_trip(trip_ss, tab_name, trip)
        except Exception as exc:
            print(f"  [WARN] Trip sheet write failed: {exc}", flush=True)
            traceback.print_exc()

    return stage_snapshot, stage_map


# ── Entry point ────────────────────────────────────────────────────────────────

def run_once(dry_run: bool = False):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{ts}] Refreshing…", flush=True)
    vehicles = fetch_vehicles()

    # ── Master file: all vehicles + side tabs ─────────────────────────────────
    ss, ws = connect()                       # master
    shared = load_shared(ss, vehicles)
    master_snapshot, master_stage_map = write_tracking(
        ss, ws, vehicles, shared, dry_run=dry_run,
        do_side_effects=True, remove_strangers=False)

    # ── Console summary: vehicles at Reached-Unloading this cycle ────────────
    reached_vehicles = [
        v for v in vehicles
        if master_stage_map.get((v.get("vehicleNumber") or "").strip())
        == "Reached-Unloading"
    ]
    if reached_vehicles:
        print(f"\n  ╔═ REACHED THIS CYCLE ({len(reached_vehicles)} vehicle(s)) ════════════════════",
              flush=True)
        for rv in reached_vehicles:
            rvno = (rv.get("vehicleNumber") or "").strip()
            rps  = (fmt(rv.get("tripId")) if rv.get("isOnTrip")
                    else master_snapshot.get(rvno, {}).get("rps", ""))
            dest = (rv.get("consigneeName") or "").split(";")[-1].strip()
            print(f"  ║  {rvno:15s} | RPS {rps or 'unknown':12s} | Dest: {dest}",
                  flush=True)
        print(f"  ╚{'═' * 55}", flush=True)
    else:
        print("\n  [REACHED] No vehicles at final destination this cycle.", flush=True)

    # ── Hub mirrors: each gets ONLY its hub's vehicles (Tracking tab) ────────
    vehicle_hub = shared["vehicle_hub"]
    for hub_name, (sheet_id, tab) in HUB_TRACKING_SHEETS.items():
        subset = [v for v in vehicles
                  if vehicle_hub.get((v.get("vehicleNumber") or "").strip(), "")
                  == hub_name]
        print(f"\n  → Hub '{hub_name}': {len(subset)} vehicle(s)", flush=True)
        try:
            hub_ss, hub_ws = connect(sheet_id, tab)
            trip_sid = HUB_TRIP_SHEETS.get(hub_name) or None
            # Step 1: copy reached rows from sheet BEFORE write_tracking blanks them
            if trip_sid and not dry_run:
                try:
                    n = sync_reached_to_trip_sheet(
                        hub_ws, trip_sid,
                        shared["sla_map"], shared["vt_map"],
                        dry_run=False,
                    )
                    if n:
                        print(f"  [Trip Sync] {n} reached row(s) copied to trip sheet", flush=True)
                except Exception as exc:
                    print(f"  [WARN] Trip sync error: {exc}", flush=True)
                    traceback.print_exc()
            # Step 2: update the tracking sheet (trip copy already handled above)
            write_tracking(hub_ss, hub_ws, subset, shared, dry_run=dry_run,
                           do_side_effects=False, remove_strangers=True,
                           trip_sheet_id=None)
        except Exception as exc:
            # One bad hub sheet must not break the master or the other hubs.
            print(f"  [ERROR] Hub '{hub_name}' update failed: {exc}", flush=True)
            traceback.print_exc()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done.", flush=True)


def main():
    ap = argparse.ArgumentParser(
        description="FMS Smart → Google Sheets live tracker")
    ap.add_argument("--loop",     action="store_true",
                    help="Run continuously on a fixed interval")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"Loop interval in seconds (default {DEFAULT_INTERVAL})")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Fetch data and print counts, but do not write to the sheet")
    args = ap.parse_args()

    if args.loop:
        print(f"Loop mode — every {args.interval}s. Ctrl+C to stop.", flush=True)
        while True:
            try:
                run_once(dry_run=args.dry_run)
            except Exception as e:
                # Print full traceback so production errors are diagnosable
                print(f"  [ERROR] {e}", flush=True)
                traceback.print_exc()
            print(f"  Next refresh in {args.interval}s…", flush=True)
            time.sleep(args.interval)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
