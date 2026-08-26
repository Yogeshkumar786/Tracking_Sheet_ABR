"""
FMS live dashboard client.
==========================
The one non-spreadsheet source this suite reads: where every vehicle is, right
now. Deliberately self-contained — it does not import any module that knows
about a Google Sheet, so there is no path from here to a second workbook.

One endpoint is used:

    POST {BASE_API}/dashboard/post/json/getTransporterStatusDashbaordDetails
    {"userId": <USER_ID>}

which returns one record per vehicle carrying, among other fields:

    vehicleNumber  vehicleId  latitude  longitude  speed
    lastLocation   lastLocationDatetime
    isOnTrip  isRunning  isStopped  tripId  dispatchDate

Credentials and endpoints come from .env (BASE_API, USER_ID), never from a file
in the repo.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import requests

from tracking_suite import config

BASE_API = os.getenv("BASE_API", "https://fmssmart.dsmsoft.com/FMSSmart")
USER_ID = int(os.getenv("USER_ID", "3435"))
API_REFERER = os.getenv("API_REFERER", "https://fmssmart.dsmsoft.com/FMSSmartApp/")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": API_REFERER,
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36"),
}

MAX_RETRIES = 3
RETRY_DELAY = 2.0


def _post(url: str, payload: dict):
    """POST with backoff. Raises after the last attempt rather than returning
    something empty that would read downstream as 'no vehicles'."""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(url, headers=HEADERS, json=payload,
                              timeout=config.API_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (2 ** attempt)
                print(f"    [WARN] FMS error (attempt {attempt + 1}/{MAX_RETRIES}), "
                      f"retrying in {delay:.0f}s: {exc}", flush=True)
                time.sleep(delay)
    raise RuntimeError(f"FMS API failed after {MAX_RETRIES} attempts: {last}") from last


def _extract_list(resp) -> list:
    if not isinstance(resp, dict):
        return resp or []
    inner = resp.get("data", [])
    if isinstance(inner, list):
        return inner
    if isinstance(inner, dict):
        return inner.get("data", []) or []
    return []


def live_vehicles(user_id: int | None = None) -> list[dict]:
    """Every vehicle on the FMS dashboard, with its current position.

    user_id selects the account. Everything here is parameterised by it so the
    same code can be pointed at a second account (a contractor's larger fleet)
    without a fork — pass --user-id and nothing else changes.
    """
    uid = user_id or USER_ID
    url = f"{BASE_API}/dashboard/post/json/getTransporterStatusDashbaordDetails"
    rows = _extract_list(_post(url, {"userId": uid}))
    if not rows:
        raise RuntimeError(f"FMS returned no vehicles for userId={uid}")
    print(f"  [FMS] {len(rows)} vehicles on the dashboard (userId={uid})", flush=True)
    return rows


def vehicle_ids(user_id: int | None = None) -> dict:
    """{VEHICLE_NO: vehicleId} from the dashboard.

    Deliberately built from the dashboard rather than getUserGenericData, which
    is slow enough under load to time out — the dashboard already carries the
    ids for every vehicle.
    """
    out = {}
    for v in live_vehicles(user_id=user_id):
        vno, vid = veh_no(v), v.get("vehicleId")
        if vno and vid:
            try:
                out[vno] = int(vid)
            except (TypeError, ValueError):
                pass
    return out


def tracking_report(vehicle_id: int, from_dt, to_dt,
                    user_id: int | None = None) -> list[dict]:
    """Raw GPS pings for one vehicle over a window.

    Payload shape matters and is easy to get wrong: uId (not userId), vehicleId
    as a LIST, fDate/tDate (not fromDate/toDate). The older spelling is silently
    ignored by the server, which then returns an empty list rather than an error
    — so a wrong payload looks exactly like a vehicle that never moved.

    Returns [] on failure rather than raising: one unreadable vehicle should not
    end a sweep of two hundred.
    """
    uid = user_id or USER_ID
    url = f"{BASE_API}/report/post/json/getTrackingReport"
    payload = {
        "uId": uid,
        "vehicleId": [vehicle_id],
        "fDate": from_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "tDate": to_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        return _extract_list(_post(url, payload))
    except Exception as exc:
        print(f"      [track] vehicleId={vehicle_id}: {str(exc)[:70]}", flush=True)
        return []


# ── Field accessors ─────────────────────────────────────────────────────────
# One place that knows the API's field names, so a rename is a one-line fix.

def veh_no(v: dict) -> str:
    return str(v.get("vehicleNumber") or "").strip().upper()


def position(v: dict) -> tuple[float, float]:
    """(lat, lon). (0.0, 0.0) when the record carries no fix at all."""
    try:
        return float(v.get("latitude") or 0), float(v.get("longitude") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def on_trip(v: dict) -> bool:
    return bool(v.get("isOnTrip"))


def running(v: dict) -> bool:
    return bool(v.get("isRunning"))


def current_rps(v: dict) -> str:
    return str(v.get("tripId") or "").strip() if on_trip(v) else ""


def last_location(v: dict) -> str:
    return str(v.get("lastLocation") or "").strip()


_DT_FORMATS = (
    "%d-%b-%Y %I:%M:%S %p", "%d-%b-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
)


_CODE_IN = __import__("re").compile(r'\(([A-Za-z0-9]{2,8})\)')


def current_dest_code(v: dict) -> str:
    """The active trip's FINAL destination hub code. consigneeCode is
    'IDR11;DHU11' (via;final) — the last one is the real destination."""
    raw = str(v.get("consigneeCode") or "").strip()
    parts = [p.strip().upper() for p in raw.split(";") if p.strip()
             and p.strip().upper() != "NA"]
    return parts[-1] if parts else ""


def origin_code(v: dict) -> str:
    """The active trip's origin hub code, parsed from consignerName '(HRS11)'."""
    m = _CODE_IN.search(str(v.get("consignerName") or ""))
    return m.group(1).upper() if m else ""


def consigner_name(v: dict) -> str:
    """The active trip's origin, as a name (consignerName has no code sometimes)."""
    return str(v.get("consignerName") or "").strip()


def consignee_name(v: dict) -> str:
    """The active trip's FINAL destination, as a name (last of a ';' list)."""
    raw = str(v.get("consigneeName") or "").strip()
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    return parts[-1] if parts else ""


def remaining_km(v: dict) -> float:
    try:
        return float(v.get("remainingDistance") or 0)
    except (TypeError, ValueError):
        return 0.0


def stopped_since(v: dict):
    """When the vehicle last stopped (start of its current halt), or None."""
    raw = str(v.get("stoppedStartDate") or "").strip()
    if not raw or raw.upper() == "NA":
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def last_gps_dt(v: dict):
    """datetime of the last fix, or None if it cannot be parsed.

    Returns None rather than a guess: an unparseable timestamp must read as
    'we don't know how old this is', not as 'it is fresh'.
    """
    raw = str(v.get("lastLocationDatetime") or "").strip()
    if not raw:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None
