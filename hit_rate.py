"""Read-only Lab 07 hit-rate table: python -B hit_rate.py FOLDER.

Reads every completed run.json recursively (including FOLDER/run.json).
A run is complete when status is 'completed', completed is true, or its folder
has verified_report.md. Minimal new run record:
{"status":"completed", "ticker":"UNIT", "filing_time":"2026-01-02T08:00:00-05:00",
 "view":{"time":"2026-01-02T09:00:00-05:00", "direction":"up"}}
An explicit view.json with time/direction is also accepted. No run-start/mtime
fallback is allowed for view time. Missing metadata remains unavailable.
Directions: up/down/flat/not computed. Exact saved value comparisons 'more than
the current price', 'less than the current price', 'equal to the current price'
map to up/down/flat. No free-form thesis or recommendation is interpreted.

Reaction = first official close strictly AFTER filing / last official close
strictly BEFORE filing - 1; its sign is up/down/flat. Agree iff that sign equals
the saved direction. Close timestamps follow the existing reader: 16:00 Eastern.
View later than the first post-filing close => after and excluded. Not computed,
unknown view/time/after status and unavailable reactions are also excluded.
No significance claim, model, network, persistence, moving or rewriting history.
New reaction evidence is returned only in the table, never written to disk.
"""
import argparse
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import closes_reader

NA = 'not available'
EASTERN = ZoneInfo('America/New_York')
DIRECTIONS = {'up': 'up', 'down': 'down', 'flat': 'flat',
              'more than the current price': 'up', 'less than the current price': 'down',
              'equal to the current price': 'flat'}


def stamp(value, zone=EASTERN):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=zone) if result.tzinfo is None else result.astimezone(zone)
    except ValueError:
        return None


def close_time(day):
    return datetime.combine(datetime.fromisoformat(day).date(), time(16), EASTERN)


def positive(value):
    if isinstance(value, bool):
        raise ValueError('Boolean close')
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('Invalid close')
    return result


def reaction_evidence(instrument, before, after):
    if not isinstance(instrument, str) or closes_reader._when_issued(instrument):
        return None
    try:
        first, last = positive(before['close']), positive(after['close'])
        before_time, after_time = close_time(before['date']), close_time(after['date'])
        if before_time >= after_time:
            return None
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None
    value = last/first-1
    return {'instrument': instrument, 'before': {'date': before['date'], 'close': str(first)},
            'after': {'date': after['date'], 'close': str(last)}, 'return': value,
            'direction': 'up' if value > 0 else 'down' if value < 0 else 'flat'}


def observed(ticker, filed, as_of, reader):
    if not ticker or filed is None:
        return None, None
    # Read all locally exported history so neither missing prior days nor a
    # weekend are silently replaced with an assumed trading day.
    rows = reader(ticker, '0001-01-01', as_of.astimezone(EASTERN).date().isoformat())
    if not isinstance(rows, list) or not rows:
        return None, None
    try:
        instruments = {row['instrument'] for row in rows}
        if len(instruments) != 1:
            return None, None
        instrument = next(iter(instruments))
        if closes_reader._when_issued(instrument) or len({row['date'] for row in rows}) != len(rows):
            return None, None
        rows = sorted((row for row in rows if close_time(row['date']) <= as_of), key=lambda row: row['date'])
        for row in rows:
            positive(row['close'])
        prior = [row for row in rows if close_time(row['date']) < filed]
        later = [row for row in rows if close_time(row['date']) > filed]
        if not later:
            return None, None
        first = later[0]
        evidence = reaction_evidence(instrument, prior[-1], first) if prior else None
        return first, evidence
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None, None


def saved_reaction(value, filed, as_of):
    if not isinstance(value, dict) or filed is None:
        return None
    result = reaction_evidence(value.get('instrument'), value.get('before'), value.get('after'))
    if result is None:
        return None
    if not (close_time(result['before']['date']) < filed < close_time(result['after']['date']) <= as_of):
        return None
    if value.get('direction', result['direction']) != result['direction']:
        return None
    return result


