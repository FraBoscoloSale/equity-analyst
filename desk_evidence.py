"""Deterministic, run-scoped evidence records. Models select IDs, never metadata."""

import copy
import hashlib
import re

from claim_validation import numeric_record_errors


class EvidenceCatalog:
    def __init__(self, sources, research, financial):
        self.records, self.sources, self.line_ids = {}, {}, {}
        self.research_ids, self.financial_ids = set(), set()
        self.definition_ids, self.quarter_ids = set(), []
        for alias, source in sources.items():
            source_id = f"{source['form']}:{source['accession']}:{source['file']}"
            digest = hashlib.sha256((source_id + '\n' + '\n'.join(source['lines'].values())).encode('utf-8')).hexdigest()
            self.sources[source_id] = {key: copy.deepcopy(value) for key, value in source.items() if key != 'lines'}
            self.sources[source_id]['text_sha256'] = digest
            for line, text in source['lines'].items():
                evidence_id = f"text.{digest[:12]}.L{line}"
                self.line_ids[alias, line] = evidence_id
                self.records[evidence_id] = {'kind': 'text', 'source_id': source_id, 'line': line, 'text': text}

        def collect(groups):
            return {self.line_ids[alias, row['line']] for alias, data in groups.items()
                    for key in ('lines', 'business', 'mda') for row in data.get(key, [])}
        self.research_ids = collect(research['sources'])
        self.definition_ids = collect(financial['definition_sources'])
        self.financial_ids.update(self.definition_ids)
        self.sources['run.input'] = {'file': 'run.json', 'kind': 'user_input'}
        self.sources['step4'] = {'file': 'facts.json', 'kind': 'missing_input'}
        self.sources['calculator'] = {'file': 'metrics.json', 'kind': 'calculator'}
        for key, row in financial['facts'].items():
            record = copy.deepcopy(row)
            if key == 'price':
                record.pop('source', None)
                record['supplied_at'] = record.pop('observed_at')
                record.update(kind='user_input', source_id='run.input', input_field='price')
            else:
                source_id = f"xbrl:{row['ticker']}:{row['accn']}"
                self.sources[source_id] = {'kind': 'xbrl', 'accession': row['accn'], 'form': row['form'],
                                           'url': row['filing_url'], 'ticker': row['ticker']}
                record.update(kind='xbrl_fact', source_id=source_id)
            self.records[key] = record
            self.financial_ids.add(key)
        for key, row in financial['metrics'].items():
            self.add_calculation(key, row)

        # Step 6's saved matcher identifies candidates, not parsed numeric facts.
        # Re-read the actual referenced source lines and attach their date/column/
        # unit context. Never trust a cached quotation or synthesize a quarter value.
        release = sources.get('release')
        if release:
            headers = ('weeks ended', 'months ended', 'quarter ended', 'year ended',
                       'in millions', 'in thousands', 'except per share', 'unaudited')
            for concept, matches in financial['step6_quarter'].items():
                refs = set()
                for match in matches:
                    line = match['line']
                    if release['lines'].get(line) != match['text']:
                        raise ValueError(f"Stale Step 6 evidence at release line {line}; source text does not match.")
                    refs.add(self.line_ids['release', line])
                    # Header lines retain the release's actual periods and units.
                    for number, text in release['lines'].items():
                        if number <= line and any(marker in text.lower() for marker in headers):
                            refs.add(self.line_ids['release', number])
                    for text in match.get('context', []):
                        candidates = [n for n, original in release['lines'].items() if original == text]
                        if candidates:
                            number = min(candidates, key=lambda n: abs(n - line))
                            refs.add(self.line_ids['release', number])
                if refs:
                    key = f"quarter.{concept}"
                    self.records[key] = {'kind': 'release_evidence', 'source_id': self.records[next(iter(refs))]['source_id'],
                                         'label': f"Step 6 reported-quarter {concept} evidence (retain column and measure definitions)",
                                         'references': sorted(refs)}
                    self.quarter_ids.append(key)
                    self.financial_ids.add(key)
                    self.financial_ids.update(refs)
        self.add_peer_comparisons(financial)
        self.add_inventory_limitations(financial, research)

    def add_peer_comparisons(self, financial):
        self.sources['comparison'] = {'kind': 'comparison', 'file': 'metrics.json + peers.json',
                                      'scope': 'Available fiscal observations; not synchronized calendar periods.'}
        for subject_id in list(financial['metrics']):
            if not subject_id.startswith('subject.'):
                continue
            name = subject_id.removeprefix('subject.')
            peer_ids = sorted(key for key in financial['metrics'] if key.startswith('peer.') and key.endswith('.' + name))
            if not peer_ids:
                continue
            subject = self.records[subject_id]
            subject_label = financial['company']['ticker']
            greater, lower, equal, unavailable = [], [], [], []
            for key in peer_ids:
                peer = self.records[key]
                label = key.split('.')[1]
                if (subject['value'] is None or peer['value'] is None or subject['unit'] != peer['unit'] or
                        subject['definition'] != peer['definition'] or
                        numeric_record_errors(subject_id, self.records) or numeric_record_errors(key, self.records)):
                    unavailable.append(label)
                elif subject['value'] > peer['value']:
                    greater.append(label)
                elif subject['value'] < peer['value']:
                    lower.append(label)
                else:
                    equal.append(label)
            parts = []
            for labels, relation in ((greater, 'higher than'), (lower, 'lower than'), (equal, 'equal to')):
                if labels:
                    parts.append(relation + ' ' + ', '.join(labels))
            if unavailable:
                parts.append('not comparable with ' + ', '.join(unavailable) + ' from the supplied observations')
            statement = f"For the supplied fiscal periods, {subject_label}'s {name.replace('_', ' ')} is " + '; '.join(parts) + '.'
            key = 'comparison.' + name
            self.records[key] = {'kind': 'peer_comparison', 'source_id': 'comparison', 'statement': statement,
                                 'subject_id': subject_id, 'peer_ids': peer_ids, 'higher_than': greater,
                                 'lower_than': lower, 'equal_to': equal, 'unavailable': unavailable,
                                 'references': [subject_id] + peer_ids}
            self.financial_ids.add(key)

    def add_inventory_limitations(self, financial, research):
        self.sources['evidence.inventory'] = {'kind': 'inventory', 'file': 'evidence_catalog.json',
                                            'scope': 'Only the evidence extracted and supplied to the roles; not all disclosures in a filing.'}
        def limitation(name, label, present, role_scopes):
            if present:
                return
            key = 'missing.' + name
            self.records[key] = {'kind': 'limitation', 'source_id': 'evidence.inventory',
                                 'statement': label + ' is not available in the validated evidence supplied for this run. '
                                 'This is an extraction/input limitation, not a claim that the issuer did not disclose it.',
                                 'item': name, 'status': 'not_extracted', 'references': [],
                                 'required_for': (['Research Analyst'] if name in {'market_share', 'business', 'mda', 'earnings_release'} else
                                                  ['Financial Analyst'] if name.startswith(('quarterly_', 'peer.')) or name == 'price' else [])}
            for scope in role_scopes:
                scope.add(key)
        all_scopes = [self.research_ids, self.financial_ids]
        annual = research['sources'].get('annual', {})
        for section in ('business', 'mda'):
            limitation(section, 'The annual filing ' + section.upper() + ' section', bool(annual.get(section)), [self.research_ids])
        limitation('earnings_release', 'The earnings release', 'release' in research['sources'], [self.research_ids])
        # If explicit market-share percentages are supplied, do not assert absence.
        market_share = any(row['kind'] == 'text' and re.search(r'market.share.{0,80}\d[\d.,]*\s*%|'
                           r'\d[\d.,]*\s*%.{0,80}market.share', row['text'], re.I)
                           for key, row in self.records.items() if key in self.research_ids | self.financial_ids)
        limitation('market_share', 'A verified quantitative market-share measure', market_share, all_scopes)
        from datetime import date
        quarterly_cash = 'quarter.cash_from_operations' in self.records
        for key, row in self.records.items():
            if key.startswith('subject.') and row['kind'] == 'xbrl_fact' and 'NetCashProvidedByUsedInOperatingActivities' in row.get('tag', '') and row.get('start'):
                quarterly_cash |= 50 <= (date.fromisoformat(row['end']) - date.fromisoformat(row['start'])).days <= 130
        limitation('quarterly_cash_from_operations', 'Quarterly operating cash flow', quarterly_cash, [self.financial_ids])
        for item in ('revenue', 'diluted_eps'):
            limitation('quarterly_' + item, 'Reported-quarter ' + item.replace('_', ' '),
                       'quarter.' + item in self.records, [self.financial_ids])
        limitation('price', 'A user-supplied subject share price',
                   self.records.get('price', {}).get('kind') == 'user_input', [self.financial_ids])
        for key, row in list(self.records.items()):
            if row['kind'] == 'missing_input':
                self.financial_ids.add(key)
        # Peer price/valuation is a known input gap, not a missing filing number.
        if financial['step7_peers']:
            limitation('peer_prices', 'Peer share-price inputs for peer P/E calculations', False, [self.financial_ids])
        for ticker, data in financial['step7_peers'].items():
            limitation('peer.' + ticker, 'Verified financial data for requested peer ' + ticker,
                       'error' not in data, [self.financial_ids])

    def add_calculation(self, key, row):
        for ref in row['inputs']:
            if ref not in self.records:
                self.records[ref] = {'kind': 'missing_input', 'source_id': 'step4',
                                     'label': ref, 'reason': 'No aligned Step 4 fact or supplied price.',
                                     'required_for': ['Financial Analyst']}
                self.financial_ids.add(ref)
        source_id = 'calculator.extra' if key.startswith('extra.') else 'calculator'
        if source_id == 'calculator.extra':
            self.sources[source_id] = {'file': 'additional_calculations.json', 'kind': 'calculator'}
        self.records[key] = {**copy.deepcopy(row), 'kind': 'calculation', 'source_id': source_id,
                             'references': list(row['inputs'])}
        self.financial_ids.add(key)

    def select(self, keys):
        """Dependency closure for cited metrics/bundles; never import unknown IDs."""
        selected = {}
        def visit(key):
            if key not in self.records or key in selected:
                return
            selected[key] = copy.deepcopy(self.records[key])
            for ref in self.records[key].get('references', []):
                visit(ref)
        for key in sorted(keys):
            visit(key)
        return selected

    def source_records(self, evidence):
        return {key: copy.deepcopy(self.sources[key]) for key in sorted({r['source_id'] for r in evidence.values()})}

    def snapshot(self):
        # Do not expose uncited/unsupplied text (e.g. annual Risk Factors) as evidence.
        return {'sources': self.sources, 'evidence': self.select(self.research_ids | self.financial_ids)}

    def render(self, key, seen=None):
        """All quoted text, financial provenance and arithmetic come from records."""
        seen = set() if seen is None else seen
        if key in seen:
            return []
        seen.add(key)
        row = self.records[key]
        source = self.sources[row['source_id']]
        kind = row['kind']
        if kind == 'text':
            return [f"> {row['text']}", '',
                    f"[{source['file']}, line {row['line']}; {source['form']} {source['accession']}; "
                    f"filing report date {source['period']}]({source['url']})", '']
        if kind == 'xbrl_fact':
            period = f"{row.get('start', 'instant')} to {row['end']}"
            return [f"{row['ticker']} {row['label']}: {row['val']} {row['unit']} "
                    f"[{row['form']} {row['accn']}; {row['tag']}; {period}; {row['filing_url']}]", '']
        if kind == 'user_input':
            return [f"{row['label']}: {row['val']} {row['unit']} [user input --price; "
                    f"run.json /price; supplied at run {row['supplied_at']}; market observation time not supplied]", '']
        if kind == 'missing_input':
            return [f"{row['label']}: N/A ({row['reason']})", '']
        if kind == 'limitation':
            return [row['statement'], '']
        if kind == 'peer_comparison':
            lines = [row['statement'], '', 'Comparison scope: supplied fiscal periods and metric definitions; currencies and dates appear with each input.', '']
            for ref in row['references']:
                lines.extend(self.render(ref, seen))
            return lines
        if kind == 'calculation':
            value = f"{row['value']} {row['unit']}" if row['value'] is not None else f"N/A ({row['reason']})"
            lines = [f"{key}: {row['arithmetic']} = {value}. Definition: {row['definition']}", '']
        else:
            lines = [row['label'], '']
        for ref in row.get('references', []):
            lines.extend(self.render(ref, seen))
        return lines
