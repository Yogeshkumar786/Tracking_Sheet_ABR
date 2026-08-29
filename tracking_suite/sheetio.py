"""
Shared Google Sheets layer — and the place the lock is enforced.
===============================================================
Every spreadsheet open in this package goes through `open_sheet()`, and
`open_sheet()` calls `config.assert_locked()` first. There is no other route to
a `gspread.Spreadsheet` here, so no runner can reach a second workbook even by
mistake.

Writing style follows what already works elsewhere in this folder: one
`get_all_values()` per read, one `update()` per write, and colour only on the
cells that carry a decision — never whole rows.
"""
from __future__ import annotations

import json
import os

import gspread
from google.oauth2.service_account import Credentials

from tracking_suite import config

CREDS_FILE = "credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

COLORS = {
    "red":    {"red": 0.96, "green": 0.80, "blue": 0.80},
    "orange": {"red": 1.00, "green": 0.88, "blue": 0.70},
    "yellow": {"red": 1.00, "green": 0.95, "blue": 0.70},
    "green":  {"red": 0.85, "green": 0.93, "blue": 0.83},
    "blue":   {"red": 0.80, "green": 0.89, "blue": 0.97},
    "grey":   {"red": 0.90, "green": 0.90, "blue": 0.90},
    "white":  {"red": 1.00, "green": 1.00, "blue": 1.00},
}

HEADER_BG = {"red": 0.180, "green": 0.286, "blue": 0.490}
HEADER_FG = {"red": 1.0, "green": 1.0, "blue": 1.0}

_client_cache = None


def service_account_email() -> str:
    try:
        with open(CREDS_FILE, encoding="utf-8") as fh:
            return json.load(fh).get("client_email", "(unknown)")
    except Exception:
        return "(could not read credentials.json)"


def _creds() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw),
                                                     scopes=SCOPES)
    return Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)


def client() -> gspread.Client:
    global _client_cache
    if _client_cache is None:
        _client_cache = gspread.authorize(_creds())
    return _client_cache


def _with_quota_retry(fn, what: str):
    """Central transient-failure handling. Two expected, retryable failures:
    429s (the service account is shared with the hourly cloud job, so
    per-minute read-quota collisions are normal) and dropped connections
    (Google closing an idle socket mid-run). One place retries; nothing
    else sleeps."""
    import time
    import requests
    for attempt in range(3):
        try:
            return fn()
        except gspread.exceptions.APIError as exc:
            s = str(exc)
            if "429" in s and attempt < 2:
                wait = 65 * (attempt + 1)
                print(f"  [Quota] 429 on {what} — waiting {wait}s "
                      f"(shared service account)", flush=True)
                time.sleep(wait)
                continue
            if any(f"[{c}]" in s for c in (500, 502, 503)) and attempt < 2:
                wait = 15 * (attempt + 1)
                print(f"  [Net] Google 5xx on {what} — retrying in {wait}s",
                      flush=True)
                time.sleep(wait)
                continue
            raise
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"  [Net] connection dropped on {what} — retrying "
                      f"in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise


def open_sheet(sheet_id: str | None = None):
    """Open THE spreadsheet. Any other id raises before a request is made."""
    sid = config.assert_locked(sheet_id or config.sheet_id())
    try:
        return _with_quota_retry(lambda: client().open_by_key(sid), "open")
    except gspread.SpreadsheetNotFound as exc:
        raise RuntimeError(
            f"Spreadsheet {sid} not found, or the service account cannot see it.\n"
            f"  Share it as Editor with:  {service_account_email()}"
        ) from exc
    except gspread.exceptions.APIError as exc:
        raise RuntimeError(
            f"Google refused access to spreadsheet {sid}: {exc}\n"
            f"  Share it as Editor with:  {service_account_email()}"
        ) from exc


# One metadata fetch per spreadsheet per run — every find_tab used to make its
# own worksheets() API call (a quota hit each), for a tab list that changes at
# most once per run (when WE add a tab, which updates the memo directly).
_ws_memo: dict = {}


def _ws_list(ss):
    key = ss.id
    if key not in _ws_memo:
        _ws_memo[key] = _with_quota_retry(ss.worksheets, "list tabs")
    return _ws_memo[key]


def find_tab(ss, name: str):
    """Find a tab by name, tolerating stray case and surrounding whitespace.

    The live Vehicles tab is titled 'Vehicles ' with a trailing space. Matching
    exactly would fail on it, and renaming someone's tab to suit our code is the
    wrong fix — so match loosely and use whatever is actually there.
    """
    want = (name or "").strip().casefold()
    for ws in _ws_list(ss):
        if ws.title.strip().casefold() == want:
            return ws
    raise RuntimeError(
        f"Tab {name!r} not found in {ss.title!r}. "
        f"Tabs present: {[w.title for w in _ws_list(ss)]}"
    )


