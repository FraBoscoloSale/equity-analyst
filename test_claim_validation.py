"""Offline regressions for the second run's unsupported claims and false positives."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import equity_desk as desk
import research_roles as roles
from audit_saved_run import audit_saved_run
from claim_validation import claim_errors, financial_numeric_literals, numeric_record_errors, review_policy
from test_research_roles import setup_run, FakeClient, block, text_id


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        setup_run(self.path)
        self.cases = json.loads((Path(__file__).parent / 'tests/fixtures/lab03_claim_failures.json').read_text(encoding='utf-8'))
        annual = self.path / '10-K.txt'
        annual.write_text('Item 1. Business\nThe business sells subscriptions.\n' + self.cases['categories'] + '\n'
                          + self.cases['segments'] + '\nItem 1A. Risks\nItem 7. Management Discussion\n'
                          + self.cases['contribution'] + '\n' + self.cases['point_in_time'] + '\nItem 8. Statements\n', encoding='utf-8')
        facts = json.loads((self.path / 'facts.json').read_text(encoding='utf-8'))
        facts['concepts']['revenue']['current']['val'] = 100
        facts['concepts']['operating_income']['current']['val'] = self.cases['subject_margin'] * 100
        roles.write_json(self.path / 'facts.json', facts)
        peers = {}
        with redirect_stdout(io.StringIO()):
            roles.write_json(self.path / 'metrics.json', desk.calculate(facts, 30))
            for ticker, value in self.cases['peer_margins'].items():
                peer = copy.deepcopy(facts)
                peer['company']['ticker'] = ticker
                peer['concepts']['operating_income']['current']['val'] = value * 100
                peers[ticker] = {'facts': peer, 'metrics': desk.calculate(peer, margin_only=True)}
        roles.write_json(self.path / 'peers.json', peers)
        self.manager = roles.StrategyManager(self.path, None)
        self.catalog = self.manager.catalog
        self.financial = self.catalog.select(self.catalog.financial_ids)
        self.research = self.catalog.select(self.catalog.research_ids)
        self.network = patch.object(roles.requests.Session, 'request', side_effect=AssertionError('Network prohibited'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.tmp.cleanup()

    def test_false_cost_vs_peer_margin_comparison_is_rejected(self):
        refs = ['peer.BJ.operating_margin', 'peer.WMT.operating_margin', 'peer.TGT.operating_margin']
        errors = claim_errors(self.cases['false_comparison'], refs, self.financial)
        self.assertTrue(any('Unverified peer comparison' in error for error in errors))
        row = self.financial['comparison.operating_margin']
        self.assertEqual(row['higher_than'], ['BJ'])
        self.assertEqual(row['lower_than'], ['TGT', 'WMT'])
        self.assertEqual(row['equal_to'], [])
        self.assertFalse(claim_errors(row['statement'], ['comparison.operating_margin'], self.financial))

    def test_highest_lowest_and_true_but_model_authored_peer_rankings_are_not_accepted(self):
        for claim in ('The operating margin is highest among peers.',
                      'The operating margin is lowest among competitors.',
                      'The operating margin is higher than BJ.'):
            self.assertTrue(any('Unverified peer comparison' in error for error in
                               claim_errors(claim, ['comparison.operating_margin'], self.financial)))

    def test_peer_comparison_does_not_rank_unavailable_or_incompatible_values(self):
        peers = json.loads((self.path / 'peers.json').read_text(encoding='utf-8'))
        peers['WMT']['metrics']['operating_margin']['value'] = None
        peers['WMT']['metrics']['operating_margin']['reason'] = 'Missing inputs'
        peers['TGT']['metrics']['operating_margin']['unit'] = 'USD'
        roles.write_json(self.path / 'peers.json', peers)
        row = roles.StrategyManager(self.path, None).catalog.records['comparison.operating_margin']
        self.assertEqual(row['unavailable'], ['TGT', 'WMT'])
        self.assertNotIn('highest', row['statement'])
        self.assertIn('not comparable', row['statement'])

    def test_product_categories_cannot_be_relabelled_as_segments(self):
        categories = text_id(self.research, 3)
        actual_segments = text_id(self.research, 4)
        for refs in ([categories], [categories, actual_segments]):
            errors = claim_errors(self.cases['false_segments'], refs, self.research)
            self.assertTrue(any('Segment terminology' in error for error in errors))
        self.assertFalse(claim_errors(self.cases['segments'], [actual_segments], self.research))
        self.assertFalse(claim_errors(self.cases['categories'], [categories], self.research))

    def test_price_causality_and_acceleration_are_not_inferred_from_contributions(self):
        refs = [text_id(self.research, 7), text_id(self.research, 8)]
        for field in ('false_price_claim', 'false_acceleration', 'false_causality'):
            errors = claim_errors(self.cases[field], refs, self.research)
            self.assertTrue(any('Causality or acceleration' in error for error in errors), field)

    def test_explicit_source_causality_can_be_quoted_without_increasing_its_strength(self):
        refs = [text_id(self.research, 7)]
        # Actual contribution wording is allowed, stronger inferences are not.
        self.assertFalse(claim_errors(self.cases['contribution'], refs, self.research))
        self.assertTrue(claim_errors('Higher gasoline prices caused accelerating revenue growth.', refs, self.research))

    def test_10q_and_other_source_identifiers_are_not_financial_numeric_claims(self):
        for text in ('The 10-Q provides source context.', 'The 10\u2011Q provides source context.',
                     'Read the 10-K, 8-K and 10-Q/A.',
                     'See accession 0000000001-25-000001.', 'See subject.diluted_eps.current.',
                     'See us-gaap:EarningsPerShareDiluted.', 'Period ends 2025-06-30.', 'FY2025 source period.'):
            self.assertFalse(financial_numeric_literals(text, self.financial), text)
        for text in ('Revenue was $992.13.', 'Operating margin was 3.85%.', 'P/E is 47.79.', 'Growth was 21%.'):
            self.assertTrue(financial_numeric_literals(text, self.financial), text)
        self.assertFalse(claim_errors('The 10-Q is a source reference.', [text_id(self.research, 2)], self.research))
        self.assertFalse(financial_numeric_literals(self.cases['false_missing'].replace('10-Q', '10\u2011Q'), self.financial))

    def test_unknown_accessions_tags_and_evidence_ids_cannot_hide_financial_numbers(self):
        self.assertTrue(claim_errors('See 0000000009-99-999999.', ['subject.pe'], self.financial))
        self.assertTrue(claim_errors('See us-gaap:InventedMetric.', ['subject.pe'], self.financial))
        self.assertTrue(financial_numeric_literals('See text.invented.L999.', self.financial))

    def test_valid_deterministic_ratio_needs_no_filing_narrative(self):
        self.assertFalse(numeric_record_errors('subject.pe', self.financial))
        self.assertFalse(claim_errors('The earnings multiple informs valuation.', ['subject.pe'], self.financial))
        self.assertIsNotNone(review_policy('missing_ratio_narrative', self.cases['ratio_review']))
        # The false peer claim in that same old review is still independently blocked.
        self.assertIsNotNone(review_policy('unsupported_claim', self.cases['ratio_review']))

    def test_ratio_missing_tag_or_arithmetic_is_not_accepted(self):
        for key, field, value in [('subject.diluted_eps.current', 'tag', ''),
                                  ('subject.pe', 'arithmetic', '30 / 3'),
                                  ('subject.pe', 'references', [])]:
            scope = copy.deepcopy(self.financial)
            scope[key][field] = value
            self.assertTrue(numeric_record_errors('subject.pe', scope), field)

    def test_rounded_display_arithmetic_uses_the_exact_supplied_operands(self):
        scope = copy.deepcopy(self.financial)
        scope['subject.diluted_eps.current']['val'] = 0.00454
        scope['subject.pe']['value'] = 30 / 0.00454
        scope['subject.pe']['arithmetic'] = '30.0000 / 0.0045'
        self.assertFalse(numeric_record_errors('subject.pe', scope))
        scope['subject.pe']['arithmetic'] = '30.0000 / 0.0030'
        self.assertTrue(numeric_record_errors('subject.pe', scope))

    def test_missing_data_uses_inventory_records_not_free_form_nondisclosure(self):
        key = 'missing.quarterly_cash_from_operations'
        row = self.financial[key]
        self.assertFalse(claim_errors(row['statement'], [key], self.financial))
        self.assertTrue(claim_errors(self.cases['false_missing'], [], self.financial))
        self.assertTrue(claim_errors('The issuer does not disclose quarterly cash flow.', [key], self.financial))
        self.assertTrue(claim_errors('Quarterly revenue is not available.', ['quarter.revenue'], self.financial))
        self.assertNotIn('missing.quarterly_revenue', self.financial)
        self.assertNotIn('missing.quarterly_diluted_eps', self.financial)

    def test_market_share_absence_is_not_asserted_when_explicit_source_evidence_exists(self):
        self.assertIn('missing.market_share', self.research)
        path = self.path / '10-K.txt'
        path.write_text(path.read_text(encoding='utf-8').replace('The business sells subscriptions.',
                        'Our market share was 12%.'), encoding='utf-8')
        manager = roles.StrategyManager(self.path, None)
        self.assertNotIn('missing.market_share', manager.catalog.records)

    def test_schema_limitations_cannot_be_model_authored_and_allow_separate_limitations(self):
        result = {'sections': {roles.PARTS[2]: [block('The earnings multiple informs valuation.', ['subject.pe'])]},
                  'limitations': ['missing.quarterly_cash_from_operations'], 'calculator_requests': []}
        roles.validate_shape(result, roles.analyst_schema(roles.FINANCIAL, self.financial))
        self.assertFalse(any(roles.validate_content(roles.FINANCIAL, result, self.financial).values()))
        result['limitations'] = [self.cases['false_missing']]
        with self.assertRaises(roles.RoleError):
            roles.validate_shape(result, roles.analyst_schema(roles.FINANCIAL, self.financial))
        self.assertTrue(any(roles.validate_content(roles.FINANCIAL, result, self.financial).values()))

    def test_required_missing_inputs_must_be_disclosed(self):
        result = {'sections': {roles.PARTS[2]: [block('The earnings multiple informs valuation.', ['subject.pe'])]},
                  'limitations': [], 'calculator_requests': []}
        errors = roles.validate_content(roles.FINANCIAL, result, self.financial)
        self.assertTrue(any('Required input limitations' in e for e in errors[roles.PARTS[2]]))
        result['limitations'] = ['missing.quarterly_cash_from_operations']
        self.assertFalse(any(roles.validate_content(roles.FINANCIAL, result, self.financial).values()))

    def test_reviewer_cannot_require_limitation_placement_or_extra_deciding_metrics(self):
        self.assertIsNotNone(review_policy('limitation_placement', self.cases['limitation_review']))
        self.assertIsNotNone(review_policy('additional_deciding_metrics', self.cases['recommendation_review']))
        self.assertIsNone(review_policy('contradiction', 'The margin comparison contradicts the numeric evidence.'))

    def test_advisor_receives_only_supported_analyst_claims_and_their_evidence(self):
        manager = self.manager
        manager.supplied[roles.RESEARCH] = self.research
        manager.supplied[roles.FINANCIAL] = self.financial
        research = {'sections': {part: [block('The business sells subscriptions.', [text_id(self.research, 2)])]
                                for part in roles.PARTS[:2]}, 'limitations': ['missing.market_share']}
        financial = {'sections': {roles.PARTS[2]: [
            block(self.cases['false_comparison'], ['peer.TGT.operating_margin', 'peer.WMT.operating_margin']),
            block('The earnings multiple informs valuation.', ['subject.pe'])]},
            'limitations': ['missing.quarterly_cash_from_operations'], 'calculator_requests': []}
        manager.results = {roles.RESEARCH: manager.enrich(roles.RESEARCH, research),
                           roles.FINANCIAL: manager.enrich(roles.FINANCIAL, financial)}
        context = manager.analyst_context()
        result = context[roles.FINANCIAL]
        self.assertEqual(len(result['sections'][roles.PARTS[2]]), 1)
        self.assertNotIn('peer.TGT.operating_margin', result['cited_evidence'])
        self.assertIn('subject.pe', result['cited_evidence'])
        self.assertIn('missing.quarterly_cash_from_operations', result['cited_evidence'])

    def test_additional_recommendation_metrics_need_their_own_valid_support(self):
        self.assertTrue(claim_errors('Free cash flow supports the recommendation.', ['subject.pe'], self.financial))
        self.assertFalse(claim_errors('Free cash flow supports the recommendation.', ['subject.free_cash_flow'], self.financial))
        scope = {key: row for key, row in self.financial.items() if key != 'subject.free_cash_flow'}
        self.assertTrue(claim_errors('Free cash flow supports the recommendation.', ['subject.free_cash_flow'], scope))

    def test_recommendation_renders_the_thesis_and_additional_reason_evidence(self):
        setup_run(self.path)
        class SupportingClient(FakeClient):
            def respond(inner, role, task, context, schema):
                result = super().respond(role, task, context, schema)
                if role == roles.FINANCIAL:
                    result['sections'][roles.PARTS[2]][0]['evidence_ids'].append('subject.free_cash_flow')
                elif role == roles.ADVISOR:
                    result['reason'] = 'The earnings multiple and free cash flow inform the recommendation.'
                    result['reason_evidence_ids'] = ['subject.pe', 'subject.free_cash_flow']
                return result
        with redirect_stdout(io.StringIO()):
            report = roles.StrategyManager(self.path, SupportingClient()).run_desk().read_text(encoding='utf-8')
        recommendation = report.split('## Investment Recommendation', 1)[1]
        self.assertIn('subject.free_cash_flow:', recommendation)
        self.assertIn('The business sells subscriptions.', recommendation)
        self.assertIn('us-gaap:EarningsPerShareDiluted', recommendation)

    def test_contract_policy_records_false_positive_exclusions_and_keeps_real_failures(self):
        # Exercise the real manager policy with a deterministic fake reviewer.
        setup_run(self.path)
        class Reviewer(FakeClient):
            def respond(inner, role, task, context, schema):
                result = super().respond(role, task, context, schema)
                if role == roles.MANAGER and 'checks' in result:
                    result['checks'][roles.PARTS[2]] = {'passed': False, 'issues': [
                        {'code': 'missing_ratio_narrative', 'text': self.cases['ratio_review']}]}
                    result['checks'][roles.PARTS[0]] = {'passed': False, 'issues': [
                        {'code': 'contradiction', 'text': 'A substantive contradiction remains.'}]}
                return result
        manager = roles.StrategyManager(self.path, Reviewer())
        for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
            manager.specialist(role, 'Perform assigned analysis.')
        review = manager.review()
        self.assertTrue(review['checks'][roles.PARTS[2]]['passed'])
        self.assertTrue(review['policy_exclusions'][roles.PARTS[2]])
        self.assertFalse(review['checks'][roles.PARTS[0]]['passed'])

    def test_offline_saved_run_audit_preserves_artifacts_and_never_calls_network(self):
        setup_run(self.path)
        class RecordedClient(FakeClient):
            def call(inner, role, task, context, schema):
                result = super().call(role, task, context, schema)
                filename = f"role_{len(inner.calls):02d}_{role.lower().replace(' ', '_')}_request.json"
                roles.write_json(self.path / filename, {'messages': [
                    {'role': 'system', 'content': ''},
                    {'role': 'user', 'content': json.dumps({'context': context})}]})
                return result
        with redirect_stdout(io.StringIO()):
            roles.StrategyManager(self.path, RecordedClient()).run_desk()
        before = {file.name: file.read_bytes() for file in self.path.iterdir()}
        audit = audit_saved_run(self.path)
        self.assertEqual(before, {file.name: file.read_bytes() for file in self.path.iterdir()})
        for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
            result = audit['roles'][role]
            self.assertFalse(result['unknown_ids'])
            self.assertFalse(result['changed_records'])
            self.assertFalse(result['numerical_provenance_errors'])
            self.assertFalse(any(result['part_errors'].values()))


if __name__ == '__main__':
    unittest.main()
