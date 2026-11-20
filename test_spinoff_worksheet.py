"""Fictional filings, prices, transport and judgment; no live calls or real export."""
import copy
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from openpyxl import Workbook

import closes_reader as reader
import spinoff_worksheet as w

RULES = '''Spinoff Worksheet
1. Facts
2. Why It May Be Cheap
3. Value Range
Owner cash flow:
After-tax earnings + depreciation - capital spending
Valuation method:
Owner cash flow / required return
Low case required return: 12%
Mid case required return: 10%
4. What the Parent Pushed Into It
5. The Parent After the Spin
6. Insiders and Catalysts
7. Can You Buy It
Client purchase size: $100,000
8. View and Conviction
HIGH:
- low-case value is at least 1.25 times the current price; and
- no input required to calculate the low case is "not known".
LOW:
- otherwise.
9. Not Known
'''
NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
AS_OF = NOW.date()
PRICE = [{'date': '2026-11-18', 'close': 70, 'instrument': 'UNIT.K', 'currency': 'USD'}]
VOLUMES = [{'date': '2026-11-17', 'close': 60, 'volume': 1000, 'instrument': 'UNIT.K'},
           {'date': '2026-11-18', 'close': 70, 'volume': 2000, 'instrument': 'UNIT.K'}]


def financial_html(section='CONSOLIDATED STATEMENTS OF INCOME AND CASH FLOWS', start='2025-10-01', end='2026-09-30', missing=()):
    contexts = (f'<xbrli:context id="annual"><xbrli:entity><xbrli:identifier>101</xbrli:identifier></xbrli:entity>'
                f'<xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>'
                f'<xbrli:context id="instant"><xbrli:entity><xbrli:identifier>101</xbrli:identifier></xbrli:entity>'
                f'<xbrli:period><xbrli:instant>{end}</xbrli:instant></xbrli:period></xbrli:context>')
    units = '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit><xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>'
    values = [('after_tax_earnings', 'us-gaap:NetIncomeLoss', 100), ('depreciation', 'us-gaap:Depreciation', 20),
              ('capital_spending', 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 10),
              ('shares_outstanding', 'dei:EntityCommonStockSharesOutstanding', 10)]
    cells = ''.join(f'<tr><td>{key}</td><td><ix:nonFraction name="{tag}" contextRef="{("instant" if key=="shares_outstanding" else "annual")}" unitRef="{("shares" if key=="shares_outstanding" else "usd")}">{value}</ix:nonFraction></td></tr>'
                    for key, tag, value in values if key not in missing)
    return f'<html>{contexts}{units}<h2>{section}</h2><table>{cells}</table></html>'


def form4(codes=('P', 'J', 'A', 'S'), cik='101'):
    txs = ''.join(f'<nonDerivativeTransaction><transactionDate><value>2026-11-01</value></transactionDate>'
                  f'<transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>'
                  '<transactionAmounts><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>'
                  '<transactionShares><value>50</value></transactionShares><transactionPricePerShare><value>10</value>'
                  '</transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>' for code in codes)
    return f'<ownershipDocument><issuer><issuerCik>{cik}</issuerCik></issuer><nonDerivativeTable>{txs}</nonDerivativeTable></ownershipDocument>'


def structured_response(contract):
    if contract['availability']['value_comparison_available']:
        return {'cheapness_view': 'supported' if contract['availability']['value_supports_cheapness'] else 'not_established',
                'cheapness_reason': 'valuation_comparison', 'next_filing_change': 'new_financial_evidence'}
    return {'cheapness_view': 'not_established', 'cheapness_reason': 'Cheapness relative to value is not established.',
            'next_filing_change': 'new_financial_evidence'}


