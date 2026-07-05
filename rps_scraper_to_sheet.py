"""
RPS Scraper -> Per-Hub Monthly MIS sheets  (FULLY INDEPENDENT)

For a given date range, pulls all RPS trips from the FMS RPS Report API
and writes them to each hub's monthly MIS tab (e.g. May_2026_MIS).

Dedup rule:
    If RPS_Number already exists anywhere in the destination workbook's
    *_MIS tabs, the row is SKIPPED. Existing rows are never overwritten.
    Re-running the script is safe and produces zero duplicates.

Reliability rules (added after a 429 quota crash):
    * Every Sheets API call is wrapped in _retry(): on 429 / 5xx we back
      off exponentially (20s, 40s, 80s, 160s, 320s) up to 6 attempts.
    * If a workbook's dedup scan ULTIMATELY fails, that workbook is
      SKIPPED for writes — we never assume "no known RPS" because that
      would duplicate every record on the next pass.
    * Each new row's value + format + checkbox writes go in ONE
      atomic spreadsheets.batchUpdate request. If the call errors mid-
      transmission, Sheets rolls the request back; the row will either
      be fully present or fully absent (never half).

USAGE
-----
  python rps_scraper_to_sheet.py                       # last 10 days
  python rps_scraper_to_sheet.py --days 30
  python rps_scraper_to_sheet.py --month 2026-05       # full month (May 2026)
  python rps_scraper_to_sheet.py --from 2026-04-01 --to 2026-04-30
  python rps_scraper_to_sheet.py --dry-run
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import random
import re
import sys
import time
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

MASTER_SHEET_ID = "1-tEwE7YwZFNhfGjvgZPHYMKXJqSr_TOmrAodfuadGf0"
VEHICLES_TAB    = "Vehicles"
ROUTE_CODES_TAB = "Route Codes"
ROUTE_SLA_TAB   = "Route SLA"

HUB_TRIP_SHEETS: dict[str, str] = {
    "Ambala":       "1_unl3WrQZngLUdS1-jA95UZpkjoa1ZqZIIiu3G11DBo",
    "Ambala Local": "1SeJ06RjF2ONqCsP53_NO0FEjKhY3EV5UrNtLMmsA2N4",
    "Binola":       "1Jz5N01qzwJRStr5Vb9oIU_DeGshiEeL4kLkMiomIeA8",
    "Binola Local": "1f6IDh2ALPdH51XezH30flX2rZN5zFoVnf-TGsa1Eeuw",
    "G.Noida":      "1egmOZultBulPIzwzYbjgxYkfkoC22LmzepMlih8-arE",
}

_HUB_TRIP_SHEETS_NORM: dict[str, tuple[str, str]] = {
    k.strip().lower(): (k, v) for k, v in HUB_TRIP_SHEETS.items()
}

CREDS_FILE = Path(__file__).parent / "credentials.json"

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
RPS_REQUEST_TIMEOUT = 60
RPS_BATCH_SIZE      = 50

TRIP_HEADERS = [
    "RPS_Number", "Vehicle_Number", "Vehicle_Size",
    "Driver_Name", "Driver_Code",
    "Route", "Route_Code", "Route_TAT",
    "Start_Time", "End_Time",
    "Transit_Time", "Extra_Touching_Time", "Actual_Transit_Time",
    "Delay_Hours", "Late Reason", "Status",
    "Given_Advance", "Given_Diesel", "Diesel_Amount",
    "Given_Toll", "Given_Challan", "Extra_Diesel", "Extra_Diesel_Amount",
    "Maintainance",
    "Close_Status",
]
TRIP_NCOLS = len(TRIP_HEADERS)
EXTRA_DIESEL_AMOUNT_COL = TRIP_HEADERS.index("Extra_Diesel_Amount")

OLD_TRIP_HEADERS = [
    h for h in TRIP_HEADERS if h != "Extra_Diesel_Amount"
]

HEADER_COLOR = {"red": 0.043, "green": 0.329, "blue": 0.580}
WHITE        = {"red": 1.0,   "green": 1.0,   "blue": 1.0}

MONTHS = ["January","February","March","April","May","June",
         "July","August","September","October","November","December"]

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


# ── 429 / 5xx retry helper ────────────────────────────────────────────────────
# Sheets enforces 60 reads + 60 writes / minute / user.  A full Binola dedup
# sweep can blow past that on its own.  Every gspread call below is routed
# through _retry() so a quota hit triggers exponential backoff instead of a
# crash that would leave the run half-applied.

_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, gspread.exceptions.APIError):
        msg = str(exc)
        if any(f"[{s}]" in msg for s in _RETRY_STATUSES):
            return True
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        return status in _RETRY_STATUSES
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    return False


def _retry(fn, *args, _label: str = "", max_attempts: int = 6,
           base_delay: float = 20.0, **kwargs):
    """Run fn(*args, **kwargs); retry on 429/5xx with exponential backoff.

    Backoff sequence: 20s, 40s, 80s, 160s, 320s (with up to +3s jitter
    per step so parallel runners don't synchronize).  After max_attempts
    the original exception is re-raised so the caller can decide.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == max_attempts:
                if attempt == max_attempts:
                    print(f"  [retry]{(' ' + _label) if _label else ''} "
                          f"giving up after {max_attempts} attempts: {exc}",
                          flush=True)
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 3)
            tag = f" {_label}" if _label else ""
            print(f"  [retry]{tag} 429/5xx; sleeping {delay:.1f}s "
                  f"(attempt {attempt}/{max_attempts})", flush=True)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


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


