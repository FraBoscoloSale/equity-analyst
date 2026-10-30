"""Offline contract tests: role boundaries, corrections, evidence and arithmetic."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import equity_desk as desk
import research_roles as roles
from test_equity_desk import COMPANY, fixture


def block(text="The business has recurring demand.", evidence=None):
    return {"text": text, "evidence_ids": evidence or []}


def text_id(records, line=2, filename='10-K.txt'):
    return next(key for key, row in records.items() if row['kind'] == 'text' and
                row['line'] == line and row['source_id'].endswith(':' + filename))


def setup_run(path):
    run = {"company": COMPANY, "run_time": "2026-10-29T12:00:00-04:00", "price": 30,
           "peers": ["PEER"], "step": None}
    roles.write_json(path / "run.json", run)
    filings = {form: {"accessionNumber": "0000000001-25-000001", "reportDate": "2025-06-30",
                      "document_url": "https://www.sec.gov/test", "release_url": "https://www.sec.gov/release"}
               for form in ("10-K", "10-Q", "8-K")}
    roles.write_json(path / "filings.json", filings)
    (path / "10-K.txt").write_text("Item 1. Business\nThe business sells subscriptions.\nItem 1A. Risks\n"
                                  "Item 7. Management's Discussion\nRevenue includes subscriptions.\nItem 8. Statements\n",
                                  encoding="utf-8")
    (path / "10-Q.txt").write_text("Diluted EPS is calculated using diluted shares.\n", encoding="utf-8")
    (path / "earnings_release.txt").write_text("Quarter ended June 30, 2025\nRevenue was $40 million.\n"
                                              "Diluted EPS was $0.50 (GAAP).\n", encoding="utf-8")
    facts = desk.extract_facts(fixture(), COMPANY)
    roles.write_json(path / "facts.json", facts)
    with redirect_stdout(io.StringIO()):
        metrics = desk.calculate(facts, 30)
        peer_metrics = desk.calculate(facts, margin_only=True)
    roles.write_json(path / "metrics.json", metrics)
    roles.write_json(path / "peers.json", {"PEER": {"facts": facts, "metrics": peer_metrics}})
    roles.write_json(path / "release_lines.json", desk.release_lines((path / "earnings_release.txt").read_text()))


class FakeClient:
    def __init__(self, fail_once=False, fail_forever=False, calculator=False):
        self.calls = []
        self.research_calls = 0
        self.fail_once, self.fail_forever, self.calculator = fail_once, fail_forever, calculator

    def call(self, role, task, context, schema):
        self.calls.append((role, task, copy.deepcopy(context)))
        result = self.respond(role, task, context, schema)
        roles.validate_shape(result, schema)
        return result

    def respond(self, role, task, context, schema):
        if role == roles.MANAGER and "tasks" in schema["properties"]:
            return {"tasks": {key: "Perform your assigned research." for key in
                              (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR)}}
        if role == roles.MANAGER:
            result = {"checks": {part: {"passed": True, "issues": []} for part in roles.PARTS}}
            if self.fail_forever or (self.fail_once and self.research_calls == 1):
                result["checks"][roles.PARTS[0]] = {"passed": False, "issues": [
                    {'code': 'missing_analysis', 'text': 'Clarify the business model.'}]}
            return result
        if role == roles.RESEARCH:
            self.research_calls += 1
            return {"sections": {part: [block(evidence=[text_id(context['evidence'])])] for part in roles.PARTS[:2]},
                    'limitations': [key for key, row in context['evidence'].items() if role in row.get('required_for', [])]}
        if role == roles.FINANCIAL:
            requests = []
            ids = ["subject.operating_margin", "subject.pe", "peer.PEER.operating_margin"]
            if self.calculator and "calculator_results" not in context:
                requests = [{"id": "cash_margin", "operation": "divide",
                             "inputs": ["subject.cash_from_operations.current", "subject.revenue.current"]}]
            elif self.calculator:
                ids.append("extra.cash_margin")
            return {"sections": {roles.PARTS[2]: [block("Profitability supports valuation.",
                                                     evidence=ids + ['subject.net_income.current', 'quarter.revenue'])]},
                    'limitations': [key for key, row in context['evidence'].items() if role in row.get('required_for', [])],
                    "calculator_requests": requests}
        records = {key: row for result in context.values() for key, row in result['cited_evidence'].items()}
        return {"sections": {part: [block("The business supports the thesis.",
                                          evidence=[text_id(records), 'subject.pe'])] for part in roles.PARTS[3:]},
                "thesis": "Recurring demand supports the thesis.", "recommendation": "Hold",
                "deciding_evidence_id": "subject.pe", "reason": "The earnings multiple decides the rating.",
                'thesis_evidence_ids': [text_id(records)], 'reason_evidence_ids': ['subject.pe'],
                "limitations": []}


class WorkflowTests(unittest.TestCase):
    def test_report_headers_sections_provenance_and_advisor_isolation(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            path = Path(tmp)
            setup_run(path)
            client = FakeClient()
            report = roles.StrategyManager(path, client).run_desk()
            text = report.read_text(encoding="utf-8")
            self.assertTrue(text.splitlines()[0].startswith("Filings read: "))
            self.assertIn("0000000001-25-000001", text.splitlines()[0])
            self.assertEqual(text.splitlines()[1], "Run time: 2026-10-29T12:00:00-04:00")
            for part in roles.PARTS:
                self.assertIn("## " + part, text)
            self.assertIn("30.0000 / 2.0000 = 15.0 multiple", text)
            self.assertIn("us-gaap:EarningsPerShareDiluted", text)
            self.assertIn("2024-07-01 to 2025-06-30", text)
            self.assertIn("10-K.txt, line 2", text)
            self.assertIn("user input --price", text)
            self.assertIn("PEER", text)
            advisor = next(context for role, _, context in client.calls if role == roles.ADVISOR)
            self.assertEqual(set(advisor), {roles.RESEARCH, roles.FINANCIAL})
            self.assertEqual({key for key, row in advisor[roles.FINANCIAL]['cited_evidence'].items() if row['kind'] == 'calculation'},
                             {"subject.operating_margin", "subject.pe", "peer.PEER.operating_margin"})
            self.assertNotIn("subject.revenue_growth", json.dumps(advisor))
            self.assertEqual([role for role, _, _ in client.calls],
                             [roles.MANAGER, roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR, roles.MANAGER])

    def test_one_correction_then_reconcile_advisor_and_recheck(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            path = Path(tmp)
            setup_run(path)
            client = FakeClient(fail_once=True)
            roles.StrategyManager(path, client).run_desk()
            self.assertEqual(client.research_calls, 2)
            self.assertEqual(sum(role == roles.FINANCIAL for role, _, _ in client.calls), 1)
            self.assertEqual(sum(role == roles.ADVISOR for role, _, _ in client.calls), 2)
            self.assertEqual(sum(role == roles.MANAGER for role, _, _ in client.calls), 3)
            self.assertTrue((path / "review_final.json").exists())

    def test_persistent_failure_retains_analysis_flagged_for_audit_after_one_correction(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            path = Path(tmp)
            setup_run(path)
            client = FakeClient(fail_forever=True)
            text = roles.StrategyManager(path, client).run_desk().read_text(encoding="utf-8")
            self.assertEqual(client.research_calls, 2)
            self.assertIn("Review unresolved after one correction", text)
            self.assertIn("Clarify the business model.", text)
            overview = text.split("## Overview", 1)[1].split("## ", 1)[0]
            self.assertIn("The business has recurring demand.", overview)
            self.assertIn('HUMAN AUDIT REQUIRED — Block 1', overview)
            self.assertNotIn('This part is withheld', text)
            final_review = json.loads((path / 'review_final.json').read_text(encoding='utf-8'))
            self.assertFalse(final_review['checks'][roles.PARTS[0]]['passed'])
            self.assertIn('Clarify the business model.', final_review['checks'][roles.PARTS[0]]['issues'])
            self.assertEqual(sum(role == roles.MANAGER for role, _, _ in client.calls), 3)
            for part in roles.PARTS:
                self.assertIn("## " + part, text)

    def test_calculator_round_returns_sourced_python_arithmetic(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            path = Path(tmp)
            setup_run(path)
            client = FakeClient(calculator=True)
            text = roles.StrategyManager(path, client).run_desk().read_text(encoding="utf-8")
            self.assertIn("35.0 / 120.0 = 0.2916666666666667", text)
            self.assertEqual(sum(role == roles.FINANCIAL for role, _, _ in client.calls), 2)
            self.assertTrue((path / "additional_calculations.json").exists())

    def test_failed_financial_claim_is_preserved_verbatim_with_valid_evidence_and_findings(self):
        wrong_claim = 'Operating margin was 99.99%.'
        class BadFinancialClient(FakeClient):
            def respond(inner, role, task, context, schema):
                result = super().respond(role, task, context, schema)
                if role == roles.FINANCIAL:
                    result['sections'][roles.PARTS[2]].append(block(wrong_claim, ['subject.operating_margin']))
                return result
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            path = Path(tmp)
            setup_run(path)
            client = BadFinancialClient()
            text = roles.StrategyManager(path, client).run_desk().read_text(encoding='utf-8')
            section = text.split('## Valuation Insights', 1)[1].split('## ', 1)[0]
            self.assertIn(wrong_claim, section)
            self.assertIn('HUMAN AUDIT REQUIRED — Block 2', section)
            self.assertIn('Financial numeric prose must be rendered from deterministic evidence', section)
            self.assertIn('Deterministic evidence (does not revise the generated claim)', section)
            self.assertIn('subject.operating_margin:', section)
            self.assertEqual(sum(role == roles.FINANCIAL for role, _, _ in client.calls), 2)
            self.assertEqual(sum(role == roles.MANAGER for role, _, _ in client.calls), 3)
            review = json.loads((path / 'review_final.json').read_text(encoding='utf-8'))
            self.assertFalse(review['checks'][roles.PARTS[2]]['passed'])
            self.assertTrue(any('Financial numeric prose' in issue for issue in review['checks'][roles.PARTS[2]]['issues']))
            outputs = json.loads((path / 'role_outputs.json').read_text(encoding='utf-8'))
            self.assertEqual(outputs[roles.FINANCIAL]['sections'][roles.PARTS[2]][1]['text'], wrong_claim)

    def test_failed_recommendation_thesis_reason_and_all_sections_remain_visible(self):
        class BadAdvisorClient(FakeClient):
            def respond(inner, role, task, context, schema):
                result = super().respond(role, task, context, schema)
                if role == roles.ADVISOR:
                    result['thesis'] = 'The thesis claims 99.99% growth.'
                    result['reason'] = 'The deciding number causes accelerating performance.'
                return result
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            path = Path(tmp)
            setup_run(path)
            client = BadAdvisorClient()
            text = roles.StrategyManager(path, client).run_desk().read_text(encoding='utf-8')
            recommendation = text.split('## Investment Recommendation', 1)[1]
            self.assertIn('The thesis claims 99.99% growth.', recommendation)
            self.assertIn('The deciding number causes accelerating performance.', recommendation)
            self.assertIn('HUMAN AUDIT REQUIRED — Thesis', recommendation)
            self.assertIn('HUMAN AUDIT REQUIRED — Deciding-number reason', recommendation)
            self.assertIn('**Hold**', recommendation)
            self.assertIn('subject.pe:', recommendation)
            for part in roles.PARTS:
                self.assertIn('## ' + part, text)
            self.assertNotIn('This part is withheld', text)
            self.assertEqual(sum(role == roles.ADVISOR for role, _, _ in client.calls), 2)

    def test_legacy_quote_metadata_and_unsupplied_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            setup_run(path)
            _, sources, research, financial = roles.build_context(path)
            catalog = roles.EvidenceCatalog(sources, research, financial)
            supplied = catalog.select(catalog.research_ids)
            legacy = {**block(evidence=[text_id(supplied)]), 'evidence': [
                {'source': 'mda', 'line': 2, 'quote': 'Invented quotation'}]}
            result = {"sections": {roles.PARTS[0]: [legacy],
                                    roles.PARTS[1]: [block(evidence=['CLI', 'metrics', 'mda'])]}, "limitations": []}
            errors = roles.validate_content(roles.RESEARCH, result, supplied)
            self.assertTrue(all(errors.values()))

    def test_section_extraction_avoids_toc_and_keeps_original_lines(self):
        lines = ["Item 1. Business | 3", "Item 1A. Risks | 9", "Item 7. MD&A | 23", "Item 8. Statements | 33",
                 "Item 1. Business", "Much longer actual business evidence than the table of contents.", "Item 1A. Risks"]
        result = roles.section_lines(lines, "1", ("1A", "2"))
        self.assertEqual([row["line"] for row in result], [5, 6])

    def test_calculator_rejects_unknown_units_periods_and_code(self):
        facts = {"a": {"val": 8, "unit": "USD", "start": "2024-01-01", "end": "2024-12-31"},
                 "b": {"val": 4, "unit": "USD", "start": "2024-01-01", "end": "2024-12-31"}}
        request = {"id": "ratio", "operation": "divide", "inputs": ["a", "b"]}
        self.assertEqual(roles.calculate_request(request, facts)["value"], 2)
        for bad in ("__import__('os')", "b"):
            changed = copy.deepcopy(facts)
            changed["b"]["unit"] = "EUR"
            with self.assertRaises(roles.RoleError):
                roles.calculate_request({**request, "inputs": ["a", bad]}, changed)
        facts["b"]["end"] = "2023-12-31"
        with self.assertRaises(roles.RoleError):
            roles.calculate_request(request, facts)


class CitationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        setup_run(self.path)
        self.fixture = json.loads((Path(__file__).parent / 'tests/fixtures/lab03_citation_failures.json').read_text(encoding='utf-8'))
        lines = [''] * 400
        for number, text in self.fixture['lines'].items():
            lines[int(number) - 1] = text
        for number, text in [(90, 'Item 1. Business'), (200, 'Item 1A. Risks'),
                             (370, "Item 7. Management's Discussion"), (400, 'Item 8. Statements')]:
            lines[number - 1] = text
        (self.path / '10-K.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.run, sources, research, financial = roles.build_context(self.path)
        self.catalog = roles.EvidenceCatalog(sources, research, financial)
        self.research = self.catalog.select(self.catalog.research_ids)
        self.financial = self.catalog.select(self.catalog.financial_ids)
        # Any accidental HTTP use during a regression test must fail immediately.
        self.network = patch.object(roles.requests.Session, 'request', side_effect=AssertionError('No network allowed'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.tmp.cleanup()

    def research_result(self, key):
        return {'sections': {part: [block(evidence=[key])] for part in roles.PARTS[:2]}, 'limitations': []}

    def test_saved_failure_modes_cannot_be_written_in_new_response_schema(self):
        schema = roles.analyst_schema(roles.RESEARCH, self.research)
        valid_id = text_id(self.research, 96)
        for failure in self.fixture['invalid_citations']:
            with self.subTest(failure=failure['case']):
                legacy = self.research_result(valid_id)
                legacy['sections'][roles.PARTS[0]][0]['evidence'] = [{
                    key: failure[key] for key in ('source', 'line', 'quote')}]
                with self.assertRaises(roles.RoleError):
                    roles.validate_shape(legacy, schema)
                invented_id = f"{failure['source']}.L{failure['line']}"
                roles.validate_shape(self.research_result(invented_id), schema)
                errors = roles.validate_content(roles.RESEARCH, self.research_result(invented_id), self.research)
                self.assertTrue(all(errors.values()))

    def test_regression_fixture_reproduces_unicode_ellipsis_and_wrong_line(self):
        lines = self.fixture['lines']
        failures = {row['case']: row for row in self.fixture['invalid_citations']}
        unicode_quote = failures['unicode_hyphen_replaces_ascii']['quote']
        self.assertNotIn(unicode_quote, lines['96'])
        self.assertIn(unicode_quote.replace('\u2011', '-'), lines['96'])
        self.assertNotIn(failures['ellipsis_abbreviates_line']['quote'], lines['386'])
        quote = failures['heading_instead_of_competition_paragraph']['quote']
        self.assertNotIn(quote, lines['152'])
        self.assertEqual(quote, lines['153'])

    def test_quotation_and_line_numbers_are_materialized_from_saved_text(self):
        original = (self.path / '10-K.txt').read_text(encoding='utf-8').splitlines()
        for number in (96, 152, 153, 386):
            key = text_id(self.research, number)
            record = self.research[key]
            self.assertEqual(record['text'], original[number - 1])
            rendered = self.catalog.render(key)
            self.assertEqual(rendered[0], '> ' + original[number - 1])
            self.assertIn(f'10-K.txt, line {number}', '\n'.join(rendered))
        self.assertEqual(self.catalog.render(text_id(self.research, 152))[0], '> Competition')
        self.assertIn('• We opened 28 new warehouses', self.catalog.render(text_id(self.research, 386))[0])

    def test_source_and_evidence_ids_are_stable_for_the_same_saved_sources(self):
        _, sources, research, financial = roles.build_context(self.path)
        again = roles.EvidenceCatalog(sources, research, financial)
        self.assertEqual(self.catalog.snapshot(), again.snapshot())
        for key in self.research:
            if self.research[key]['kind'] != 'text':
                continue
            source = self.catalog.sources[self.research[key]['source_id']]
            self.assertIn(source['accession'], self.research[key]['source_id'])
            self.assertTrue(source['text_sha256'])

    def test_research_cannot_cite_saved_quarterly_text_outside_its_context(self):
        key = text_id(self.catalog.records, 1, '10-Q.txt')
        self.assertNotIn(key, self.research)
        errors = roles.validate_content(roles.RESEARCH, self.research_result(key), self.research)
        self.assertTrue(all(errors.values()))
        roles.validate_shape(self.research_result(key), roles.analyst_schema(roles.RESEARCH, self.research))
        errors = roles.validate_content(roles.RESEARCH, self.research_result(key), self.research)
        self.assertTrue(all(errors.values()))

    def test_research_transport_keeps_only_compact_supplied_lines_and_limits(self):
        context = {'company': {'ticker': 'TEST'},
                   'sources': {'10-K:test:file': {'form': '10-K', 'text_sha256': 'abc'}},
                   'evidence': {'text.abc.L1': {'kind': 'text', 'source_id': '10-K:test:file',
                                                'line': 1, 'text': 'A saved filing line.'},
                               'missing.market_share': {'kind': 'limitation', 'statement': 'No extracted measure.'}}}
        compact = roles.compact_research_context(context)
        self.assertEqual(compact['evidence'], {'text.abc.L1': 'A saved filing line.'})
        self.assertEqual(compact['limitations'], {'missing.market_share': 'No extracted measure.'})
        self.assertEqual(compact['sources'], context['sources'])
        self.assertNotIn('source_id', json.dumps(compact))
        self.assertEqual(roles.analyst_schema(roles.RESEARCH, context['evidence'])['$defs']['evidence_id'],
                         {'type': 'string'})

    def test_financial_scope_keeps_steps_four_to_seven_and_compacts_provenance(self):
        manager = roles.StrategyManager(self.path, FakeClient())
        keys = roles.financial_evidence_ids(manager.catalog)
        supplied = manager.catalog.select(keys)
        self.assertTrue({'subject.revenue.current', 'subject.operating_margin',
                         'quarter.revenue', 'peer.PEER.operating_margin',
                         'comparison.operating_margin'} <= set(supplied))
        text_rows = {key: row for key, row in supplied.items() if row['kind'] == 'text'}
        self.assertTrue(text_rows)
        self.assertTrue(all(manager.catalog.sources[row['source_id']]['form'] == '10-K' or
                            key in set().union(*(set(item['references']) for item in supplied.values()
                                                 if item['kind'] == 'release_evidence'))
                            for key, row in text_rows.items()))
        context = {'company': manager.run['company'], 'evidence': supplied,
                   'sources': manager.catalog.source_records(supplied),
                   'step4_facts': manager.financial['step4_facts'],
                   'step5_metrics': manager.financial['step5_metrics'],
                   'step6_quarter': manager.catalog.quarter_ids,
                   'step7_peers': manager.financial['step7_peers']}
        compact = roles.compact_financial_context(context)
        fact = compact['evidence']['subject.revenue.current']
        self.assertNotIn('accn', fact)
        self.assertNotIn('filing_url', fact)
        self.assertIn(fact['source_id'], compact['sources'])
        self.assertEqual(compact['sources'][fact['source_id']]['accession'],
                         supplied['subject.revenue.current']['accn'])
        self.assertIsInstance(compact['evidence'][next(iter(text_rows))], str)
        self.assertNotIn('references', compact['evidence']['subject.pe'])

    def test_financial_provenance_and_arithmetic_need_no_invented_cli_or_metrics_lines(self):
        result = {'sections': {roles.PARTS[2]: [block('The earnings multiple informs valuation.',
                  ['subject.pe', 'peer.PEER.operating_margin'])]},
                  'limitations': ['missing.quarterly_cash_from_operations'], 'calculator_requests': []}
        self.assertFalse(any(roles.validate_content(roles.FINANCIAL, result, self.financial).values()))
        self.assertEqual(self.financial['subject.pe']['arithmetic'], '30.0000 / 2.0000')
        self.assertEqual(self.financial['subject.pe']['references'], ['price', 'subject.diluted_eps.current'])
        eps = self.financial['subject.diluted_eps.current']
        self.assertEqual((eps['accn'], eps['tag'], eps['start'], eps['end']),
                         ('0000000001-25-000001', 'us-gaap:EarningsPerShareDiluted', '2024-07-01', '2025-06-30'))
        rendered = '\n'.join(self.catalog.render('subject.pe'))
        self.assertIn('30.0000 / 2.0000 = 15.0 multiple', rendered)
        self.assertIn('run.json /price', rendered)
        self.assertIn('market observation time not supplied', rendered)
        self.assertIn('us-gaap:EarningsPerShareDiluted', rendered)
        self.assertNotIn('CLI, line', rendered)
        self.assertNotIn('metrics, line', rendered)

    def test_quarter_evidence_keeps_saved_release_period_and_actual_lines(self):
        quarter = self.financial['quarter.revenue']
        actual = (self.path / 'earnings_release.txt').read_text(encoding='utf-8').splitlines()
        refs = [self.financial[key] for key in quarter['references']]
        self.assertTrue(any(row['text'] == actual[0] for row in refs))
        self.assertTrue(any(row['text'] == actual[1] for row in refs))
        for row in refs:
            self.assertEqual(row['text'], actual[row['line'] - 1])
            self.assertEqual(self.catalog.sources[row['source_id']]['accession'], '0000000001-25-000001')
        self.assertIn('Quarter ended June 30, 2025', '\n'.join(self.catalog.render('quarter.revenue')))

    def test_stale_step6_quote_is_rejected_instead_of_reused(self):
        path = self.path / 'release_lines.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['revenue'][0]['text'] = 'A model invented a different total.'
        roles.write_json(path, data)
        with self.assertRaisesRegex(roles.RoleError, 'Stale Step 6 evidence'):
            roles.StrategyManager(self.path, FakeClient())

    def populated_manager(self):
        # Use the small standard source for the fake client's text selector.
        setup_run(self.path)
        manager = roles.StrategyManager(self.path, FakeClient())
        for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
            manager.specialist(role, 'Perform the assigned analysis.')
        return manager

    def test_manager_rejects_unsupplied_id_even_when_review_model_passes(self):
        manager = self.populated_manager()
        manager.results[roles.FINANCIAL]['sections'][roles.PARTS[2]][0]['evidence_ids'].append('metrics')
        review = manager.review()
        self.assertFalse(review['checks'][roles.PARTS[2]]['passed'])
        self.assertIn('Unknown or unsupplied evidence ID: metrics.', review['checks'][roles.PARTS[2]]['issues'])
        brief = manager.client.calls[-1][2]
        self.assertIn('citation_audit', brief)
        self.assertIn('supplied_evidence', brief)
        self.assertNotIn('allowed_evidence_ids', brief)
        self.assertIn('Unknown or unsupplied evidence ID: metrics.',
                      ' '.join(brief['citation_audit'][roles.PARTS[2]]))
        self.assertNotIn('metrics', brief['supplied_evidence'])

    def test_manager_rejects_replaced_payload_quote_tag_period_or_arithmetic(self):
        manager = self.populated_manager()
        cases = [('subject.pe', 'arithmetic', 'invented calculation'),
                 ('subject.diluted_eps.current', 'tag', 'us-gaap:InventedTag'),
                 ('subject.diluted_eps.current', 'end', '1900-01-01'),
                 ('subject.diluted_eps.current', 'accn', 'invented accession')]
        for key, field, value in cases:
            with self.subTest(field=field):
                record = manager.results[roles.FINANCIAL]['cited_evidence'][key]
                original = record[field]
                record[field] = value
                review = manager.review()
                self.assertFalse(review['checks'][roles.PARTS[2]]['passed'])
                self.assertTrue(any('differs from supplied record' in issue for issue in review['checks'][roles.PARTS[2]]['issues']))
                record[field] = original
        key = text_id(manager.results[roles.RESEARCH]['cited_evidence'])
        manager.results[roles.RESEARCH]['cited_evidence'][key]['text'] = 'An invented quotation.'
        self.assertFalse(manager.review()['checks'][roles.PARTS[0]]['passed'])

    def test_advisor_schema_excludes_uncited_global_calculations(self):
        manager = self.populated_manager()
        supplied = manager.supplied[roles.ADVISOR]
        self.assertNotIn('subject.revenue_growth', supplied)
        raw = copy.deepcopy(manager.results[roles.ADVISOR])
        raw.pop('cited_evidence')
        raw.pop('evidence_sources')
        raw['deciding_evidence_id'] = 'subject.revenue_growth'
        with self.assertRaises(roles.RoleError):
            roles.validate_shape(raw, roles.analyst_schema(roles.ADVISOR, supplied))
        errors = roles.validate_content(roles.ADVISOR, raw, supplied)
        self.assertTrue(errors[roles.PARTS[4]])

    def test_manager_requires_complete_evidence_closure_and_canonical_source_metadata(self):
        manager = self.populated_manager()
        result = manager.results[roles.FINANCIAL]
        missing = result['cited_evidence'].pop('subject.diluted_eps.current')
        self.assertFalse(manager.review()['checks'][roles.PARTS[2]]['passed'])
        result['cited_evidence']['subject.diluted_eps.current'] = missing
        result['evidence_sources'][missing['source_id']]['accession'] = 'invented accession'
        self.assertFalse(manager.review()['checks'][roles.PARTS[2]]['passed'])

    def test_audit_report_retains_claims_and_labels_unknown_or_altered_evidence_without_repair(self):
        manager = self.populated_manager()
        result = manager.results[roles.FINANCIAL]
        original_claim = 'Keep this generated claim unchanged for the human audit.'
        result['sections'][roles.PARTS[2]][0]['text'] = original_claim
        result['sections'][roles.PARTS[2]][0]['evidence_ids'].append('invented.source.L999')
        result['cited_evidence']['subject.pe']['arithmetic'] = '999 / 1'
        review = manager.review()
        before_outputs = copy.deepcopy(manager.results)
        before_review = copy.deepcopy(review)
        with redirect_stdout(io.StringIO()):
            text = manager.compile_report(review).read_text(encoding='utf-8')
        section = text.split('## Valuation Insights', 1)[1].split('## ', 1)[0]
        self.assertIn(original_claim, section)
        self.assertIn('Unvalidated evidence reference:** `invented.source.L999`', section)
        self.assertIn('Unknown or unsupplied evidence ID.', section)
        self.assertIn('Unvalidated evidence reference:** `subject.pe`', section)
        self.assertIn('Submitted payload (unvalidated; preserved for audit)', section)
        self.assertIn('999 / 1', section)
        self.assertNotIn('subject.pe: 30.0000 / 2.0000', section)
        self.assertFalse(review['checks'][roles.PARTS[2]]['passed'])
        self.assertEqual(before_outputs, manager.results)
        self.assertEqual(before_review, review)

    def test_audit_report_preserves_unvalidated_free_form_limitation_entry(self):
        manager = self.populated_manager()
        limitation = 'This generated unsupported missing-data assertion must remain visible.'
        manager.results[roles.FINANCIAL]['limitations'].append(limitation)
        review = manager.review()
        with redirect_stdout(io.StringIO()):
            text = manager.compile_report(review).read_text(encoding='utf-8')
        self.assertIn(limitation, text)
        self.assertIn('HUMAN AUDIT REQUIRED — Unvalidated generated limitation entry', text)
        self.assertFalse(review['checks'][roles.PARTS[2]]['passed'])

    def test_manager_brief_deduplicates_shared_payloads_without_dropping_analysis_or_evidence(self):
        setup_run(self.path)
        source = self.path / '10-K.txt'
        source.write_text(source.read_text(encoding='utf-8').replace(
            'The business sells subscriptions.', 'The business sells subscriptions. ' + 'Source evidence. ' * 4000), encoding='utf-8')
        manager = roles.StrategyManager(self.path, FakeClient())
        for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
            manager.specialist(role, 'Perform the assigned analysis.')
        manager.review()
        context = manager.client.calls[-1][2]
        compact = roles.compact_strategy_context(context)
        self.assertNotIn('allowed_evidence_ids', compact)
        self.assertEqual(compact['outputs'], context['outputs'])
        self.assertEqual(compact['citation_audit'], context['citation_audit'])
        text_keys = [key for key, row in context['supplied_evidence'].items() if row['kind'] == 'text']
        if text_keys:
            self.assertEqual(compact['supplied_evidence'][text_keys[0]],
                             context['supplied_evidence'][text_keys[0]]['text'])
        fact_keys = [key for key, row in context['supplied_evidence'].items() if row['kind'] == 'xbrl_fact']
        if fact_keys:
            fact = context['supplied_evidence'][fact_keys[0]]
            compact_fact = compact['supplied_evidence'][fact_keys[0]]
            self.assertNotIn('accn', compact_fact)
            self.assertEqual(compact['sources'][fact['source_id']]['accession'], fact['accn'])
        for role, original in manager.results.items():
            self.assertEqual(context['outputs'][role]['sections'], original['sections'])
            self.assertNotIn('cited_evidence', context['outputs'][role])
            self.assertNotIn('evidence_sources', context['outputs'][role])
            for key, row in original['cited_evidence'].items():
                self.assertEqual(context['supplied_evidence'][key], row)
                self.assertEqual(context['sources'][row['source_id']], original['evidence_sources'][row['source_id']])
        envelope = {'task': 'Review all parts.', 'context': compact, 'expected_output': roles.REVIEW_SCHEMA}
        input_size = len(roles.INSTRUCTIONS[roles.MANAGER]) + len(json.dumps(envelope, ensure_ascii=False)) + len(json.dumps(roles.REVIEW_SCHEMA))
        self.assertLess(input_size, 190_000)
        duplicated = copy.deepcopy(context)
        duplicated['outputs'] = manager.results
        old_size = len(json.dumps({**envelope, 'context': duplicated}, ensure_ascii=False))
        self.assertGreater(old_size, 190_000)

    def test_missing_financial_input_remains_na_and_is_not_a_calculator_operand(self):
        facts = json.loads((self.path / 'facts.json').read_text(encoding='utf-8'))
        facts['concepts']['current_debt']['current'] = None
        roles.write_json(self.path / 'facts.json', facts)
        with redirect_stdout(io.StringIO()):
            metrics = desk.calculate(facts, 30)
        roles.write_json(self.path / 'metrics.json', metrics)
        manager = roles.StrategyManager(self.path, FakeClient())
        supplied = manager.catalog.select(manager.catalog.financial_ids)
        self.assertEqual(supplied['subject.current_debt.current']['kind'], 'missing_input')
        self.assertIsNone(supplied['subject.debt_to_equity']['value'])
        rendered = '\n'.join(manager.catalog.render('subject.debt_to_equity'))
        self.assertIn('N/A', rendered)
        operands = roles.analyst_schema(roles.FINANCIAL, supplied)['properties']['calculator_requests']['items']['properties']['inputs']['items']['enum']
        self.assertNotIn('subject.current_debt.current', operands)

    def test_empty_evidence_scope_can_disclose_missing_sources_without_inventing_ids(self):
        result = {'sections': {part: [block('Source evidence is unavailable.')] for part in roles.PARTS[:2]},
                  'limitations': []}
        schema = roles.analyst_schema(roles.RESEARCH, {})
        roles.validate_shape(result, schema)
        result['sections'][roles.PARTS[0]][0]['evidence_ids'] = ['invented']
        with self.assertRaises(roles.RoleError):
            roles.validate_shape(result, schema)


class TransportTests(unittest.TestCase):
    def test_documented_endpoint_model_isolated_envelope_and_no_key_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            client = roles.ModelClient("secret-test-key", tmp)
            response = Mock(ok=True, status_code=200)
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"tasks": {role: "Work." for role in
                                                  (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR)}})}}]}
            client.session.post = Mock(return_value=response)
            client.call(roles.MANAGER, "Delegate", {"only": "this context"}, roles.PLAN_SCHEMA)
            args, kwargs = client.session.post.call_args
            self.assertEqual(args[0], roles.ENDPOINT)
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-test-key")
            body = kwargs["json"]
            self.assertEqual(body["model"], "gpt-oss:120b")
            self.assertEqual(len(body["messages"]), 2)
            envelope = json.loads(body["messages"][1]["content"])
            self.assertEqual(set(envelope), {"task", "context", "expected_output"})
            self.assertEqual(envelope["context"], {"only": "this context"})
            self.assertTrue(all("secret-test-key" not in p.read_text() for p in Path(tmp).iterdir()))

    def test_null_rate_limit_is_retried_and_failure_does_not_leak_key(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), patch.object(roles.time, "sleep"):
            client = roles.ModelClient("secret-test-key", tmp)
            response = Mock(ok=True, status_code=200)
            response.json.return_value = None
            client.session.post = Mock(return_value=response)
            with self.assertRaisesRegex(roles.RoleError, "null after rate-limit retries") as error:
                client.call(roles.MANAGER, "Delegate", {}, roles.PLAN_SCHEMA)
            self.assertEqual(client.session.post.call_count, 4)
            self.assertNotIn("secret-test-key", str(error.exception))


class CommandTests(unittest.TestCase):
    def test_full_command_runs_seven_steps_then_roles_with_dotenv_key_and_prints_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup_run(root)
            facts = desk.extract_facts(fixture(), COMPANY)
            printed = io.StringIO()
            report = root / "report.md"
            with patch.object(desk, "ROOT", root), patch.object(desk, "dotenv_values", return_value={
                "SEC_USER_AGENT": "Contact contact@example.test", "API_KEY": " file-key "}) as env, \
                patch.object(desk, "resolve", return_value=COMPANY), \
                patch.object(desk, "select_filings", return_value={}), \
                patch.object(desk, "download_documents", return_value={"release": str(root / "earnings_release.txt")}), \
                patch.object(desk, "collect_facts", return_value=facts), \
                patch.object(roles, "run_roles", return_value=report) as run_roles, redirect_stdout(printed):
                self.assertEqual(desk.main(["TEST", "--peers", "PEER", "--price", "30"]), 0)
            env.assert_called_once_with(root / ".env")
            self.assertEqual(run_roles.call_args.args[1], "file-key")
            self.assertEqual(run_roles.call_args.args[0].parent, root / "research_data")
            for step in range(1, 8):
                self.assertIn(f"Step {step}/7", printed.getvalue())
            self.assertIn("Report: " + str(report.resolve()), printed.getvalue())

    def test_full_command_requires_key_before_sec_requests(self):
        with patch.object(desk, "dotenv_values", return_value={"SEC_USER_AGENT": "Contact contact@example.test"}), \
             patch.object(desk, "SECClient") as client:
            with self.assertRaisesRegex(desk.DeskError, "API_KEY"):
                desk.main(["TEST", "--price", "30"])
            client.assert_not_called()

    def test_single_step_does_not_call_roles_or_require_api_key(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(desk, "ROOT", Path(tmp)), \
             patch.object(desk, "dotenv_values", return_value={"SEC_USER_AGENT": "Contact contact@example.test"}), \
             patch.object(desk, "resolve", return_value=COMPANY), \
             patch.object(roles, "run_roles") as run_roles, redirect_stdout(io.StringIO()):
            self.assertEqual(desk.main(["TEST", "--step", "1"]), 0)
            run_roles.assert_not_called()


if __name__ == "__main__":
    unittest.main()
