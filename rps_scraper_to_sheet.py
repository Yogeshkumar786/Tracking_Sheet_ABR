"""
RPS Scraper -> Per-Hub Monthly MIS sheets  (FULLY INDEPENDENT)

For a given date range, pulls all RPS trips from the FMS RPS Report API
and writes them to each hub's monthly MIS tab (e.g. May_2026_MIS).

Dedup rule (the whole point of this rewrite):
    If RPS_Number already exists anywhere in the destination workbook's
    *_MIS tabs, the row is SKIPPED. Existing rows are never overwritten.
    Re-running the script is safe and produces zero duplicates.

This script has ZERO imports from fms_to_sheet.py. Everything it needs
(credentials, API call, vehicle→hub mapping, sheet writers) is inlined.

USAGE
-----
  python rps_scraper_to_sheet.py                       # last 10 days
  python rps_scraper_to_sheet.py --days 30
  python rps_scraper_to_sheet.py --from 2026-04-01 --to 2026-04-30
  python rps_scraper_to_sheet.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

# Master spreadsheet — used ONLY to read Vehicles, Route Codes, Route SLA tabs.
MASTER_SHEET_ID = "1-tEwE7YwZFNhfGjvgZPHYMKXJqSr_TOmrAodfuadGf0"
VEHICLES_TAB    = "Vehicles"
ROUTE_CODES_TAB = "Route Codes"
ROUTE_SLA_TAB   = "Route SLA"

# Per-hub destination spreadsheets — fill in your hub_name → sheet_id pairs.
# hub_name MUST match values in the master Vehicles tab "Vehicle Hub" column.
HUB_TRIP_SHEETS: dict[str, str] = {
    # "Ambala":       "PUT_AMBALA_MIS_SHEET_ID_HERE",
    "Ambala Local": "1SeJ06RjF2ONqCsP53_NO0FEjKhY3EV5UrNtLMmsA2N4",
    # Example single destination from earlier conversation:
    "Ambala": "1_unl3WrQZngLUdS1-jA95UZpkjoa1ZqZIIiu3G11DBo"
}

CREDS_FILE = Path(__file__).parent / "credentials.json"

# RPS Report API
RPS_REPORT_URL = (
    "http://smart.dsmsoft.com/FMSSmartApp/"
    "Safex_RPS_Reports/WebService.asmx/getRpsReportData"
)
RPS_REPORT_HEADERS = {
    "Accept":           "*/*",
    "Content-Type":     "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           "http://smart.dsmsoft.com",
    "Referer":          ("http://smart.dsmsoft.com/FMSSmartApp/"
                         "Safex_RPS_Reports/RPS_Reports.aspx?usergroup=NRM.101"),
    "User-Agent":       ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/148.0.0.0 Safari/537.36"),
}
RPS_REQUEST_TIMEOUT = 60   # seconds
RPS_BATCH_SIZE      = 50   # vehicles per API call (server-side limit varies)

# Destination tab schema — kept in sync with the Apps Script the user defined.
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

HEADER_COLOR = {"red": 0.043, "green": 0.329, "blue": 0.580}  # #0b5394
WHITE        = {"red": 1.0,   "green": 1.0,   "blue": 1.0}

MONTHS = ["January","February","March","April","May","June",
         "July","August","September","October","November","December"]

# Field-name fallbacks for the RPS Report API (names vary across versions).
F_RPS     = ("RPS_Number", "rpsNumber", "rps_number", "rpsNo", "RPS_No",
             "lrNumber", "tripId")
F_VEH     = ("Vehicle_Number", "vehicleNumber", "vehicle_no", "vehicleNo",
             "VehicleNo", "VEHICLE_NUMBER")
F_DRIVER_NAME = ("Driver_Name", "driverName", "DRIVER_NAME", "driver_name")
F_DRIVER_CODE = ("Driver_Code", "driverCode", "DRIVER_CODE", "driver_code",
                 "driverId")
F_VTYPE   = ("Vehicle_Size", "vehicleType", "VEHICLE_TYPE", "vehicleSize",
             "VEHICLE_SIZE")
F_ROUTE   = ("Route", "routeName", "ROUTE_NAME", "route_name", "ROUTE")
F_RCODE   = ("Route_Code", "routeCode", "ROUTE_CODE", "route_code")
F_START   = ("Start_Time", "dispatchDate", "DISPATCH_DATE", "dispatch_date",
             "startDate", "START_TIME", "tripStartDate")
F_END     = ("End_Time", "POD_DATE", "pod_date", "closureDate", "endDate",
             "END_TIME", "DELIVERY_DATE", "podDate")


# ── Utilities ─────────────────────────────────────────────────────────────────

def fmt(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.upper() in ("NA", "NULL", "NONE") else s


def first(rec: dict, keys: tuple) -> str:
    for k in keys:
        if k in rec:
            v = fmt(rec[k])
            if v:
                return v
    return ""


def parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for f in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
              "%d-%m-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
              "%d/%m/%Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S",
              "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def parse_cli_dt(s: str, name: str) -> datetime:
    dt = parse_dt(s)
    if not dt:
        raise argparse.ArgumentTypeError(
            f"--{name}: '{s}' is not YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'")
    return dt


def tab_name_for(start_dt: datetime) -> str:
    return f"{MONTHS[start_dt.month - 1]}_{start_dt.year}_MIS"


# ── gspread bootstrap ─────────────────────────────────────────────────────────

def gspread_client():
    creds = Credentials.from_service_account_file(
        str(CREDS_FILE),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)


def load_vehicles_tab(ws) -> tuple[dict, dict]:
    """Master Vehicles tab → ({vno: hub}, {vno: vehicle_type})."""
    rows = ws.get_all_values()
    hub_map: dict[str, str] = {}
    vt_map:  dict[str, str] = {}
    for r in rows[1:]:
        vno = (r[0].strip() if len(r) > 0 else "").upper()
        if not vno:
            continue
        if len(r) > 1 and r[1].strip():
            vt_map[vno] = r[1].strip()
        if len(r) > 2 and r[2].strip():
            hub_map[vno] = r[2].strip()
    return hub_map, vt_map


def load_route_sla(ws) -> dict[str, float]:
    rows = ws.get_all_values()
    sla: dict[str, float] = {}
    for r in rows[1:]:
        code = (r[0].strip() if len(r) > 0 else "").upper()
        hrs  = (r[1].strip() if len(r) > 1 else "")
        if not code or not hrs:
            continue
        try:
            sla[code] = float(hrs)
        except ValueError:
            pass
    return sla


def load_route_codes(ws) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Return ({HUB_NAME_UPPER: code}, [(hub_name, code), …]).

    The dict is used for exact lookups. The list keeps the original (cased)
    names so the fuzzy matcher in derive_route_code() can score against them.
    """
    rows = ws.get_all_values()
    out: dict[str, str] = {}
    items: list[tuple[str, str]] = []
    for r in rows[1:]:
        name = (r[0].strip() if len(r) > 0 else "")
        code = (r[1].strip() if len(r) > 1 else "")
        if name and code:
            out[name.upper()] = code
            items.append((name, code))
    return out, items