def _try_parse(s: str, formats: tuple) -> datetime | None:
    for f in formats:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


# Slash-date ambiguity: the DSM Soft RPS API serializes dates in **MM/DD/YYYY**
# order, but `02/06/2026` is locally valid in either MM/DD or DD/MM, so the
# parser silently swapped day & month before.  We now try MM/DD-first AND keep
# a DD/MM-first variant for the rescue path in parse_dt_pair().
_FMTS_MMDD_FIRST = (
    "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M:%S %p",
    "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d",
)
_FMTS_DDMM_FIRST = (
    "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p",
    "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d",
)


def parse_dt(s: str) -> datetime | None:
    """Single-value parser. Tries MM/DD-first to match the API."""
    if not s:
        return None
    return _try_parse(str(s).strip(), _FMTS_MMDD_FIRST)


def parse_dt_pair(start_raw: str, end_raw: str
                  ) -> tuple[datetime | None, datetime | None]:
    """Parse Start / End together so the day/month interpretation can be
    cross-validated.  If MM/DD-first yields an impossible pair (End < Start,
    or > 60-day trip) we retry with DD/MM-first.  This rescues the rare row
    where DSM Soft flips locale on us.
    """
    s = str(start_raw or "").strip()
    e = str(end_raw   or "").strip()
    start_a = _try_parse(s, _FMTS_MMDD_FIRST) if s else None
    end_a   = _try_parse(e, _FMTS_MMDD_FIRST) if e else None

    def _plausible(a: datetime | None, b: datetime | None) -> bool:
        if a is None:
            return False
        if b is None:
            return True
        return b >= a and (b - a).days <= 60

    if _plausible(start_a, end_a):
        return start_a, end_a

    start_b = _try_parse(s, _FMTS_DDMM_FIRST) if s else None
    end_b   = _try_parse(e, _FMTS_DDMM_FIRST) if e else None
    if _plausible(start_b, end_b):
        print(f"  [date] DD/MM rescue: '{s}' / '{e}' → "
              f"{start_b} / {end_b}", flush=True)
        return start_b, end_b

    return start_a, end_a


_SHEETS_EPOCH = datetime(1899, 12, 30)


