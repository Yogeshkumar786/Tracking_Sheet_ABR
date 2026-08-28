"""
Route_TAT from the operational trip sheets — READ-ONLY.
=======================================================
The user's rule: the TAT shown on the tracker is the operational Route_TAT from
the trip sheets (Ambala / Ambala Local / Binola workbooks, monthly *_MIS tabs),
not a guess. Learned history and distance remain as fallbacks only.

Lock discipline: these three workbooks are the ONLY spreadsheets this package
may read besides the testing sheet, they are read through one whitelisted
entry point, and nothing here can write — no write method is ever called and
the ids never reach sheetio.open_sheet().

Route_Code format in the sheets: "HRD11-HYD11;MAD11" = origin-via;dest.
TAT format: "53:00:00" (hours can exceed 24), occasionally "1:15:00".
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

import gspread
from google.oauth2.service_account import Credentials

CREDS_FILE = "credentials.json"

# Read-only whitelist. Taken from the environment (same source the legacy
# scripts use) with the known ids as fallback, so a missing .env cannot
# silently widen or lose the list.
_FALLBACK = {
    "Ambala": "1_unl3WrQZngLUdS1-jA95UZpkjoa1ZqZIIiu3G11DBo",
    "Ambala Local": "1SeJ06RjF2ONqCsP53_NO0FEjKhY3EV5UrNtLMmsA2N4",
    "Binola": "1Jz5N01qzwJRStr5Vb9oIU_DeGshiEeL4kLkMiomIeA8",
}


def trip_sheet_ids() -> dict:
    try:
        ids = json.loads(os.getenv("HUB_TRIP_SHEETS_JSON", "") or "{}")
    except json.JSONDecodeError:
        ids = {}
    return {k: str(v) for k, v in (ids or _FALLBACK).items()} or dict(_FALLBACK)


_MIS_RE = re.compile(r'^([A-Z][a-z]+)_(\d{4})_MIS$')
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_CODE_RE = re.compile(r'[A-Za-z0-9]{3,8}')


def _parse_tat_hours(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r'^(\d{1,3}):(\d{2})(?::(\d{2}))?$', s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    try:
        v = float(s)
        return v if 0.5 <= v <= 200 else None
    except ValueError:
        return None


def _endpoints(route_code: str) -> tuple:
    codes = _CODE_RE.findall((route_code or "").upper())
    if len(codes) < 2:
        return ("", "")
    return codes[0], codes[-1]


def load_sheet_tats(cluster: dict, months: int = 2) -> dict:
    """Read Route_TAT from the newest `months` MIS tabs of each trip sheet.

    Returns {"exact": {(O_CODE, D_CODE): hours}, "region": {(o_reg, d_reg): hours}}
    using the MOST COMMON value per lane — these are operational standards, so
    the mode is the standard and outliers are data-entry noise.
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:                       # cloud: the GitHub Actions secret
        creds = Credentials.from_service_account_info(json.loads(raw),
                                                      scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(CREDS_FILE,
                                                      scopes=scopes)
    gc = gspread.authorize(creds)

    exact: dict = {}
    region: dict = {}
    read = 0
    for name, sid in trip_sheet_ids().items():
        try:
            from tracking_suite.sheetio import _with_quota_retry
            ss = _with_quota_retry(lambda sid=sid: gc.open_by_key(sid),
                                   f"open trip sheet '{name}'")
            tabs = []
            for w in ss.worksheets():
                m = _MIS_RE.match(w.title)
                if m and m.group(1) in _MONTHS:
                    tabs.append((int(m.group(2)), _MONTHS.index(m.group(1)),
                                 w.title))
            tabs.sort(reverse=True)
            for _, _, title in tabs[:months]:
                rows = _with_quota_retry(
                    lambda t=title: ss.worksheet(t).get_all_values(),
                    f"read '{title}'")
                if not rows:
                    continue
                hdr = rows[0]
                try:
                    ic, it = hdr.index("Route_Code"), hdr.index("Route_TAT")
                except ValueError:
                    continue
                for r in rows[1:]:
                    if len(r) <= max(ic, it):
                        continue
                    h = _parse_tat_hours(r[it])
                    o, d = _endpoints(r[ic])
                    if h and o and d and o != d:
                        exact.setdefault((o, d), Counter())[round(h, 2)] += 1
                        rk = (cluster.get(o, o), cluster.get(d, d))
                        region.setdefault(rk, Counter())[round(h, 2)] += 1
                read += 1
        except Exception as exc:
            print(f"  [TAT-sheet] '{name}' unreadable: {str(exc)[:60]}", flush=True)

    out_e = {k: c.most_common(1)[0][0] for k, c in exact.items()}
    out_r = {k: c.most_common(1)[0][0] for k, c in region.items()}
    print(f"  [TAT-sheet] {len(out_e)} exact lane TAT(s) from {read} MIS tab(s) "
          f"(read-only)", flush=True)
    return {"exact": out_e, "region": out_r}