# ── Route_Code derivation ─────────────────────────────────────────────────────
# Produces the SAME output format as fms_to_sheet.build_route_code():
#   "ORIGIN-VIA1;VIA2;…;FINAL"      e.g.  "BLO11-BBO11;BNA11"
# The RPS API returns the route as a verbose "/"-separated path, e.g.:
#   "BENGALURU OUTBOUND-11 / SAFEXPRESS GURUGRAM SDS (BBO11) / SAFEXPRESS BINOLA (BNA11)"
# so we resolve a code for each segment and re-assemble.

_HUB_CODE_RE = re.compile(r"\(([A-Z0-9]+)\)\s*$", re.IGNORECASE)
_EMBEDDED_CODE_RE = re.compile(r"\b([A-Z]{2,5}\d{2,3})\b")

_ROUTE_STOPWORDS = {
    "SAFEXPRESS", "HUB", "OUTBOUND", "INBOUND", "THE", "AND", "OF",
    "ROADLINES", "PRIVATE", "LIMITED", "SDS",
}


def _extract_segment_code(segment: str, hub_map: dict[str, str]) -> str:
    text = str(segment or "").strip()
    if not text:
        return ""
    # "(CODE)" anywhere — trailing parens preferred, but any parens accepted
    m = _HUB_CODE_RE.search(text)
    if m:
        return m.group(1).upper()
    m2 = re.search(r"\(([^()]+)\)", text)
    if m2:
        return m2.group(1).strip().upper()
    # Bare code embedded in name (e.g. "INDORE-11" → none, "BLR11 hub" → BLR11)
    m3 = _EMBEDDED_CODE_RE.search(text.upper())
    if m3:
        return m3.group(1).upper()
    # Fall back to Route Codes tab lookup
    key  = text.upper()
    norm = re.sub(r"\([^)]*\)", "", text).strip().upper()
    return str(hub_map.get(key) or hub_map.get(norm) or "").strip().upper()


def _route_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"\([^)]*\)", " ", str(text or "").upper())
    cleaned = re.sub(r"[^A-Z0-9 ]", " ", cleaned)
    return {p for p in cleaned.split()
            if len(p) >= 3 and p not in _ROUTE_STOPWORDS}