def to_sheets_serial(dt: datetime) -> float:
    """datetime → Google Sheets date serial number."""
    delta = dt - _SHEETS_EPOCH
    return delta.days + delta.seconds / 86400.0


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
    rows = _retry(ws.get_all_values, _label="Vehicles.get_all_values")
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
    rows = _retry(ws.get_all_values, _label="Route SLA.get_all_values")
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
    rows = _retry(ws.get_all_values, _label="Route Codes.get_all_values")
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
    m = _HUB_CODE_RE.search(text)
    if m:
        return m.group(1).upper()
    m2 = re.search(r"\(([^()]+)\)", text)
    if m2:
        return m2.group(1).strip().upper()
    m3 = _EMBEDDED_CODE_RE.search(text.upper())
    if m3:
        return m3.group(1).upper()
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
    try:
        header = _retry(ws.row_values, 1, _label=f"{ws.title}.row_values")
    except Exception:
        return
    targets = [i for i, h in enumerate(header)
               if (h or "").strip().lower() in {n.lower() for n in names}]
    if not targets:
        return
    reqs = [{"deleteDimension": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
            }} for i in sorted(targets, reverse=True)]
    try:
        _retry(ss.batch_update, {"requests": reqs},
               _label=f"{ws.title}.delete_named_columns")
        print(f"  [strip] {ws.title}: deleted {len(targets)} foreign "
              f"column(s) by name {names}", flush=True)
    except Exception as exc:
        print(f"  [strip] {ws.title}: named-column delete failed ({exc})",
              flush=True)


def _strip_extra_columns(ss, ws):
    try:
        meta = _retry(ss.fetch_sheet_metadata,
                      params={"fields": "sheets(properties(sheetId,gridProperties))"},
                      _label=f"{ss.id}.fetch_sheet_metadata")
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
        _retry(ss.batch_update, {"requests": [{"deleteDimension": {
            "range": {
                "sheetId":    ws.id,
                "dimension":  "COLUMNS",
                "startIndex": TRIP_NCOLS,
                "endIndex":   actual_cols,
            },
        }}]}, _label=f"{ws.title}.strip_extra_columns")
    except Exception as exc:
        print(f"  [strip] {ws.title}: column trim skipped ({exc})", flush=True)


def _ensure_trip_columns_and_header(ss, ws, tab_name: str) -> None:
    header = _retry(ws.row_values, 1, _label=f"{tab_name}.row_values")
    if header == TRIP_HEADERS:
        return

    if header == OLD_TRIP_HEADERS:
        _retry(ss.batch_update, {"requests": [{"insertDimension": {
            "range": {"sheetId": ws.id,
                      "dimension": "COLUMNS",
                      "startIndex": EXTRA_DIESEL_AMOUNT_COL,
                      "endIndex": EXTRA_DIESEL_AMOUNT_COL + 1},
            "inheritFromBefore": True,
        }}]}, _label=f"{tab_name}.insert_extra_diesel_amount")
        print(f"  [migrate] {tab_name}: inserted Extra_Diesel_Amount column",
              flush=True)
    elif ws.col_count < TRIP_NCOLS:
        _retry(ws.resize, cols=TRIP_NCOLS, _label=f"{tab_name}.resize_cols")

    _retry(ws.update, values=[TRIP_HEADERS], range_name="A1",
           value_input_option="RAW",
           _label=f"{tab_name}.update_header")


def _currency_fmt() -> dict:
    return {"type": "CURRENCY", "pattern": '"₹"#,##0.00'}


def _format_extra_diesel_amount_column(ss, ws, tab_name: str) -> None:
    _retry(ss.batch_update, {"requests": [{"repeatCell": {
        "range": {"sheetId": ws.id,
                  "startRowIndex": 1,
                  "startColumnIndex": EXTRA_DIESEL_AMOUNT_COL,
                  "endColumnIndex": EXTRA_DIESEL_AMOUNT_COL + 1},
        "cell": {"userEnteredFormat": {"numberFormat": _currency_fmt()}},
        "fields": "userEnteredFormat.numberFormat",
    }}]}, _label=f"{tab_name}.format_extra_diesel_amount")


