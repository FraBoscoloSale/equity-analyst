"""Minimal read-only factor check of completed seeker report_*.md snapshots.

Usage: python -B factor_check.py FOLDER
No downloads, models, history updates, parameter fitting or significance tests.
For each saved view and issuer pair, use the earliest two common saved price
dates strictly after the view date. These are sparse observed-price returns,
not a fixed holding-period backtest or evidence of statistical significance.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path
import re
import sys

from spinoff_seeker import load_factors

ROOT = Path(__file__).resolve().parent


def number(text):
    try:
        value = Decimal(text.replace(',', '').strip())
        return value if value.is_finite() else None
    except (InvalidOperation, AttributeError):
        return None


def read_report(path, spec):
    text = path.read_text(encoding='utf-8-sig')
    if not text.rstrip().endswith('screens, not recommendations'):
        return None
    header = re.search(r'^Run date: ([^|\r\n]+)', text, re.M)
    digest = re.search(r'Factor file SHA256: ([a-f0-9]{64})\.', text)
    if not header:
        return None
    stamp = datetime.fromisoformat(header[1].strip())
    if stamp.tzinfo is None:
        raise ValueError(str(path)+': saved run date must include a timezone')
    marker = '## In their window, ordered by rank'
    if marker not in text:
        return None
    section = text.split(marker, 1)[1].split('\n## ', 1)[0]
    rows, current = {}, None
    for line in section.splitlines():
        issuer = re.match(r'^\d+\. (.*?) \| CIK (\d+) \| total points ', line)
        if issuer:
            cik = issuer[2].lstrip('0') or '0'
            if cik in rows:
                raise ValueError(str(path)+': duplicate issuer in saved view')
            current = rows[cik] = {'factors': {}, 'price': None}
        elif current is not None:
            measure = re.match(r'^- (.*?): value ([^;]+); points [^;]+; weight ([^;]+); ([^;]+); ', line)
            if measure:
                key = measure[1].casefold()
                if key in current['factors']:
                    raise ValueError(str(path)+': duplicate factor measurement')
                current['factors'][key] = (number(measure[2]), number(measure[3]), measure[4].casefold())
            price = re.match(r'^- latest official close: (\S+) \| price date (\d{4}-\d\d-\d\d) \| export instrument (\S+)\s*$', line)
            if price:
                close, day, instrument = number(price[1]), date.fromisoformat(price[2]), price[3]
                if (close is not None and close > 0 and day <= stamp.date() and
                    not re.search(r'when|(?:WI|W)(?:\.[A-Z]+)?$', instrument, re.I)):
                    current['price'] = day, close, instrument
    return {'stamp': stamp, 'rows': rows, 'spec_matches': bool(digest and digest[1] == spec['sha256'])}


def check(folder, factors_path=ROOT/'spinoff_factors.txt'):
    """Only read inputs; a conflicting same-time historical view is an error."""
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('Completed-runs folder does not exist: '+str(folder))
    spec = load_factors(factors_path)  # Fixed before any outcome is read.
    reports = []
    for path in sorted(folder.rglob('report_*.md')):
        report = read_report(path, spec)
        if report is not None:
            reports.append(report)
    prices = defaultdict(lambda: defaultdict(set))
    saved_views = {}
    for report in reports:
        for cik, row in report['rows'].items():
            identity = report['stamp'], cik
            signature = row['factors'], report['spec_matches']
            if identity in saved_views and saved_views[identity] != signature:
                raise ValueError('Conflicting saved historical view for CIK '+cik+' at '+report['stamp'].isoformat()+'; refusing replacement')
            saved_views[identity] = signature
            if row['price']:
                day, close, instrument = row['price']
                prices[cik][day].add((close, instrument))
    # Repeated identical saved prices are one observation. Conflicts are missing,
    # never resolved using whichever price produces a favorable result.
    prices = {cik: {day: next(iter(values)) for day, values in days.items() if len(values) == 1}
              for cik, days in prices.items()}
    counts = {f['key']: {'valid': 0, 'concordant': 0, 'discordant': 0, 'return_ties': 0} for f in spec['factors']}
    compared_views = set()
    for report in sorted(reports, key=lambda item: item['stamp']):
        if not report['spec_matches'] or report['stamp'] in compared_views:
            continue
        compared_views.add(report['stamp'])
        # Combine identical-time records without counting duplicate reports twice.
        rows = {cik: signature[0] for (stamp, cik), signature in saved_views.items() if stamp == report['stamp'] and signature[1]}
        for left, right in combinations(sorted(rows), 2):
            common = sorted(day for day in set(prices.get(left, {})) & set(prices.get(right, {})) if day > report['stamp'].date())
            if len(common) < 2:
                continue
            entry, exit_day = common[:2]  # Date-only rule; never inspect returns to choose dates.
            returns = {}
            for cik in (left, right):
                first, last = prices[cik][entry], prices[cik][exit_day]
                if first[1] == last[1]:
                    returns[cik] = last[0]/first[0]-1
            if len(returns) != 2:
                continue
            for factor in spec['factors']:
                key, weight = factor['key'], Decimal(str(factor['weight']))
                a, b = rows[left].get(key), rows[right].get(key)
                if (weight == 0 or a is None or b is None or a[0] is None or b[0] is None or
                    a[1:] != (weight, factor['direction'].casefold()) or
                    b[1:] != (weight, factor['direction'].casefold()) or a[0] == b[0]):
                    continue
                preference = (a[0]-b[0])*factor['sign']
                outcome = returns[left]-returns[right]
                result = counts[key]
                result['valid'] += 1
                result['return_ties' if outcome == 0 else 'concordant' if preference*outcome > 0 else 'discordant'] += 1
    lines = ['Fixed factor file SHA256: '+spec['sha256'],
             'Completed reports read: '+str(len(reports)),
             'Return rule: earliest two common saved price dates strictly after each saved view date; same instrument per issuer.',
             'Valid comparisons: '+str(sum(item['valid'] for item in counts.values()))]
    for factor in spec['factors']:
        result = counts[factor['key']]
        lines.append(f'{factor["name"]} | direction: {factor["direction"]} | weight: {factor["weight"]}'
                     f' | valid comparisons: {result["valid"]} | concordant: {result["concordant"]}'
                     f' | discordant: {result["discordant"]} | return ties: {result["return_ties"]}')
    lines.append('Counts are descriptive factor/issuer-pair comparisons, not independent trials; no statistical significance is claimed, especially with a small sample. Missing data and factor ties are excluded; incomplete or different-specification reports are not baseline views.')
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('completed_runs', type=Path)
    parser.add_argument('--factors', type=Path, default=ROOT/'spinoff_factors.txt')
    args = parser.parse_args(argv)
    try:
        print(check(args.completed_runs, args.factors), end='')
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