def _best_code_guess(segment: str,
                     route_code_items: list[tuple[str, str]]) -> str:
    """Fuzzy match a segment against (hub_name, hub_code) pairs by token overlap."""
    seg_tokens = _route_tokens(segment)
    if not seg_tokens:
        return ""
    best_code = ""
    best_score = 0.0
    for name, code in route_code_items:
        if not code:
            continue
        name_tokens = _route_tokens(name)
        if not name_tokens:
            continue
        inter = len(seg_tokens & name_tokens)
        union = len(seg_tokens | name_tokens)
        score = (inter / union) if union else 0.0
        if score > best_score:
            best_score = score
            best_code  = code.strip().upper()
    return best_code if best_score >= 0.35 else ""


def derive_route_code(route_name: str,
                      hub_map: dict[str, str],
                      route_code_items: list[tuple[str, str]]) -> str:
    """Split the RPS route on '/', resolve each segment, format ORIGIN-VIA;FINAL."""
    route = str(route_name or "").strip()
    if not route:
        return ""
    parts = [p.strip() for p in route.split("/") if p.strip()]
    if not parts:
        return ""

    codes: list[str] = []
    for part in parts:
        code = _extract_segment_code(part, hub_map)
        if not code:
            code = _best_code_guess(part, route_code_items)
        if code:
            codes.append(code)

    if not codes:
        return ""
    if len(codes) == 1:
        return codes[0]
    return f"{codes[0]}-{';'.join(codes[1:])}"


# ── Destination tab handling ──────────────────────────────────────────────────


def _delete_named_columns(ss, ws, names: set[str]):
    """Delete any column whose header (row 1) matches one of `names`.

    Catches the '_sort_key' column re-injected by the destination workbook's
    own Apps Script, even after `_strip_extra_columns` removed it once.
    """
    try:
        header = ws.row_values(1)
    except Exception:
        return
    targets = [i for i, h in enumerate(header)
               if (h or "").strip().lower() in {n.lower() for n in names}]
    if not targets:
        return
    # Delete right-to-left so earlier indices stay valid.
    reqs = [{"deleteDimension": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
            }} for i in sorted(targets, reverse=True)]
    try:
        ss.batch_update({"requests": reqs})
        print(f"  [strip] {ws.title}: deleted {len(targets)} foreign "
              f"column(s) by name {names}", flush=True)
    except Exception as exc:
        print(f"  [strip] {ws.title}: named-column delete failed ({exc})",
              flush=True)


def _strip_extra_columns(ss, ws):
    """Delete any columns past column W (TRIP_NCOLS), regardless of header.

    Defensive: a foreign Apps Script bound to the destination spreadsheet
    has been observed appending a '_sort_key' helper column to newly
    created tabs. We don't want it, so we nuke anything past our schema
    every time we touch the tab.
    """
    # ws.col_count can be stale (gspread caches metadata). Refetch.
    try:
        meta = ss.fetch_sheet_metadata(
            params={"fields": "sheets(properties(sheetId,gridProperties))"})
        actual_cols = next(
            (s["properties"]["gridProperties"]["columnCount"]
             for s in meta.get("sheets", [])
             if s["properties"]["sheetId"] == ws.id),
            ws.col_count,
        )
    except Exception:
        actual_cols = ws.col_count

    if actual_cols <= TRIP_NCOLS:
        return
    try:
        ss.batch_update({"requests": [{"deleteDimension": {
            "range": {
                "sheetId":    ws.id,
                "dimension":  "COLUMNS",
                "startIndex": TRIP_NCOLS,        # 0-based; col 23 = X
                "endIndex":   actual_cols,
            },
        }}]})
    except Exception as exc:
        print(f"  [strip] {ws.title}: column trim skipped ({exc})", flush=True)


def get_or_create_trip_tab(ss, tab_name: str):
    try:
        ws = ss.worksheet(tab_name)
        if ws.row_values(1) != TRIP_HEADERS:
            ws.update(values=[TRIP_HEADERS], range_name="A1",
                      value_input_option="RAW")
        _delete_named_columns(ss, ws, {"_sort_key", "_sortKey", "sort_key"})
        _strip_extra_columns(ss, ws)
        return ws
    except gspread.WorksheetNotFound:
        pass

    ws = ss.add_worksheet(title=tab_name, rows=2000, cols=TRIP_NCOLS)
    ws.update(values=[TRIP_HEADERS], range_name="A1", value_input_option="RAW")
    ss.batch_update({"requests": [
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": TRIP_NCOLS},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HEADER_COLOR,
                "textFormat": {"foregroundColor": WHITE, "bold": True,
                               "fontSize": 10},
                "horizontalAlignment": "CENTER",
            }},
            "fields": ("userEnteredFormat("
                       "backgroundColor,textFormat,horizontalAlignment)"),
        }},
        # Freeze header
        {"updateSheetProperties": {
            "properties": {"sheetId": ws.id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        # Close_Status checkbox for all data rows up-front
        {"setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1,
                      "endRowIndex": 2000,
                      "startColumnIndex": TRIP_NCOLS - 1,
                      "endColumnIndex": TRIP_NCOLS},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
        }},
    ]})
    _strip_extra_columns(ss, ws)
    return ws


