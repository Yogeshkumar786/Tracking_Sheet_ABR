"""
ctyf_to_sheets.py
─────────────────────────────────────────────────────────────────────────────
Supplements the FMS tracking sheets with live GPS data from CTYF / GTrophy.

Groups tracked:
  - ABR ROADLINES PRIVATE LIMITED  (group_id = 4)
  - TARGET TIME LOGISTICS           (group_id = 63)

User-edit protection (shared with fms_to_sheets.py):
  Both scripts read/write the same hidden '_shadow' tab.
  Before writing any script-managed cell, CTYF checks:
    live_val == shadow_val  →  script last wrote it  →  safe to update
    live_val != shadow_val  →  user edited it        →  preserve, skip
  After writing, CTYF updates the shadow so FMS also sees the new baseline.

Columns CTYF writes (and shadows):
  I  Status               — Moving / On Trip-Stopped / Stopped / Untracked
  J  Current_Stage        — In Transit / At Loading / Halted on Road / Untracked
  K  Ontime_Delay         — "Not On Trip" when RPS blank; skip when RPS set
  L  Current_Location     — NearestLocation from CTYF GPS
  O  Current_Stop_Duration— idle time converted to FMS format (00D 00H 00M 00S)
  P  Last_GPS_Update      — timestamp converted to DD/MM/YYYY HH:MM:SS
  Q  Last_Refreshed       — datetime.now()

Columns NEVER touched (user / FMS owned):
  A  S.No          B  Vehicle_Route    C  RPS_No
  D  Vehicle_No    E  Vehicle_Type     F  Route
  G  Route_Code    H  Route_Start_Date_Time
  M  Delay_Hrs     N  Current_Stop_Since

Hub routing mirrors fms_to_sheets.py:
  Master → ALL CTYF vehicles
  Ambala / Ambala Local → filtered by "Vehicle Hub" in Vehicles tab
"""

import warnings
import sys
from datetime import datetime
from pathlib import Path

import gspread
import requests
from google.oauth2.service_account import Credentials

from ctyf_auth import CtyFAuth

# Reconfigure stdout to support unicode prints on Windows console without crashes
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

warnings.filterwarnings("ignore")

# ── Sheet config (mirrors fms_to_sheets.py exactly) ───────────────────────────
MASTER_SHEET_ID = "1-tEwE7YwZFNhfGjvgZPHYMKXJqSr_TOmrAodfuadGf0"
TRACKING_TAB    = "Tracking"
VEHICLES_TAB    = "Vehicles"
SHADOW_TAB      = "_shadow"          # shared with fms_to_sheets.py
CREDS_FILE      = Path(__file__).parent / "credentials.json"

HUB_TRACKING_SHEETS = {
    "Ambala":       "1xHxlccSE3z4cE-HqI8bh9Lwja7I_VkbkkTStWCcLvpE",
    "Ambala Local": "1C9BePLnuPL1DfnNtuKheZ1uWu5j1ob_zoMXsXo0REgQ",
    "Binola": "1dagH3DjC4dXMQwVHVoE9mMJUQRPYEH6KDPc6OolLm5A",
    "Binola Local": "15xvjwps6zuOP3ZKCPzsGRHUQuh24-4wh8Mm9tT8O-i8",
    "G.Noida": "16DgFINLCJ3-AUirn1MRSZS2LrrgO4LoudyzoMzaMk-U",
}

DATA_START  = 2
TOTAL_COLS  = 21