def read_runs(folder):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('Run folder does not exist: '+str(folder))
    runs, originals = [], {}
    for path in sorted(folder.rglob('run.json')):
        raw = path.read_bytes(); originals[path] = raw
        run = json.loads(raw.decode('utf-8-sig'))
        if not (run.get('status') == 'completed' or run.get('completed') is True or (path.parent/'verified_report.md').is_file()):
            continue
        view_path = path.parent/'view.json'
        if view_path.is_file():
            view_bytes = view_path.read_bytes(); originals[view_path] = view_bytes
            view = json.loads(view_bytes.decode('utf-8-sig'))
            if isinstance(run.get('view'), dict) and run['view'] != view:
                raise ValueError(str(path)+': conflicting saved views; no historical view will be selected or rewritten')
        else:
            view = run.get('view', {})
        if isinstance(view, str):
            view = {'direction': view, 'time': run.get('view_time')}
        if not isinstance(view, dict):
            view = {}
        runs.append((path, run, view))
    return runs, originals


def row_for(run, view, as_of, reader):
    ticker = run.get('ticker') or run.get('company', {}).get('ticker') or NA
    filing = run.get('filing_time', {})
    filed_text = filing.get('effective_filing_time') if isinstance(filing, dict) else filing
    zone = ZoneInfo(filing.get('timezone', 'America/New_York')) if isinstance(filing, dict) else EASTERN
    filed = stamp(filed_text, zone)
    view_text = view.get('time') or view.get('timestamp') or run.get('view_time')
    view_time = stamp(view_text)
    direction = view.get('direction', run.get('direction', NA))
    canonical = DIRECTIONS.get(str(direction).casefold().strip())
    first, current = observed(ticker, filed, as_of, reader)
    saved = saved_reaction(run.get('reaction'), filed, as_of)
    # Never replace an already saved reaction with a changed observation. If
    # both sources exist but conflict, retain history and exclude the row.
    raw_reaction = run.get('reaction')
    missing_reaction = raw_reaction is None or (isinstance(raw_reaction, str) and
                       raw_reaction.casefold().strip() in (NA, 'not computed', 'not known', 'unavailable'))
    evidence = current if missing_reaction else saved
    if saved and current and saved != current:
        evidence = None
    if saved and first and saved['after']['date'] != first['date']:
        evidence = None
    # A saved reaction alone cannot prove that its post close was the FIRST.
    # An unavailable first-close check therefore cannot establish eligibility.
    is_after = view_time > close_time(first['date']) if view_time and first else None
    eligible = canonical is not None and view_time is not None and filed is not None and is_after is False and evidence is not None
    agree = (canonical == evidence['direction']) if eligible else None
    reaction = NA
    if evidence:
        reaction = (f'{evidence["direction"]} ({evidence["return"]*100}%; '
                    f'{evidence["instrument"]}; pre {evidence["before"]["date"]}={evidence["before"]["close"]}; '
                    f'first {evidence["after"]["date"]}={evidence["after"]["close"]})')
    after = 'after' if is_after else '-' if is_after is False else NA
    fields = [ticker, filed_text or NA, view_text or NA, direction, reaction,
              'agree' if agree else 'disagree' if agree is False else 'excluded', after]
    return [str(value).replace('|', '/').replace('\n', ' ') for value in fields], agree


def table(folder, reader=None, as_of=None):
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError('as_of must include a timezone')
    runs, originals = read_runs(folder)
    lines = ['Rule: reaction = first close strictly after filing / last close strictly before filing - 1; agree iff saved direction equals its sign (up/down/flat).',
             'Close timestamps: 16:00 America/New_York, following closes_reader; naive saved timestamps are Eastern. View later than first close = after. Missing metadata, not computed views, after rows and unavailable reactions are excluded.',
             'Descriptive sample only; no statistical significance is claimed. Reaction evidence is computed in memory; saved history is never written.',
             'ticker | filing time | view time | direction | reaction | agree or disagree | after']
    agreements, denominator = 0, 0
    for path, run, view in runs:
        fields, agreement = row_for(run, view, as_of, reader or closes_reader.daily_closes_for_ticker)
        lines.append(' | '.join(fields))
        if agreement is not None:
            denominator += 1
            agreements += int(agreement)
    # Byte-for-byte check, including historical view/direction/timestamps. No
    # writes, sidecars or atomic replacement are needed because nothing persists.
    for path, before in originals.items():
        if path.read_bytes() != before:
            raise ValueError(str(path)+': historical run/view bytes changed during read; refusing results')
    rate = f'{Decimal(agreements)/denominator*100:.2f}%' if denominator else 'not computed'
    lines.append(f'Agreements: {agreements} | Eligible runs with a reaction: {denominator} | Hit rate: {rate}')
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('folder', type=Path, help='Any completed-run folder, e.g. runs/live; folders are never moved')
    args = parser.parse_args(argv)
    try:
        print(table(args.folder), end='')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
