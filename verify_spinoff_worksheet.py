"""Read-only, offline numeric audit of the actual worksheet. Never reads its audit JSON.

Exit zero silently on success; exit two with the first worksheet-line failure.
--hook consumes Codex PostToolUse/Stop JSON on stdin. No hook is installed here.
"""
import argparse
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
from statistics import median
import sys
import xml.etree.ElementTree as ET

import closes_reader

ROOT = Path(__file__).resolve().parent
NUM = r'(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?(?!\w)'
NUMBER = re.compile(NUM)
WORDS = re.compile(r'\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
                   r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|'
                   r'thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)\b', re.I)
UNKNOWN = ('not known', 'not computed', 'not available', 'not applicable')


def dec(value):
    result = Decimal(str(value).replace(',', ''))
    if not result.is_finite():
        raise ValueError('nonfinite numeric value')
    return result


def norm(value):
    return ' '.join(str(value).split()).casefold()


def identifiers(text):
    """Mask only explicit identifiers, retaining character offsets for diagnostics."""
    months = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
    # A bare four-digit amount is still a claim. A year needs an explicit
    # temporal context, and currency signs / amount units retain precedence.
    year = r'(?<![\w$£€¥.])(?:19|20|21)\d{2}'
    year_end = (r'(?!\w|\.\d|\s*(?:%|shares?\b|dollars?\b|USD\b|EUR\b|GBP\b|'
                r'units?\b|million\b|billion\b|thousand\b|basis points?\b|bps\b))')
    patterns = [r'\b\d{4}-\d\d-\d\d(?:T[\d:.+\-]+(?:Z)?)?',
                r'\b\d{10}-\d{2}-\d{6}\b',
                r'\bCIK\s*[:=]?\s*\d+\b', r'\b(?:line|lines)\s+\d+(?:\s*[-–,]\s*\d+)*',
                r'\bsection\s+\d+(?:\.\d+)*\b',
                r'\b(?:Form\s+)?(?:10-(?:K|Q|12[BG])(?:/A)?|8-K|Form\s+4)\b',
                r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',
                r'\b'+months+r'\s+'+year+year_end,
                year+r'\s*[-–/]\s*(?:\d{2}|(?:19|20|21)\d{2})\s+(?:fiscal\s+)?(?:season|years?)\b',
                r'\b(?:in|during|since|until|before|after|through|throughout|from)\s+'+year+year_end,
                r'\b(?:(?:calendar|fiscal)\s+year|year|FY|date)\s*:?\s*'+year+year_end,
                year+r'\s+(?:(?:calendar|fiscal)\s+year|season)\b',
                r'(?<=SHA256: )[a-fA-F0-9]{64}\b',
                r'(?m)^\s*#{1,6}\s+\d+\.\s*',
                # These pipe-delimited fields identify sources, not amounts.
                r'\|\s*(?:filing|section|date|tag|period|start|end|unit)\s+[^|]*',
                r'looked in \[[^\]]*\]',
                r'\b(?:us-gaap|ifrs-full|dei):[A-Za-z0-9_]+']
    for pattern in patterns:
        text = re.sub(pattern, lambda m: ' '*len(m[0]), text, flags=re.I)
    return text


def tokens(text):
    return list(NUMBER.finditer(identifiers(text)))


class Failure(ValueError):
    def __init__(self, path, line, text, amount, source, reason):
        super().__init__(f'{path}:line {line}: unsupported/mismatched amount {amount!s}; '
                         f'claimed source: {source}; {reason}\nWorksheet line: {text}')