def _normalize_rps(v) -> str:
    """Canonicalize an RPS value so dedup ignores formatting differences.

    Google Sheets stores RPS_Number cells as numbers (USER_ENTERED parses
    "7965359" as 7965359). On read-back, FORMATTED_VALUE may come through
    as "7,965,359" or "7.96E+06" depending on cell format — none of which
    match the raw "7965359" string the API returned. So we strip every
    non-digit/letter char and trim, then compare.
    """
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    # Drop commas, spaces, decimal trailing zeros from "7965359.0"
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    s = s.replace(",", "").replace(" ", "")
    # API returns RPS with leading zeros ('0007944139'); sheet stores the
    # number 7944139 and loses them on read-back. Strip leading zeros so
    # both sides canonicalize to the same key.
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def existing_rps_in_workbook(ss) -> tuple[set[str], dict[str, tuple[str, int, bool, bool]]]:
    """Scan every *_MIS tab and return:
        all_rps  : {normalized_rps}
            — every RPS number already in this workbook (for dedup).
              No new row will be inserted for these.
        backfill : {normalized_rps: (tab_name, row, need_start, need_end)}
            — rows missing Start_Time and/or End_Time that should be
              updated on the next run.  need_start / need_end are True
              when the corresponding cell is blank.  Both fields are
              tracked and filled INDEPENDENTLY of each other.

    Uses UNFORMATTED_VALUE so number-formatted RPS cells (7,965,359 /
    scientific notation) round-trip correctly.
    """
    all_rps:  set[str] = set()
    backfill: dict[str, tuple[str, int, bool, bool]] = {}
    for ws in ss.worksheets():
        if not ws.title.endswith("_MIS"):
            continue
        rows_data: list = []   # (rps, start, end) tuples starting at sheet row 2
        try:
            resp = ss.values_get(
                f"'{ws.title}'!A2:J",
                params={"valueRenderOption": "UNFORMATTED_VALUE"},
            )
            for row in resp.get("values", []):
                rps_cell   = row[0] if len(row) > 0 else ""
                start_cell = row[8] if len(row) > 8 else ""   # col I
                end_cell   = row[9] if len(row) > 9 else ""   # col J
                rows_data.append((rps_cell, start_cell, end_cell))
        except Exception as exc:
            print(f"  [dedup] {ws.title}: values_get failed ({exc}), "
                  f"falling back to col_values", flush=True)
            try:
                a     = ws.col_values(1)[1:]
                i_col = ws.col_values(9)[1:]   # col I = Start_Time
                j     = ws.col_values(10)[1:]  # col J = End_Time
                rows_data = list(zip(
                    a,
                    i_col + [""] * max(0, len(a) - len(i_col)),
                    j     + [""] * max(0, len(a) - len(j)),
                ))
            except Exception as exc2:
                print(f"  [dedup] {ws.title}: col_values failed ({exc2})",
                      flush=True)
                continue

        loaded = 0
        backfill_candidates = 0
        for i, (rps_cell, start_cell, end_cell) in enumerate(rows_data, start=2):
            key = _normalize_rps(rps_cell)
            if not key:
                continue
            loaded += 1
            end_str   = str(end_cell   or "").strip() if end_cell   is not None else ""
            start_str = str(start_cell or "").strip() if start_cell is not None else ""
            all_rps.add(key)
            need_start = not bool(start_str)
            need_end   = not bool(end_str)
            if need_start or need_end:
                if key not in backfill:
                    backfill[key] = (ws.title, i, need_start, need_end)
                    backfill_candidates += 1
            else:
                # Row is fully complete — remove stale backfill entry if any.
                backfill.pop(key, None)
        bf_start = sum(1 for _, _, ns, _ in backfill.values() if ns)
        bf_end   = sum(1 for _, _, _, ne in backfill.values() if ne)
        print(f"  [dedup] {ws.title}: {loaded} RPS row(s); "
              f"{backfill_candidates} needing backfill", flush=True)
    print(f"  [dedup] Workbook totals: {len(all_rps)} known RPS, "
          f"{len(backfill)} awaiting backfill "
          f"(start={sum(1 for _,_,ns,_ in backfill.values() if ns)}, "
          f"end={sum(1 for _,_,_,ne in backfill.values() if ne)})", flush=True)
    return all_rps, backfill