# 0-based column indices — identical to fms_to_sheets.py COLUMNS
COL_SNO       = 0   # A  S.No
COL_VEH_RT    = 1   # B  Vehicle_Route
COL_RPS       = 2   # C  RPS_No          ← user-owned (CTYF never writes)
COL_VEH_NO    = 3   # D  Vehicle_No      ← KEY
COL_VEH_TYPE  = 4   # E  Vehicle_Type
COL_ROUTE     = 5   # F  Route           ← user-owned (CTYF never writes)
COL_RT_CODE   = 6   # G  Route_Code      ← user-owned (CTYF never writes)
COL_RT_TAT    = 7   # H  Route_TAT       ← user-owned (CTYF never writes)
COL_START_DT  = 8   # I  Route_Start_Date_Time ← user-owned (CTYF never writes)
COL_SCHED_ARR = 9   # J  Route_Scheduled_Arrival ← user-owned (CTYF never writes)
COL_END_DT    = 10  # K  Route_End_Date_Time ← user-owned (CTYF never writes)
COL_STATUS    = 11  # L  Status          ← CTYF writes + shadows
COL_STAGE     = 12  # M  Current_Stage   ← CTYF writes + shadows
COL_ONTIME    = 13  # N  Ontime_Delay    ← CTYF writes (no-RPS only) + shadows
COL_LOCATION  = 14  # O  Current_Location← CTYF always writes + shadows
COL_REASON    = 15  # P  Reason          ← user-owned (CTYF never writes)
COL_DELAY     = 16  # Q  Delay_Hrs       ← CTYF never writes
COL_STOP_SNC  = 17  # R  Current_Stop_Since ← CTYF never writes
COL_STOP_DUR  = 18  # S  Current_Stop_Duration ← CTYF always writes + shadows
COL_LAST_GPS  = 19  # T  Last_GPS_Update ← CTYF always writes + shadows
COL_LAST_REF  = 20  # U  Last_Refreshed  ← CTYF always writes + shadows

# Columns CTYF writes — shadow checked before each write
CTYF_MANAGED_COLS = {COL_STATUS, COL_STAGE, COL_ONTIME,
                     COL_LOCATION, COL_STOP_DUR, COL_LAST_GPS, COL_LAST_REF}

# ── Colors (exact copies from fms_to_sheets.py) ───────────────────────────────
STAGE_COLORS = {
    "In Transit":        {"red": 0.714, "green": 0.933, "blue": 0.714},
    "At Loading":        {"red": 1.000, "green": 0.851, "blue": 0.600},
    "Loading Overdue":   {"red": 0.850, "green": 0.330, "blue": 0.100},
    "At Via Stop":       {"red": 1.000, "green": 0.953, "blue": 0.714},
    "Almost Reached":    {"red": 0.745, "green": 0.850, "blue": 0.980},
    "Reached-Unloading": {"red": 0.678, "green": 0.847, "blue": 0.980},
    "Halted on Road":    {"red": 1.000, "green": 0.800, "blue": 0.800},
    "NRD":               {"red": 0.900, "green": 0.700, "blue": 0.700},
    "Untracked":         {"red": 0.850, "green": 0.850, "blue": 0.850},
    "Unknown":           {"red": 0.930, "green": 0.930, "blue": 0.930},
}
STATUS_COLORS = {
    "Moving":            {"red": 0.714, "green": 0.933, "blue": 0.714},
    "On Trip - Stopped": {"red": 1.000, "green": 0.953, "blue": 0.714},
    "Stopped":           {"red": 1.000, "green": 0.851, "blue": 0.600},
    "Reached":           {"red": 0.678, "green": 0.847, "blue": 0.980},
    "NRD":               {"red": 0.900, "green": 0.700, "blue": 0.700},
    "Untracked":         {"red": 0.850, "green": 0.850, "blue": 0.850},
    "Unknown":           {"red": 0.930, "green": 0.930, "blue": 0.930},
}
ONTIME_COLORS = {
    "On Time":     {"red": 0.714, "green": 0.933, "blue": 0.714},
    "Early":       {"red": 0.714, "green": 0.933, "blue": 0.714},
    "Delayed":     {"red": 0.850, "green": 0.330, "blue": 0.100},
    "Not On Trip": {"red": 0.850, "green": 0.850, "blue": 0.850},
}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

# Hub keywords — matches fms_to_sheets.py HUB_KEYWORDS
HUB_KEYWORDS = frozenset([
    "safexpress", "hub", "terminal", "depot", "warehouse",
    "abr binola", "abr", "logistics park", "freight station",
])

