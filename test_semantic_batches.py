"""Offline tests of deterministic evidence selection, scheduling and coverage."""

import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

import agent_team as team
from desk_verifier import (Verifier, normalized, semantic_response_schema, SemanticResponseError)
from model_budget import MODEL_INPUT_CHAR_LIMIT, request_characters
from semantic_batches import plan_batches


def sources_fixture(count=7, noise=0):
    lines, report, checks = [], ['Filings read: synthetic saved filing'], []
    for i in range(1, count + 1):
        section = [f'Note {i} - Measure definition', '(amounts in millions)',
                   '52 Weeks Ended', 'June 30, 2025 | June 30, 2024',
                   f'Measure amount was ${i} million.', f'Footnote: measure {i} excludes the stated adjustment.']
        lines.extend(section)
        report.extend([f'## Claim {i}', f'Claim amount: {i}.', '> ' + section[4]])
        checks.append({'id': f'number_{i}', 'original': str(i), 'semantic': True,
                       'file': '10-K.txt', 'quote': section[4], 'period': '2025-06-30',
                       'definition': 'Reported annual measure in millions'})
    if noise:
        lines.extend(['Note 999 - Unrelated information', 'UNRELATED_SENTINEL_' + 'z' * noise])
        report.extend(['## Unrelated report section', 'UNRELATED_REPORT_' + 'y' * noise])
    sources = {'compiled_report': '\n'.join(report), 'saved_filing_texts': {'10-K.txt': '\n'.join(lines)},
               'saved_facts': {}}
    return sources, checks


def plan(checks, sources, **kwargs):
    instructions = (Path(__file__).parent / 'roles/verifier.txt').read_text(encoding='utf-8')
    return plan_batches(checks, sources, sources['compiled_report'], instructions,
                        semantic_response_schema, normalized, SemanticResponseError, **kwargs)