class Audit:
    def __init__(self, worksheet, rules_path=ROOT/'spinoff_worksheet.txt'):
        self.path = Path(worksheet).resolve()
        self.folder = self.path.parent
        self.lines = self.path.read_text(encoding='utf-8-sig').splitlines()
        self.rules_path = Path(rules_path)
        self.rules_text = self.rules_path.read_text(encoding='utf-8-sig')
        self.fields = {}
        for line in self.lines:
            if ': ' in line:
                key, value = line.split(': ', 1)
                self.fields.setdefault(key.casefold(), []).append(value)
        self.computed = {}
        self.source_cache = {}
        self.rows_cache = {}
        self.line = 0
        self.text = ''

    def fail(self, reason, amount=None, source=None):
        if amount is None:
            found = tokens(self.text)
            amount = found[0][0] if found else '(claim)'
        if source is None:
            source = self.text.split('| filing ', 1)[-1].split(' | date ', 1)[0] if '| filing ' in self.text else self.text.split(':', 1)[0]
        raise Failure(self.path, self.line, self.text, amount, source, reason)

    def require(self, condition, reason, amount=None, source=None):
        if not condition:
            self.fail(reason, amount, source)

    def rule(self, pattern):
        found = re.findall(pattern, self.rules_text, re.I)
        self.require(len(found) == 1, 'missing or ambiguous worksheet rule', source=str(self.rules_path))
        return dec(found[0])

    def field(self, name):
        values = self.fields.get(name.casefold(), [])
        self.require(len(values) == 1, 'missing or ambiguous printed input '+name)
        return values[0].split(' | ', 1)[0]

    def input(self, name):
        value = self.field(name)
        self.require(not any(word in value.lower() for word in UNKNOWN), 'required printed input is unavailable: '+name)
        match = NUMBER.match(value)
        self.require(match is not None, 'missing numeric printed input '+name)
        return dec(match[0])

    def exact(self, actual, expected):
        # Decimal arithmetic and the writer's Decimal precision: no broad tolerance.
        self.require(dec(actual) == dec(expected), 'expected '+str(expected), actual)

    def local(self, value):
        path = Path(value.strip().strip('"'))
        path = path if path.is_absolute() else self.folder/path
        path = path.resolve()
        self.require(path.is_relative_to(self.folder), 'filing path escapes the saved run', source=str(path))
        self.require(path.is_file(), 'saved source file does not exist', source=str(path))
        return path

    def fact_records(self, value):
        if isinstance(value, dict):
            if 'tag' in value and ('val' in value or 'value' in value):
                yield value
            for item in value.values():
                yield from self.fact_records(item)
        elif isinstance(value, list):
            for item in value:
                yield from self.fact_records(item)

    def facts(self):
        tag = re.search(r'\| tag ([^|]+)', self.text)
        period = re.search(r'\| period (\d{4}-\d\d-\d\d)(?: through (\d{4}-\d\d-\d\d))?', self.text)
        unit = re.search(r'\| unit ([^|]+)', self.text)
        scale = re.search(r'\| scale ([+-]?\d+)\b', self.text)
        self.require(all((tag, period, unit, scale)), 'facts claim requires exact tag, period, unit and scale')
        records = list(self.fact_records(json.loads(self.local('facts.json').read_text(encoding='utf-8-sig'))))
        start, end = (period[1], period[2]) if period[2] else (None, period[1])
        records = [r for r in records if r['tag'] == tag[1].strip() and r.get('start') == start
                   and r.get('end', r.get('instant')) == end and r.get('unit') == unit[1].strip()
                   and int(r.get('scale', 0)) == int(scale[1])]
        self.require(bool(records), 'no exact facts-file tag/period/unit/scale match')
        claim = NUMBER.match(self.text.split(': ', 1)[1])
        self.require(claim is not None, 'facts claim lacks a numeric value')
        body = self.text.split(': ', 1)[1].split(' | ', 1)[0]
        self.require(body in (claim[0], claim[0]+' '+unit[1].strip()),
                     'printed facts value unit/scale differs from the cited record', claim[0])
        self.require(all(dec(r.get('val', r.get('value'))) == dec(claim[0]) for r in records), 'facts-file value mismatch', claim[0])
        remainder = re.sub(r'\| scale [+-]?\d+\b', '', self.text)
        self.require(len(tokens(remainder)) == 1, 'unverified additional numeric facts claim')

    def filing_paths(self, source):
        paths = []
        for part in source.split('; '):
            explicit = re.search(r'(?:[A-Za-z]:[\\/]|[^\s]+[\\/])[^|]*?\.(?:txt|html?|xml)\b|\b[^\s]+\.(?:txt|html?|xml)\b', part)
            if explicit:
                value = explicit[0]
                # Absolute Windows paths may contain spaces before the filename.
                drive = re.search(r'[A-Za-z]:[\\/].*\.(?:txt|html?|xml)\b', part)
                path = self.local(drive[0] if drive else value)
                if path.suffix.lower() in ('.html', '.htm') and path.with_suffix('.txt').is_file():
                    path = path.with_suffix('.txt')
                paths.append(path)
            else:
                acc = re.search(r'\b\d{10}-\d{2}-\d{6}\b', part)
                self.require(acc is not None, 'numeric claim lacks a saved filing path/accession')
                found = sorted(self.folder.glob('worksheet_sources/**/'+acc[0]+'*.txt'))
                self.require(len(found) == 1, 'accession does not resolve to exactly one saved filing')
                paths.extend(found)
        return paths

    def saved_text(self, path):
        if path not in self.source_cache:
            text = path.read_text(encoding='utf-8-sig')
            if path.suffix.lower() in ('.html', '.htm'):
                from bs4 import BeautifulSoup
                text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
            self.source_cache[path] = text
        return self.source_cache[path]

    def passage(self, path, section):
        lines = self.saved_text(path).splitlines()
        match = re.fullmatch(r'lines? (\d+)(?:[-–](\d+))?', section, re.I)
        if match:
            first, last = int(match[1]), int(match[2] or match[1])
            self.require(1 <= first <= last <= len(lines), 'filing line identifier is out of range')
            return '\n'.join(lines[first-1:last])
        positions = [i for i, text in enumerate(lines) if norm(text) == norm(section)]
        self.require(len(positions) == 1, 'saved filing section is missing or ambiguous: '+section)
        first = positions[0]
        # Bound the passage at the next distinct heading, never search the whole filing.
        def heading(text):
            words = text.strip().split()
            return bool(words) and len(text) <= 140 and (text.isupper() or
                (len(words) > 1 and all(w[:1].isupper() or w in ('of', 'the', 'and', 'to', 'for', 'in', 'our') for w in words)))
        last = next((i for i in range(first+1, len(lines)) if heading(lines[i])), len(lines))
        return '\n'.join(lines[first:last])

    def scaled_filing_amount(self, amount, paths, passage):
        """Reconcile only an explicitly cited inline-XBRL display and scale.

        The worksheet's base-unit marker is not itself scale evidence. Read the
        original saved HTML, bind its tag, period, unit and consolidated context,
        and require its actual table row inside the cited text passage.
        """
        if '| scale base units' not in self.text:
            return False
        evidence = re.search(r'\| evidence ((?:us-gaap|ifrs-full|dei):\w+):\s*('+NUM+r')\s*$', self.text)
        period = re.search(r'\| period (\d{4}-\d\d-\d\d) through (\d{4}-\d\d-\d\d)', self.text)
        unit = re.search(r'\| unit ([A-Z]{3})\b', self.text)
        body = self.text.split(': ', 1)[1].split(' | ', 1)[0]
        if not all((evidence, period, unit)) or body != str(amount)+' '+unit[1]:
            return False
        if dec(evidence[2]) not in {dec(t[0]) for t in tokens(passage)}:
            return False
        run_path = self.folder/'run.json'
        run = json.loads(run_path.read_text(encoding='utf-8-sig')) if run_path.is_file() else {}
        cik = str(run.get('company', {}).get('cik', run.get('cik', ''))).lstrip('0')
        from bs4 import BeautifulSoup
        matched = []
        for path in paths:
            html_paths = [path] if path.suffix.lower() in ('.html', '.htm') else [path.with_suffix('.html'), path.with_suffix('.htm')]
            for html in html_paths:
                if not html.is_file():
                    continue
                html = self.local(str(html))
                soup = BeautifulSoup(html.read_text(encoding='utf-8-sig'), 'html.parser')
                def tags(name, scope=soup):
                    return scope.find_all(lambda t: t.name and t.name.split(':')[-1].lower() == name)
                contexts = {t.get('id'): t for t in tags('context')}
                units = {t.get('id'): t for t in tags('unit')}
                for node in tags('nonfraction'):
                    if node.get('name') != evidence[1]:
                        continue
                    context, currency = contexts.get(node.get('contextref')), units.get(node.get('unitref'))
                    if context is None or currency is None or tags('segment', context) or tags('scenario', context):
                        continue
                    starts, ends = tags('startdate', context), tags('enddate', context)
                    ids, measures = tags('identifier', context), tags('measure', currency)
                    if (len(starts) != 1 or len(ends) != 1 or len(ids) != 1 or len(measures) != 1
                        or starts[0].get_text(strip=True) != period[1] or ends[0].get_text(strip=True) != period[2]
                        or (cik and ids[0].get_text(strip=True).lstrip('0') != cik)
                        or measures[0].get_text(strip=True) != 'iso4217:'+unit[1] or tags('divide', currency)):
                        continue
                    row = node.find_parent('tr')
                    if row is None or norm(row.get_text(' ', strip=True)) not in norm(passage.replace('|', ' ')):
                        continue
                    literal = node.get_text('', strip=True)
                    if not re.fullmatch(NUM, literal) or dec(literal) != dec(evidence[2]):
                        continue
                    # No default exponent: reconciliation requires saved scale.
                    scale = node.get('scale')
                    if scale is None or not re.fullmatch(r'[+-]?\d{1,2}', scale):
                        continue
                    value = dec(literal) * Decimal(10)**int(scale)
                    if node.get('sign') == '-':
                        value = -value
                    matched.append(value)
        return bool(matched) and all(value == dec(amount) for value in matched)

    def filing(self):
        source = re.search(r'\| filing (.*?) \| section (.*?) \| date ', self.text)
        self.require(source is not None, 'numeric claim lacks filing and line/section provenance')
        paths = self.filing_paths(source[1])
        passage = '\n'.join(self.passage(path, source[2]) for path in paths)
        amounts = {dec(t[0]) for t in tokens(passage)}
        claims = tokens(self.text)
        for index, claim in enumerate(claims):
            supported = dec(claim[0]) in amounts
            if not supported and index == 0:
                supported = self.scaled_filing_amount(claim[0], paths, passage)
            self.require(supported, 'amount not found in the cited saved filing passage or explicit matching XBRL scale', claim[0], source[1]+' / '+source[2])
        # Also audit spelled-out numerical claims in a filing-supported sentence.
        words = set(m[0].casefold() for m in WORDS.finditer(identifiers(passage)))
        for claim in WORDS.finditer(identifiers(self.text)):
            self.require(claim[0].casefold() in words, 'written numeric amount not found in the cited passage', claim[0])

    def rows(self, instrument, start, end, volumes=False):
        self.require(not closes_reader._when_issued(instrument), 'when-issued instrument is forbidden', source=instrument)
        key = instrument, start, end, volumes
        if key not in self.rows_cache:
            if volumes:
                result = closes_reader.daily_close_volumes(instrument, start, end)
            else:
                result = closes_reader.daily_closes(instrument, start, end)
            self.require(isinstance(result, list) and bool(result), 'reader data unavailable', source=f'CLOSES_FILE {instrument} {start} through {end}')
            self.require(len({r['date'] for r in result}) == len(result), 'duplicate reader dates')
            self.require(all(start <= r['date'] <= end and (not volumes or r.get('instrument') == instrument) for r in result), 'reader instrument/date provenance mismatch')
            self.rows_cache[key] = sorted(result, key=lambda row: row['date'])
        return self.rows_cache[key]

    def price(self):
        found = re.search(r'\| filing CLOSES_FILE (\S+) \| section (.*?) \| date (\d{4}-\d\d-\d\d)', self.text)
        self.require(found is not None, 'price lacks exact instrument/date')
        instrument, section, day = found.groups()
        rows = self.rows(instrument, day, day, 'Volume' in section)
        if self.text.startswith('First official close:'):
            distribution = date.fromisoformat(self.field('distribution date'))
            end = re.search(r'run date: (\d{4}-\d\d-\d\d)', '\n'.join(self.lines))
            self.require(end is not None, 'first-close verification requires printed run date')
            first = self.rows(instrument, (distribution+timedelta(days=1)).isoformat(), end[1])[0]
            self.require(first['date'] == day, 'not the first exported official close after distribution', day)
        claims = tokens(self.text)
        body = self.text.split(' | ', 1)[0]
        if 'volume' in body.casefold() and 'close' in body.casefold():
            self.require(len(claims) == 2 and rows[0].get('volume') is not None, 'close/volume claim needs both reader values')
            self.exact(claims[0][0], rows[0]['close']); self.exact(claims[1][0], rows[0]['volume'])
        else:
            self.require(len(claims) == 1, 'unverified extra price/volume number')
            key = 'volume' if body.casefold().startswith('volume:') else 'close'
            self.require(rows[0].get(key) is not None, 'reader '+key+' unavailable')
            self.exact(claims[0][0], rows[0][key])
        currency = re.search(r'\b([A-Z]{3})\s*\| filing', self.text)
        if currency:
            self.require(closes_reader.instrument_currency(instrument) == currency[1], 'reader currency mismatch')

    def liquidity(self):
        purchase = self.rule(r'Client purchase size:\s*\$([\d,]+(?:\.\d+)?)')
        claimed = re.search(r'client purchase ('+NUM+r')', self.text)
        self.require(claimed is not None, 'liquidity lacks printed client purchase')
        self.exact(claimed[1], purchase)
        remaining = self.text[:claimed.start(1)]+self.text[claimed.end(1):]
        med = re.search(r'median daily (dollar volume|close-times-volume) ('+NUM+r')', self.text)
        if not med:
            self.require(not tokens(remaining), 'unverified liquidity number')
            return
        interval = re.search(r'reader interval (\d{4}-\d\d-\d\d) through (\d{4}-\d\d-\d\d)', self.text)
        instrument = re.search(r'export instrument ([^;\s]+)', self.text)
        observed = re.search(r'observed dates (\d{4}-\d\d-\d\d) through (\d{4}-\d\d-\d\d)', self.text)
        sessions = re.search(r'sessions (\d+)\b', self.text)
        self.require(all((interval, instrument, observed, sessions)), 'liquidity lacks exact instrument, interval, dates or count')
        rows = self.rows(instrument[1], interval[1], interval[2], True)
        self.require(all(r.get('volume') is not None for r in rows), 'reader volume unavailable')
        expected = median([dec(r['close'])*dec(r['volume']) for r in rows])
        self.exact(med[2], expected); self.exact(sessions[1], len(rows))
        self.require((observed[1], observed[2]) == (rows[0]['date'], rows[-1]['date']), 'liquidity observed date mismatch')
        currency = closes_reader.instrument_currency(instrument[1])
        if med[1] == 'dollar volume':
            self.require(currency == 'USD', 'USD dollar volume lacks reader currency evidence')
        elif currency != 'USD':
            self.require('price currency is not evidenced as USD' in self.text and
                         'client USD purchase/median comparison not computed' in self.text,
                         'missing currency limitation for quote-unit liquidity')
        ratio = re.search(r'purchase/median ('+NUM+r')%', self.text)
        if ratio:
            self.require(currency == 'USD' and expected > 0, 'purchase ratio requires comparable USD volume')
            self.exact(ratio[1], purchase/expected*100)
        relative = re.search(r'client purchase is (smaller than|equal to|larger than)', self.text)
        if relative:
            wanted = 'smaller than' if purchase < expected else 'equal to' if purchase == expected else 'larger than'
            self.require(currency == 'USD' and relative[1] == wanted, 'purchase/median comparison mismatch')
        # Check every number, not just the fields recognized above.
        residue = self.text
        for pattern in (r'client purchase '+NUM, r'median daily (?:dollar volume|close-times-volume) '+NUM,
                        r'sessions \d+\b', r'purchase/median '+NUM+r'%'):
            residue = re.sub(pattern, '', residue)
        self.require(not tokens(residue), 'unverified additional liquidity amount')

    def insider(self):
        source = re.search(r'\| filing (.*?) \| section nonDerivativeTransaction/transactionCoding', self.text)
        self.require(source is not None, 'insider count lacks transaction evidence')
        count = 0
        run = json.loads(self.local('run.json').read_text(encoding='utf-8-sig'))
        cik = str(run['company']['cik']).lstrip('0')
        paths = self.filing_paths(source[1])
        self.require(len(set(paths)) == len(paths), 'duplicate Form 4 source')
        for path in paths:
            xml = path.with_suffix('.xml') if path.with_suffix('.xml').is_file() else path
            root = ET.fromstring(xml.read_text(encoding='utf-8-sig'))
            for node in root.iter():
                node.tag = node.tag.rsplit('}', 1)[-1]
            self.require(root.findtext('.//issuerCik', '').lstrip('0') == cik, 'Form 4 issuer mismatch')
            for tx in root.findall('.//nonDerivativeTransaction'):
                if tx.findtext('./transactionCoding/transactionCode') == 'P' and tx.findtext('./transactionAmounts/transactionAcquiredDisposedCode/value') == 'A':
                    count += 1
        for claim in tokens(self.text):
            self.exact(claim[0], count)

    def arithmetic(self, label, body):
        if label == 'owner cash flow arithmetic':
            if body == 'not computed':
                return
            expected_inputs = [self.input(k) for k in ('after tax earnings', 'depreciation', 'capital spending')]
            match = re.fullmatch('('+NUM+r') \+ ('+NUM+r') - ('+NUM+r') = ('+NUM+r')', body)
            self.require(match is not None, 'malformed owner cash flow arithmetic')
            for raw, expected in zip(match.groups()[:3], expected_inputs):
                self.exact(raw, expected)
            result = expected_inputs[0]+expected_inputs[1]-expected_inputs[2]
            self.exact(match[4], result)
            # Inputs must share explicit filing-supported annual period/unit/basis.
            signatures = []
            for name in ('after tax earnings', 'depreciation', 'capital spending'):
                printed = self.fields[name][0]
                p = re.search(r'\| period (\d{4}-\d\d-\d\d) through (\d{4}-\d\d-\d\d)', printed)
                u = re.search(r'\| unit ([^|]+)', printed)
                self.require(p is not None and u is not None, 'annual calculation lacks printed input period/unit')
                self.require(330 <= (date.fromisoformat(p[2])-date.fromisoformat(p[1])).days <= 380, 'annual calculation cannot use quarterly inputs')
                signatures.append((p[1], p[2], u[1].strip()))
            self.require(len(set(signatures)) == 1, 'owner cash flow input periods/units differ')
            self.computed['owner cash flow'] = result
            return
        if label in ('low case', 'mid case'):
            case = label.split()[0]
            rate = self.rule(case+r' case required return:\s*([\d.]+)%')/100
            if body == f'owner cash flow not computed / {rate} = not computed':
                return
            match = re.fullmatch(r'owner cash flow ('+NUM+r') / ('+NUM+r') = ('+NUM+r')', body)
            # Decimal textual formatting may differ for unavailable operands.
            if not match and body.startswith('owner cash flow not computed / ') and body.endswith(' = not computed'):
                self.exact(body.split(' / ')[1].split(' = ')[0], rate)
                return
            self.require(match is not None and 'owner cash flow' in self.computed, 'value requires verified owner cash flow')
            self.exact(match[1], self.computed['owner cash flow']); self.exact(match[2], rate)
            result = self.computed['owner cash flow']/rate
            self.exact(match[3], result); self.computed[label] = result
            return
        if label in ('low case per share', 'mid case per share'):
            case = label.replace(' per share', '')
            incomplete = re.fullmatch('('+NUM+r') / not known = not computed', body)
            if incomplete:
                self.require(self.field('shares outstanding') == 'not known',
                             'incomplete per-share calculation requires shares outstanding marked not known')
                self.require(case in self.computed, 'incomplete per-share calculation lacks verified total')
                self.exact(incomplete[1], self.computed[case])
                return
            if not tokens(body):
                self.require(body == 'not computed / not known = not computed', 'invalid unavailable per-share expression')
                return
            missing = re.fullmatch(r'not computed / ('+NUM+r') = not computed', body)
            if missing:
                self.exact(missing[1], self.input('shares outstanding'))
                return
            match = re.fullmatch('('+NUM+r') / ('+NUM+r') = ('+NUM+r')', body)
            self.require(match is not None and case in self.computed, 'per-share value lacks verified total')
            self.exact(match[1], self.computed[case]); self.exact(match[2], self.input('shares outstanding'))
            result = self.computed[case]/self.input('shares outstanding')
            self.exact(match[3], result); self.computed[label] = result
            return
        if label == 'price against low case':
            if body == 'not computed':
                return
            match = re.fullmatch(r'current price ('+NUM+r'); low-case value per share ('+NUM+r'); difference ('+NUM+r')', body)
            self.require(match is not None and 'low case per share' in self.computed, 'comparison lacks verified value')
            close, low = self.input('price'), self.computed['low case per share']
            self.exact(match[1], close); self.exact(match[2], low); self.exact(match[3], low-close)

    def verify(self):
        for self.line, self.text in enumerate(self.lines, 1):
            claims = tokens(self.text)
            label, _, body = self.text.partition(': ')
            label = label.casefold()
            try:
                if '| tag ' in self.text or '| facts facts.json' in self.text:
                    self.facts()
                elif label in ('low case required return', 'mid case required return'):
                    expected = self.rule(label+r':\s*([\d.]+)%')
                    self.require(re.fullmatch('('+NUM+r')%', body) is not None, 'invalid required return')
                    self.exact(body[:-1], expected)
                elif label in ('owner cash flow arithmetic', 'low case', 'mid case', 'low case per share', 'mid case per share', 'price against low case'):
                    self.arithmetic(label, body)
                elif label == 'liquidity':
                    self.liquidity()
                elif label == 'insider purchases' and claims:
                    self.insider()
                elif label == 'market value' and claims and 'price times disclosed outstanding shares' in self.text:
                    self.require(len(claims) == 1, 'extra market value number')
                    self.exact(claims[0][0], self.input('price')*self.input('shares outstanding'))
                elif '| filing CLOSES_FILE ' in self.text and claims:
                    self.price()
                elif label == 'conviction' and claims:
                    self.require(len(claims) == 1, 'extra conviction number')
                    self.exact(claims[0][0], self.rule(r'low-case value is at least ([\d.]+) times'))
                    low = self.computed.get('low case per share')
                    price = self.field('price') if low is not None else 'not computed'
                    available = not any(word in price.lower() for word in UNKNOWN)
                    expected = 'HIGH' if low is not None and available and low >= dec(claims[0][0])*self.input('price') else 'LOW'
                    self.require(body.split(' | ', 1)[0] == expected, 'conviction does not follow verified inputs')
                elif label == 'deciding number' and claims:
                    self.require('low case per share' in self.computed and len(claims) == 1, 'deciding number lacks verified value')
                    self.exact(claims[0][0], self.computed['low case per share'])
                elif claims or WORDS.search(identifiers(self.text)):
                    # Never treat a registry, generated audit JSON or quotation alone as evidence.
                    self.filing()
            except Failure:
                raise
            except (ValueError, TypeError, KeyError, OSError, ArithmeticError, ET.ParseError) as exc:
                self.fail(type(exc).__name__+': '+str(exc))