def build_row(rec: dict, vt_map: dict, sla_map: dict, rc_map: dict,
              rc_items: list[tuple[str, str]],
              row_num: int) -> list:
    """Build one destination row. row_num is 1-based for the formulas."""
    rps    = first(rec, F_RPS)
    vno    = first(rec, F_VEH).upper()
    vtype  = first(rec, F_VTYPE) or vt_map.get(vno, "")
    driver = first(rec, F_DRIVER_NAME)
    dcode  = first(rec, F_DRIVER_CODE)
    route  = first(rec, F_ROUTE)

    # Route_Code — ALWAYS derive from the Route field so the output matches
    # fms_to_sheet.build_route_code()'s "ORIGIN-VIA;FINAL" format. The
    # API's own Route_Code (when present) is verbose like "DR_…" and not
    # what the live tracker uses.
    rcode = derive_route_code(route, rc_map, rc_items)
    if not rcode:
        # Last-ditch: take whatever the API gave us.
        rcode = first(rec, F_RCODE)

    # Route_TAT as a day-fraction so [h]:mm formatting + arithmetic work.
    tat_hours = sla_map.get(rcode.upper()) if rcode else None
    tat_value = (tat_hours / 24.0) if tat_hours else ""

    start_raw = first(rec, F_START)
    end_raw   = first(rec, F_END)
    start_dt  = parse_dt(start_raw)
    end_dt    = parse_dt(end_raw)
    start_cell = start_dt.strftime("%d/%m/%Y %H:%M:%S") if start_dt else start_raw
    end_cell   = end_dt.strftime("%d/%m/%Y %H:%M:%S") if end_dt else end_raw

    r = row_num
    return [
        rps,                                          # A  RPS_Number
        vno,                                          # B  Vehicle_Number
        vtype,                                        # C  Vehicle_Size
        driver,                                       # D  Driver_Name
        dcode,                                        # E  Driver_Code
        route,                                        # F  Route
        rcode,                                        # G  Route_Code
        tat_value,                                    # H  Route_TAT
        start_cell,                                   # I  Start_Time
        end_cell,                                     # J  End_Time
        # K  Transit_Time — matches the Apps Script formula (works on
        # dd/MM/yyyy HH:mm:ss whether stored as datetime or text)
        (
            f'=IFERROR('
            f'(DATE(VALUE(RIGHT(TEXT(J{r},"dd/MM/yyyy HH:mm:ss"),4)),'
            f'VALUE(MID(TEXT(J{r},"dd/MM/yyyy HH:mm:ss"),4,2)),'
            f'VALUE(LEFT(TEXT(J{r},"dd/MM/yyyy HH:mm:ss"),2)))'
            f'+TIMEVALUE(RIGHT(TEXT(J{r},"dd/MM/yyyy HH:mm:ss"),8)))'
            f'-(DATE(VALUE(RIGHT(TEXT(I{r},"dd/MM/yyyy HH:mm:ss"),4)),'
            f'VALUE(MID(TEXT(I{r},"dd/MM/yyyy HH:mm:ss"),4,2)),'
            f'VALUE(LEFT(TEXT(I{r},"dd/MM/yyyy HH:mm:ss"),2)))'
            f'+TIMEVALUE(RIGHT(TEXT(I{r},"dd/MM/yyyy HH:mm:ss"),8))),"")'
        ),
        "",                                           # L  Extra_Touching_Time
        f"=IF(L{r}=\"\",K{r},K{r}-L{r})",             # M  Actual_Transit_Time
        f"=IF(M{r}>H{r},M{r}-H{r},0)",                # N  Delay_Hours
        f'=IF(N{r}=0,"On Time","Delayed")',           # O  Status
        "", "", "", "", "", "", "",                   # P–V  manual money/etc
        False,                                        # W  Close_Status checkbox
    ]


def format_new_rows(ss, ws, start_row: int, count: int):
    """Apply date format to I/J, [h]:mm to H/K/M/N, and the BOOLEAN/checkbox
    data-validation rule to Close_Status (W) for the just-written rows.

    The checkbox rule MUST be re-applied on every write — the tab-creation
    rule covers rows 1–2000 only and is absent on legacy tabs, so the
    raw `False` value would otherwise render as plain text instead of an
    unchecked checkbox.
    """
    reqs = []
    # Date format on Start_Time (I=8 zero-based) and End_Time (J=9)
    for ci in (8, 9):
        reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id,
                      "startRowIndex": start_row - 1,
                      "endRowIndex":   start_row - 1 + count,
                      "startColumnIndex": ci, "endColumnIndex": ci + 1},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "DATE_TIME",
                                 "pattern": "dd/MM/yyyy HH:mm:ss"},
            }},
            "fields": "userEnteredFormat.numberFormat",
        }})
    # [h]:mm on Route_TAT(H=7), Transit_Time(K=10), Actual_Transit_Time(M=12),
    # Delay_Hours(N=13)
    for ci in (7, 10, 12, 13):
        reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id,
                      "startRowIndex": start_row - 1,
                      "endRowIndex":   start_row - 1 + count,
                      "startColumnIndex": ci, "endColumnIndex": ci + 1},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "TIME", "pattern": "[h]:mm"},
            }},
            "fields": "userEnteredFormat.numberFormat",
        }})
    # Checkbox validation on Close_Status (W = last column)
    reqs.append({"setDataValidation": {
        "range": {"sheetId": ws.id,
                  "startRowIndex": start_row - 1,
                  "endRowIndex":   start_row - 1 + count,
                  "startColumnIndex": TRIP_NCOLS - 1,
                  "endColumnIndex":   TRIP_NCOLS},
        "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
    }})
    if reqs:
        ss.batch_update({"requests": reqs})


