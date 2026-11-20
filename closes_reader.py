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


def _export_rows(include_volume=False, include_currency=False):
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
                header = {str(cell).strip().casefold(): i for i, cell in enumerate(row) if cell is not None}
                if {"instrument", "date", "price close"} <= header.keys():
                    # Each Workspace Lite block has its own column map. Later
                    # blocks can add Volume even when the first block lacks it.
                    columns = header
                    continue
                if max(columns[key] for key in ("instrument", "date", "price close")) >= len(row):
                    continue
                record = (str(row[columns["instrument"]]).strip(),
                          row[columns["date"]], row[columns["price close"]])
                if include_volume:
                    index = columns.get('volume')
                    record += (row[index] if index is not None and index < len(row) else None,)
                if include_currency:
                    index = columns.get('currency')
                    record += (row[index] if index is not None and index < len(row) else None,)
                yield record
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


def daily_close_volumes_for_ticker(ticker, start_date, end_date):
    """Read exact resolved Instrument closes and Volume; never infer volume.

    Missing/conflicting volumes remain None, so a caller cannot silently compute
    liquidity from just the rows whose volume happened to be available.
    The existing closes-only APIs keep their original return shapes.
    """
    try:
        instrument = resolve_export_instrument(ticker)
        if instrument == NOT_AVAILABLE:
            return NOT_AVAILABLE
        start, end = _date(start_date), _date(end_date)
        found = {}
        for name, raw_day, raw_close, raw_volume in _export_rows(include_volume=True):
            if name.upper() != instrument.upper():
                continue
            try:
                day, close = _date(raw_day), float(raw_close)
                if isinstance(raw_close, bool) or not start <= day <= end or not math.isfinite(close) or close <= 0:
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            try:
                volume = float(raw_volume)
                if isinstance(raw_volume, bool) or not math.isfinite(volume) or volume < 0:
                    volume = None
            except (ValueError, TypeError, OverflowError):
                volume = None
            record = {'date': day.isoformat(), 'close': close, 'volume': volume, 'instrument': instrument}
            if day not in found:
                found[day] = record
            elif found[day] is not None:
                if found[day]['close'] != close:
                    found[day] = None
                elif found[day]['volume'] != volume:
                    found[day]['volume'] = None
        return [row for _, row in sorted(found.items()) if row is not None] or NOT_AVAILABLE
    except Exception:
        return NOT_AVAILABLE


def daily_close_volumes(ticker, start_date, end_date):
    """Return resolved Instrument closes and volumes, preserving missing values."""
    return daily_close_volumes_for_ticker(ticker, start_date, end_date)


def instrument_currency(ticker):
    """Return an explicit, unambiguous Currency column value, never infer FX."""
    try:
        instrument = resolve_export_instrument(ticker)
        if instrument == NOT_AVAILABLE:
            return NOT_AVAILABLE
        values = {str(currency).strip().upper() for name, _, _, currency in _export_rows(include_currency=True)
                  if name.upper() == instrument.upper()}
        return next(iter(values)) if len(values) == 1 and re.fullmatch(r'[A-Z]{3}', next(iter(values))) else NOT_AVAILABLE
    except Exception:
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