def desk_report_copy(path):
    """Identify the saved desk container by its manifest and component boundaries."""
    manifest_path = path.parent/'desk_manifest.json'
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    team_path = path.parent/'verified_report.md'
    if not team_path.is_file():
        return False
    text = path.read_text(encoding='utf-8-sig')
    team_lines = team_path.read_text(encoding='utf-8-sig').splitlines()
    return (bool(team_lines)
            and text.startswith('Filings read: '+'; '.join(manifest['filings'])+'\n\n'+team_lines[0]+'\n')
            and re.search(r'^## .*View and Conviction\s*$', text, re.M) is not None)


def verify(worksheet, rules_path=ROOT/'spinoff_worksheet.txt'):
    path = Path(worksheet)
    if path.name == 'desk_report.md':
        from one_desk import verify_desk_report
        verify_desk_report(path)
        return
    if desk_report_copy(path):
        from one_desk import verify_desk_report, compose, read_json
        try:
            verify_desk_report(path)
        except ValueError as exc:
            if str(exc) != 'Final report differs from the exact saved team report and worksheet view.':
                raise
            expected = compose(path.parent, read_json(path.parent/'desk_manifest.json')).decode('utf-8').splitlines()
            actual = path.read_text(encoding='utf-8-sig').splitlines()
            for index in range(max(len(expected), len(actual))):
                source = expected[index] if index < len(expected) else '(no source line)'
                line = actual[index] if index < len(actual) else '(missing line)'
                if line != source:
                    amounts = [m[0] for m in tokens(line)]
                    originals = [m[0] for m in tokens(source)]
                    changed = next((a for i, a in enumerate(amounts)
                                    if i >= len(originals) or a != originals[i]), '(text)')
                    raise Failure(path, index+1, line, changed, source,
                                  'does not match the exact saved source output') from exc
            raise
        return
    if path.name == 'verified_report.md' and (path.parent/'verifier_persona.txt').is_file():
        from one_desk import verify_team
        verify_team(path.parent)
        return
    Audit(worksheet, rules_path).verify()


