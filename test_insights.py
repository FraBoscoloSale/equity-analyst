"""Offline tests of sourced Insights, bounded revisions and mandatory verification."""

import copy
import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agent_team as team
import research_roles as roles
from desk_verifier import Verifier, load_verifier_sources, SemanticResponseError
from insight_contract import (figures, insight_errors, opening_paragraph, part_insight_audit,
                              insight_locations, role_insight_audit)
from test_agent_team import ScriptedClient, saved_fixture
from test_sourced_claims import SemanticClient


class InsightContractTests(unittest.TestCase):
    def test_locations_derive_last_owned_part_from_existing_mappings(self):
        self.assertEqual(insight_locations(roles.PARTS, roles.OWNERS), {
            roles.RESEARCH: 'Market Positioning and Growth Outlook',
            roles.FINANCIAL: 'Valuation Insights', roles.ADVISOR: 'Investment Recommendation'})
        self.assertEqual(insight_locations(['first', 'second', 'third'],
                         {'first': 'alpha', 'second': 'beta', 'third': 'alpha'}),
                         {'alpha': 'third', 'beta': 'second'})

    def test_whole_role_scope_detects_duplicates_misplacement_embedded_and_missing(self):
        source = {'k': {'kind': 'text', 'source_id': '10-K:x:10-K.txt', 'line': 4,
                        'text': 'Operating income was $12 million.'}}
        line = {'text': 'Insight: Operating income was $12 million. [10-K.txt, line 4]', 'evidence_ids': ['k']}
        prose = {'text': 'The filing supports the analysis.', 'evidence_ids': ['k']}
        opening = {'text': 'The issuer announced its results.'}
        valid = {'first': [prose], 'last': [prose, line]}
        self.assertTrue(all(not issues for issues in role_insight_audit(valid, source, opening, 'last')[0].values()))
        cases = {
            'missing': {'first': [prose], 'last': [prose]},
            'duplicate': {'first': [prose], 'last': [prose, line, line]},
            'duplicate_other_part': {'first': [prose, line], 'last': [prose, line]},
            'misplaced': {'first': [prose, line], 'last': [prose]},
            'embedded': {'first': [prose], 'last': [{**line, 'text': 'Analysis. '+line['text']}]},
            'multiline': {'first': [prose], 'last': [{**line, 'text': 'Analysis.\n'+line['text']}]},
            'nonfinal': {'first': [prose], 'last': [line, prose]},
        }
        for name, sections in cases.items():
            with self.subTest(case=name):
                before = copy.deepcopy(sections)
                errors, audit = role_insight_audit(sections, source, opening, 'last')
                self.assertTrue(errors['last'])
                if name in ('misplaced', 'duplicate_other_part'):
                    self.assertTrue(errors['first'])
                self.assertEqual(sections, before)
        errors, _ = role_insight_audit(valid, source, opening, 'last', extra_text=[line['text']])
        self.assertTrue(errors['last'])

    def test_one_claim_requires_an_actual_supplied_line(self):
        row = {'kind': 'text', 'source_id': '10-K:accession:10-K.txt', 'line': 7,
               'text': 'Operating income was $12 million. Revenue was $80 million.'}
        supplied = {'text.saved.L7': row}
        block = {'text': 'Insight: Operating income was $12 million. [10-K.txt, line 7]',
                 'evidence_ids': ['text.saved.L7']}
        self.assertEqual(insight_errors(block, supplied), [])
        for changed in ('Insight: Operating income was $13 million. [10-K.txt, line 7]',
                        'Insight: Operating income was $12 million. [10-Q.txt, line 7]',
                        'Insight: Operating income was $12 million. [10-K.txt, line 8]',
                        'Insight: Operating income was $12 million. Revenue was $80 million. [10-K.txt, line 7]',
                        'Insight: Operating income was $12 million.'):
            with self.subTest(text=changed):
                self.assertTrue(insight_errors({**block, 'text': changed}, supplied))
        self.assertTrue(insight_errors({**block, 'evidence_ids': []}, supplied))

    def test_opening_paragraph_is_first_narrative_not_first_numeric_paragraph(self):
        text = ('EX-99.1\nEXHIBIT\nOPERATING RESULTS\n'
                'The issuer announced its results for the quarter.\n'
                'Revenue was $70 million.\n')
        opening = opening_paragraph(text)
        self.assertEqual(opening['line'], 4)
        self.assertEqual(opening['text'], 'The issuer announced its results for the quarter.')
        self.assertNotIn('70', opening['text'])
        wrapped = opening_paragraph('RESULTS\nRevenue increased to $70 million\nfrom $60 million last year.\n\nOther details.')
        self.assertEqual(wrapped['text'], 'Revenue increased to $70 million from $60 million last year.')

    def test_missing_misplaced_and_headline_insights_fail(self):
        claim = 'Revenue was $70 million.'
        supplied = {'k': {'kind': 'text', 'source_id': '8-K:x:earnings_release.txt', 'line': 2, 'text': claim}}
        block = {'text': f'Insight: {claim} [earnings_release.txt, line 2]', 'evidence_ids': ['k']}
        opening = {'text': claim}
        issues, _ = part_insight_audit([block], supplied, opening)
        self.assertTrue(any('repeats a figure' in issue for issue in issues))
        for blocks in ([], [block, {'text': 'More prose.', 'evidence_ids': []}], [block, block]):
            self.assertTrue(part_insight_audit(blocks, supplied, opening)[0])
        self.assertEqual(figures('$70 million'), figures('$0.070 billion'))
        self.assertEqual(figures('7 percent'), figures('7%'))


