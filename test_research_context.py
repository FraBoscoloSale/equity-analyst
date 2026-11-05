"""Offline Research scope/budget regressions through the real node transport."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import agent_team as team
import equity_desk as desk
import research_roles as roles
from insight_contract import INSIGHT_OUTPUT_CONTRACT
from model_budget import MODEL_INPUT_CHAR_LIMIT, request_characters
from research_context import research_evidence_ids, TEXT_BRIEF_LIMIT
from test_agent_team import ScriptedClient, saved_fixture


def large_fixture(folder, *, units=True, period=True):
    saved_fixture(folder)
    # Irrelevant paragraphs greatly exceed the budget but no paragraph is cut.
    noise = ('The legal correspondence archive retains routine administrative correspondence.\n' * 5000)
    table = ('(In millions, except per share amounts)\n' if units else '')
    table += ('Year ended | 2025 | 2024\n' if period else '')
    table += ('Revenue | 120 | 100\nDiluted EPS | 2.40 | 2.00\n'
              '*Revenue includes service and subscription consideration.\n')
    annual = ('Item 1. Business\nThe business sells subscriptions.\n' + noise +
              'Competition\nCustomer demand grew 12% in the period.\nItem 1A. Risks\n'
              'Forbidden risk evidence.\nItem 7. Management Discussion\n'
              'Revenue includes subscriptions.\nSUMMARY RESULTS\n' + table +
              'Item 8. Statements\nForbidden statement evidence.\n')
    (folder/'10-K.txt').write_text(annual, encoding='utf-8')


class ResearchContextTests(unittest.TestCase):
    def manager(self, folder):
        return team.TeamManager(folder, ScriptedClient())

    def test_large_filing_actual_first_and_revision_requests_fit_and_remain_source_bound(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            large_fixture(folder)
            persona = roles.load_instructions(roles.RESEARCH)
            client = team.NodeClient('offline', folder)
            manager = team.TeamManager(folder, client)
            scripted = ScriptedClient()
            sent = []

            def model(*args, **kwargs):
                body = kwargs['json']
                envelope = json.loads(body['messages'][1]['content'])
                sent.append(copy.deepcopy(envelope))
                self.assertLessEqual(request_characters(body['messages'][0]['content'], envelope['task'],
                                     envelope['context'], envelope['expected_output']), MODEL_INPUT_CHAR_LIMIT)
                self.assertEqual(body['messages'][0]['content'], persona+'\n\n'+INSIGHT_OUTPUT_CONTRACT)
                output = scripted.call(roles.RESEARCH, envelope['task'], envelope['context'], envelope['expected_output'])
                return Mock(ok=True, json=lambda: {'choices':[{'finish_reason':'stop',
                            'message':{'content':json.dumps(output)}}]})

            with patch.object(team.requests, 'post', side_effect=model) as post:
                manager.specialist(roles.RESEARCH)
                manager.specialist(roles.RESEARCH, {roles.PARTS[1]: ['Clarify the supported growth interpretation.']})
                self.assertEqual(post.call_count, 2)
            self.assertEqual(roles.load_instructions(roles.RESEARCH), persona)
            self.assertEqual(set(sent[0]), {'task','context','expected_output'})
            for envelope in sent:
                context = envelope['context']
                texts = {k:v for k,v in context['evidence'].items() if isinstance(v,str)}
                joined = '\n'.join(texts.values())
                self.assertIn('Revenue | 120 | 100', joined)
                self.assertIn('Diluted EPS | 2.40 | 2.00', joined)
                self.assertIn('(In millions, except per share amounts)', joined)
                self.assertIn('Year ended | 2025 | 2024', joined)
                self.assertIn('*Revenue includes service and subscription consideration.', joined)
                self.assertNotIn('routine administrative correspondence', joined)
                self.assertNotIn('Forbidden', joined)
                self.assertNotIn('step4_facts', context)
                self.assertNotIn('step5_metrics', context)
                self.assertNotIn('price', context['evidence'])
                self.assertTrue(any(k.startswith('peer.') for k in context['evidence']))
                self.assertFalse(any(k.startswith('subject.') for k in context['evidence']))
                margin = next(row for k,row in context['evidence'].items()
                              if k.startswith('peer.') and k.endswith('.operating_margin'))
                self.assertEqual(context['sources'][margin['source_id']]['file'], 'peers.json')
                self.assertTrue(all(ref.startswith('peer.') for ref in margin['inputs']))
                choices = envelope['expected_output']['properties']['insight']['properties']['evidence_ids']['items']['enum']
                self.assertTrue(set(choices) <= set(texts))
                for key,text in texts.items():
                    record = manager.supplied[roles.RESEARCH][key]
                    source = manager.catalog.sources[record['source_id']]
                    actual = (folder/source['file']).read_text(encoding='utf-8').splitlines()
                    self.assertEqual(text, actual[record['line']-1])

    def test_selection_is_deterministic_and_peer_closure_cannot_import_subject_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); large_fixture(folder)
            manager = self.manager(folder)
            choose = lambda: research_evidence_ids(manager.catalog, manager.research, manager.sources, roles.RoleError)
            self.assertEqual(choose(), choose())
            peer = next(k for k in manager.catalog.records if k.startswith('peer.') and k.endswith('.operating_margin'))
            manager.catalog.records[peer]['references'].append('subject.revenue.current')
            with self.assertRaisesRegex(roles.RoleError, 'forbidden desk-step'):
                choose()

    def test_missing_required_period_or_units_fails_before_model(self):
        for field in ('period','units'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp); large_fixture(folder, **{field:False})
                manager = self.manager(folder)
                with patch.object(team.requests, 'post') as post, self.assertRaisesRegex(roles.RoleError, field):
                    manager.specialist(roles.RESEARCH)
                post.assert_not_called()

    def test_required_line_is_never_shortened_to_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); large_fixture(folder)
            p = folder/'10-K.txt'
            original = p.read_text(encoding='utf-8')
            giant = 'Revenue | 120 | 100 | ' + 'Required definition ' * TEXT_BRIEF_LIMIT
            p.write_text(original.replace('Revenue | 120 | 100', giant), encoding='utf-8')
            manager = self.manager(folder)
            with self.assertRaisesRegex(roles.RoleError, 'intact; no evidence was truncated'):
                manager.specialist(roles.RESEARCH)
            self.assertIn(giant, p.read_text(encoding='utf-8'))

    def test_referenced_definition_is_complete_and_missing_definition_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); large_fixture(folder)
            p=folder/'10-K.txt'
            original=p.read_text(encoding='utf-8')
            text=original.replace('*Revenue includes service and subscription consideration.',
                                 '*Refer to the Revenue Definition section.')
            text=text.replace('Item 8. Statements', 'Revenue Definition\nRevenue includes service and subscription consideration.\nItem 8. Statements')
            p.write_text(text,encoding='utf-8')
            manager=self.manager(folder); manager.specialist(roles.RESEARCH)
            supplied=manager.supplied[roles.RESEARCH]
            self.assertIn('Revenue includes service and subscription consideration.', [r.get('text') for r in supplied.values()])
            p.write_text(text.replace('Revenue Definition\nRevenue includes service and subscription consideration.\n',''),encoding='utf-8')
            manager=self.manager(folder)
            with self.assertRaisesRegex(roles.RoleError,'missing referenced Revenue Definition'):
                manager.specialist(roles.RESEARCH)


if __name__ == '__main__':
    unittest.main()