# ── Format converters ──────────────────────────────────────────────────────────

def _idle_to_dhms(idle: str) -> str:
    """'H:MM:SS' / 'HH:MM:SS'  →  FMS format '00D 00H 00M 00S'."""
    try:
        parts = idle.strip().split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            total_s = h * 3600 + m * 60 + s
            d, rem  = divmod(total_s, 86400)
            hh, rem = divmod(rem, 3600)
            mm, ss  = divmod(rem, 60)
            return f"{d:02d}D {hh:02d}H {mm:02d}M {ss:02d}S"
    except Exception:
        pass
    return idle


def _gps_date_to_sheet(date_str: str) -> str:
    """'30 Jun 26 18:56'  →  FMS format '30/06/2026 18:56:00'."""
    s = date_str.strip()
    for fmt in ("%d %b %y %H:%M", "%d %b %Y %H:%M",
                "%d %b %y %H:%M:%S", "%d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%d/%m/%Y %H:%M:%S")
        except ValueError:
            pass
    return s


# CTYF API
LOCATE_API_URL   = "https://api-locate.com/api/v1.0/dashboard/data"
LOCATE_HEADERS   = {
    "accept":        "*/*",
    "content-type":  "application/json",
    "origin":        "https://ctyf.co.in",
    "referer":       "https://ctyf.co.in/",
    "user-agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
TARGET_GROUP_IDS = {"4", "63"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def col_letter(idx: int) -> str:
    result, n = "", idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


COL_LETTERS = [col_letter(i) for i in range(TOTAL_COLS)]


def _gspread_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(
        str(CREDS_FILE),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def _cell_color_req(sheet_id: int, row: int, col: int, color: dict) -> dict:
    return {"repeatCell": {
        "range": {
            "sheetId":          sheet_id,
            "startRowIndex":    row - 1,
            "endRowIndex":      row,
            "startColumnIndex": col,
            "endColumnIndex":   col + 1,
        },
        "cell": {"userEnteredFormat": {"backgroundColor": color}},
        "fields": "userEnteredFormat.backgroundColor",
    }}


# ── Stage / Status logic ───────────────────────────────────────────────────────

def _at_hub(location: str) -> bool:
    loc = (location or "").strip().lower()
    if loc.startswith("at "):
        return any(kw in loc[3:] for kw in HUB_KEYWORDS)
    if loc.startswith("near"):
        return any(kw in loc for kw in HUB_KEYWORDS)
    return False


def derive_stage_status_ontime(
    raw_status: str,
    location:   str,
    has_rps:    bool,
    cur_ontime: str,
) -> tuple[str, dict, str, dict, str | None, dict]:
    """
    Returns: stage, stage_color, status, status_color, ontime (None=skip), ontime_color
    """
    is_moving  = raw_status == "R"
    is_stopped = raw_status in ("S", "I")
    no_signal  = raw_status not in ("R", "S", "I")

    # Stage
    if no_signal:
        stage = "Untracked"
    elif is_moving:
        stage = "In Transit"
    elif _at_hub(location):
        stage = "At Loading"
    else:
        stage = "Halted on Road"
    stage_color = STAGE_COLORS.get(stage, STAGE_COLORS["Unknown"])

    # Status
    if no_signal:
        status = "Untracked"
    elif has_rps:
        status = "Moving" if is_moving else "On Trip - Stopped"
    else:
        status = "Moving" if is_moving else "Stopped"
    status_color = STATUS_COLORS.get(status, WHITE)

    # Ontime_Delay
    if has_rps:
        ontime       = None   # user manages this once RPS is set
        ontime_color = ONTIME_COLORS.get(cur_ontime, ONTIME_COLORS["Not On Trip"])
    else:
        ontime       = "Not On Trip"
        ontime_color = ONTIME_COLORS["Not On Trip"]

    return stage, stage_color, status, status_color, ontime, ontime_color


# ── CTYF data fetch ───────────────────────────────────────────────────────────

def fetch_ctyf_vehicles() -> dict[str, dict]:
    payload = CtyFAuth().get_payload()
    r = requests.post(LOCATE_API_URL, json=payload, headers=LOCATE_HEADERS,
                      verify=False, timeout=30)
    r.raise_for_status()
    raw = r.json()
    cols = raw["cols"]
    rows = raw["rows"]
    ci   = {col: i for i, col in enumerate(cols)}

    def get(row, col):
        idx = ci.get(col)
        return row[idx] if idx is not None and idx < len(row) else None

    result: dict[str, dict] = {}
    for row in rows:
        if str(get(row, "group_id") or "") not in TARGET_GROUP_IDS:
            continue
        vno = str(get(row, "Vehicle") or "").strip().upper()
        if not vno:
            continue
        result[vno] = {
            "vehicle":    vno,
            "group":      str(get(row, "group") or ""),
            "location":   str(get(row, "NearestLocation") or "").strip(),
            "last_gps":   _gps_date_to_sheet(str(get(row, "Date") or "")),
            "idle_time":  _idle_to_dhms(str(get(row, "IdleTime") or "00:00:00")),
            "raw_status": str(get(row, "vehStatus") or "").strip(),
        }

    abr    = sum(1 for v in result.values() if "ABR"    in v["group"].upper())
    target = sum(1 for v in result.values() if "TARGET" in v["group"].upper())
    print(f"  [CTYF] {len(result)} vehicles — {abr} ABR  +  {target} TARGET TIME\n")

    # Persist vehicle list so fms_to_sheets.py knows to protect these rows
    import json as _json
    _veh_file = Path(__file__).parent / "ctyf_vehicles.json"
    _veh_file.write_text(_json.dumps(sorted(result.keys()), indent=2))

    print(f"  {'Vehicle':<14} {'St':<3} {'Idle':<15} {'Last GPS':<21} Location")
    print(f"  {'-'*95}")
    for v in result.values():
        print(f"  {v['vehicle']:<14} {v['raw_status']:<3} {v['idle_time']:<15} "
              f"{v['last_gps']:<21} {v['location'][:40]}")
    print()
    return result


# ── Sheet helpers ─────────────────────────────────────────────────────────────

def load_vehicles_tab(master_ss: gspread.Spreadsheet) -> tuple[dict, dict]:
    """Returns hub_map {VNO: hub} and detail_map {VNO: raw_row}."""
    try:
        ws   = master_ss.worksheet(VEHICLES_TAB)
        rows = ws.get_all_values()
        hub_map:    dict = {}
        detail_map: dict = {}
        for r in rows[1:]:
            if not r or not r[0].strip():
                continue
            vno = r[0].strip().upper()
            hub_map[vno]    = r[2].strip() if len(r) > 2 else ""
            detail_map[vno] = r
        print(f"  [Sheet] Vehicles tab: {len(hub_map)} vehicles")
        return hub_map, detail_map
    except Exception as exc:
        print(f"  [Sheet] WARNING — Vehicles tab: {exc}")
        return {}, {}


def load_tracking_index(ws: gspread.Worksheet) -> tuple[dict, list]:
    """Returns row_map {VNO_UPPER: 1-based row} and all_rows."""
    all_rows = ws.get_all_values()
    row_map: dict[str, int] = {}
    for i, row in enumerate(all_rows[DATA_START - 1:], start=DATA_START):
        vno = row[COL_VEH_NO].strip().upper() if len(row) > COL_VEH_NO else ""
        if vno:
            row_map[vno] = i
    return row_map, all_rows


def load_shadow(ss: gspread.Spreadsheet) -> tuple[gspread.Worksheet,
                                                   dict[str, int],
                                                   dict[str, list]]:
    """
    Load (or create) the shared _shadow tab in `ss`.

    Mirrors fms_to_sheets.py's get_or_create_shadow — if the tab is missing
    (e.g. CTYF runs before FMS has ever touched a hub sheet), we create it
    here so user-edit protection works immediately.

    Returns (shadow_ws, shadow_row_of {VNO: row}, shadow_by_vno {VNO: row_list}).
    """
    # Column headers — same order as fms_to_sheets.py COLUMNS (21 total)
    HEADERS = [
        "S.No", "Vehicle_Route", "RPS_No", "Vehicle_No", "Vehicle_Type",
        "Route", "Route_Code", "Route_TAT", "Route_Start_Date_Time",
        "Route_Scheduled_Arrival", "Route_End_Date_Time",
        "Status", "Current_Stage", "Ontime_Delay", "Current_Location",
        "Reason", "Delay_Hrs", "Current_Stop_Since", "Current_Stop_Duration",
        "Last_GPS_Update", "Last_Refreshed",
    ]

    try:
        shadow_ws = ss.worksheet(SHADOW_TAB)
    except gspread.WorksheetNotFound:
        print(f"  [Shadow] Creating _shadow tab in '{ss.title}'…")
        shadow_ws = ss.add_worksheet(title=SHADOW_TAB, rows=1000, cols=TOTAL_COLS)
        shadow_ws.update(values=[HEADERS], range_name="A1",
                         value_input_option="RAW")
        # Hide it so it doesn't clutter the UI (same as FMS does)
        ss.batch_update({"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": shadow_ws.id, "hidden": True},
            "fields": "hidden",
        }}]})

    try:
        shadow_all = shadow_ws.get_all_values()
    except Exception as exc:
        print(f"  [Shadow] WARNING — could not read shadow: {exc}")
        return shadow_ws, {}, {}

    row_of:  dict[str, int]  = {}
    by_vno:  dict[str, list] = {}
    for i, row in enumerate(shadow_all[1:], start=DATA_START):
        vno = row[COL_VEH_NO].strip().upper() if len(row) > COL_VEH_NO else ""
        if vno:
            row_of[vno] = i
            by_vno[vno] = row

    return shadow_ws, row_of, by_vno


