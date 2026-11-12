"""Read official daily closes from the configured export, without writing to it.

Dates are inclusive for daily_closes; last_close_before uses a strict cutoff.
Instrument values are matched to the requested ticker, ignoring case and spaces.
No ticker aliases or company-specific mappings are inferred.
daily_closes_for_ticker separately resolves a unique regular-way Instrument
equal to the SEC ticker or that ticker plus a dot-delimited vendor suffix.
"""

import argparse
from datetime import date, datetime, time
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
NOT_AVAILABLE = "not available"


def _eastern(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    eastern = ZoneInfo("America/New_York")
    return stamp.replace(tzinfo=eastern) if stamp.tzinfo is None else stamp.astimezone(eastern)


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _export_rows():
    # Lazy import lets the rest of the team run even without the Excel dependency.
    from openpyxl import load_workbook

    configured = dotenv_values(ROOT / ".env").get("CLOSES_FILE")
    if not configured:
        return
    path = Path(configured)
    if not path.is_absolute():
        path = ROOT / path
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            columns = None
            # Allow title rows before the actual export header.
            for row in rows:
                header = {str(cell).strip().casefold(): i for i, cell in enumerate(row) if cell is not None}
                if {"instrument", "date", "price close"} <= header.keys():
                    columns = header
                    break
            if columns is None:
                continue
            for row in rows:
                if max(columns[key] for key in ("instrument", "date", "price close")) >= len(row):
                    continue
                if all(str(row[columns[key]]).strip().casefold() == key
                       for key in ("instrument", "date", "price close")):
                    continue  # Workspace Lite may repeat the header between export blocks.
                yield (str(row[columns["instrument"]]).strip(),
                       row[columns["date"]], row[columns["price close"]])
    finally:
        workbook.close()


def _rows(ticker):
    found = {}
    wanted = ticker.strip().upper()
    for instrument, raw_day, raw in _export_rows():
        if instrument.upper() != wanted:
            continue
        try:
            day = _date(raw_day)
            if isinstance(raw, bool):
                continue
            close = float(raw)
            if not math.isfinite(close) or close <= 0:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        # Conflicting duplicate observations are not an authoritative close.
        if day in found and found[day] != close:
            found[day] = None
        else:
            found[day] = close
    return [{"date": day.isoformat(), "close": close}
            for day, close in sorted(found.items()) if close is not None]


def _when_issued(instrument):
    # Check both a ticker and the stem before a vendor suffix (.K, .O, etc.).
    return ('when' in instrument.casefold() or
            bool(re.search(r'(?:WI|W)(?:\.[A-Z]+)?$', instrument, re.I)))


def resolve_export_instrument(ticker):
    """Resolve only a sole exact ticker or ticker.<letters> in Instrument.

    Inspect the whole export, regardless of date/price availability: an exact
    match never wins over another plausible instrument. No prefix matching,
    issuer aliases, suffix preferences, or when-issued identifiers are allowed.
    Return the export's spelling for price provenance, or 'not available'.
    """
    try:
        wanted = ticker.strip()
        if not wanted or _when_issued(wanted):
            return NOT_AVAILABLE
        pattern = re.compile(re.escape(wanted) + r'(?:\.[A-Z]+)?', re.I)
        matches = {instrument for instrument, _, _ in _export_rows()
                   if pattern.fullmatch(instrument) and not _when_issued(instrument)}
        return next(iter(matches)) if len(matches) == 1 else NOT_AVAILABLE
    except Exception:
        return NOT_AVAILABLE


def daily_closes_for_ticker(ticker, start_date, end_date):
    """Resolve an SEC ticker, then read exact Instrument closes with provenance."""
    instrument = resolve_export_instrument(ticker)
    if instrument == NOT_AVAILABLE:
        return NOT_AVAILABLE
    rows = daily_closes(instrument, start_date, end_date)
    if not isinstance(rows, list):
        return NOT_AVAILABLE
    return [dict(row, instrument=instrument) for row in rows]


def daily_closes(ticker, start_date, end_date):
    """Return [{date: YYYY-MM-DD, close: number}, ...], or 'not available'."""
    try:
        start, end = _date(start_date), _date(end_date)
        rows = [row for row in _rows(ticker) if start <= _date(row["date"]) <= end]
        return rows or NOT_AVAILABLE
    except Exception:
        # Export/dependency/format failures must not prevent a team run.
        return NOT_AVAILABLE


def last_close_before(ticker, filed):
    """Return one close (float) strictly before an Eastern timestamp, or 'not available'.

    Every exported trading date is observed at 16:00 America/New_York, including
    dates on which an exchange may actually have closed early, per this contract.
    """
    try:
        cutoff = _eastern(filed)
        eastern = cutoff.tzinfo
        eligible = [row for row in _rows(ticker)
                    if datetime.combine(_date(row["date"]), time(16), eastern) < cutoff]
        return eligible[-1]["close"] if eligible else NOT_AVAILABLE
    except Exception:
        return NOT_AVAILABLE


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--filed", required=True, help="YYYY-MM-DDTHH:MM:SS Eastern")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", args.filed):
        parser.error("--filed must use YYYY-MM-DDTHH:MM:SS Eastern")
    try:
        datetime.fromisoformat(args.filed)
    except ValueError:
        parser.error("--filed must be a valid date and time")
    print(json.dumps(last_close_before(args.ticker, args.filed)))