def strings(value):
    if isinstance(value, str):
        yield value
        if value.lstrip().startswith(('{', '[')):
            try:
                yield from strings(json.loads(value))
            except ValueError:
                pass
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def output_paths(value, cwd):
    """Only actual worksheet paths in tool outputs/edits; never guess 'latest'."""
    result = set()
    filename = r'(?:spinoff_worksheet_[^\\/\s"<>|]+|desk_report)\.md'
    pattern = re.compile(r'(?:[A-Za-z]:[\\/]|/|(?:(?:research_data|runs)[\\/]))[^\r\n"<>|]*?'+filename+r'|\b'+filename)
    for text in strings(value):
        for match in pattern.finditer(text):
            path = Path(match[0])
            result.add((path if path.is_absolute() else Path(cwd)/path).resolve())
    return result


def hook(payload, rules_path=ROOT/'spinoff_worksheet.txt'):
    if not isinstance(payload, dict):
        raise ValueError('hook input must be a JSON object')
    cwd = Path(payload.get('cwd', ROOT))
    event = payload.get('hook_event_name')
    if event == 'PostToolUse':
        response = payload.get('tool_response', {})
        paths = output_paths(response, cwd)
        if payload.get('tool_name') in ('apply_patch', 'Write', 'Edit'):
            paths |= output_paths(payload.get('tool_input', {}), cwd)
        command = payload.get('tool_input', {})
        command = command.get('command', command.get('cmd', '')) if isinstance(command, dict) else str(command)
        if not paths and re.search(r'\bpython(?:\.exe)?\s+(?:-[Bu]\s+)*["\']?(?:[^\s"\']*[/\\])?spinoff_worksheet\.py["\']?\s+', command):
            raise ValueError('worksheet tool returned no actual saved Markdown output path; completion refused')
    elif event == 'Stop':
        paths = output_paths(payload.get('last_assistant_message', ''), cwd)
        transcript = payload.get('transcript_path')
        if transcript:
            # Read only the current user turn's tool outputs, including unified-exec polls.
            entries = [json.loads(line) for line in Path(transcript).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
            last_user = max((i for i, row in enumerate(entries) if
                             row.get('payload', {}).get('type') == 'user_message' or
                             (row.get('payload', {}).get('role') == 'user')), default=-1)
            for row in entries[last_user+1:]:
                item = row.get('payload', row)
                if item.get('type') in ('function_call_output', 'custom_tool_call_output', 'tool_result') or item.get('role') == 'tool':
                    paths |= output_paths(item, cwd)
    else:
        raise ValueError('unsupported hook event; expected PostToolUse or Stop')
    for path in sorted(paths):
        verify(path, rules_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('worksheet', nargs='?', type=Path)
    parser.add_argument('--rules', type=Path, default=ROOT/'spinoff_worksheet.txt')
    parser.add_argument('--hook', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.hook:
            if args.worksheet:
                raise ValueError('--hook receives the actual output path through stdin, not an argument')
            hook(json.load(sys.stdin), args.rules)
        else:
            if not args.worksheet:
                raise ValueError('a saved worksheet path is required')
            verify(args.worksheet, args.rules)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
