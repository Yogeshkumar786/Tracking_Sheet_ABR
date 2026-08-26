"""
Sheets layer — credentials from env, locked to the master workbook.
"""
from __future__ import annotations

import json
import os

import gspread
from google.oauth2.service_account import Credentials

from tracking_suite import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER_BG = {"red": 0.180, "green": 0.286, "blue": 0.490}
HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}

_client_cache = None


def _creds() -> Credentials:
    """Prefer the GOOGLE_CREDENTIALS_JSON secret; fall back to a local file."""
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
    return Credentials.from_service_account_file(path, scopes=SCOPES)


def client() -> gspread.Client:
    global _client_cache
    if _client_cache is None:
        _client_cache = gspread.authorize(_creds())
    return _client_cache


def open_sheet(sheet_id: str | None = None):
    sid = config.assert_locked(sheet_id or config.sheet_id())
    try:
        return client().open_by_key(sid)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot open spreadsheet {sid}: {exc}\n"
            f"  Share it as Editor with the service account, and check the "
            f"GOOGLE_CREDENTIALS_JSON secret is set.") from exc


def find_tab(ss, name: str):
    want = (name or "").strip().casefold()
    for ws in ss.worksheets():
        if ws.title.strip().casefold() == want:
            return ws
    raise RuntimeError(f"Tab {name!r} not found. Present: "
                       f"{[w.title for w in ss.worksheets()]}")


def get_or_create(ss, title: str, ncols: int, nrows: int = 100):
    try:
        return find_tab(ss, title)
    except RuntimeError:
        return ss.add_worksheet(title=title, rows=max(nrows, 100), cols=max(ncols, 10))


def col_a1(idx0: int) -> str:
    s, n = "", idx0
    while True:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def read_rows(ss, tab: str):
    return find_tab(ss, tab).get_all_values()


def read_records(ss, tab: str):
    rows = read_rows(ss, tab)
    if not rows:
        return []
    headers = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any((c or "").strip() for c in r):
            continue
        out.append({h: (r[i].strip() if i < len(r) and r[i] is not None else "")
                    for i, h in enumerate(headers) if h})
    return out
