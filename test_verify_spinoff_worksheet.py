"""Fictional local sources only; never open the user's export or call a model."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import verify_spinoff_worksheet as v


RULES = '''Low case required return: 12%
Mid case required return: 10%
Client purchase size: $100,000
low-case value is at least 1.25 times the current price
'''


class NumericHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.rules = self.root/'rules.txt'; self.rules.write_text(RULES, encoding='utf-8')
        self.path = self.root/'spinoff_worksheet_fictional.md'
        self.filing = self.root/'annual.txt'
        self.filing.write_text('INCOME STATEMENT\nRevenue was 100 USD.\nDEBT\nBorrowings were 900 USD.\n', encoding='utf-8')
        self.records = [dict(tag=tag, val=value, unit=unit, scale=0, start=start, end='2025-12-31')
                        for tag, value, unit, start in [('us-gaap:NetIncomeLoss', 100, 'USD', '2025-01-01'),
                                                       ('us-gaap:Depreciation', 20, 'USD', '2025-01-01'),
                                                       ('us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', 10, 'USD', '2025-01-01'),
                                                       ('dei:EntityCommonStockSharesOutstanding', 10, 'shares', None)]]
        (self.root/'facts.json').write_text(json.dumps({'concepts': self.records}), encoding='utf-8')
        (self.root/'run.json').write_text(json.dumps({'company': {'cik': '123'}}), encoding='utf-8')
        self.close = patch.object(v.closes_reader, 'daily_closes', side_effect=AssertionError('Real export forbidden')).start()
        self.volume = patch.object(v.closes_reader, 'daily_close_volumes', side_effect=AssertionError('Real export forbidden')).start()
        self.currency = patch.object(v.closes_reader, 'instrument_currency', side_effect=AssertionError('Real export forbidden')).start()
        self.addCleanup(patch.stopall)
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start()

    def write(self, text):
        self.path.write_text(text+'\n', encoding='utf-8')
        return self.path

    def check(self, text):
        v.verify(self.write(text), self.rules)

    def rejects(self, text, pattern=None):
        with self.assertRaises(v.Failure) as cm:
            self.check(text)
        if pattern:
            self.assertRegex(str(cm.exception), pattern)
        return str(cm.exception)

    def fact(self, name='After tax earnings', index=0, value=None):
        r = self.records[index]
        period = r['start']+' through '+r['end'] if r['start'] else r['end']
        return (f'{name}: {r["val"] if value is None else value} {r["unit"]} | facts facts.json | tag {r["tag"]}'
                f' | period {period} | unit {r["unit"]} | scale 0')

    def financials(self):
        return '\n'.join(self.fact(name, i) for i, name in enumerate(('After tax earnings', 'Depreciation', 'Capital spending', 'Shares outstanding')))

    def calculations(self):
        return self.financials()+'''
Owner cash flow arithmetic: 100 + 20 - 10 = 110
Low case: owner cash flow 110 / 0.12 = 916.6666666666666666666666667
Mid case: owner cash flow 110 / 0.1 = 1.10E+3
Low case per share: 916.6666666666666666666666667 / 10 = 91.66666666666666666666666667
Mid case per share: 1.10E+3 / 10 = 110
'''.rstrip()

    def test_valid_filing_number(self):
        self.check('Revenue: 100 USD | filing annual.txt | section INCOME STATEMENT | date 2026-01-01')

    def scaled_filing(self, scale='6'):
        self.filing.write_text('CONSOLIDATED STATEMENTS OF CASH FLOWS\n(Amounts in millions)\n'
                               'Depreciation and amortization | 14,203\n', encoding='utf-8')
        attribute = '' if scale is None else f' scale="{scale}"'
        html = ('<html><xbrli:context id="annual"><xbrli:entity><xbrli:identifier>123</xbrli:identifier>'
                '</xbrli:entity><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate>'
                '<xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>'
                '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
                '<h2>CONSOLIDATED STATEMENTS OF CASH FLOWS</h2><table><tr><td>Depreciation and amortization</td>'
                '<td><ix:nonFraction name="us-gaap:DepreciationAmortizationAndAccretionNet" '
                f'contextRef="annual" unitRef="usd"{attribute}>14,203</ix:nonFraction></td></tr></table></html>')
        (self.root/'annual.html').write_text(html, encoding='utf-8')
        return ('Depreciation: 14203000000 USD | filing annual.html | section CONSOLIDATED STATEMENTS OF CASH FLOWS'
                ' | date 2026-01-01 | historical | period 2025-01-01 through 2025-12-31 | unit USD'
                ' | scale base units | evidence us-gaap:DepreciationAmortizationAndAccretionNet: 14,203')

    def test_explicit_saved_xbrl_scale_reconciles_normalized_filing_amount(self):
        self.check(self.scaled_filing())

    def test_wrong_saved_filing_scale_fails(self):
        self.rejects(self.scaled_filing(scale='3'), '14203000000')

    def test_missing_saved_scale_never_inferred_from_millions_caption(self):
        self.rejects(self.scaled_filing(scale=None), '14203000000')

    def test_unscaled_filing_amount_still_verifies_literally(self):
        self.check('Revenue: 100 USD | filing annual.txt | section INCOME STATEMENT | date 2026-01-01')

    def test_scale_evidence_must_match_saved_tag_period_unit_issuer_and_section(self):
        for old, new in (('2025-01-01', '2025-04-01'), ('iso4217:USD', 'iso4217:EUR'),
                         ('<xbrli:identifier>123', '<xbrli:identifier>999'),
                         ('us-gaap:DepreciationAmortizationAndAccretionNet', 'us-gaap:Depreciation')):
            with self.subTest(change=(old, new)):
                text = self.scaled_filing()
                path = self.root/'annual.html'
                path.write_text(path.read_text(encoding='utf-8').replace(old, new), encoding='utf-8')
                self.rejects(text)
        text = self.scaled_filing()
        self.filing.write_text('CONSOLIDATED STATEMENTS OF CASH FLOWS\nUnrelated amount 14,203\n', encoding='utf-8')
        self.rejects(text)

    def test_wrong_filing_amount_names_exact_line_and_source(self):
        error = self.rejects('# Worksheet\nRevenue: 101 USD | filing annual.txt | section INCOME STATEMENT | date 2026-01-01')
        self.assertIn('line 2', error); self.assertIn('101', error); self.assertIn('annual.txt', error)
        self.assertIn('Worksheet line: Revenue: 101', error)

    def test_wrong_filing_section_fails(self):
        self.rejects('Debt: 900 USD | filing annual.txt | section INCOME STATEMENT | date 2026-01-01')

    def test_valid_saved_filing_line(self):
        self.check('Revenue: 100 USD | filing annual.txt | section line 2 | date 2026-01-01')

    def test_valid_facts_file_number(self):
        self.check(self.fact())

    def test_wrong_facts_value(self):
        self.rejects(self.fact(value=101))

    def test_wrong_facts_period(self):
        self.rejects(self.fact().replace('2025-01-01', '2025-04-01'), 'tag/period/unit/scale')

    def test_wrong_facts_tag(self):
        self.rejects(self.fact().replace('us-gaap:NetIncomeLoss', 'us-gaap:Revenues'), 'tag/period/unit/scale')

    def test_wrong_facts_unit(self):
        self.rejects(self.fact().replace('| unit USD', '| unit EUR'))

    def test_wrong_facts_scale(self):
        self.rejects(self.fact().replace('| scale 0', '| scale 6'))

    def test_facts_display_cannot_change_unit_or_magnitude(self):
        self.rejects(self.fact().replace('100 USD | facts', '100 EUR | facts'))
        self.rejects(self.fact().replace('100 USD | facts', '100 million USD | facts'))

    def test_valid_calculations(self):
        self.check(self.calculations())

    def test_changed_owner_cash_flow(self):
        self.rejects(self.calculations().replace('= 110\nLow', '= 111\nLow'), '111')

    def test_changed_valuation_result(self):
        self.rejects(self.calculations().replace('= 1.10E+3', '= 1200'), '1200')

    def test_unavailable_owner_cash_flow_with_supported_share_count(self):
        self.check(self.fact('Shares outstanding', 3)+'\nLow case per share: not computed / 10 = not computed')

    def incomplete_per_share_inputs(self, shares='not known'):
        for record, value in zip(self.records, (12, 0, 0, 5)):
            record['val'] = value
        (self.root/'facts.json').write_text(json.dumps({'concepts': self.records}), encoding='utf-8')
        return ('\n'.join(self.fact(name, i) for i, name in enumerate(
                    ('After tax earnings', 'Depreciation', 'Capital spending')))
                +'\nOwner cash flow arithmetic: 12 + 0 - 0 = 12'
                +'\nLow case: owner cash flow 12 / 0.12 = 100\n'
                +('Shares outstanding: not known' if shares == 'not known' else self.fact('Shares outstanding', 3)))

    def test_incomplete_per_share_requires_verified_total_and_explicit_missing_shares(self):
        prefix = self.incomplete_per_share_inputs()
        expression = '\nLow case per share: 100 / not known = not computed'
        self.check(prefix+expression)
        self.rejects(prefix.replace('Shares outstanding: not known', self.fact('Shares outstanding', 3))+expression,
                     'marked not known')
        self.rejects(prefix.replace('\nShares outstanding: not known', '')+expression, 'printed input')
        self.rejects(prefix+expression.replace('100 /', '101 /'), 'expected')
        self.rejects('Shares outstanding: not known'+expression, 'verified total')

    def test_known_per_share_inputs_cannot_claim_not_computed(self):
        prefix = self.incomplete_per_share_inputs(shares='known')
        self.rejects(prefix+'\nLow case per share: 100 / 5 = not computed')

    def test_missing_per_share_input_cannot_produce_numeric_result(self):
        prefix = self.incomplete_per_share_inputs()
        self.rejects(prefix+'\nLow case per share: 100 / not known = 20')

    def test_completed_per_share_calculations_still_verify_every_operand_and_result(self):
        prefix = self.incomplete_per_share_inputs(shares='known')
        expression = '\nLow case per share: 100 / 5 = 20'
        self.check(prefix+expression)
        for wrong in ('101 / 5 = 20', '100 / 4 = 20', '100 / 5 = 21'):
            with self.subTest(expression=wrong):
                self.rejects(prefix+'\nLow case per share: '+wrong)

    def test_price_comparison_recomputed(self):
        self.close.side_effect = None; self.close.return_value = [{'date': '2026-01-02', 'close': 25}]
        text = (self.calculations()+'\nPrice: 25 USD | filing CLOSES_FILE UNIT.K | section Instrument / Date / Price Close | date 2026-01-02\n'
                'Price against low case: current price 25; low-case value per share 91.66666666666666666666666667; difference 66.66666666666666666666666667')
        self.currency.side_effect = None; self.currency.return_value = 'USD'
        self.check(text)
        self.rejects(text.replace('difference 66.66666666666666666666666667', 'difference 67'), '67')

    def test_market_value_calculation(self):
        self.close.side_effect = None; self.close.return_value = [{'date': '2026-01-02', 'close': 25}]
        text = ('Price: 25 | filing CLOSES_FILE UNIT.K | section Instrument / Date / Price Close | date 2026-01-02\n'
                +self.fact('Shares outstanding', 3)+'\nMarket value: 250 USD | filing CLOSES_FILE UNIT.K + annual.txt | section price times disclosed outstanding shares | date 2026-01-02')
        self.check(text)
        self.rejects(text.replace('250 USD', '251 USD'), '251')

    def test_low_conviction_with_missing_close(self):
        self.check(self.calculations()+'\nPrice: not known\nConviction: LOW | rule: low case >= 1.25 times price and no required low-case input unknown')

    def test_calculation_cannot_invent_input(self):
        self.rejects(self.calculations().replace('100 + 20', '101 + 20'))

    def test_quarter_not_annualized(self):
        self.records[0]['start'] = '2025-10-01'
        (self.root/'facts.json').write_text(json.dumps(self.records), encoding='utf-8')
        self.rejects(self.calculations(), 'quarterly')

    def test_rate_matches_rules(self):
        self.check('Low case required return: 12.00%\nMid case required return: 10.0%')

    def test_changed_rate_fails(self):
        self.rejects('Low case required return: 11%', '11')

    def test_unknown_calculations_still_check_divisor(self):
        self.check('Owner cash flow arithmetic: not computed\nLow case: owner cash flow not computed / 0.12 = not computed')
        self.rejects('Low case: owner cash flow not computed / 0.13 = not computed')

    def test_valid_reader_price_volume(self):
        self.volume.side_effect = None
        self.volume.return_value = [{'date': '2026-01-02', 'close': 25, 'volume': 4000, 'instrument': 'UNIT.K'}]
        self.check('Close and volume: 25 and 4000 | filing CLOSES_FILE UNIT.K | section Instrument / Date / Price Close / Volume | date 2026-01-02')
        self.volume.assert_called_once_with('UNIT.K', '2026-01-02', '2026-01-02')

    def test_wrong_reader_price(self):
        self.close.side_effect = None; self.close.return_value = [{'date': '2026-01-02', 'close': 25}]
        self.rejects('Price: 26 | filing CLOSES_FILE UNIT.K | section Instrument / Date / Price Close | date 2026-01-02')

    def test_first_official_close_must_be_first_after_distribution(self):
        self.close.side_effect = None
        def rows(instrument, start, end):
            data = [{'date': '2026-01-02', 'close': 25}, {'date': '2026-01-05', 'close': 30}]
            return [r for r in data if start <= r['date'] <= end]
        self.close.side_effect = rows
        base = 'Ticker: UNIT | run date: 2026-01-06T12:00:00-05:00\nDistribution date: 2026-01-01\n'
        good = 'First official close: 25 | filing CLOSES_FILE UNIT.K | section Instrument / Date / Price Close; first exported regular-way close after distribution | date 2026-01-02'
        self.check(base+good)
        self.rejects(base+good.replace('25 |', '30 |').replace('date 2026-01-02', 'date 2026-01-05'), 'not the first')

    def test_when_issued_never_used(self):
        self.rejects('Price: 25 | filing CLOSES_FILE UNITWI.K | section Instrument / Date / Price Close | date 2026-01-02', 'when-issued')
        self.close.assert_not_called()

    def test_liquidity_recomputed_and_quote_currency_disclosed(self):
        self.volume.side_effect = None
        self.volume.return_value = [{'date': '2026-01-02', 'close': 25, 'volume': 4000, 'instrument': 'UNIT.K'},
                                    {'date': '2026-01-05', 'close': 30, 'volume': 4000, 'instrument': 'UNIT.K'}]
        self.currency.side_effect = None; self.currency.return_value = 'not available'
        line = ('Liquidity: client purchase 100000; reader interval 2026-01-02 through 2026-01-05; '
                'median daily close-times-volume 110000 (export quote units); sessions 2; '
                'observed dates 2026-01-02 through 2026-01-05; export instrument UNIT.K; '
                'median daily dollar volume not known; price currency is not evidenced as USD; '
                'client USD purchase/median comparison not computed; purchase reasonableness not known')
        self.check(line)
        self.rejects(line.replace('110000', '110001'), '110001')
        self.rejects(line.replace('price currency is not evidenced as USD;', ''), 'currency limitation')

    def test_liquidity_usd_purchase_comparison(self):
        self.volume.side_effect = None
        self.volume.return_value = [{'date': '2026-01-02', 'close': 25, 'volume': 8000, 'instrument': 'UNIT.K'}]
        self.currency.side_effect = None; self.currency.return_value = 'USD'
        line = ('Liquidity: client purchase 100000; reader interval 2026-01-02 through 2026-01-02; '
                'median daily dollar volume 200000; purchase/median 50%; sessions 1; '
                'observed dates 2026-01-02 through 2026-01-02; export instrument UNIT.K; '
                'client purchase is smaller than the observed daily median; purchase reasonableness not known')
        self.check(line)
        self.rejects(line.replace('50%', '51%'), '51')

    def test_standalone_number_fails(self):
        self.rejects('My unsupported estimate: 987.65', '987.65')

    def test_written_numeric_claim_requires_source(self):
        self.rejects('Revenue: one million dollars')

    def test_insider_count_recomputed_only_p_acquisitions(self):
        xml = ('<ownershipDocument><issuer><issuerCik>123</issuerCik></issuer>'
               +''.join('<nonDerivativeTransaction><transactionCoding><transactionCode>'+code+
                        '</transactionCode></transactionCoding><transactionAmounts><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>'
                        for code in ('P', 'A', 'J', 'S'))+'</ownershipDocument>')
        (self.root/'form4.xml').write_text(xml)
        text = 'Insider purchases: 1 | filing form4.xml | section nonDerivativeTransaction/transactionCoding | date 2026-01-02 | evidence P acquisitions observed: 1; full coverage True'
        self.check(text)
        self.rejects(text.replace('purchases: 1', 'purchases: 4'), '4')

    def test_generated_audit_json_cannot_launder_number(self):
        self.path.with_suffix('.json').write_text(json.dumps({'estimate': 987.65}))
        self.rejects('My unsupported estimate: 987.65')

    def test_registry_rank_not_a_permitted_numeric_source(self):
        self.rejects('Rank and points: rank 1 | filing spinoff registry | section saved ranking | date 2026-01-02', '1')

    def test_identifiers_skipped(self):
        self.check('## 1. Facts\nDate: 2026-01-02\nAccession: 0000000123-26-000001\nCIK: 0000000123\nline 45\nsection 2.02\nForm 4\n10-Q')

    def test_month_and_calendar_year_are_date_identifiers(self):
        self.check('Agreement amended in June 2025.')
        self.assertEqual(v.tokens('June 2025'), [])

    def test_season_range_is_temporal_identifier(self):
        self.check('Rights expire after the 2028-29 season.')
        self.assertEqual(v.tokens('2028-29 season'), [])

    def test_iso_date_is_still_skipped(self):
        self.check('Agreement expires on 2026-11-19.')
        self.assertEqual(v.tokens('2026-11-19'), [])

    def test_four_digit_dollar_amount_remains_claim(self):
        self.rejects('Revenue: $2025', '2025')
        self.assertEqual([m[0] for m in v.tokens('$2025')], ['2025'])

    def test_four_digit_share_amount_remains_claim(self):
        self.rejects('Holding: 2025 shares', '2025')
        self.assertEqual([m[0] for m in v.tokens('2025 shares')], ['2025'])

    def test_four_digit_revenue_amount_remains_claim(self):
        self.rejects('Revenue was 2025', '2025')
        self.rejects('Revenue of 2025', '2025')

    def test_year_without_month_needs_temporal_context(self):
        for text in ('Amended in 2025.', 'Effective since 2025.', 'Calendar year: 2025',
                     'Fiscal year 2025', 'FY 2025', '2025 fiscal year', '2028-2029 season',
                     '2028/29 fiscal year'):
            with self.subTest(text=text):
                self.check(text)
                self.assertEqual(v.tokens(text), [])
        self.rejects('Unsupported amount: 2025', '2025')

    def test_temporal_prepositions_do_not_hide_amounts(self):
        for text in ('Paid in 2025 shares', 'Revenue in $2025', 'Increase in 2025%',
                     'Revenue in 2025 USD', 'Revenue in 2025 million', 'Increase in 2025.50'):
            with self.subTest(text=text):
                self.assertTrue(v.tokens(text))
                self.rejects(text)

    def test_temporal_identifiers_do_not_hide_adjacent_financial_claim(self):
        text = 'Revenue was 2025 in June 2025 for the 2028-29 season.'
        self.assertEqual([m[0] for m in v.tokens(text)], ['2025'])
        self.rejects(text, '2025')

    def test_clean_worksheet_silent(self):
        self.write('Revenue: 100 USD | filing annual.txt | section INCOME STATEMENT | date 2026-01-01')
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = v.main([str(self.path), '--rules', str(self.rules)])
        self.assertEqual(code, 0); self.assertEqual(stdout.getvalue(), ''); self.assertEqual(stderr.getvalue(), '')

    def test_first_failure_exit_two_stderr_only(self):
        self.write('Unsupported: 987\nUnsupported: 654')
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = v.main([str(self.path), '--rules', str(self.rules)])
        self.assertEqual(code, 2); self.assertEqual(stdout.getvalue(), '')
        self.assertIn('line 1', stderr.getvalue()); self.assertNotIn('654', stderr.getvalue())

    def test_hook_checks_actual_path_not_latest(self):
        self.write('Unsupported: 987')
        (self.root/'spinoff_worksheet_newer.md').write_text('No numeric claims')
        with self.assertRaisesRegex(v.Failure, '987'):
            v.hook({'hook_event_name': 'PostToolUse', 'cwd': str(self.root), 'tool_name': 'Bash',
                    'tool_response': {'output': str(self.path)}}, self.rules)

    def test_stop_rechecks_tool_output_and_does_not_bypass_second_failure(self):
        self.write('Unsupported: 987')
        transcript = self.root/'session.jsonl'
        transcript.write_text('\n'.join(json.dumps(row) for row in [
            {'payload': {'type': 'user_message', 'message': 'Write worksheet'}},
            {'payload': {'type': 'function_call_output', 'output': str(self.path)}}]))
        with self.assertRaisesRegex(v.Failure, '987'):
            v.hook({'hook_event_name': 'Stop', 'transcript_path': str(transcript), 'stop_hook_active': True}, self.rules)

    def test_unrelated_hook_silent(self):
        v.hook({'hook_event_name': 'PostToolUse', 'tool_response': 'Ordinary output'}, self.rules)

    def test_source_read_does_not_imply_live_worksheet_write(self):
        v.hook({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                'tool_input': {'command': 'Get-Content spinoff_worksheet.py'}, 'tool_response': 'source code'}, self.rules)

    def test_successful_worksheet_command_without_output_path_is_not_done(self):
        with self.assertRaisesRegex(ValueError, 'no actual saved'):
            v.hook({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                    'tool_input': {'command': 'python -B spinoff_worksheet.py "saved"'}, 'tool_response': ''}, self.rules)

    def test_disabled_config_is_outside_discovery_and_wires_blocking_events(self):
        config = json.loads((v.ROOT/'lab07_hooks.disabled.json').read_text())
        self.assertEqual(set(config['hooks']), {'PostToolUse', 'Stop'})
        for entries in config['hooks'].values():
            self.assertIn('--hook', entries[0]['hooks'][0]['command'])
            self.assertFalse(entries[0]['hooks'][0].get('async', False))


if __name__ == '__main__':
    unittest.main()