class InsightFlowTests(unittest.TestCase):
    def manager(self, folder, client):
        saved_fixture(folder)
        manager = team.TeamManager(folder, client)
        for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
            manager.specialist(role)
        return manager

    def test_exactly_three_insights_in_correct_parts_and_no_requirement_on_other_parts(self):
        class CleanClient(ScriptedClient):
            def call(self, role, task, context, schema):
                result = super().call(role, task, context, schema)
                if role == roles.ADVISOR and 'thesis' in result:
                    result['thesis'] = 'Recurring demand supports the thesis.'
                return result
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            client = CleanClient()
            manager = self.manager(Path(tmp), client)
            text = manager.finish().read_text(encoding='utf-8')
            designated = set(insight_locations(roles.PARTS, roles.OWNERS).values())
            self.assertEqual(sum(line.startswith('Insight:') for line in text.splitlines()), 3)
            for part in roles.PARTS:
                section = text.split('## '+part+'\n', 1)[1].split('\n## ', 1)[0]
                visible = [line for line in section.splitlines() if line.strip() and not line.startswith('<!--')]
                count = sum(line.startswith('Insight:') for line in visible)
                self.assertEqual(count, 1 if part in designated else 0)
                if part in designated:
                    self.assertTrue(visible[-1].startswith('Insight: '), visible[-1])
                    self.assertIn('[10-K.txt, line ', visible[-1])
            call = next(call for call in client.calls if call[0] == roles.MANAGER)
            self.assertIn('opening_paragraph', call[2]['insight_requirements'])
            for part, check in call[3]['properties']['checks']['properties'].items():
                self.assertEqual('insight_present' in check['required'], part in designated)
                self.assertEqual('insight_not_headline_restatement' in check['required'], part in designated)
            self.assertEqual(max(Counter(call[0] for call in client.calls).values()), 1)

    def test_missing_insight_retries_only_its_part_once_and_preserves_other_parts(self):
        class MissingOnce(ScriptedClient):
            def call(self, role, task, context, schema):
                result = super().call(role, task, context, schema)
                if role == roles.RESEARCH and 'revision' not in context:
                    result.pop('insight')
                return result
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            client = MissingOnce()
            manager = self.manager(Path(tmp), client)
            other = copy.deepcopy(manager.results[roles.RESEARCH]['sections'][roles.PARTS[0]])
            manager.finish()
            calls = [c for c in client.calls if c[0] == roles.RESEARCH]
            self.assertEqual(len(calls), 2)
            self.assertEqual(set(calls[1][2]['revision']), {roles.PARTS[1]})
            self.assertEqual(manager.results[roles.RESEARCH]['sections'][roles.PARTS[0]], other)
            self.assertLessEqual(max(Counter(c[0] for c in client.calls).values()), 2)

    def test_paraphrased_headline_fails_separate_manager_check_and_retry_is_bounded(self):
        class Restatement(ScriptedClient):
            def call(self, role, task, context, schema):
                result = super().call(role, task, context, schema)
                if role == roles.RESEARCH and roles.PARTS[1] in result['sections']:
                    claim = 'Quarterly revenue amounted to $0.040 billion.'
                    key = next(key for key, text in context['evidence'].items() if text == claim)
                    filename = context['insight_source_files'][key.rsplit('.L', 1)[0]]
                    number = key.rsplit('.L', 1)[1]
                    result['insight'] = {'part': roles.PARTS[1],
                        'text': f'Insight: {claim} [{filename}, line {number}]', 'evidence_ids': [key]}
                if role == roles.MANAGER:
                    result['checks'][roles.PARTS[1]]['insight_not_headline_restatement'] = False
                return result
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            client = Restatement(); folder = Path(tmp)
            saved_fixture(folder)
            with (folder/'earnings_release.txt').open('a', encoding='utf-8') as file:
                file.write('Quarterly revenue amounted to $0.040 billion.\n')
            manager = team.TeamManager(folder, client)
            for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
                manager.specialist(role)
            manager.finish()
            calls = [c for c in client.calls if c[0] == roles.RESEARCH]
            self.assertEqual(len(calls), 2)
            self.assertEqual(set(calls[1][2]['revision']), {roles.PARTS[1]})
            review_call = next(c for c in client.calls if c[0] == roles.MANAGER)
            self.assertEqual(review_call[2]['insight_requirements']['parts'][roles.PARTS[1]]['opening_figure_overlap'],
                             [{'unit': 'amount', 'value': '40000000'}])
            final = json.loads((folder/'review_final.json').read_text(encoding='utf-8'))
            self.assertFalse(final['checks'][roles.PARTS[1]]['passed'])
            self.assertLessEqual(max(Counter(c[0] for c in client.calls).values()), 2)

    def test_persistently_missing_insights_still_run_mandatory_verifier(self):
        class MissingAlways(ScriptedClient):
            def call(self, role, task, context, schema):
                result = super().call(role, task, context, schema)
                if role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
                    result.pop('insight', None)
                return result
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder=Path(tmp); client=MissingAlways()
            def tools(args, settings, on_step):
                saved_fixture(folder)
                return folder
            with patch.object(team.desk, 'run_seven_steps', side_effect=tools), patch.object(team, 'NodeClient', return_value=client):
                flow=team.DeskFlow({'API_KEY':'test','SEC_USER_AGENT':'test@example.test'})
                report=Path(flow.kickoff(inputs={'ticker':'TEST','peers':[],'price':30}))
            self.assertTrue(report.is_file())
            self.assertEqual([c[0] for c in client.calls[:4]], [roles.RESEARCH,roles.FINANCIAL,roles.ADVISOR,roles.MANAGER])
            self.assertEqual(client.calls[-1][0], 'Verifier')
            self.assertLessEqual(max(Counter(c[0] for c in client.calls).values()),2)

    def test_persistent_duplicate_misplaced_embedded_and_nonfinal_insights_are_not_repaired(self):
        for mode in ('duplicate', 'misplaced', 'embedded', 'nonfinal'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
                class Faulty(ScriptedClient):
                    def call(self, role, task, context, schema):
                        result = super().call(role, task, context, schema)
                        if role == roles.RESEARCH:
                            blocks = result['sections'][roles.PARTS[1]]
                            insight = result.pop('insight')
                            insight = {key: insight[key] for key in ('text', 'evidence_ids')}
                            if mode == 'duplicate':
                                blocks.extend([copy.deepcopy(insight), insight])
                            elif mode == 'misplaced':
                                result['sections'][roles.PARTS[0]].append(insight)
                            elif mode == 'embedded':
                                blocks.append({**insight, 'text': 'More analysis. '+insight['text']})
                            else:
                                blocks.insert(0, insight)
                        return result
                folder = Path(tmp); client = Faulty()
                def tools(args, settings, on_step):
                    saved_fixture(folder)
                    return folder
                with patch.object(team.desk, 'run_seven_steps', side_effect=tools), patch.object(team, 'NodeClient', return_value=client):
                    flow = team.DeskFlow({'API_KEY':'test', 'SEC_USER_AGENT':'test@example.test'})
                    verified = Path(flow.kickoff(inputs={'ticker':'TEST','peers':[],'price':30}))
                self.assertTrue(verified.is_file())
                self.assertEqual(client.calls[-1][0], 'Verifier')
                counts = Counter(call[0] for call in client.calls)
                self.assertEqual(counts[roles.RESEARCH], 2)
                self.assertLessEqual(max(counts.values()), 2)
                revisions = [c for c in client.calls if c[0] == roles.RESEARCH and 'revision' in c[2]]
                expected = {roles.PARTS[0], roles.PARTS[1]} if mode == 'misplaced' else {roles.PARTS[1]}
                self.assertEqual(set(revisions[0][2]['revision']), expected)
                final = json.loads((folder/'review_final.json').read_text(encoding='utf-8'))
                for part in expected:
                    self.assertFalse(final['checks'][part]['passed'])
                outputs = json.loads((folder/'role_outputs.json').read_text(encoding='utf-8'))
                compiled = (folder/'compiled_report.md').read_text(encoding='utf-8')
                for part in roles.PARTS[:2]:
                    section = compiled.split('## '+part+'\n',1)[1].split('\n## ',1)[0]
                    cursor = 0
                    for block in outputs[roles.RESEARCH]['sections'][part]:
                        marker = '<!-- desk-prose -->\n'+block['text']+'\n'
                        position = section.find(marker, cursor)
                        self.assertGreaterEqual(position, cursor, 'Compiler removed, changed, or moved a submitted block.')
                        cursor = position + len(marker)
                if mode == 'duplicate':
                    self.assertEqual(sum(line.startswith('Insight:') for line in compiled.splitlines()), 4)
                self.assertIn('Review unresolved after the one permitted revision.', compiled)


class InsightVerifierTests(unittest.TestCase):
    def run_case(self, insight, client=None, extra_release=''):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        folder=Path(tmp.name); saved_fixture(folder)
        if extra_release:
            with (folder/'earnings_release.txt').open('a',encoding='utf-8') as file: file.write(extra_release)
        (folder/'compiled_report.md').write_text('Filings read: synthetic\n\n'+insight+'\n',encoding='utf-8')
        path=Verifier(client or SemanticClient()).run(load_verifier_sources(folder),folder)
        return path.read_text(encoding='utf-8'),json.loads((folder/'verification_marks.json').read_text(encoding='utf-8'))

    def test_numeric_insight_has_code_and_semantic_checks(self):
        line='Insight: Revenue was $40 million. [earnings_release.txt, line 2]'
        text,checks=self.run_case(line)
        self.assertIn(line,text)
        self.assertEqual([(c['original'],c['mark']) for c in checks],[('40','correct')])
        self.assertTrue(checks[0]['semantic_reason'])
        self.assertIn('line 2: Revenue was $40 million.',checks[0]['source'])

    def test_wrong_value_wrong_line_and_no_source_cannot_pass(self):
        for line in ('Insight: Revenue was $41 million. [earnings_release.txt, line 2]',
                     'Insight: Revenue was $40 million. [earnings_release.txt, line 3]',
                     'Insight: Revenue was $40 million. [earnings_release.txt, line 99999]',
                     'Insight: Revenue was $40 million.'):
            with self.subTest(line=line):
                _,checks=self.run_case(line)
                self.assertTrue(checks)
                self.assertTrue(all(c['mark']!='correct' for c in checks))

    def test_semantic_rejection_is_not_overridden(self):
        _,checks=self.run_case('Insight: Revenue was $40 million. [earnings_release.txt, line 2]',SemanticClient(reject=True))
        self.assertEqual(checks[0]['mark'],'unsupported')

    def test_missing_semantic_check_does_not_write_verified_report(self):
        with self.assertRaises(SemanticResponseError):
            self.run_case('Insight: Revenue was $40 million. [earnings_release.txt, line 2]',SemanticClient(omit=True))

    def test_insight_growth_still_recomputes_quoted_inputs(self):
        sentence=('Revenue growth was 30.00%, using "$60 million" for June 30, 2025 and '
                  '"$50 million" for June 30, 2024; definition: current / prior - 1.')
        _,checks=self.run_case('Insight: '+sentence+' [earnings_release.txt, line 6]',
                              extra_release='(amounts in millions)\nJune 30, 2025 | June 30, 2024\nTotal revenue | 60 | 50\n')
        growth=next(c for c in checks if c.get('quoted_calculation'))
        self.assertEqual(growth['mark'],'wrong')
        self.assertIn('20',growth['correction'])
        self.assertIn('60',growth['source'])
        self.assertIn('50',growth['source'])


if __name__=='__main__': unittest.main()