class WorksheetTests(unittest.TestCase):
    def setUp(self):
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start(); self.addCleanup(guard.stop)
        guard = patch.object(reader, 'dotenv_values', side_effect=AssertionError('Real export forbidden'))
        guard.start(); self.addCleanup(guard.stop)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.rules_path = self.root/'rules.txt'; self.rules_path.write_text(RULES, encoding='utf-8')
        self.persona_path = self.root/'persona.txt'; self.persona_path.write_text('Original fictional persona. Do not calculate.', encoding='utf-8')
        self.folder = self.root/'saved'; self.folder.mkdir()
        (self.folder/'run.json').write_text(json.dumps({'company': {'ticker': 'UNIT', 'cik': '101', 'name': 'Example Unit'}}))
        (self.folder/'facts.json').write_text(json.dumps({'company': {'ticker': 'UNIT', 'cik': '101'}, 'current_period': {'start': '2025-10-01', 'end': '2026-09-30'}}))
        (self.folder/'filings.json').write_text(json.dumps({'10-K': {'form': '10-K', 'accessionNumber': '0000000101-26-000001', 'filingDate': '2026-10-15'}}))
        (self.folder/'10-K.html').write_text(financial_html(), encoding='utf-8')
        self.rules = w.Rules.read(self.rules_path)
        self.judge = Mock(side_effect=lambda persona, payload: structured_response(payload))
        self.price = Mock(return_value=copy.deepcopy(PRICE)); self.volumes = Mock(return_value=copy.deepcopy(VOLUMES))

    def judgment_contract(self, available=True):
        facts = self.financials()
        values = w.compute_values(facts, self.rules, PRICE[0] if available else reader.NOT_AVAILABLE)
        return w.judgment_payload(facts, values)

    def run_sheet(self, registry=None, **kwargs):
        return w.run_worksheet(self.folder, registry or {'candidates': {}}, self.rules_path, self.persona_path,
                               self.judge, reader=self.price, volume_reader=self.volumes, now=NOW, as_of=AS_OF, **kwargs)

    def financials(self, distribution='2026-10-01', raw=None):
        raw = raw or financial_html()
        return w.extract_financials([w.Document('annual.html', '10-K', 'accession', '2026-10-15', '101', '', raw)], '101', distribution, AS_OF)

    def registry(self):
        url = 'https://www.sec.gov/Archives/edgar/data/101/000000010126000002/info.htm'
        text = 'Information Statement\nSeparation\nParent: Example Holding\nDistribution ratio: one share for every two shares\nRecord date: 2026-01-01\nDistribution date: 2026-01-02\n'
        claims = {}
        for key, value, quote in [('parent', 'Example Holding', 'Parent: Example Holding'),
                                 ('distribution_ratio', 'one share for every two shares', 'Distribution ratio: one share for every two shares'),
                                 ('record_date', '2026-01-01', 'Record date: 2026-01-01'),
                                 ('distribution_date', '2026-01-02', 'Distribution date: 2026-01-02')]:
            claims[key] = {'value': value, 'literal': value, 'quote': quote, 'section': 'Separation', 'document_url': url,
                           'form': '10-12B', 'accession': '0000000101-26-000002', 'filing_date': '2026-01-03', 'filer_cik': '101'}
        claims['ticker'] = {'value': 'UNIT'}
        row = {'cik': '101', 'form': '10-12B', 'accession': '0000000101-26-000002', 'filing_date': '2026-01-03'}
        item = dict(claims['parent'], text=text)
        bundle = {'complete': True, 'information_statement': True, 'documents': [item]}
        entry = {'cik': '101', 'name': 'Example Unit', 'facts': claims, 'filings': [row]}
        return {'candidates': {'101': entry}, 'factors': {'window_days': 90}}, bundle

    def test_registry_match_fetches_and_saves_filing_using_existing_sec_inventory(self):
        registry, bundle = self.registry(); client = Mock()
        client.text.return_value = '<h1>Information Statement</h1><h2>Separation</h2><p>Parent: Example Holding</p>'
        with patch.object(w.seeker, 'filing_documents', return_value=bundle) as inventory:
            path = self.run_sheet(registry, sec_client=client)
        inventory.assert_called_once()
        text = path.read_text(encoding='utf-8')
        self.assertIn('Parent: Example Holding', text)
        self.assertIn('Distribution date: 2026-01-02', text)
        self.assertTrue(list((self.folder/'worksheet_sources').glob('*/*.txt')))
        self.assertTrue(list((self.folder/'worksheet_sources').glob('*/*.html')))

    def test_registry_form4_metadata_is_read_even_without_cached_insider_evidence(self):
        registry, bundle = self.registry(); entry = registry['candidates']['101']
        row = {'cik':'101', 'form':'4', 'accession':'0000000101-26-000003', 'filing_date':'2026-11-02'}
        entry['filings'].append(row); entry['insider_complete'] = True
        url = 'https://www.sec.gov/Archives/edgar/data/101/000000010126000003/ownership.xml'
        raw = form4()
        item = {'text':raw, 'document_url':url}
        ownership = {'complete':True, 'documents':[item]}
        client = Mock(); client.text.side_effect = lambda address: raw if address == url else '<h1>Information Statement</h1>'
        with patch.object(w.seeker,'filing_documents',side_effect=[bundle,ownership]) as inventory:
            path = self.run_sheet(registry,sec_client=client)
        self.assertEqual(inventory.call_count,2)
        self.assertIn('Insider purchases: 1',path.read_text())
        self.assertTrue(list((self.folder/'worksheet_sources').glob('*/*.xml')))

    def test_registry_rank_and_factor_points_are_not_worksheet_output(self):
        registry, bundle = self.registry()
        entry = registry['candidates']['101']
        entry.update(rank=7, factor_points={'example factor': 9876.54321})
        before = copy.deepcopy(registry)
        client = Mock(); client.text.return_value = '<h1>Information Statement</h1>'
        with patch.object(w.seeker, 'filing_documents', return_value=bundle):
            path = self.run_sheet(registry, sec_client=client)
        text = path.read_text(encoding='utf-8')
        audit = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        self.assertNotIn('Rank and points:', text)
        self.assertNotIn('rank_and_points', text)
        self.assertNotIn('9876.54321', text)
        self.assertNotIn('rank_and_points', audit['facts'])
        self.assertNotIn('factor_points', audit['facts'])
        self.assertIn('Parent: Example Holding', text)
        self.assertIn('Distribution date: 2026-01-02', text)
        self.assertIn('Owner cash flow arithmetic: 100 + 20 - 10 = 110', text)
        self.assertEqual(registry, before)

    def test_unranked_registry_does_not_add_missing_rank_placeholder(self):
        registry, bundle = self.registry()
        client = Mock(); client.text.return_value = '<h1>Information Statement</h1>'
        with patch.object(w.seeker, 'filing_documents', return_value=bundle):
            path = self.run_sheet(registry, sec_client=client)
        text = path.read_text(encoding='utf-8')
        audit = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        self.assertNotIn('Rank and points:', text)
        self.assertNotIn('rank_and_points', audit['facts'])
        self.assertNotIn('rank_and_points', audit['not_known'])

    def test_not_in_registry_spinoff_specific_facts_are_not_applicable(self):
        path = self.run_sheet()
        text = path.read_text(encoding='utf-8')
        for label in ('Parent', 'Distribution date', 'Debt raised', 'Parent after spin'):
            self.assertIn(label+': not applicable', text)
        self.judge.assert_called_once()

    def test_placeholder_registry_identity_does_not_establish_spinoff(self):
        registry = {'candidates': {'101': {'cik': '101', 'facts': {}, 'filings': [], 'stage': 'registered'}}}
        path = self.run_sheet(registry)
        text = path.read_text(encoding='utf-8')
        for label in ('Parent', 'Distribution ratio', 'Record date', 'Distribution date', 'Debt raised', 'Cash destination', 'Parent after spin'):
            self.assertIn(label+': not applicable', text)

    def test_supported_partial_spinoff_retains_unresolved_handling(self):
        registry, bundle = self.registry()
        entry = registry['candidates']['101']
        del entry['facts']['distribution_date']
        self.assertIs(w.registry_entry(registry, {'cik': '101', 'ticker': 'UNIT'}), entry)
        client = Mock(); client.text.return_value = '<h1>Information Statement</h1>'
        with patch.object(w.seeker, 'filing_documents', return_value=bundle):
            path = self.run_sheet(registry, sec_client=client)
        self.assertIn('Distribution date: not known', path.read_text(encoding='utf-8'))

    def test_parent_identity_alone_is_not_filing_supported_spinoff_evidence(self):
        registry, _ = self.registry()
        entry = registry['candidates']['101']
        entry['facts'] = {'parent': entry['facts']['parent']}
        self.assertIsNone(w.registry_entry(registry, {'cik': '101', 'ticker': 'UNIT'}))

    def test_saved_spinoff_finding_retained_without_resolved_distribution_facts(self):
        registry, _ = self.registry()
        entry = registry['candidates']['101']
        finding = dict(entry['facts']['parent'], kind='spinoff')
        entry['facts'] = {}; entry['findings'] = [finding]
        self.assertIs(w.registry_entry(registry, {'cik': '101', 'ticker': 'UNIT'}), entry)

    def test_annual_provenance_and_calculation_pass_unmodified_numeric_hook(self):
        import verify_spinoff_worksheet as verifier
        financials = self.financials(distribution=None)
        saved = json.loads((self.folder/'facts.json').read_text())
        saved['worksheet_inputs'] = [dict(tag=fact.tag, val=str(fact.value), start=fact.start or None,
                                         end=fact.date, unit=fact.unit, scale=0)
                                      for fact in financials.values() if fact.known]
        (self.folder/'facts.json').write_text(json.dumps(saved))
        # Instant SEC records omit start rather than using null.
        for row in saved['worksheet_inputs']:
            if row['start'] is None:
                del row['start']
        (self.folder/'facts.json').write_text(json.dumps(saved))
        before = (self.folder/'facts.json').read_bytes()
        path = self.run_sheet()
        text = path.read_text(encoding='utf-8')
        self.assertIn('| period 2025-10-01 through 2026-09-30 | unit USD', text)
        self.assertIn('| facts facts.json | tag us-gaap:NetIncomeLoss | scale 0', text)
        self.assertIn('Owner cash flow arithmetic: 100 + 20 - 10 = 110', text)
        # Verify the actual printed annual section against saved facts and
        # printed operands using the existing hook, without any rule changes.
        annual = text.split('## 3. Value Range\n', 1)[1].split('## 4.', 1)[0]
        price_line = next(line for line in text.splitlines() if line.startswith('Price: '))
        proof = self.folder/'annual_proof.md'; proof.write_text(price_line+'\n'+annual, encoding='utf-8')
        with patch.object(reader, 'daily_closes', return_value=[{'date': '2026-11-18', 'close': 70}]), \
             patch.object(reader, 'instrument_currency', return_value='USD'):
            verifier.verify(proof, self.rules_path)
        self.assertEqual((self.folder/'facts.json').read_bytes(), before)

    def test_worksheet_reference_leaves_frozen_view_bytes_unchanged(self):
        from live_run import freeze_view
        view = freeze_view(self.folder, {'direction': 'not computed', 'thesis': 'Frozen original'}, NOW)
        before = view.read_bytes()
        self.run_sheet()
        self.assertEqual(view.read_bytes(), before)
        self.assertTrue(list((self.folder/'live_references').glob('worksheet_*.json')))

    def test_scaled_inline_annual_inputs_use_normalized_saved_sec_values(self):
        import equity_desk as desk
        import verify_spinoff_worksheet as verifier
        raw = financial_html().replace(' unitRef=', ' scale="3" unitRef=')
        (self.folder/'10-K.html').write_text(raw, encoding='utf-8')
        extracted = self.financials(distribution=None, raw=raw)
        companyfacts = {'facts': {}}
        for fact in extracted.values():
            namespace, name = fact.tag.split(':')
            row = dict(val=int(fact.value), end=fact.date, filed='2026-10-15', accn='0000000101-26-000001')
            if fact.start:
                row['start'] = fact.start
            companyfacts['facts'].setdefault(namespace, {})[name] = {'units': {fact.unit: [row]}}
        saved = json.loads((self.folder/'facts.json').read_text())
        saved['worksheet_inputs'] = desk.worksheet_input_records(companyfacts, {'cik': '101'})
        (self.folder/'facts.json').write_text(json.dumps(saved))
        path = self.run_sheet(); text = path.read_text(encoding='utf-8')
        self.assertIn('After tax earnings: 100000 USD', text)
        self.assertIn('Owner cash flow arithmetic: 100000 + 20000 - 10000 = 110000', text)
        self.assertIn('| tag us-gaap:Depreciation | scale 0', text)
        audit = json.loads(path.with_suffix('.json').read_text())
        self.assertEqual(audit['facts']['depreciation']['source_scale'], 3)
        annual = text.split('## 3. Value Range\n', 1)[1].split('## 4.', 1)[0]
        price_line = next(line for line in text.splitlines() if line.startswith('Price: '))
        proof = self.folder/'scaled_annual_proof.md'; proof.write_text(price_line+'\n'+annual, encoding='utf-8')
        with patch.object(reader, 'daily_closes', return_value=[{'date': '2026-11-18', 'close': 70}]), \
             patch.object(reader, 'instrument_currency', return_value='USD'):
            verifier.verify(proof, self.rules_path)

    def test_missing_depreciation_is_unknown_and_never_combined_amortization(self):
        raw = financial_html().replace('us-gaap:Depreciation"', 'us-gaap:DepreciationDepletionAndAmortization"')
        (self.folder/'10-K.html').write_text(raw, encoding='utf-8')
        path = self.run_sheet(); text = path.read_text(encoding='utf-8')
        self.assertIn('Depreciation: not known', text)
        self.assertIn('Owner cash flow arithmetic: not computed', text)
        self.assertIn('Conviction: LOW', text)

    def test_null_saved_period_still_uses_supported_annual_filing_inputs(self):
        path = self.folder/'facts.json'
        saved = json.loads(path.read_text())
        saved['current_period'] = None
        path.write_text(json.dumps(saved))
        before = path.read_bytes()
        result = self.run_sheet()
        audit = json.loads(result.with_suffix('.json').read_text())
        self.assertEqual(audit['facts']['after_tax_earnings']['start'], '2025-10-01')
        self.assertEqual(str(audit['values']['owner_cash_flow']), '110')
        self.assertEqual(path.read_bytes(), before)

    def test_null_or_missing_period_without_annual_filing_keeps_annual_inputs_unknown(self):
        registry, bundle = self.registry()
        (self.folder/'filings.json').write_text(json.dumps({
            '10-Q': {'form': '10-Q', 'accessionNumber': '0000000101-26-000004', 'filingDate': '2026-11-06'},
            '8-K': {'form': '8-K', 'accessionNumber': '0000000101-26-000005', 'filingDate': '2026-11-06'}}))
        (self.folder/'10-Q.html').write_text(financial_html(start='2026-07-01'), encoding='utf-8')
        (self.folder/'8-K.txt').write_text('Item 2.02 Results of Operations\nQuarterly results.', encoding='utf-8')
        self.judge.side_effect = lambda persona, payload: structured_response(payload)
        client = Mock()
        client.text.return_value = '<h1>Information Statement</h1><h2>Separation</h2>'
        for period in ('null', 'missing'):
            with self.subTest(current_period=period):
                saved = {'company': {'ticker': 'UNIT', 'cik': '101'}}
                if period == 'null':
                    saved['current_period'] = None
                facts_path = self.folder/'facts.json'
                facts_path.write_text(json.dumps(saved))
                before = facts_path.read_bytes()
                with patch.object(w.seeker, 'filing_documents', return_value=bundle):
                    result = self.run_sheet(registry, sec_client=client)
                text = result.read_text(encoding='utf-8')
                audit = json.loads(result.with_suffix('.json').read_text())
                for key in ('after_tax_earnings', 'depreciation', 'capital_spending'):
                    self.assertEqual(audit['facts'][key]['value'], w.UNKNOWN)
                    self.assertEqual(audit['facts'][key]['start'], '')
                for key in ('owner_cash_flow', 'low_total', 'mid_total', 'low_per_share', 'mid_per_share', 'comparison', 'view'):
                    self.assertEqual(audit['values'][key], w.NC)
                self.assertIn('After tax earnings: not known', text)
                self.assertIn('Owner cash flow arithmetic: not computed', text)
                shares = audit['facts']['shares_outstanding']
                self.assertEqual(str(shares['value']), '10')
                self.assertIn('10-Q 0000000101-26-000004', shares['filing'])
                self.assertEqual(shares['date'], '2026-09-30')
                self.assertEqual(shares['classification'], 'reported after the spin')
                self.assertEqual(audit['facts']['distribution_date']['value'], '2026-01-02')
                self.assertEqual(str(audit['facts']['price']['value']), '70')
                # The evidenced distribution supplies a trading interval, not
                # an annual accounting period or a substituted quarterly period.
                self.price.assert_called_with('UNIT', '2026-01-03', AS_OF.isoformat())
                self.assertNotIn('liquidity observation period start: not known', text)
                self.assertEqual(facts_path.read_bytes(), before)

    def test_historical_pro_forma_and_reported_after_spin_follow_statement_period(self):
        self.assertEqual(self.financials('2026-10-01')['after_tax_earnings'].classification, 'historical')
        self.assertEqual(self.financials('2025-09-01')['after_tax_earnings'].classification, 'reported after the spin')
        pro = financial_html(section='UNAUDITED PRO FORMA STATEMENTS OF INCOME AND CASH FLOWS')
        self.assertEqual(self.financials(raw=pro)['after_tax_earnings'].classification, 'pro forma')
        # Publication after distribution alone does not make pre-spin results post-spin.
        self.assertEqual(w.classification('Statements of Income', '2025-01-01', '2025-12-31', '2026-01-01'), 'historical')

    def test_code_p_acquisitions_only_with_filing_and_section(self):
        raw = form4()
        doc = w.Document('ownership.xml', '4', 'ownership-accession', '2026-11-02', '101', raw, raw)
        fact, purchases = w.insider_purchases([doc, doc], '101', '2026-01-02', AS_OF, True)
        self.assertEqual(fact.value, 1); self.assertEqual(len(purchases), 1)
        self.assertIn('ownership-accession', purchases[0]['filing'])
        self.assertEqual(purchases[0]['section'], 'nonDerivativeTransaction/transactionCoding')

    def test_amended_or_incomplete_form4_coverage_is_unknown_not_zero(self):
        raw = form4(('J',))
        doc = w.Document('form4.xml', '4', 'a', '2026-11-02', '101', raw)
        self.assertEqual(w.insider_purchases([doc], '101', None, AS_OF, False)[0].value, w.UNKNOWN)
        amended = copy.copy(doc); amended.form = '4/A'; amended.accession = 'b'
        self.assertEqual(w.insider_purchases([doc, amended], '101', None, AS_OF, True)[0].value, w.UNKNOWN)

    def test_incomplete_form4_coverage_never_prints_observed_purchase_count(self):
        for codes in (('J',), ('P', 'P')):
            with self.subTest(codes=codes):
                raw = form4(codes)
                doc = w.Document('ownership.xml', '4', 'ownership-accession', '2026-11-02', '101', raw)
                fact, _ = w.insider_purchases([doc], '101', None, AS_OF, False)
                self.assertEqual(fact.value, w.UNKNOWN)
                self.assertIn('coverage is incomplete', fact.quote)
                # Form 4 is a form identifier; no observed count is disclosed.
                self.assertNotRegex(fact.quote.replace('Form 4', ''), r'\d|\bzero\b')
                self.assertNotIn('acquisitions observed:', fact.quote)
        fact, _ = w.insider_purchases([], '101', None, AS_OF, False)
        self.assertEqual(fact.value, w.UNKNOWN)
        self.assertNotRegex(fact.quote.replace('Form 4', ''), r'\d|\bzero\b')

    def test_complete_form4_coverage_preserves_sourced_zero_purchase_count(self):
        raw = form4(('J', 'A', 'S'))
        doc = w.Document('ownership.xml', '4', 'ownership-accession', '2026-11-02', '101', raw)
        fact, purchases = w.insider_purchases([doc], '101', None, AS_OF, True)
        self.assertEqual(fact.value, 0)
        self.assertEqual(purchases, [])
        self.assertEqual(fact.quote, 'P acquisitions observed: 0; full coverage True')
        self.assertEqual(fact.filing, '4 ownership-accession ownership.xml')
        self.assertEqual(fact.section, 'nonDerivativeTransaction/transactionCoding')

    def test_complete_form4_coverage_preserves_qualifying_purchase_count(self):
        raw = form4(('P', 'J', 'P'))
        doc = w.Document('ownership.xml', '4', 'ownership-accession', '2026-11-02', '101', raw)
        fact, purchases = w.insider_purchases([doc], '101', None, AS_OF, True)
        self.assertEqual(fact.value, 2)
        self.assertEqual(len(purchases), 2)
        self.assertEqual(fact.quote, 'P acquisitions observed: 2; full coverage True')
        self.assertEqual(fact.filing, '4 ownership-accession ownership.xml')
        self.assertTrue(all(p['filing'] == fact.filing for p in purchases))

    def test_owner_cash_flow_low_mid_and_price_arithmetic(self):
        facts = self.financials()
        values = w.compute_values(facts, self.rules, PRICE[0])
        self.assertEqual(values['owner_cash_flow'], Decimal(110))
        self.assertEqual(values['mid_total'], Decimal(1100))
        self.assertEqual(values['low_total'], Decimal(110)/Decimal('.12'))
        self.assertEqual(values['low_per_share'], Decimal(110)/Decimal('.12')/10)
        self.assertEqual(values['view'], 'more than the current price')
        self.assertEqual(values['conviction'], 'HIGH')

    def test_depreciation_amortization_tag_requires_annual_unit_and_source_context(self):
        tag = 'us-gaap:DepreciationAmortizationAndAccretionNet'
        raw = financial_html().replace('us-gaap:Depreciation"', tag+'"')
        raw = raw.replace('<td>depreciation</td>', '<td>Depreciation and amortization</td>')
        raw = raw.replace('unitRef="usd">20</ix:nonFraction>', 'unitRef="usd" scale="3">20</ix:nonFraction>')
        fact = self.financials(raw=raw)['depreciation']
        self.assertEqual(fact.value, Decimal(20000))
        self.assertEqual(fact.tag, tag)
        self.assertEqual((fact.start, fact.date, fact.unit, fact.source_scale),
                         ('2025-10-01', '2026-09-30', 'USD', 3))
        self.assertEqual(fact.filing, '10-K accession annual.html')
        self.assertEqual(fact.section, 'CONSOLIDATED STATEMENTS OF INCOME AND CASH FLOWS')
        self.assertEqual(fact.quote, tag+': 20')
        self.assertIn('Depreciation and amortization', raw)
        for name, invalid in {
            'quarter': raw.replace('<xbrli:startDate>2025-10-01', '<xbrli:startDate>2026-07-01'),
            'nonmonetary unit': raw.replace('iso4217:USD', 'xbrli:shares'),
            'wrong issuer': raw.replace('<xbrli:identifier>101', '<xbrli:identifier>202'),
            'missing period context': raw.replace('contextRef="annual"', 'contextRef="missing"'),
            'missing statement section': raw.replace('<h2>CONSOLIDATED STATEMENTS OF INCOME AND CASH FLOWS</h2>', ''),
        }.items():
            with self.subTest(name=name):
                facts = self.financials(raw=invalid)
                self.assertEqual(facts['depreciation'].value, w.UNKNOWN)
                self.assertEqual(w.compute_values(facts, self.rules, PRICE[0])['owner_cash_flow'], w.NC)

    def test_no_mixed_periods_units_or_classification_and_no_quarter_annualization(self):
        facts = self.financials(); facts['depreciation'].date = '2025-09-30'
        self.assertEqual(w.compute_values(facts, self.rules, PRICE[0])['low_total'], w.NC)
        quarter = financial_html(start='2026-07-01')
        self.assertEqual(self.financials(raw=quarter)['after_tax_earnings'].value, w.UNKNOWN)

    def test_unavailable_close_price_and_view_not_computed(self):
        self.price.return_value = 'not available'
        text = self.run_sheet().read_text(encoding='utf-8')
        self.assertIn('Price against low case: not computed', text)
        self.assertIn('View: not computed', text)
        self.assertIn('Conviction: LOW', text)

    def test_rates_client_size_and_conviction_multiple_read_from_rules(self):
        self.rules_path.write_text(RULES.replace('12%', '20%').replace('10%', '15%').replace('1.25 times', '2.00 times').replace('$100,000', '$25,000'))
        rules = w.Rules.read(self.rules_path)
        values = w.compute_values(self.financials(), rules, PRICE[0])
        self.assertEqual(values['low_total'], Decimal(550)); self.assertEqual(values['conviction'], 'LOW')
        self.assertEqual(rules.purchase, Decimal(25000))
        self.assertEqual(values['mid_total'], Decimal(110)/Decimal('.15'))

    def test_one_judgment_exact_persona_and_no_reader_history_or_liquidity_in_payload(self):
        def judgment(persona, payload):
            self.assertEqual(persona, self.persona_path.read_text())
            self.volumes.assert_not_called()
            self.assertEqual(set(payload), {'facts', 'availability', 'allowed_claims'})
            for forbidden in ('volume', 'UNIT.K', 'median', 'purchase_size', 'conviction'):
                self.assertNotIn(forbidden, w.dump(payload))
            return structured_response(payload)
        self.judge.side_effect = judgment
        self.run_sheet(); self.judge.assert_called_once(); self.volumes.assert_called_once()

    def test_numeric_model_output_rejected_without_retry(self):
        bad = structured_response(self.judgment_contract())
        bad['cheapness_reason'] = 'The calculated value is 999.'
        self.judge.return_value = bad; self.judge.side_effect = None
        with self.assertRaisesRegex(ValueError, 'number/calculation'):
            self.run_sheet()
        self.judge.assert_called_once()
        self.assertFalse(list(self.folder.glob('spinoff_worksheet_*.md')))
        audit = json.loads(next(self.folder.glob('worksheet_judgment_rejected_*.json')).read_text())
        self.assertEqual(audit['status'], 'rejected')
        self.assertEqual(audit['response'], bad)
        self.assertIn('cheapness_reason', audit['error'])
        self.assertNotIn('judgment', audit)

    def test_valid_qualitative_judgment_passes_unchanged(self):
        contract = self.judgment_contract(False)
        result = structured_response(contract)
        self.assertIs(w.validate_judgment(result, contract), result)
        text = '\n'.join(w.render_judgment(result, contract))
        self.assertIn('Cheapness relative to value is not established.', text)
        self.assertFalse(contract['availability']['value_comparison_available'])

    def test_unsupported_judgment_evidence_rejected_without_retry(self):
        bad = structured_response(self.judgment_contract())
        bad['cheapness_reason'] = 'separation_structure:invented_source'
        self.judge.side_effect = None; self.judge.return_value = bad
        with self.assertRaisesRegex(ValueError, 'unsupported claim or evidence ID'):
            self.run_sheet()
        self.judge.assert_called_once()
        self.assertFalse(list(self.folder.glob('spinoff_worksheet_*.md')))
        audit = json.loads(next(self.folder.glob('worksheet_judgment_rejected_*.json')).read_text())
        self.assertEqual(audit['status'], 'rejected')

    def test_malformed_judgment_blocks_and_empty_text_are_rejected(self):
        contract = self.judgment_contract(False)
        valid = structured_response(contract)
        malformed = [None, [], {'cheapness_view': 'not_established'}, dict(valid, extra='not allowed'),
                     dict(valid, cheapness_reason={'text': 'Qualitative prose'}),
                     dict(valid, cheapness_reason=''), dict(valid, next_filing_change=[]),
                     dict(valid, cheapness_view='unknown')]
        for result in malformed:
            with self.subTest(result=result), self.assertRaises(ValueError):
                w.validate_judgment(result, contract)

    def test_judgment_numeric_restatements_and_number_words_remain_rejected(self):
        contract = self.judgment_contract(False)
        for text in ('The price is 70.', 'twelve percent', 'zero purchases', 'twenty times earnings',
                     'a million dollars', '100 / 5', 'two plus three', 'a * b', 'double the value'):
            result = structured_response(contract); result['cheapness_reason'] = text
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'number/calculation'):
                w.validate_judgment(result, contract)

    def test_saved_output_unique_even_same_run_time_inputs_preserved(self):
        before = {p: p.read_bytes() for p in self.folder.iterdir()}
        rules_before, persona_before = self.rules_path.read_bytes(), self.persona_path.read_bytes()
        a = self.run_sheet(); saved = a.read_bytes(); b = self.run_sheet()
        self.assertNotEqual(a, b); self.assertEqual(a.read_bytes(), saved)
        for p, data in before.items():self.assertEqual(p.read_bytes(), data)
        self.assertEqual(self.rules_path.read_bytes(), rules_before)
        self.assertEqual(self.persona_path.read_bytes(), persona_before)
        self.assertIn(NOW.isoformat(), a.read_text())
        self.assertEqual(self.judge.call_count, 2)

    def test_liquidity_median_arithmetic_and_missing_volume_not_estimated(self):
        line, unknown = w.liquidity(VOLUMES, self.rules, '2026-11-01', '2026-11-19')
        self.assertIn('median daily dollar volume 100000', line)
        self.assertIn('purchase/median 100', line)
        self.assertTrue(unknown)  # No execution participation threshold in rules.
        bad = copy.deepcopy(VOLUMES); bad[0]['volume'] = None
        self.assertIn('median daily dollar volume not known', w.liquidity(bad, self.rules, '2026-11-01', '2026-11-19')[0])

    def test_existing_reader_volume_interface_resolves_instrument_read_only(self):
        path = self.root/'export.xlsx'
        workbook = Workbook(); sheet = workbook.active
        sheet.append(['Instrument', 'Date', 'Price Close', 'Volume'])
        sheet.append(['UNIT.K', datetime(2026, 11, 17), 60, 1000])
        sheet.append(['UNIT.K', datetime(2026, 11, 18), 70, 2000])
        workbook.save(path); workbook.close(); before = path.read_bytes()
        with patch.object(reader, 'dotenv_values', return_value={'CLOSES_FILE': str(path)}):
            rows = reader.daily_close_volumes_for_ticker('UNIT', '2026-11-17', '2026-11-18')
            self.assertEqual(rows, VOLUMES)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_volume_column_stays_unknown(self):
        path = self.root/'no_volume.xlsx'; workbook = Workbook(); sheet = workbook.active
        sheet.append(['Instrument', 'Date', 'Price Close']); sheet.append(['UNIT.K', datetime(2026, 11, 18), 70])
        workbook.save(path); workbook.close()
        with patch.object(reader, 'dotenv_values', return_value={'CLOSES_FILE': str(path)}):
            rows = reader.daily_close_volumes_for_ticker('UNIT', '2026-11-18', '2026-11-18')
        self.assertIsNone(rows[0]['volume'])

    def test_wrong_issuer_dimensional_financial_fact_not_accepted(self):
        wrong = financial_html().replace('<xbrli:identifier>101', '<xbrli:identifier>202')
        self.assertEqual(self.financials(raw=wrong)['after_tax_earnings'].value, w.UNKNOWN)
        dimensional = financial_html().replace('</xbrli:entity>', '<xbrli:segment>Subsidiary</xbrli:segment></xbrli:entity>')
        self.assertEqual(self.financials(raw=dimensional)['after_tax_earnings'].value, w.UNKNOWN)

    def test_conflicting_financial_amount_is_unknown(self):
        raw = financial_html()
        amended = raw.replace('>100</ix:nonFraction>', '>999</ix:nonFraction>')
        docs = [w.Document('a.html', '10-K', 'a', '2026-10-15', '101', '', raw),
                w.Document('b.html', '10-K', 'b', '2026-10-15', '101', '', amended)]
        self.assertEqual(w.extract_financials(docs, '101', None, AS_OF)['after_tax_earnings'].value, w.UNKNOWN)

    def test_zero_purchases_requires_coverage_and_wrong_issuer_is_not_coverage(self):
        raw = form4(('J', 'A', 'S'))
        doc = w.Document('f.xml', '4', 'a', '2026-11-02', '101', raw)
        self.assertEqual(w.insider_purchases([doc], '101', None, AS_OF, True)[0].value, 0)
        doc.text = form4(('P',), '202')
        self.assertEqual(w.insider_purchases([doc], '101', None, AS_OF, True)[0].value, w.UNKNOWN)

    def test_unknown_share_count_never_compares_company_value_with_share_price(self):
        facts = self.financials(); facts['shares_outstanding'] = w.Fact()
        result = w.compute_values(facts, self.rules, PRICE[0])
        self.assertNotEqual(result['low_total'], w.NC)
        self.assertEqual(result['low_per_share'], w.NC)
        self.assertEqual(result['view'], w.NC); self.assertEqual(result['conviction'], 'LOW')

    def test_currency_mismatch_never_guesses_exchange_rate(self):
        result = w.compute_values(self.financials(), self.rules, dict(PRICE[0], currency='EUR'))
        self.assertEqual(result['comparison'], w.NC); self.assertEqual(result['view'], w.NC)
        result = w.compute_values(self.financials(), self.rules, {k:v for k,v in PRICE[0].items() if k!='currency'})
        self.assertEqual(result['comparison'], w.NC)

    def test_judgment_transport_one_request_no_retry_exact_persona_and_context(self):
        payload = self.judgment_contract(False); result = structured_response(payload)
        response = Mock(); response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
        with patch.object(w.requests, 'post', return_value=response) as post:
            returned = w.JudgmentClient('fictional-key')(self.persona_path.read_text(), payload)
        post.assert_called_once(); self.assertEqual(returned, result)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['messages'][0]['content'], self.persona_path.read_text())
        task = json.loads(body['messages'][1]['content'])
        self.assertEqual(set(task['context']), {'facts', 'availability', 'allowed_claims'})
        self.assertEqual(task['context'], json.loads(w.dump(payload)))
        self.assertIn('Do not compute', task['task'])
        schema = task['expected_output']
        self.assertEqual(set(schema['required']), {'cheapness_view', 'cheapness_reason', 'next_filing_change'})
        self.assertFalse(schema['additionalProperties'])
        for field in ('cheapness_reason', 'next_filing_change'):
            self.assertEqual(schema['properties'][field]['type'], 'string')
            self.assertEqual(schema['properties'][field]['enum'], sorted(payload['allowed_claims'][field]))
        self.assertNotIn('supported', schema['properties']['cheapness_view']['enum'])
        response.json.return_value['choices'][0]['finish_reason'] = 'length'
        with patch.object(w.requests, 'post', return_value=response) as post:
            with self.assertRaisesRegex(ValueError, 'Incomplete judgment'):
                w.JudgmentClient('fictional-key')(self.persona_path.read_text(), payload)
        post.assert_called_once()

    def test_malformed_transport_response_is_preserved_without_second_call(self):
        response = Mock()
        response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{broken JSON'}}]}
        judge = w.JudgmentClient('fictional-key')
        with patch.object(w.requests, 'post', return_value=response) as post:
            with self.assertRaises(json.JSONDecodeError):
                w.run_worksheet(self.folder, {'candidates': {}}, self.rules_path, self.persona_path,
                                judge, reader=self.price, volume_reader=self.volumes, now=NOW, as_of=AS_OF)
        post.assert_called_once()
        audit = json.loads(next(self.folder.glob('worksheet_judgment_rejected_*.json')).read_text())
        self.assertEqual(audit['status'], 'rejected')
        self.assertEqual(audit['raw_content'], '{broken JSON')
        self.assertFalse(list(self.folder.glob('spinoff_worksheet_*.md')))

    def test_zero_or_wrong_numeric_judgment_evidence_is_rejected(self):
        contract = self.judgment_contract(False)
        for text in ('two times the price', 'a calculated value', '50 percent upside'):
            bad = structured_response(contract); bad['next_filing_change'] = text
            with self.subTest(text=text), self.assertRaises(ValueError):
                w.validate_judgment(bad, contract)

    def test_malformed_rules_fail_instead_of_supplying_defaults(self):
        for text in (RULES.replace('Owner cash flow / required return', 'Discounted cash flow'),
                     RULES.replace('12%', 'unknown'), RULES.replace('$100,000', 'unknown')):
            self.rules_path.write_text(text)
            with self.assertRaises(ValueError):w.Rules.read(self.rules_path)
        self.rules_path.write_text(RULES+'\nLow case value:\nOwner cash flow / 0.30\n')
        with self.assertRaisesRegex(ValueError, 'Conflicting worksheet'):
            w.Rules.read(self.rules_path)

    def test_output_uses_worksheet_headings_and_lists_unknowns_after_judgment_and_liquidity(self):
        text = self.run_sheet().read_text()
        for heading in self.rules.headings:self.assertIn('## '+heading+'\n', text)
        self.assertLess(text.index('Why cheap:'), text.index('Liquidity:'))
        self.assertLess(text.index('Liquidity:'), text.index('View:'))
        self.assertIn('index: not known', text)
        self.assertIn('insider_purchases: not known', text)

    def test_parent_debt_is_not_recorded_as_child_debt(self):
        registry, _ = self.registry()
        raw = 'Separation\nThe Parent incurred $500 million in debt.\n'
        doc = w.Document('separation.txt', '10-12B', 'reg', '2026-10-01', '101', raw)
        facts, _ = w.worksheet_facts([doc], registry['candidates']['101'], {'cik':'101'}, AS_OF)
        self.assertEqual(facts['debt_raised'].value, w.UNKNOWN)

    def test_spinoff_uses_existing_resolving_reader_and_post_distribution_volume_interval(self):
        registry, bundle = self.registry()
        (self.folder/'facts.json').write_text(json.dumps({'company': {'ticker': 'UNIT', 'cik': '101'}, 'current_period': None}))
        (self.folder/'filings.json').write_text(json.dumps({'10-Q': {'form': '10-Q', 'accessionNumber': 'quarter', 'filingDate': '2026-11-06'}}))
        (self.folder/'10-Q.html').write_text(financial_html(start='2026-07-01'))
        self.judge.side_effect = lambda persona, payload: structured_response(payload)
        path = self.root/'resolved.xlsx'
        workbook = Workbook(); sheet = workbook.active
        sheet.append(['Instrument', 'Date', 'Price Close', 'Volume', 'Currency'])
        sheet.append(['UNIT.K', datetime(2026, 1, 2), 999, 999, 'USD'])
        sheet.append(['UNITw.K', datetime(2026, 1, 3), 1, 1, 'USD'])
        sheet.append(['Instrument', 'Date', 'Price Close', 'Volume', 'Currency'])
        sheet.append(['UNIT.K', datetime(2026, 1, 5), 20, 1000, 'USD'])
        sheet.append(['UNIT.K', datetime(2026, 1, 6), 30, 2000, 'USD'])
        workbook.save(path); workbook.close(); before = path.read_bytes()
        self.price = reader.daily_closes_for_ticker
        self.volumes = reader.daily_close_volumes_for_ticker
        with patch.object(reader, 'dotenv_values', return_value={'CLOSES_FILE': str(path)}), \
             patch.object(w.seeker, 'filing_documents', return_value=bundle):
            result = self.run_sheet(registry, sec_client=Mock(text=Mock(return_value='<h1>Information Statement</h1>')), currency_reader=reader.instrument_currency)
        text = result.read_text()
        audit = json.loads(result.with_suffix('.json').read_text())
        self.assertEqual(audit['facts']['price']['value'], 30.0)
        self.assertEqual(audit['facts']['price']['date'], '2026-01-06')
        first = audit['facts']['first_official_close']
        self.assertEqual(first['value'], 20.0)
        self.assertEqual(first['date'], '2026-01-05')
        self.assertEqual(first['filing'], 'CLOSES_FILE UNIT.K')
        self.assertEqual(audit['reader_interval']['start'], '2026-01-03')
        self.assertEqual(audit['facts']['after_tax_earnings']['value'], w.UNKNOWN)
        self.assertEqual(audit['values']['owner_cash_flow'], w.NC)
        self.assertEqual(audit['values']['low_total'], w.NC)
        self.assertIn('median daily dollar volume 40000', text)
        self.assertIn('client purchase 100000', text)
        self.assertIn('purchase/median 250', text)
        self.assertIn('client purchase is larger than', text)
        self.assertIn('export instrument UNIT.K', text)
        self.assertEqual(path.read_bytes(), before)

    def test_worksheet_never_selects_when_issued_or_ambiguous_export_instruments(self):
        registry, bundle = self.registry()
        path = self.root/'unavailable.xlsx'
        for instruments in (['UNITw.K', 'UNIT.WI'], ['UNIT.K', 'UNIT.O']):
            with self.subTest(instruments=instruments):
                workbook = Workbook(); sheet = workbook.active
                sheet.append(['Instrument', 'Date', 'Price Close', 'Volume', 'Currency'])
                for instrument in instruments:
                    sheet.append([instrument, datetime(2026, 1, 5), 20, 1000, 'USD'])
                workbook.save(path); workbook.close()
                self.price = reader.daily_closes_for_ticker
                self.volumes = reader.daily_close_volumes_for_ticker
                with patch.object(reader, 'dotenv_values', return_value={'CLOSES_FILE': str(path)}), \
                     patch.object(w.seeker, 'filing_documents', return_value=bundle):
                    result = self.run_sheet(registry, sec_client=Mock(text=Mock(return_value='<h1>Information Statement</h1>')), currency_reader=reader.instrument_currency)
                audit = json.loads(result.with_suffix('.json').read_text())
                self.assertEqual(audit['facts']['price']['value'], w.UNKNOWN)
                self.assertEqual(audit['facts']['first_official_close']['value'], w.UNKNOWN)
                self.assertEqual(audit['values']['view'], w.NC)

    def test_missing_currency_preserves_numeric_median_without_inventing_dollars(self):
        line, _ = w.liquidity(VOLUMES, self.rules, '2026-11-01', '2026-11-19', w.UNKNOWN)
        self.assertIn('median daily close-times-volume 100000', line)
        self.assertIn('export quote units', line)
        self.assertIn('median daily dollar volume not known', line)
        self.assertIn('client USD purchase/median comparison not computed', line)
        self.price.return_value[0].pop('currency')
        text = self.run_sheet(currency_reader=Mock(return_value=reader.NOT_AVAILABLE)).read_text()
        self.assertIn('median daily close-times-volume 100000', text)
        self.assertIn('median daily dollar volume: not known', text)

    def test_method_is_printed_when_annual_inputs_are_missing(self):
        (self.folder/'10-K.html').write_text(financial_html(missing=('depreciation',)))
        text = self.run_sheet().read_text()
        self.assertIn('Valuation method: Owner cash flow / required return', text)
        self.assertIn('Owner cash flow = after-tax earnings + depreciation - capital spending', text)
        self.assertIn('Low case required return: 12', text)
        self.assertIn('Mid case required return: 10', text)
        self.assertIn('Low case: owner cash flow not computed / 0.12 = not computed', text)
        self.assertIn('Depreciation: not known', text)

    def test_shareholder_cash_mechanics_do_not_establish_financing_cash_destination(self):
        registry, _ = self.registry()
        for sentence in (
            'Fractional shares were converted to cash and paid to stockholders.',
            'Cash dividends were paid to shareholders following the distribution.',
            'Under the separation agreement, cash was paid to stockholders of the parent.',
            'Trading proceeds were paid to selling shareholders.',
            'Proceeds from ordinary sales of shares were distributed to the parent.'):
            with self.subTest(sentence=sentence):
                doc = w.Document('separation.txt', '10-12B', 'reg', '2026-01-03', '101', 'Separation\n'+sentence)
                facts, _ = w.worksheet_facts([doc], registry['candidates']['101'], {'cik': '101'}, AS_OF)
                self.assertEqual(facts['cash_destination'].value, w.UNKNOWN)
                self.assertIn('found none', facts['cash_destination'].quote)

    def test_explicit_financing_and_separation_cash_uses_preserve_filing_evidence(self):
        registry, _ = self.registry()
        for sentence in (
            'The Company used $40 million of proceeds from its loan to repay the parent.',
            'Borrowing proceeds of $40 million were used to fund working capital.',
            'Under the separation agreement, cash of $40 million was transferred to the parent.'):
            with self.subTest(sentence=sentence):
                doc = w.Document('separation.txt', '10-12B', 'reg', '2026-01-03', '101', 'Separation\n'+sentence)
                facts, _ = w.worksheet_facts([doc], registry['candidates']['101'], {'cik': '101'}, AS_OF)
                fact = facts['cash_destination']
                self.assertEqual(fact.value, sentence)
                self.assertEqual(fact.amount, 'USD 40000000')
                self.assertIn('10-12B reg', fact.filing)
                self.assertEqual(fact.section, 'Separation')

    def test_unavailable_comparison_cannot_become_cheapness_or_shareholder_cash_thesis(self):
        contract = self.judgment_contract(False)
        for selection in ('valuation_comparison', 'forced_selling:catalysts', 'financing_use:cash_destination',
                          'Cheapness relative to value is not established.'):
            bad = {'cheapness_view': 'supported', 'cheapness_reason': selection,
                   'next_filing_change': 'new_financial_evidence'}
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                w.validate_judgment(bad, contract)
        self.assertIsNotNone(w.validate_judgment(structured_response(contract), contract))

    def test_judgment_payload_excludes_misclassified_cash_destination_and_screen_rank(self):
        facts = self.financials()
        facts['cash_destination'] = w.Fact('Fractional shares were converted to cash and paid to stockholders.',
                                          '8-K synthetic', 'Distribution', quote='Fractional-share cash settlement')
        facts['rank_and_points'] = w.Fact('rank 1', 'spinoff registry', 'ranking', quote='rank')
        payload = w.judgment_payload(facts, w.compute_values(facts, self.rules, PRICE[0]))
        self.assertNotIn('cash_destination', payload['facts'])
        self.assertNotIn('rank_and_points', payload['facts'])

    def test_judgment_projection_excludes_shareholder_mechanics_from_every_fact(self):
        for sentence in (
            'Fractional shares were settled in cash.',
            'Cash was paid to shareholders.',
            'Stockholders received payments in the distribution.',
            'The Company paid a dividend.',
            'Trading proceeds were received by the selling holders.',
            'Proceeds from sales of common shares were paid to investors.'):
            for key in ('catalysts', 'restrictions', 'cash_destination', 'parent_after_spin'):
                with self.subTest(sentence=sentence, key=key):
                    fact = w.Fact(sentence, '8-K fictional', 'Separation', quote=sentence)
                    self.assertNotIn(key, w.judgment_evidence({key: fact}))

    def test_judgment_projection_keeps_safe_separation_sentences_and_provenance(self):
        facts = {
            'distribution_ratio': w.Fact('A share for each pair of parent shares.', '8-K fictional', 'Separation',
                                        quote='A share for each pair of parent shares. Fractional shares were settled in cash.'),
            'parent': w.Fact('Example Holding', '10-12B fictional', 'Separation', quote='Our parent is Example Holding.'),
            'liabilities_left': w.Fact('The Company retained its contractual obligations.', '10-12B fictional', 'Obligations',
                                      quote='The Company retained its contractual obligations.'),
            'parent_restrictions': w.Fact('The separation agreement restricts asset transfers.', '10-12B fictional', 'Covenants',
                                         quote='The separation agreement restricts asset transfers.'),
            'insider_purchases': w.Fact(0, '4 fictional', 'transactionCoding', quote='P acquisitions observed: 0; full coverage True')}
        facts['parent'].filing = '10-12B/A 0000000101-26-000001 C:\\fictional\\saved\\filing.txt'
        before = copy.deepcopy(facts)
        selected = w.judgment_evidence(facts)
        self.assertEqual(set(selected), set(facts))
        self.assertEqual(selected['distribution_ratio']['quote'], facts['distribution_ratio'].value)
        self.assertNotIn('fractional', w.dump(selected).lower())
        self.assertEqual(selected['liabilities_left']['filing'], '10-12B fictional')
        self.assertEqual(selected['parent_restrictions']['section'], 'Covenants')
        self.assertEqual(selected['parent']['filing'], facts['parent'].filing)
        self.assertEqual(facts, before)

    def test_judgment_projection_rejects_unknown_invalid_and_unsupported_cash_evidence(self):
        unknown = w.Fact()
        rejected = w.Fact('Unsupported statement.', '8-K fictional', 'Separation', quote='Unsupported statement.')
        rejected.status = 'rejected'
        invalid = copy.deepcopy(rejected); invalid.status = 'invalid'
        cash = 'A payment was received by the parent.'
        facts = {'parent': unknown, 'liabilities_left': rejected, 'restrictions': invalid,
                 'cash_destination': w.Fact(cash, '8-K fictional', 'Separation', quote=cash)}
        self.assertEqual(w.judgment_evidence(facts), {})
        supported = 'The Company used the loan proceeds to repay the parent.'
        facts['cash_destination'] = w.Fact(supported, '8-K fictional', 'Financing', quote=supported)
        self.assertEqual(w.judgment_evidence(facts)['cash_destination']['quote'], supported)

    def test_judgment_prompt_omits_excluded_topics_and_accepts_grounded_unknown_value_response(self):
        contract = self.judgment_contract(False)
        response = structured_response(contract)
        response['cheapness_reason'] = 'Financing evidence is not established.'
        self.assertIs(w.validate_judgment(response, contract), response)
        rendered = '\n'.join(w.render_judgment(response, contract))
        self.assertIn('Financing evidence is not established.', rendered)
        self.assertIn('Cheapness relative to value is not established.', rendered)
        request = w.judgment_request(contract)
        self.assertIn('copying exact keys', request['task'])
        self.assertEqual(request['expected_output'], w.judgment_schema(contract))

    def test_seller_pressure_requires_an_explicit_affirmative_source_passage(self):
        values = w.compute_values(self.financials(), self.rules, reader.NOT_AVAILABLE)
        for sentence, supported in (
            ('The parent distributed shares to its stockholders.', False),
            ('Certain funds are required to sell the distributed shares.', True),
            ('No funds are required to sell the distributed shares.', False),
            ('Certain funds may be required to sell the distributed shares.', False)):
            with self.subTest(sentence=sentence):
                facts = {'catalysts': w.Fact(sentence, '10-12B fictional', 'Distribution', quote=sentence)}
                contract = w.judgment_payload(facts, values)
                self.assertEqual(contract['availability']['forced_selling_evidence_available'], supported)
                result = {'cheapness_view': 'supported', 'cheapness_reason': 'forced_selling:catalysts',
                          'next_filing_change': 'review:catalysts'}
                if supported:
                    self.assertIs(w.validate_judgment(result, contract), result)
                    self.assertIn('Cheapness relative to value is not established.', '\n'.join(w.render_judgment(result, contract)))
                else:
                    with self.assertRaises(ValueError): w.validate_judgment(result, contract)

    def test_structured_validation_accepts_negative_financing_but_rejects_unsupported_positive_claim(self):
        contract = self.judgment_contract(False)
        result = structured_response(contract)
        for statement in ('Financing evidence is not established.',
                          'Financing cash proceeds are not established by the supplied evidence.'):
            result['cheapness_reason'] = statement
            self.assertIs(w.validate_judgment(result, contract), result)
        result['cheapness_reason'] = 'financing_use:cash_destination'
        with self.assertRaisesRegex(ValueError, 'unsupported claim or evidence ID'):
            w.validate_judgment(result, contract)

    def test_availability_is_code_generated_and_supported_claims_bind_to_supplied_ids(self):
        facts = self.financials()
        for key, text in (
            ('cash_destination', 'The Company used loan proceeds to repay the parent.'),
            ('liabilities_left', 'The Company retained contractual obligations.'),
            ('parent_restrictions', 'The separation agreement restricts asset transfers.')):
            facts[key] = w.Fact(text, '10-12B fictional', 'Separation', quote=text)
        facts['insider_purchases'] = w.Fact(0, '4 fictional', 'transactionCoding', quote='P acquisitions observed: 0; full coverage True')
        before = copy.deepcopy(facts)
        values = w.compute_values(facts, self.rules, PRICE[0])
        contract = w.judgment_payload(facts, values)
        flags = contract['availability']
        for flag in ('value_comparison_available', 'financing_cash_destination_available',
                     'liabilities_evidence_available', 'restrictions_evidence_available', 'insider_purchase_evidence_available'):
            self.assertIs(flags[flag], True)
        self.assertIs(flags['forced_selling_evidence_available'], False)
        self.assertEqual(facts, before)
        self.assertNotIn('value_range', contract)
        for record in contract['facts'].values():
            self.assertNotIn('value', record)
            self.assertIsNone(w.JUDGMENT_NUMBER.search(record['statement']))
        reason = 'liabilities:liabilities_left'
        result = {'cheapness_view': 'not_that_story', 'cheapness_reason': reason,
                  'next_filing_change': 'review:liabilities_left'}
        self.assertIs(w.validate_judgment(result, contract), result)
        self.assertIn('liabilities_left', '\n'.join(w.render_judgment(result, contract)))
        # Bindings are checked independently of enum membership.
        contract['allowed_claims']['cheapness_reason'][reason]['evidence_ids'] = ['invented_source']
        with self.assertRaisesRegex(ValueError, 'unsupported evidence ID'):
            w.validate_judgment(result, contract)

    def test_claim_prerequisites_are_checked_even_for_an_existing_menu_choice(self):
        contract = self.judgment_contract()
        result = structured_response(contract)
        contract['availability']['value_comparison_available'] = False
        with self.assertRaisesRegex(ValueError, 'supporting availability'):
            w.validate_judgment(result, contract)

    def test_reader_explicit_currency_and_conflicting_duplicate_volume(self):
        path = self.root/'data.xlsx'; workbook = Workbook(); sheet = workbook.active
        sheet.append(['Instrument','Date','Price Close','Volume','Currency'])
        sheet.append(['UNIT.K',datetime(2026,11,18),70,100,'USD'])
        sheet.append(['UNIT.K',datetime(2026,11,18),70,200,'USD'])
        workbook.save(path); workbook.close()
        with patch.object(reader,'dotenv_values',return_value={'CLOSES_FILE':str(path)}):
            self.assertEqual(reader.instrument_currency('UNIT'),'USD')
            rows = reader.daily_close_volumes_for_ticker('UNIT','2026-11-18','2026-11-18')
        self.assertIsNone(rows[0]['volume'])


if __name__ == '__main__':
    unittest.main()