class PlannerTests(unittest.TestCase):
    def mixed_column_fixture(self, header=None):
        header = header or '(In millions, except percentages and per share amounts) | | 2033 | | 2032 | | Percentage Change |'
        lines = ['Item 7. Results of operations', 'SUMMARY RESULTS OF OPERATIONS',
                 '| | | | | | | |', header, '| | | | | | | |',
                 'Revenue | | $ | 864 | | $ | 720 | | 20% |',
                 'Operating income | | $ | 216 | | $ | 180 | | 20% |',
                 'Footnote: revenue includes service consideration.',
                 'Fiscal Year 2033 Compared with Fiscal Year 2032',
                 'Item 8. Financial statements', 'UNRELATED_'+'x'*220000]
        claim = 'Revenue | | $ | 864 | | $ | 720 | | 20% |'
        sources = {'compiled_report':'Filings read: synthetic\n\n## Overview\n\n> '+claim+
                   '\n\n[10-K.txt, line 6]', 'saved_filing_texts':{'10-K.txt':'\n'.join(lines)},
                   'saved_facts':{}}
        check = {'id':'number_1', 'original':'864', 'mark':'correct', 'semantic':True,
                 'quote':claim, 'file':'10-K.txt', 'period':'2033-06-30', 'definition':'Annual revenue'}
        return sources, check, lines

    def test_table_row_retains_complete_separate_mixed_unit_year_and_change_header(self):
        for header in ('(In millions, except percentages and per share amounts) | | 2033 | | 2032 | | Percentage Change |',
                       '($ in millions) | 2033 | 2032 | Percentage Change Y/Y',
                       '(Amounts in thousands) | FY2033 | FY2032 | Change'):
            with self.subTest(header=header):
                sources,check,lines=self.mixed_column_fixture(header)
                before=copy.deepcopy(sources)
                batches=plan([check],sources)
                self.assertEqual([c for b in batches for c in b['ids']], ['number_1'])
                selected=batches[0]['context']['saved_filing_texts']['10-K.txt']
                for i in (1,3,5,7,8):
                    self.assertIn(f'L{i+1}: {lines[i]}',selected)
                self.assertNotIn('UNRELATED_',selected)
                self.assertLess(batches[0]['input_characters'],15000)
                self.assertEqual(sources,before)

    def test_year_valued_body_row_cannot_substitute_for_missing_column_headers(self):
        sources,check,lines=self.mixed_column_fixture()
        lines[3]='(In millions)'
        lines[4]='Other revenue | 2033 | 2032'
        sources['saved_filing_texts']['10-K.txt']='\n'.join(lines)
        with self.assertRaisesRegex(SemanticResponseError,'missing period/column context'):
            plan([check],sources)

    def test_comparison_caption_for_other_years_is_not_bound_to_the_table(self):
        sources,check,lines=self.mixed_column_fixture()
        lines[8]='Fiscal Year 2031 Compared with Fiscal Year 2030'
        sources['saved_filing_texts']['10-K.txt']='\n'.join(lines)
        selected=plan([check],sources)[0]['context']['saved_filing_texts']['10-K.txt']
        self.assertIn('L4: '+lines[3],selected)
        self.assertNotIn(lines[8],selected)

    def test_multirow_period_and_gaap_column_labels_are_retained(self):
        sources,check,lines=self.mixed_column_fixture('2033 | 2032')
        lines[2]='(In millions, except per share amounts)'
        lines[4]='As reported (GAAP) | As adjusted (non-GAAP) | As reported (GAAP) | As adjusted (non-GAAP)'
        sources['saved_filing_texts']['10-K.txt']='\n'.join(lines)
        selected=plan([check],sources)[0]['context']['saved_filing_texts']['10-K.txt']
        for i in (2,3,4,5): self.assertIn(f'L{i+1}: {lines[i]}',selected)

    def large_cited_source(self):
        claim='Total revenue was $71 million for FY2031'
        insight='Insight: '+claim+' [10-K.txt, line 5]'
        lines=['Item 7 - Results of operations', '(amounts in millions)', '52 Weeks Ended',
               'June 30, 2031 | June 30, 2030', 'Total revenue | 71 | 63',
               'Footnote: total revenue includes membership fees.',
               'UNRELATED_SAME_SECTION_'+'x'*260000]
        source={'compiled_report':'Filings read: synthetic\n\n## Investment Recommendation\n\n'+insight+
                '\n\n## Unrelated\n\nUNRELATED_REPORT_'+'y'*200000,
                'saved_filing_texts':{'10-K.txt':'\n'.join(lines)},'saved_facts':{}}
        check={'id':'number_1','original':'71','mark':'unsupported','semantic':True,
               'quote':claim,'quoted_input':insight,'file':'10-K.txt','period':'2031-06-30',
               'definition':'Annual total revenue including membership fees'}
        return source,check,lines

    def test_individual_paraphrased_citation_in_large_file_selects_complete_small_evidence(self):
        sources,check,lines=self.large_cited_source()
        original=copy.deepcopy(sources)
        batch=plan([check],sources)[0]
        self.assertLess(batch['input_characters'],15000)
        self.assertLess(batch['input_characters'],MODEL_INPUT_CHAR_LIMIT)
        self.assertEqual(batch['ids'],['number_1'])
        selected=batch['context']['saved_filing_texts']['10-K.txt']
        for i in range(6): self.assertIn(f'L{i+1}: {lines[i]}',selected)
        self.assertNotIn('UNRELATED_SAME_SECTION',selected)
        self.assertNotIn('UNRELATED_REPORT',batch['context']['compiled_report'])
        self.assertEqual(sources,original)
        # Selecting an actual cited line never upgrades the already-failed
        # deterministic quotation check or fabricates a semantic result.
        self.assertEqual(json.loads(batch['task'].split('Code checks: ',1)[1])[0]['mark'],'unsupported')

    def test_missing_required_table_unit_or_period_context_fails_before_model(self):
        for missing in ('units','period'):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as tmp:
                sources,check,lines=self.large_cited_source()
                if missing=='units': lines[1]=''
                else: lines[2]=''; lines[3]=''
                sources['saved_filing_texts']['10-K.txt']='\n'.join(lines)
                client=Mock()
                with self.assertRaisesRegex(SemanticResponseError,'missing (?:units/scale|period/column) context'):
                    Verifier(client).run(sources,Path(tmp))
                client.call.assert_not_called()
                self.assertFalse((Path(tmp)/'verified_report.md').exists())

    def test_missing_explicit_definition_reference_fails_closed(self):
        sources,checks=sources_fixture(count=1)
        sources['saved_filing_texts']['10-K.txt']=sources['saved_filing_texts']['10-K.txt'].replace(
            'Footnote: measure 1 excludes the stated adjustment.','See Note 2 for the alternative definition.')
        with self.assertRaisesRegex(SemanticResponseError,'missing referenced Note 2'):
            plan(checks,sources)

    def test_missing_required_period_on_narrative_amount_also_fails(self):
        sources,checks=sources_fixture(count=1)
        lines=sources['saved_filing_texts']['10-K.txt'].splitlines()
        lines[2]=''; lines[3]=''
        sources['saved_filing_texts']['10-K.txt']='\n'.join(lines)
        with self.assertRaisesRegex(SemanticResponseError,'missing period/column context'):
            plan(checks,sources)

    def test_document_metadata_applies_to_notes_without_borrowing_other_note_units(self):
        sources,checks=sources_fixture(count=1)
        lines=['All amounts are in millions.', '52 Weeks Ended', 'June 30, 2025 | June 30, 2024',
               'Note 1 - Other information', 'Other narrative.', 'Note 2 - Revenue definition', 'Revenue | 71 | 63']
        sources['saved_filing_texts']['10-K.txt']='\n'.join(lines)
        sources['compiled_report']='Filings read: synthetic\n\n> Revenue | 71 | 63'
        check={**checks[0],'original':'71','quote':'Revenue | 71 | 63'}
        selected=plan([check],sources)[0]['context']['saved_filing_texts']['10-K.txt']
        for n in (0,1,2,5,6): self.assertIn(f'L{n+1}: {lines[n]}',selected)
        self.assertNotIn('Other narrative.',selected)
        lines[0]=''; lines[4]='(amounts in thousands)'
        sources['saved_filing_texts']['10-K.txt']='\n'.join(lines)
        with self.assertRaisesRegex(SemanticResponseError,'missing units/scale context'):
            plan([check],sources)

    def test_xbrl_context_uses_exact_record_identity_not_other_same_value_facts(self):
        sources,checks=sources_fixture(count=1)
        row={'val':71000000,'tag':'us-gaap:Revenues','start':'2030-07-01','end':'2031-06-30',
             'unit':'USD','accn':'synthetic-observation'}
        data={'company':{'ticker':'SYNTH'},'concepts':{
            'revenue':{'label':'Total revenue','current':row,'prior':{**row,'end':'2030-06-30'}},
            'different':{'label':'Different measure','current':{**row,'tag':'us-gaap:Assets'},'prior':None}}}
        sources['saved_facts']={'facts.json':data,'PEER_facts.json':{**data,'company':{'ticker':'PEER'}}}
        statement='Annual total revenue was $71 million.'
        sources['compiled_report']='Filings read: synthetic\n\n'+statement
        check={**checks[0],'file':None,'original':'71000000','quote':json.dumps(row),
               'source_binding':{'statement':statement,'operands':[{'source':{'fact':{
                   'file':'facts.json','pointer':'/concepts/revenue/current','record':row}}}]}}
        context=plan([check],sources)[0]['context']
        self.assertEqual(context['saved_filing_texts'],{})
        self.assertEqual(set(context['saved_facts']),{'facts.json'})
        concepts=context['saved_facts']['facts.json']['concepts']
        self.assertEqual(set(concepts),{'revenue'})
        self.assertEqual(concepts['revenue']['current'],row)
        self.assertNotIn('prior',concepts['revenue'])

    def test_deterministic_disjoint_complete_plan_and_exact_budget_accounting(self):
        sources, checks = sources_fixture(count=12, noise=200000)
        before = copy.deepcopy(sources)
        first = plan(checks, sources, max_checks=3)
        second = plan(checks, sources, max_checks=3)
        self.assertEqual(first, second)
        self.assertEqual([b['ids'] for b in first], [[f'number_{i}' for i in range(n, n+3)] for n in (1,4,7,10)])
        self.assertEqual(Counter(key for b in first for key in b['ids']), Counter(c['id'] for c in checks))
        instructions = (Path(__file__).parent / 'roles/verifier.txt').read_text(encoding='utf-8')
        for batch in first:
            self.assertEqual(set(batch['context']), {'compiled_report', 'saved_filing_texts', 'saved_facts'})
            self.assertNotIn('UNRELATED_SENTINEL', json.dumps(batch['context']))
            self.assertNotIn('UNRELATED_REPORT', json.dumps(batch['context']))
            self.assertEqual(batch['input_characters'], request_characters(instructions, batch['task'], batch['context'], batch['schema']))
            self.assertLessEqual(batch['input_characters'], MODEL_INPUT_CHAR_LIMIT)
        self.assertEqual(sources, before)

    def test_complete_table_units_periods_distant_headers_and_footnotes_are_preserved(self):
        sources, checks = sources_fixture(count=1)
        content = sources['saved_filing_texts']['10-K.txt'].splitlines()
        content[4:4] = [f'Other table row | {i}' for i in range(180)]
        sources['saved_filing_texts']['10-K.txt'] = '\n'.join(content)
        batches = plan(checks, sources)
        selected = batches[0]['context']['saved_filing_texts']['10-K.txt']
        for i in (0,1,2,3,184,185):
            self.assertIn(f'L{i+1}: {content[i]}', selected)
        self.assertNotIn('Other table row |', selected)
        self.assertIn('Footnote: measure 1 excludes the stated adjustment.', selected)

    def test_explicit_note_references_retain_complete_definitions_in_another_section(self):
        sources, checks = sources_fixture(count=2)
        sources['saved_filing_texts']['10-K.txt'] = sources['saved_filing_texts']['10-K.txt'].replace(
            'Footnote: measure 1 excludes the stated adjustment.', 'See Note 2 for the alternative definition.')
        batches = plan(checks[:1], sources)
        self.assertIn('Note 2 - Measure definition', batches[0]['context']['saved_filing_texts']['10-K.txt'])
        self.assertIn('Footnote: measure 2 excludes the stated adjustment.', batches[0]['context']['saved_filing_texts']['10-K.txt'])

    def test_short_numeric_quote_does_not_match_every_section_containing_those_digits(self):
        sources, checks = sources_fixture(count=2)
        sources['saved_filing_texts']['10-K.txt'] += '\nNote 3 - Page marker\n25\n'
        sources['compiled_report'] += '\n## Page reference\n> 25\n'
        check = {**checks[0], 'quote': '25', 'original': '25'}
        selected = plan([check], sources)[0]['context']['saved_filing_texts']['10-K.txt']
        self.assertIn('Note 3 - Page marker', selected)
        self.assertNotIn('June 30, 2025', selected)

    def test_size_limit_splits_batches_without_clipping_any_evidence(self):
        sources, checks = sources_fixture(count=6)
        single_sizes = [plan([c], sources)[0]['input_characters'] for c in checks]
        limit = max(single_sizes) + 100
        batches = plan(checks, sources, max_characters=limit)
        self.assertGreater(len(batches), 1)
        self.assertEqual([key for b in batches for key in b['ids']], [c['id'] for c in checks])
        self.assertTrue(all(b['input_characters'] <= limit for b in batches))
        for batch in batches:
            for key in batch['ids']:
                n = key.split('_')[1]
                self.assertIn(f'Footnote: measure {n} excludes the stated adjustment.', batch['context']['saved_filing_texts']['10-K.txt'])

    def test_single_oversized_check_fails_explicitly_instead_of_truncating(self):
        sources, checks = sources_fixture(count=1)
        sources['saved_filing_texts']['10-K.txt'] += '\nRequired definition ' + 'q' * 200000
        with self.assertRaisesRegex(SemanticResponseError, 'number_1.*cannot fit intact.*no checks were skipped'):
            plan(checks, sources)

    def test_complete_fact_records_are_selected_and_unrelated_facts_excluded(self):
        sources, checks = sources_fixture(count=1)
        match = {'val': 1000000, 'tag': 'us-gaap:Revenues', 'end': '2025-06-30', 'accn': 'test', 'unit': 'USD'}
        sources['saved_facts'] = {'facts.json': {'company': {'ticker':'SYNTH'}, 'concepts': {
            'revenue': {'label':'Revenue', 'current':match, 'prior':None},
            'unrelated': {'label':'Unrelated', 'current':{**match,'val':987654321}, 'prior':None}}}}
        selected = plan(checks, sources)[0]['context']['saved_facts']['facts.json']['concepts']
        self.assertEqual(selected['revenue']['current'], match)
        self.assertNotIn('unrelated', selected)