def _should_write(col_idx: int,
                  live_row:   list | None,
                  shadow_row: list | None) -> bool:
    """
    Return True if CTYF should write to this cell.

    Logic (mirrors fms_to_sheets.py):
      live == shadow      →  script last wrote it   →  safe to overwrite
      live != shadow      →  user changed it         →  preserve, skip
      shadow is empty     →  no baseline yet         →  write freely
      live is empty       →  user cleared it / blank →  write freely (re-enable)
    """
    live_val   = (live_row[col_idx].strip()
                  if live_row and col_idx < len(live_row) else "")
    shadow_val = (shadow_row[col_idx].strip()
                  if shadow_row and col_idx < len(shadow_row) else "")
    if not shadow_val:
        return True          # no baseline → write freely
    if not live_val:
        return True          # cell is empty (user cleared it) → write again
    return live_val == shadow_val


def _next_sno(all_rows: list) -> int:
    max_sno = 0
    for row in all_rows[1:]:
        try:
            max_sno = max(max_sno, int(str(row[COL_SNO]).strip()))
        except (ValueError, IndexError):
            pass
    return max_sno + 1


# ── Core write logic ──────────────────────────────────────────────────────────

def process_worksheet(
    ss:          gspread.Spreadsheet,
    ws:          gspread.Worksheet,
    ctyf_data:   dict[str, dict],
    detail_map:  dict[str, list],
    now_str:     str,
    label:       str,
):
    row_map,  all_rows   = load_tracking_index(ws)
    shadow_ws, shd_row_of, shd_by_vno = load_shadow(ss)

    # Shadow needs its own next-row counter (keyed by VNO, independent of Tracking)
    next_shd_row = max(shd_row_of.values(), default=DATA_START - 1) + 1

    tracking_updates: list[dict] = []
    shadow_updates:   list[dict] = []
    color_requests:   list[dict] = []
    to_add:           dict       = {}
    updated:          list[str]  = []
    edits_kept:       int        = 0

    for vno, vdata in ctyf_data.items():
        trk_row = row_map.get(vno)
        if not trk_row:
            to_add[vno] = vdata
            continue

        live_row   = all_rows[trk_row - 1] if (trk_row - 1) < len(all_rows) else None
        shadow_row = shd_by_vno.get(vno)

        def _cell(col):
            return live_row[col].strip() if live_row and col < len(live_row) else ""

        cur_rps    = _cell(COL_RPS)
        cur_ontime = _cell(COL_ONTIME)
        has_rps    = bool(cur_rps)

        stage, s_col, status, st_col, ontime, ot_col = derive_stage_status_ontime(
            vdata["raw_status"], vdata["location"], has_rps, cur_ontime
        )

        # Build the cells CTYF wants to write for this vehicle
        candidates: dict[int, str] = {
            COL_STATUS:   status,
            COL_STAGE:    stage,
            COL_LOCATION: vdata["location"],
            COL_STOP_DUR: vdata["idle_time"],
            COL_LAST_GPS: vdata["last_gps"],
            COL_LAST_REF: now_str,
        }
        if ontime is not None:
            candidates[COL_ONTIME] = ontime

        # Assign shadow row for this vehicle (create if new)
        if vno not in shd_row_of:
            shd_row_of[vno] = next_shd_row
            next_shd_row   += 1
        shd_row = shd_row_of[vno]

        wrote_any = False
        for col_idx, new_val in candidates.items():
            if not _should_write(col_idx, live_row, shadow_row):
                edits_kept += 1
                continue
            tracking_updates.append({
                "range":  f"{COL_LETTERS[col_idx]}{trk_row}",
                "values": [[new_val]],
            })
            shadow_updates.append({
                # Shadow also needs Vehicle_No in its row so FMS can look it up
                "range":  f"{COL_LETTERS[col_idx]}{shd_row}",
                "values": [[new_val]],
            })
            wrote_any = True

        if wrote_any:
            # Always anchor Vehicle_No in shadow row so FMS can find it by VNO
            shadow_updates.append({
                "range":  f"{COL_LETTERS[COL_VEH_NO]}{shd_row}",
                "values": [[vno]],
            })

        # Colors (always applied to match current computed values)
        color_requests += [
            _cell_color_req(ws.id, trk_row, COL_STAGE,  s_col),
            _cell_color_req(ws.id, trk_row, COL_STATUS, st_col),
            _cell_color_req(ws.id, trk_row, COL_ONTIME, ot_col),
        ]
        updated.append(vno)

    # ── Flush existing-row updates ─────────────────────────────────────────────
    if tracking_updates:
        ws.batch_update(tracking_updates, value_input_option="USER_ENTERED")
    if shadow_updates:
        if shadow_ws.row_count < next_shd_row:
            shadow_ws.resize(rows=next_shd_row + 50)
        shadow_ws.batch_update(shadow_updates, value_input_option="USER_ENTERED")
    if color_requests:
        ss.batch_update({"requests": color_requests})

    msg = f"  [OK] {label}: updated {len(updated)}"
    if edits_kept:
        msg += f" ({edits_kept} user edit(s) preserved)"
    if updated:
        print(msg + f" - {', '.join(sorted(updated))}")

    # ── Add new rows ───────────────────────────────────────────────────────────
    if not to_add:
        return

    next_sno       = _next_sno(all_rows)
    new_trk_rows:  list = []
    new_shd_rows:  list = []
    new_vno_order: list = []

    for vno, vdata in sorted(to_add.items()):
        vtab   = detail_map.get(vno, [])
        vtype  = vtab[1].strip() if len(vtab) > 1 else ""
        vroute = vtab[3].strip() if len(vtab) > 3 else ""

        stage, s_col, status, st_col, ontime, ot_col = derive_stage_status_ontime(
            vdata["raw_status"], vdata["location"],
            has_rps=False, cur_ontime=""
        )
        ontime = ontime or "Not On Trip"

        trk_row = [""] * TOTAL_COLS
        trk_row[COL_SNO]      = str(next_sno)
        trk_row[COL_VEH_RT]   = vroute
        trk_row[COL_VEH_NO]   = vno
        trk_row[COL_VEH_TYPE] = vtype
        trk_row[COL_STATUS]   = status
        trk_row[COL_STAGE]    = stage
        trk_row[COL_ONTIME]   = ontime
        trk_row[COL_LOCATION] = vdata["location"]
        trk_row[COL_STOP_DUR] = vdata["idle_time"]
        trk_row[COL_LAST_GPS] = vdata["last_gps"]
        trk_row[COL_LAST_REF] = now_str

        # Shadow row = same values (script baseline)
        shd_row_data = [""] * TOTAL_COLS
        for col in (COL_VEH_NO, COL_STATUS, COL_STAGE, COL_ONTIME,
                    COL_LOCATION, COL_STOP_DUR, COL_LAST_GPS, COL_LAST_REF):
            shd_row_data[col] = trk_row[col]

        new_trk_rows.append(trk_row)
        new_shd_rows.append(shd_row_data)
        new_vno_order.append((vno, s_col, st_col, ot_col))
        next_sno += 1

    # Append tracking rows
    ws.append_rows(new_trk_rows, value_input_option="USER_ENTERED")

    # Append shadow rows
    if shadow_ws.row_count < next_shd_row + len(new_shd_rows):
        shadow_ws.resize(rows=next_shd_row + len(new_shd_rows) + 50)
    shadow_ws.append_rows(new_shd_rows, value_input_option="USER_ENTERED")

    # Apply colors to newly added rows
    fresh_all  = ws.get_all_values()
    fresh_map  = {
        row[COL_VEH_NO].strip().upper(): i
        for i, row in enumerate(fresh_all[DATA_START - 1:], start=DATA_START)
        if len(row) > COL_VEH_NO
    }
    new_colors: list[dict] = []
    for vno, s_col, st_col, ot_col in new_vno_order:
        new_row = fresh_map.get(vno)
        if new_row:
            new_colors += [
                _cell_color_req(ws.id, new_row, COL_STAGE,  s_col),
                _cell_color_req(ws.id, new_row, COL_STATUS, st_col),
                _cell_color_req(ws.id, new_row, COL_ONTIME, ot_col),
            ]
    if new_colors:
        ss.batch_update({"requests": new_colors})

    print(f"  [OK] {label}: added {len(to_add)} new row(s) "
          f"- {', '.join(sorted(to_add))}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    print(f"\n{'='*62}")
    print(f"  CTYF / GTrophy  ->  Google Sheets   [{now_str}]")
    print(f"{'='*62}")

    print("\n[1] Fetching CTYF vehicle data...")
    try:
        ctyf_data = fetch_ctyf_vehicles()
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return
    if not ctyf_data:
        print("  No vehicles returned.")
        return

    print("[2] Connecting to Google Sheets...")
    gc        = _gspread_client()
    master_ss = gc.open_by_key(MASTER_SHEET_ID)
    hub_map, detail_map = load_vehicles_tab(master_ss)

    print("\n[3] Master sheet (all vehicles)...")
    try:
        master_ws = master_ss.worksheet(TRACKING_TAB)
        process_worksheet(master_ss, master_ws, ctyf_data, detail_map, now_str, "Master")
    except Exception as exc:
        print(f"  ERROR: {exc}")

    print("\n[4] Hub sheets...")
    for hub_name, hub_sheet_id in HUB_TRACKING_SHEETS.items():
        hub_vehicles = {
            vno: vdata for vno, vdata in ctyf_data.items()
            if hub_map.get(vno, "").strip().lower() == hub_name.lower()
        }
        if not hub_vehicles:
            print(f"  [Skip] {hub_name}: no CTYF vehicles assigned here")
            continue
        try:
            hub_ss = gc.open_by_key(hub_sheet_id)
            hub_ws = hub_ss.worksheet(TRACKING_TAB)
            process_worksheet(hub_ss, hub_ws, hub_vehicles, detail_map, now_str, hub_name)
        except Exception as exc:
            print(f"  ERROR ({hub_name}): {exc}")

    print(f"\n[OK] CTYF update complete  |  {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()
