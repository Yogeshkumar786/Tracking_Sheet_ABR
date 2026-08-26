"""
Cloud config — the hourly holding report.
=========================================
Reads the Vehicles tab from, and writes the two holding tabs to, ONE workbook:
the vehicle master. Its id is fixed here (overridable by env for testing) and
every sheet open is checked against it, so the job can only ever touch that
workbook.

Credentials come from the GOOGLE_CREDENTIALS_JSON environment variable (the
GitHub Actions secret), never from a file in the repo.
"""
from __future__ import annotations

import os

# The vehicle master workbook — reads Vehicles, writes the holding tabs.
MASTER_SHEET_ID = os.getenv(
    "MASTER_SHEET_ID", "1-tEwE7YwZFNhfGjvgZPHYMKXJqSr_TOmrAodfuadGf0")

TAB_VEHICLES = os.getenv("TAB_VEHICLES", "Vehicles")
TAB_HUB_LIST = os.getenv("TAB_HUB_LIST", "Hub List")
TAB_AMBALA = "Ambala Holding"
TAB_BINOLA = "Binola Holding"

# Presence thresholds (kept identical to the local suite).
HUB_RADIUS_M = float(os.getenv("HUB_RADIUS_M", "1500"))
HUB_AMBIGUOUS_M = float(os.getenv("HUB_AMBIGUOUS_M", "1500"))
GPS_STALE_HRS = float(os.getenv("GPS_STALE_HRS", "6"))
API_TIMEOUT = int(os.getenv("FMS_API_TIMEOUT", "90"))


class SheetLockError(RuntimeError):
    """Raised if anything tries to open a workbook other than the master."""


def assert_locked(sheet_id: str) -> str:
    sid = (sheet_id or "").strip()
    if sid != MASTER_SHEET_ID:
        raise SheetLockError(
            f"Refusing to touch spreadsheet {sid!r}; this job is locked to "
            f"{MASTER_SHEET_ID} (the vehicle master).")
    return sid


def sheet_id() -> str:
    return MASTER_SHEET_ID


def describe() -> str:
    return (f"  Master sheet   : {MASTER_SHEET_ID}\n"
            f"  Vehicles tab   : {TAB_VEHICLES}\n"
            f"  Writes tabs    : {TAB_AMBALA}, {TAB_BINOLA}\n"
            f"  Hub List tab   : {TAB_HUB_LIST} (read from the master sheet)")