class BatchExecutionTests(unittest.TestCase):
    def run_fixture(self, count=5):
        sources, _ = sources_fixture(count=count)
        # Use supported report citation records so all numbers reach the real code checks.
        report = ['Filings read: synthetic saved filing']
        for i in range(1, count + 1):
            payload = {'record': {'kind':'text','text':f'Measure amount was ${i} million.'},
                       'source':{'file':'10-K.txt','period':'2025-06-30'}}
            report.extend([f'## Claim {i}', '<!-- desk-record ' + json.dumps(payload) + ' -->',
                           '> ' + payload['record']['text'], '<!-- /desk-record -->', ''])
        sources['compiled_report'] = '\n'.join(report)
        return sources

    def forced_small_batches(self):
        def make(*args, **kwargs):
            kwargs['max_checks'] = 1
            return plan_batches(*args, **kwargs)
        return patch('desk_verifier.plan_batches', side_effect=make)

    def test_more_than_two_batches_use_one_http_request_each_and_validate_all_results(self):
        sources = self.run_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            client = team.NodeClient('SECRET', folder)
            ids = []
            def respond(*args, **kwargs):
                envelope = json.loads(kwargs['json']['messages'][1]['content'])
                required = envelope['expected_output']['properties']['checks']['required']
                ids.extend(required)
                answer = {'checks': {key:{'supported':True,'reason':'Period and definition match.','correction':None} for key in required}}
                response = Mock(ok=True)
                response.json.return_value = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer)}}]}
                return response
            with self.forced_small_batches(), patch.object(team.requests,'post',side_effect=respond) as post:
                report = Verifier(client).run(sources,folder)
            self.assertEqual(post.call_count,5)
            self.assertEqual(client.counts,{'Verifier':5})
            results = json.loads((folder/'verifier_semantic_results.json').read_text(encoding='utf-8'))
            self.assertTrue(results['complete'])
            self.assertEqual(set(ids), set(results['checks']))
            self.assertEqual(len(ids),len(set(ids)))
            self.assertTrue(report.exists())
            self.assertEqual(len(list(folder.glob('verifier_semantic_batch_*_output.json'))),5)
            self.assertTrue(all(c['semantic_reason'] for c in json.loads((folder/'verification_marks.json').read_text(encoding='utf-8')) if c.get('semantic')))

    def test_later_missing_or_wrong_batch_result_aborts_without_verified_report(self):
        for invalid in ('missing','wrong_id','malformed'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                client = Mock()
                calls = []
                def respond(role, task, context, schema):
                    ids = schema['properties']['checks']['required']
                    calls.append(ids)
                    checks = {key:{'supported':True,'reason':'Checked.','correction':None} for key in ids}
                    if len(calls)==2:
                        if invalid=='missing': checks={}
                        elif invalid=='wrong_id': checks={calls[0][0]:next(iter(checks.values()))}
                        else: checks[ids[0]]['supported']='true'
                    return {'checks':checks}
                client.call.side_effect=respond
                with self.forced_small_batches(), self.assertRaises(SemanticResponseError):
                    Verifier(client).run(self.run_fixture(),folder)
                self.assertEqual(len(calls),2)
                self.assertFalse((folder/'verified_report.md').exists())
                self.assertFalse((folder/'verifier_semantic_results.json').exists())
                self.assertEqual(json.loads((folder/'verifier_semantic_error.json').read_text(encoding='utf-8'))['batch'],2)

    def test_unfit_evidence_is_detected_before_any_model_request(self):
        source=self.run_fixture(count=1)
        source['saved_filing_texts']['10-K.txt']+='\nRequired explanation '+'x'*200000
        with tempfile.TemporaryDirectory() as tmp:
            client=Mock()
            with self.assertRaises(SemanticResponseError):
                Verifier(client).run(source,Path(tmp))
            client.call.assert_not_called()
            self.assertFalse((Path(tmp)/'verified_report.md').exists())

    def test_repeated_identical_citations_receive_their_validated_shared_result(self):
        source=self.run_fixture(count=1)
        block=source['compiled_report'].split('\n',1)[1]
        source['compiled_report']+='\n'+block
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            client=Mock()
            def respond(role,task,context,schema):
                return {'checks':{key:{'supported':True,'reason':'Same cited number, period and definition.','correction':None}
                                  for key in schema['properties']['checks']['required']}}
            client.call.side_effect=respond
            Verifier(client).run(source,folder)
            result=json.loads((folder/'verifier_semantic_results.json').read_text(encoding='utf-8'))
            self.assertEqual(len(result['checks']),1)
            self.assertEqual(len(result['aliases']),2)
            marks=json.loads((folder/'verification_marks.json').read_text(encoding='utf-8'))
            self.assertEqual(len([c for c in marks if c.get('semantic_reason')]),2)

    def test_later_transport_failure_is_not_retried_and_cannot_write_a_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            client=team.NodeClient('SECRET',folder)
            calls=[]
            def respond(*args,**kwargs):
                envelope=json.loads(kwargs['json']['messages'][1]['content'])
                ids=envelope['expected_output']['properties']['checks']['required']
                calls.append(ids)
                if len(calls)==2:
                    return Mock(ok=False,status_code=500)
                result={'checks':{key:{'supported':True,'reason':'Checked.','correction':None} for key in ids}}
                response=Mock(ok=True)
                response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}]}
                return response
            with self.forced_small_batches(),patch.object(team.requests,'post',side_effect=respond) as post:
                with self.assertRaisesRegex(team.roles.RoleError,'HTTP 500'):
                    Verifier(client).run(self.run_fixture(),folder)
            self.assertEqual(post.call_count,2)
            self.assertFalse((folder/'verified_report.md').exists())
            self.assertFalse((folder/'verifier_semantic_results.json').exists())

    def test_registered_batch_order_and_nonverifier_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=team.NodeClient('SECRET',tmp)
            client.prepare_verifier_batches([['number_1'],['number_2'],['number_3']])
            with patch.object(team.requests,'post') as post:
                with self.assertRaisesRegex(team.roles.RoleError,'out-of-order'):
                    client.call('Verifier','work',{},semantic_response_schema(['number_2']))
                post.assert_not_called()
            other=team.NodeClient('SECRET',tmp)
            other.counts[team.roles.RESEARCH]=2
            with self.assertRaisesRegex(team.roles.RoleError,'maximum of two'):
                other.call(team.roles.RESEARCH,'work',{},team.roles.object_schema({'value':team.roles.STRING}))


if __name__=='__main__':
    unittest.main()