def get_or_create(ss, title: str, ncols: int, nrows: int = 100):
    try:
        return find_tab(ss, title)
    except RuntimeError:
        ws = ss.add_worksheet(title=title, rows=max(nrows, 100),
                              cols=max(ncols, 10))
        if ss.id in _ws_memo:
            _ws_memo[ss.id].append(ws)
        return ws


def col_a1(idx0: int) -> str:
    s, n = "", idx0
    while True:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def read_rows(ss, tab: str) -> list[list]:
    """One read. Returns raw rows including the header."""
    return _with_quota_retry(lambda: find_tab(ss, tab).get_all_values(),
                             f"read '{tab}'")


def _to_records(rows: list) -> list[dict]:
    if not rows:
        return []
    headers = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any((c or "").strip() for c in r):
            continue
        rec = {}
        for i, h in enumerate(headers):
            if h:
                rec[h] = (r[i].strip() if i < len(r) and r[i] is not None else "")
        out.append(rec)
    return out


def read_records(ss, tab: str) -> list[dict]:
    """Rows as dicts keyed by header name, so a column inserted by hand in the
    UI does not shift anything. Headers are stripped of stray whitespace."""
    return _to_records(read_rows(ss, tab))


def read_many(ss, tabs: list) -> dict:
    """{tab_name: records} for many tabs in ONE values.batchGet API call —
    ten startup reads collapse to one, which is what stops the 429 collisions
    with the hourly cloud job. A tab that does not exist yet maps to []."""
    have = {}
    for want in tabs:
        w = (want or "").strip().casefold()
        for ws in _ws_list(ss):
            if ws.title.strip().casefold() == w:
                have[want] = ws.title
                break
    if not have:
        return {t: [] for t in tabs}
    titles = list(have.values())
    resp = _with_quota_retry(
        lambda: ss.values_batch_get([f"'{t}'" for t in titles]),
        f"batch read {len(titles)} tab(s)")
    ranges = resp.get("valueRanges", [])
    out = {t: [] for t in tabs}
    for want, vr in zip(have.keys(), ranges):
        out[want] = _to_records(vr.get("values") or [])
    return out


def rebuild_tab(ss, title: str, headers: list, rows: list, *, quiet: bool = False):
    """Clear the tab and write headers + rows in one update."""
    ws = get_or_create(ss, title, len(headers), len(rows) + 10)
    ws.clear()
    matrix = [headers] + [["" if r.get(h) is None else r.get(h, "") for h in headers]
                          for r in rows]
    ws.update(values=matrix, range_name="A1", value_input_option="USER_ENTERED")
    if not quiet:
        print(f"  [Sheet] '{title}': wrote {len(rows)} row(s)", flush=True)
    return ws


def format_header(ws, ncols: int):
    ws.format(f"A1:{col_a1(ncols - 1)}1", {
        "backgroundColor": HEADER_BG,
        "textFormat": {"bold": True, "foregroundColor": HEADER_FG},
        "horizontalAlignment": "CENTER",
    })
    try:
        ws.freeze(rows=1)
    except Exception:
        pass


def paint_cells(ws, headers: list, rows: list, *, quiet: bool = False):
    """Reset the data block to white, then colour only the flagged cells.
    Each row may carry '_colors': {header_name: colour_key}."""
    if not rows:
        return
    col_of = {h: i for i, h in enumerate(headers)}
    ws.format(f"A2:{col_a1(len(headers) - 1)}{len(rows) + 1}",
              {"backgroundColor": COLORS["white"]})
    specs = []
    for i, r in enumerate(rows):
        for hdr, colour in (r.get("_colors") or {}).items():
            if hdr in col_of and colour in COLORS:
                specs.append({
                    "range": f"{col_a1(col_of[hdr])}{i + 2}",
                    "format": {"backgroundColor": COLORS[colour]},
                })
    for k in range(0, len(specs), 200):
        ws.batch_format(specs[k:k + 200])
    if not quiet:
        print(f"  [Sheet] coloured {len(specs)} cell(s)", flush=True)


def write_report(ss, title: str, headers: list, rows: list, *, quiet: bool = False):
    ws = rebuild_tab(ss, title, headers, rows, quiet=quiet)
    format_header(ws, len(headers))
    paint_cells(ws, headers, rows, quiet=quiet)
    return ws