def get_or_create_trip_tab(ss, tab_name: str):
    try:
        ws = ss.worksheet(tab_name)
        _ensure_trip_columns_and_header(ss, ws, tab_name)
        _format_extra_diesel_amount_column(ss, ws, tab_name)
        _delete_named_columns(ss, ws, {"_sort_key", "_sortKey", "sort_key"})
        _strip_extra_columns(ss, ws)
        return ws
    except gspread.WorksheetNotFound:
        pass

    ws = _retry(ss.add_worksheet, title=tab_name, rows=2000, cols=TRIP_NCOLS,
                _label=f"add_worksheet({tab_name})")
    _retry(ws.update, values=[TRIP_HEADERS], range_name="A1",
           value_input_option="RAW",
           _label=f"{tab_name}.write_header")
    _retry(ss.batch_update, {"requests": [
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
        {"updateSheetProperties": {
            "properties": {"sheetId": ws.id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        {"setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1,
                      "endRowIndex": 2000,
                      "startColumnIndex": TRIP_NCOLS - 1,
                      "endColumnIndex": TRIP_NCOLS},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
        }},
        {"repeatCell": {
            "range": {"sheetId": ws.id,
                      "startRowIndex": 1,
                      "startColumnIndex": EXTRA_DIESEL_AMOUNT_COL,
                      "endColumnIndex": EXTRA_DIESEL_AMOUNT_COL + 1},
            "cell": {"userEnteredFormat": {"numberFormat": _currency_fmt()}},
            "fields": "userEnteredFormat.numberFormat",
        }},
    ]}, _label=f"{tab_name}.init_format")
    _strip_extra_columns(ss, ws)
    return ws


def _normalize_rps(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    s = s.replace(",", "").replace(" ", "")
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s


def _rps_sheet_value(v):
    key = _normalize_rps(v)
    if key.isdigit():
        return int(key)
    return key


class DedupReadError(Exception):
    """Raised when we couldn't reliably read existing RPS rows. The caller
    MUST skip writes to that workbook — otherwise we'd duplicate everything.
    """


def existing_rps_in_workbook(ss) -> tuple[set[str], dict[str, tuple[str, int, bool, bool]]]:
    """Scan every *_MIS tab and collect existing RPS numbers + back-fill
    candidates.  Raises DedupReadError if any tab can't be read — we'd
    rather skip writes than duplicate.
    """
    all_rps:  set[str] = set()
    backfill: dict[str, tuple[str, int, bool, bool]] = {}
    worksheets = _retry(ss.worksheets, _label=f"{ss.id}.worksheets")
    for ws in worksheets:
        if not ws.title.endswith("_MIS"):
            continue
        # Tiny pause between tabs softens the read-quota pressure.
        time.sleep(1.0)

        rows_data: list = []
        try:
            resp = _retry(
                ss.values_get,
                f"'{ws.title}'!A2:J",
                params={"valueRenderOption": "UNFORMATTED_VALUE"},
                _label=f"{ws.title}.values_get",
            )
            for row in resp.get("values", []):
                rps_cell   = row[0] if len(row) > 0 else ""
                start_cell = row[8] if len(row) > 8 else ""
                end_cell   = row[9] if len(row) > 9 else ""
                rows_data.append((rps_cell, start_cell, end_cell))
        except Exception as exc:
            # After 6 retries with 5-minute total backoff, give up safely
            # rather than silently treat the tab as empty.
            raise DedupReadError(
                f"could not read {ws.title} after retries: {exc}") from exc

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
                backfill.pop(key, None)
        print(f"  [dedup] {ws.title}: {loaded} RPS row(s); "
              f"{backfill_candidates} needing backfill", flush=True)
    print(f"  [dedup] Workbook totals: {len(all_rps)} known RPS, "
          f"{len(backfill)} awaiting backfill "
          f"(start={sum(1 for _,_,ns,_ in backfill.values() if ns)}, "
          f"end={sum(1 for _,_,_,ne in backfill.values() if ne)})", flush=True)
    return all_rps, backfill


# ── Row builders ──────────────────────────────────────────────────────────────

def _duration_fmt() -> dict:
    """Sheets 'Duration' number-format object.

    Internally Duration is a NUMBER type with the [h]:mm:ss pattern — the
    brackets around `h` make hours cumulative beyond 24, which is what we
    want for Transit_Time / Actual_Transit_Time / Delay_Hours / Route_TAT.
    """
    return {"type": "NUMBER", "pattern": "[h]:mm:ss"}


def _datetime_fmt() -> dict:
    return {"type": "DATE_TIME", "pattern": "dd/MM/yyyy HH:mm:ss"}


def build_row_values(rec: dict, vt_map: dict, sla_map: dict, rc_map: dict,
                     rc_items: list[tuple[str, str]],
                     row_num: int) -> tuple[list, datetime | None, datetime | None]:
    """Return (values_for_row, start_dt, end_dt). row_num is 1-based."""
    rps    = _rps_sheet_value(first(rec, F_RPS))
    vno    = first(rec, F_VEH).upper()
    vtype  = first(rec, F_VTYPE) or vt_map.get(vno, "")
    driver = first(rec, F_DRIVER_NAME)
    dcode  = first(rec, F_DRIVER_CODE)
    route  = first(rec, F_ROUTE)

    rcode = derive_route_code(route, rc_map, rc_items)
    if not rcode:
        rcode = first(rec, F_RCODE)

    tat_hours = sla_map.get(rcode.upper()) if rcode else None
    tat_value = (tat_hours / 24.0) if tat_hours else ""

    start_raw = first(rec, F_START)
    end_raw   = first(rec, F_END)
    start_dt, end_dt = parse_dt_pair(start_raw, end_raw)
    start_cell = to_sheets_serial(start_dt) if start_dt else start_raw
    end_cell   = to_sheets_serial(end_dt)   if end_dt   else end_raw

    r = row_num
    return [
        rps, vno, vtype, driver, dcode, route, rcode,
        tat_value, start_cell, end_cell,
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
        "",
        f"=IF(L{r}=\"\",K{r},K{r}-L{r})",
        f"=IF(M{r}>H{r},M{r}-H{r},0)",
        "",  # Late Reason (free text)
        f'=IF(N{r}=0,"On Time","Delayed")',
        "", "", "", "", "", "", "", "",
        False,
    ], start_dt, end_dt


def _row_to_cell_data(row_values: list) -> list[dict]:
    """Convert a list of Python values into Sheets API CellData objects,
    embedding both the value AND the number format so a single
    updateCells request writes everything atomically.

    Column index → format:
        7  H Route_TAT             Duration
        8  I Start_Time            DateTime
        9  J End_Time              DateTime
        10 K Transit_Time          Duration  (formula)
        12 M Actual_Transit_Time   Duration  (formula)
        13 N Delay_Hours           Duration  (formula)
        14 O Late Reason           (free text)
        22 W Extra_Diesel_Amount   Currency
        24 Y Close_Status          checkbox  (handled via dataValidation)
    """
    duration_cols = {7, 10, 12, 13}
    datetime_cols = {8, 9}
    currency_cols = {EXTRA_DIESEL_AMOUNT_COL}
    out: list[dict] = []
    for ci, v in enumerate(row_values):
        cd: dict = {}
        # Value
        if isinstance(v, str) and v.startswith("="):
            cd["userEnteredValue"] = {"formulaValue": v}
        elif isinstance(v, bool):
            cd["userEnteredValue"] = {"boolValue": v}
        elif isinstance(v, (int, float)):
            cd["userEnteredValue"] = {"numberValue": float(v)}
        elif v == "" or v is None:
            cd["userEnteredValue"] = {"stringValue": ""}
        else:
            cd["userEnteredValue"] = {"stringValue": str(v)}
        # Format
        fmt_obj: dict | None = None
        if ci in datetime_cols:
            fmt_obj = _datetime_fmt()
        elif ci in duration_cols:
            fmt_obj = _duration_fmt()
        elif ci in currency_cols:
            fmt_obj = _currency_fmt()
        if fmt_obj is not None:
            cd["userEnteredFormat"] = {"numberFormat": fmt_obj}
        out.append(cd)
    return out


def write_rows_atomically(ss, ws, start_row: int, rows: list[list]) -> None:
    """Write `rows` starting at sheet row `start_row` as a SINGLE
    spreadsheets.batchUpdate request — values, number formats, AND
    checkbox data-validation all land together or not at all.

    No retry is allowed to write a partial state: the call is one network
    request, and Sheets applies it transactionally.  If _retry() ultimately
    raises, NOTHING was written for these rows.
    """
    if not rows:
        return
    grid_rows = [{"values": _row_to_cell_data(r)} for r in rows]
    requests = [
        {"updateCells": {
            "rows":   grid_rows,
            "fields": "userEnteredValue,userEnteredFormat.numberFormat",
            "start":  {"sheetId":     ws.id,
                       "rowIndex":    start_row - 1,
                       "columnIndex": 0},
        }},
        # Re-apply checkbox validation on the just-written Close_Status column.
        {"setDataValidation": {
            "range": {"sheetId":          ws.id,
                      "startRowIndex":    start_row - 1,
                      "endRowIndex":      start_row - 1 + len(rows),
                      "startColumnIndex": TRIP_NCOLS - 1,
                      "endColumnIndex":   TRIP_NCOLS},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
        }},
    ]
    _retry(ss.batch_update, {"requests": requests},
           _label=f"{ws.title}.atomic_write[{len(rows)}rows]")


# ── RPS API ───────────────────────────────────────────────────────────────────

def _parse_rps_response(body) -> list[dict]:
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
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--month", dest="month", default=None, metavar="YYYY-MM")
    ap.add_argument("--from", dest="from_dt",
                    type=lambda s: parse_cli_dt(s, "from"), default=None)
    ap.add_argument("--to",   dest="to_dt",
                    type=lambda s: parse_cli_dt(s, "to"),   default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.month:
        try:
            month_dt = datetime.strptime(args.month.strip(), "%Y-%m")
        except ValueError:
            ap.error(f"--month must be YYYY-MM (e.g. 2026-05), got: {args.month!r}")
        last_day = calendar.monthrange(month_dt.year, month_dt.month)[1]
        from_dt  = month_dt.replace(day=1)
        to_dt    = month_dt.replace(day=last_day)
        label    = f"{MONTHS[month_dt.month - 1]} {month_dt.year} (full month)"
    elif args.from_dt and args.to_dt:
        if args.from_dt >= args.to_dt:
            ap.error("--from must be earlier than --to")
        from_dt, to_dt = args.from_dt, args.to_dt
        label = f"{from_dt:%Y-%m-%d}  →  {to_dt:%Y-%m-%d}"
    elif args.from_dt or args.to_dt:
        ap.error("--from and --to must be used together (or use --days / --month)")
    else:
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=args.days)
        label   = f"last {args.days} day(s)"

    print(f"[rps_scraper] window: {label}", flush=True)
    print(f"[rps_scraper] mode:   {'DRY RUN' if args.dry_run else 'WRITE'}",
          flush=True)

    if not HUB_TRIP_SHEETS:
        print("[rps_scraper] HUB_TRIP_SHEETS is empty.", flush=True)
        sys.exit(1)

    print("\n[rps_scraper] Loading master sheet lookups…", flush=True)
    client = gspread_client()
    master = client.open_by_key(MASTER_SHEET_ID)
    vehicle_hub, vt_map = load_vehicles_tab(master.worksheet(VEHICLES_TAB))
    sla_map = load_route_sla(master.worksheet(ROUTE_SLA_TAB))
    rc_map, rc_items = load_route_codes(master.worksheet(ROUTE_CODES_TAB))
    print(f"  Vehicles:    {len(vehicle_hub)} vehicle→hub mappings", flush=True)
    print(f"  Route SLA:   {len(sla_map)} routes with TAT hours",   flush=True)
    print(f"  Route Codes: {len(rc_map)} hub-name→code mappings",   flush=True)

    def _canonical_hub(raw: str) -> str | None:
        if not raw:
            return None
        match = _HUB_TRIP_SHEETS_NORM.get(raw.strip().lower())
        return match[0] if match else None

    vehicle_hub = {vno: ch
                   for vno, hub in vehicle_hub.items()
                   if (ch := _canonical_hub(hub))}

    hub_counts: dict[str, int] = {}
    for h in vehicle_hub.values():
        hub_counts[h] = hub_counts.get(h, 0) + 1
    target_vehicles = sorted(vehicle_hub.keys())
    if not target_vehicles:
        print("[rps_scraper] No vehicles match any hub in HUB_TRIP_SHEETS.",
              flush=True)
        sys.exit(1)
    print(f"  Targeting:   {len(target_vehicles)} vehicle(s) across "
          f"{len(HUB_TRIP_SHEETS)} hub(s):", flush=True)
    for hub_name in HUB_TRIP_SHEETS:
        print(f"    {hub_name:<14} → {hub_counts.get(hub_name, 0):>3} vehicle(s)",
              flush=True)

    print("\n[rps_scraper] Calling RPS Report API…", flush=True)
    records = fetch_rps_trips(target_vehicles, from_dt, to_dt)
    print(f"[rps_scraper] {len(records)} total records returned", flush=True)
    if not records:
        return

    total_added       = 0
    total_skipped     = 0
    total_unrouted    = 0
    total_endfilled   = 0
    total_failed_rows = 0
    skipped_workbooks: list[str] = []

    workbook_cache: dict[str, tuple] = {}

    grouped: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        vno = first(rec, F_VEH).upper()
        if not vno:
            continue
        hub = vehicle_hub.get(vno)
        if not hub or hub not in HUB_TRIP_SHEETS:
            total_unrouted += 1
            continue
        start_dt, _ = parse_dt_pair(first(rec, F_START), first(rec, F_END))
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
        if sheet_id in skipped_workbooks:
            continue

        if sheet_id not in workbook_cache:
            ss = _retry(client.open_by_key, sheet_id,
                        _label=f"open_by_key({hub})")
            try:
                all_rps, backfill = existing_rps_in_workbook(ss)
            except DedupReadError as exc:
                # CRITICAL: refuse to write to this workbook on a failed
                # dedup.  Otherwise we'd treat every API row as "new" and
                # create thousands of duplicate rows.
                print(f"[rps_scraper] SKIPPING {hub} workbook — dedup "
                      f"unreadable ({exc}). Re-run later.", flush=True)
                skipped_workbooks.append(sheet_id)
                continue
            workbook_cache[sheet_id] = (ss, all_rps, backfill)
        ss, existing, backfill = workbook_cache[sheet_id]

        fresh: list[dict] = []
        end_updates: list[dict] = []
        for rec in recs:
            rps = first(rec, F_RPS)
            if not rps:
                continue
            key = _normalize_rps(rps)
            if key in existing:
                if key in backfill:
                    tab_title, row_num, need_start, need_end = backfill[key]
                    start_dt2, end_dt2 = parse_dt_pair(
                        first(rec, F_START), first(rec, F_END))
                    end_val   = to_sheets_serial(end_dt2)   if (need_end   and end_dt2)   else None
                    start_val = to_sheets_serial(start_dt2) if (need_start and start_dt2) else None
                    if end_val or start_val:
                        end_updates.append({
                            "tab":         tab_title,
                            "row":         row_num,
                            "end_value":   end_val,
                            "start_value": start_val,
                        })
                        backfill.pop(key, None)
                    else:
                        total_skipped += 1
                else:
                    total_skipped += 1
                continue
            existing.add(key)
            fresh.append(rec)

        # ── Atomic per-row back-fills (Start_Time and/or End_Time) ──────
        if end_updates and not args.dry_run:
            by_tab: dict[str, list[dict]] = {}
            for u in end_updates:
                by_tab.setdefault(u["tab"], []).append(u)
            for tab_title, ups in by_tab.items():
                try:
                    target_ws = ss.worksheet(tab_title)
                except gspread.WorksheetNotFound:
                    continue
                for u in ups:
                    # ONE batchUpdate per back-fill row: value + format land
                    # together, or nothing for that row lands.
                    cell_requests: list[dict] = []
                    if u.get("end_value") is not None:
                        cell_requests.append({"updateCells": {
                            "rows": [{"values": [{
                                "userEnteredValue": {"numberValue": float(u["end_value"])},
                                "userEnteredFormat": {"numberFormat": _datetime_fmt()},
                            }]}],
                            "fields": "userEnteredValue,userEnteredFormat.numberFormat",
                            "start": {"sheetId": target_ws.id,
                                      "rowIndex": u["row"] - 1,
                                      "columnIndex": 9},
                        }})
                    if u.get("start_value") is not None:
                        cell_requests.append({"updateCells": {
                            "rows": [{"values": [{
                                "userEnteredValue": {"numberValue": float(u["start_value"])},
                                "userEnteredFormat": {"numberFormat": _datetime_fmt()},
                            }]}],
                            "fields": "userEnteredValue,userEnteredFormat.numberFormat",
                            "start": {"sheetId": target_ws.id,
                                      "rowIndex": u["row"] - 1,
                                      "columnIndex": 8},
                        }})
                    if cell_requests:
                        try:
                            _retry(ss.batch_update, {"requests": cell_requests},
                                   _label=f"{tab_title}.backfill_row{u['row']}")
                        except Exception as exc:
                            print(f"  [backfill] {tab_title} row {u['row']}: "
                                  f"FAILED ({exc}) — row left untouched",
                                  flush=True)
                end_filled   = sum(1 for u in ups if u.get("end_value")   is not None)
                start_filled = sum(1 for u in ups if u.get("start_value") is not None)
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

        if not fresh:
            if not end_updates:
                print(f"  [{hub}/{tab_name}] {len(recs)} record(s) — all "
                      f"duplicates, nothing to write", flush=True)
            continue

        print(f"  [{hub}/{tab_name}] {len(fresh)} new / "
              f"{len(end_updates)} backfilled / "
              f"{len(recs) - len(fresh) - len(end_updates)} duplicate(s)",
              flush=True)

        if args.dry_run:
            total_added += len(fresh)
            continue

        ws = get_or_create_trip_tab(ss, tab_name)

        # Find current last row.
        col_a = _retry(ws.col_values, 1, _label=f"{tab_name}.col_values")
        start_row = len(col_a) + 1
        needed = start_row + len(fresh) + 5
        if ws.row_count < needed:
            _retry(ws.resize, rows=needed, _label=f"{tab_name}.resize")

        # Re-check after resize.
        col_a = _retry(ws.col_values, 1, _label=f"{tab_name}.col_values2")
        start_row = len(col_a) + 1

        # ── Atomic per-row insert ───────────────────────────────────────
        # Each row's value + every per-cell format is wrapped into ONE
        # spreadsheets.batchUpdate; if it fails (after retries), nothing
        # for that row was written. We move on to the next row instead of
        # aborting the entire workbook.
        for offset, rec in enumerate(fresh):
            row_num = start_row + offset
            values, _sd, _ed = build_row_values(
                rec, vt_map, sla_map, rc_map, rc_items, row_num)
            try:
                write_rows_atomically(ss, ws, row_num, [values])
                total_added += 1
            except Exception as exc:
                total_failed_rows += 1
                print(f"  [{hub}/{tab_name}] row {row_num} RPS "
                      f"{first(rec, F_RPS) or '?'} FAILED ({exc}) — "
                      f"row NOT inserted", flush=True)
                # Remove from in-memory dedup set so a future re-run can
                # retry this RPS instead of treating it as already-written.
                key_fail = _normalize_rps(first(rec, F_RPS))
                if key_fail:
                    existing.discard(key_fail)
                continue

        _delete_named_columns(ss, ws, {"_sort_key", "_sortKey", "sort_key"})
        _strip_extra_columns(ss, ws)

    # Final foreign-column sweep (defensive — bound Apps Scripts re-add it).
    print("\n[rps_scraper] Final foreign-column sweep…", flush=True)
    for sheet_id, cached in workbook_cache.items():
        ss = cached[0]
        try:
            worksheets = _retry(ss.worksheets, _label=f"{sheet_id}.worksheets")
        except Exception as exc:
            print(f"  [strip] {sheet_id}: worksheets() failed ({exc})",
                  flush=True)
            continue
        for ws in worksheets:
            if not ws.title.endswith("_MIS"):
                continue
            _ensure_trip_columns_and_header(ss, ws, ws.title)
            _format_extra_diesel_amount_column(ss, ws, ws.title)
            _delete_named_columns(ss, ws,
                                  {"_sort_key", "_sortKey", "sort_key"})
            _strip_extra_columns(ss, ws)

    print(f"\n[rps_scraper] Done. Added: {total_added}  "
          f"Back-filled: {total_endfilled}  "
          f"Skipped(dup): {total_skipped}  "
          f"Failed rows: {total_failed_rows}  "
          f"Workbooks skipped: {len(skipped_workbooks)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[rps_scraper] FATAL ERROR: {exc}", flush=True)
        traceback.print_exc()
        sys.exit(1)