# ── RPS API ───────────────────────────────────────────────────────────────────

def _parse_rps_response(body) -> list[dict]:
    """Unwrap the {"d": "count*id*[…]"} envelope."""
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    val = body.get("d")
    if val is None:
        return []
    if isinstance(val, list):
        return val
    s = str(val).strip()
    if s and s[0].isdigit():
        bracket = s.find("[")
        brace   = s.find("{")
        start = bracket if (bracket != -1 and (brace == -1 or bracket < brace)) \
                       else brace
        if start != -1:
            s = s[start:]
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def fetch_rps_trips(vehicles: list[str],
                    from_dt: datetime,
                    to_dt:   datetime) -> list[dict]:
    """Call the RPS Report API in batches and concatenate all records."""
    out: list[dict] = []
    payload_base = {
        "from_time": from_dt.strftime("%Y-%m-%d 00:00:00"),
        "to_time":   to_dt.strftime("%Y-%m-%d 23:59:59"),
    }
    batches = [vehicles[i:i + RPS_BATCH_SIZE]
               for i in range(0, len(vehicles), RPS_BATCH_SIZE)] or [[]]
    for i, batch in enumerate(batches, 1):
        payload = {**payload_base, "vehicleno": batch}
        try:
            r = requests.post(RPS_REPORT_URL, headers=RPS_REPORT_HEADERS,
                              json=payload, timeout=RPS_REQUEST_TIMEOUT,
                              verify=False)
            r.raise_for_status()
            recs = _parse_rps_response(r.json())
            print(f"  [API] batch {i}/{len(batches)}  "
                  f"({len(batch)} vehicles)  →  {len(recs)} records",
                  flush=True)
            out.extend(recs)
        except Exception as exc:
            print(f"  [API] batch {i} FAILED: {exc}", flush=True)
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Backfill RPS trips into per-hub monthly MIS sheets.")
    ap.add_argument("--days", type=int, default=10,
                    help="Days back from now (default 10). Ignored with --from/--to.")
    ap.add_argument("--from", dest="from_dt",
                    type=lambda s: parse_cli_dt(s, "from"), default=None)
    ap.add_argument("--to",   dest="to_dt",
                    type=lambda s: parse_cli_dt(s, "to"),   default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + plan, but do not write to sheets.")
    args = ap.parse_args()

    if args.from_dt and args.to_dt:
        if args.from_dt >= args.to_dt:
            ap.error("--from must be earlier than --to")
        from_dt, to_dt = args.from_dt, args.to_dt
        label = (f"{from_dt:%Y-%m-%d}  →  {to_dt:%Y-%m-%d}")
    elif args.from_dt or args.to_dt:
        ap.error("--from and --to must be used together (or use --days)")
    else:
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=args.days)
        label   = f"last {args.days} day(s)"

    print(f"[rps_scraper] window: {label}", flush=True)
    print(f"[rps_scraper] mode:   {'DRY RUN' if args.dry_run else 'WRITE'}",
          flush=True)

    if not HUB_TRIP_SHEETS:
        print("[rps_scraper] HUB_TRIP_SHEETS is empty — fill in hub→sheet_id "
              "pairs at the top of this script.", flush=True)
        sys.exit(1)

    # ── Load master lookups ───────────────────────────────────────────────────
    print("\n[rps_scraper] Loading master sheet lookups…", flush=True)
    client = gspread_client()
    master = client.open_by_key(MASTER_SHEET_ID)
    vehicle_hub, vt_map = load_vehicles_tab(master.worksheet(VEHICLES_TAB))
    sla_map = load_route_sla(master.worksheet(ROUTE_SLA_TAB))
    rc_map, rc_items = load_route_codes(master.worksheet(ROUTE_CODES_TAB))
    print(f"  Vehicles:    {len(vehicle_hub)} vehicle→hub mappings", flush=True)
    print(f"  Route SLA:   {len(sla_map)} routes with TAT hours",   flush=True)
    print(f"  Route Codes: {len(rc_map)} hub-name→code mappings",   flush=True)

    # Only ask the RPS API about vehicles whose hub is in HUB_TRIP_SHEETS.
    target_vehicles = sorted(vno for vno, hub in vehicle_hub.items()
                             if hub in HUB_TRIP_SHEETS)
    if not target_vehicles:
        print("[rps_scraper] No vehicles match any hub in HUB_TRIP_SHEETS — "
              "check the master Vehicles tab.", flush=True)
        sys.exit(1)
    print(f"  Targeting:   {len(target_vehicles)} vehicle(s) across "
          f"{len(HUB_TRIP_SHEETS)} hub(s)", flush=True)

    # ── Fetch RPS records ─────────────────────────────────────────────────────
    print("\n[rps_scraper] Calling RPS Report API…", flush=True)
    records = fetch_rps_trips(target_vehicles, from_dt, to_dt)
    print(f"[rps_scraper] {len(records)} total records returned", flush=True)
    if not records:
        return

    # ── Open one hub workbook at a time, dedup, append ───────────────────────
    total_added       = 0
    total_skipped     = 0
    total_unrouted    = 0
    total_endfilled   = 0

    # Cache per workbook: (spreadsheet, all_rps_set, backfill_map).
    workbook_cache: dict[str, tuple[gspread.Spreadsheet,
                                    set[str],
                                    dict[str, tuple[str, int, bool, bool]]]] = {}

    # Group records by destination workbook + tab.
    # Key: (hub_name, tab_name)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        vno = first(rec, F_VEH).upper()
        if not vno:
            continue
        hub = vehicle_hub.get(vno)
        if not hub or hub not in HUB_TRIP_SHEETS:
            total_unrouted += 1
            continue
        start_dt = parse_dt(first(rec, F_START))
        if not start_dt:
            print(f"  [skip] {vno} RPS {first(rec, F_RPS) or '?'} — "
                  f"unparseable Start_Time", flush=True)
            continue
        grouped.setdefault((hub, tab_name_for(start_dt)), []).append(rec)

    if total_unrouted:
        print(f"[rps_scraper] {total_unrouted} record(s) skipped — vehicle "
              f"not mapped to any configured hub", flush=True)

    for (hub, tab_name), recs in sorted(grouped.items()):
        sheet_id = HUB_TRIP_SHEETS[hub]
        if sheet_id not in workbook_cache:
            ss = client.open_by_key(sheet_id)
            all_rps, backfill = existing_rps_in_workbook(ss)
            workbook_cache[sheet_id] = (ss, all_rps, backfill)
        ss, existing, backfill = workbook_cache[sheet_id]

        # Classify each API record:
        #   - not in existing  → new RPS, append as a full row
        #   - in existing + in backfill → queue Start_Time/End_Time updates
        #                                  independently (each field is filled
        #                                  only when it is blank AND the API
        #                                  has a value for it)
        #   - in existing, nothing missing → skip
        fresh: list[dict] = []
        end_updates: list[dict] = []   # [{tab, row, end_value, start_value}]
        for rec in recs:
            rps = first(rec, F_RPS)
            if not rps:
                continue
            key = _normalize_rps(rps)
            if key in existing:
                # Row already exists — check independent fields.
                if key in backfill:
                    tab_title, row_num, need_start, need_end = backfill[key]
                    end_val   = None
                    start_val = None
                    if need_end:
                        end_raw = first(rec, F_END)
                        end_dt  = parse_dt(end_raw)
                        if end_dt:
                            end_val = end_dt.strftime("%d/%m/%Y %H:%M:%S")
                    if need_start:
                        start_raw = first(rec, F_START)
                        start_dt  = parse_dt(start_raw)
                        if start_dt:
                            start_val = start_dt.strftime("%d/%m/%Y %H:%M:%S")
                    if end_val or start_val:
                        end_updates.append({
                            "tab":         tab_title,
                            "row":         row_num,
                            "end_value":   end_val,    # None → don't touch col J
                            "start_value": start_val,  # None → don't touch col I
                        })
                        backfill.pop(key, None)  # won't touch again this run
                    else:
                        total_skipped += 1  # API still has no data for missing fields
                else:
                    total_skipped += 1
                continue
            existing.add(key)            # de-dupe within this batch too
            fresh.append(rec)

        # Apply Start_Time / End_Time back-fills independently.
        # Grouped by tab so each sheet gets one batch_update call.
        if end_updates and not args.dry_run:
            by_tab: dict[str, list[dict]] = {}
            for u in end_updates:
                by_tab.setdefault(u["tab"], []).append(u)
            for tab_title, ups in by_tab.items():
                try:
                    target_ws = ss.worksheet(tab_title)
                except gspread.WorksheetNotFound:
                    continue
                # Write only the cells that are actually missing.
                batch: list[dict] = []
                for u in ups:
                    if u.get("end_value"):
                        batch.append({"range": f"J{u['row']}",
                                      "values": [[u["end_value"]]]})
                    if u.get("start_value"):
                        batch.append({"range": f"I{u['row']}",
                                      "values": [[u["start_value"]]]})
                if batch:
                    target_ws.batch_update(batch,
                                           value_input_option="USER_ENTERED")
                # Re-apply dd/MM/yyyy HH:mm:ss format on every written cell.
                fmt_reqs: list[dict] = []
                for u in ups:
                    if u.get("end_value"):
                        fmt_reqs.append({"repeatCell": {
                            "range": {"sheetId": target_ws.id,
                                      "startRowIndex": u["row"] - 1,
                                      "endRowIndex":   u["row"],
                                      "startColumnIndex": 9, "endColumnIndex": 10},
                            "cell": {"userEnteredFormat": {
                                "numberFormat": {"type": "DATE_TIME",
                                                 "pattern": "dd/MM/yyyy HH:mm:ss"},
                            }},
                            "fields": "userEnteredFormat.numberFormat",
                        }})
                    if u.get("start_value"):
                        fmt_reqs.append({"repeatCell": {
                            "range": {"sheetId": target_ws.id,
                                      "startRowIndex": u["row"] - 1,
                                      "endRowIndex":   u["row"],
                                      "startColumnIndex": 8, "endColumnIndex": 9},
                            "cell": {"userEnteredFormat": {
                                "numberFormat": {"type": "DATE_TIME",
                                                 "pattern": "dd/MM/yyyy HH:mm:ss"},
                            }},
                            "fields": "userEnteredFormat.numberFormat",
                        }})
                if fmt_reqs:
                    ss.batch_update({"requests": fmt_reqs})
                end_filled   = sum(1 for u in ups if u.get("end_value"))
                start_filled = sum(1 for u in ups if u.get("start_value"))
                parts = []
                if end_filled:
                    parts.append(f"End_Time on {end_filled} row(s)")
                if start_filled:
                    parts.append(f"Start_Time on {start_filled} row(s)")
                if parts:
                    print(f"  [{hub}/{tab_title}] back-filled "
                          + ", ".join(parts), flush=True)
            total_endfilled += len(end_updates)
        elif end_updates and args.dry_run:
            total_endfilled += len(end_updates)
            end_would   = sum(1 for u in end_updates if u.get("end_value"))
            start_would = sum(1 for u in end_updates if u.get("start_value"))
            parts = []
            if end_would:
                parts.append(f"End_Time on {end_would} row(s)")
            if start_would:
                parts.append(f"Start_Time on {start_would} row(s)")
            if parts:
                print(f"  [{hub}/{tab_name}] would back-fill "
                      + ", ".join(parts), flush=True)

        if not fresh:
            if not end_updates:
                print(f"  [{hub}/{tab_name}] {len(recs)} record(s) — all "
                      f"duplicates, nothing to write", flush=True)
            continue

        print(f"  [{hub}/{tab_name}] {len(fresh)} new / "
              f"{len(end_updates)} end-filled / "
              f"{len(recs) - len(fresh) - len(end_updates)} duplicate(s)",
              flush=True)

        if args.dry_run:
            total_added += len(fresh)
            continue

        ws = get_or_create_trip_tab(ss, tab_name)
        start_row = max(ws.row_count, len(ws.col_values(1))) + 1
        # Grow if needed
        needed = start_row + len(fresh) + 5
        if ws.row_count < needed:
            ws.resize(rows=needed)
        # Re-evaluate start_row from actual last filled row in col A
        start_row = len(ws.col_values(1)) + 1

        rows = [build_row(rec, vt_map, sla_map, rc_map, rc_items,
                          start_row + i)
                for i, rec in enumerate(fresh)]
        ws.update(values=rows, range_name=f"A{start_row}",
                  value_input_option="USER_ENTERED")
        format_new_rows(ss, ws, start_row, len(rows))
        _delete_named_columns(ss, ws, {"_sort_key", "_sortKey", "sort_key"})
        _strip_extra_columns(ss, ws)
        total_added += len(rows)

    # Final cleanup pass — the destination workbook's bound Apps Script may
    # re-add '_sort_key' AFTER we strip it during the write phase. Sweep all
    # touched workbooks/tabs once more before we exit.
    print("\n[rps_scraper] Final foreign-column sweep…", flush=True)
    for sheet_id, (ss, _, _) in workbook_cache.items():
        for ws in ss.worksheets():
            if not ws.title.endswith("_MIS"):
                continue
            _delete_named_columns(ss, ws,
                                  {"_sort_key", "_sortKey", "sort_key"})
            _strip_extra_columns(ss, ws)

    print(f"\n[rps_scraper] Done. Added: {total_added}  "
          f"End_Time back-filled: {total_endfilled}  "
          f"Skipped: {total_skipped}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[rps_scraper] FATAL ERROR: {exc}", flush=True)
        traceback.print_exc()
        sys.exit(1)
