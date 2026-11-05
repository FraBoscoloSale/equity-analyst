"""Actual HTTP response-schema coverage for each role's first and revision call."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import agent_team as team
import research_roles as roles
from desk_verifier import Verifier, load_verifier_sources
from insight_contract import INSIGHT_OUTPUT_CONTRACT, insight_locations
from test_agent_team import ScriptedClient, saved_fixture


class InsightSchemaTests(unittest.TestCase):
    def test_all_three_roles_return_model_written_insights_through_real_schema_on_both_calls(self):
        requests = []
        def model(*args, **kwargs):
            body = kwargs['json']; envelope = json.loads(body['messages'][1]['content'])
            schema = envelope['expected_output']; context = envelope['context']
            requests.append((copy.deepcopy(body), copy.deepcopy(envelope)))
            if 'sections' in schema['properties']:
                parts = schema['properties']['sections']['properties']
                role = roles.OWNERS[next(iter(parts))]
                if role == roles.ADVISOR:
                    records = {k:r for output in context.values() if isinstance(output,dict)
                               for k,r in output.get('cited_evidence',{}).items()}
                    # Both analysts' prose failed validation. Only their actual,
                    # separately validated public Insight supplies this text.
                    for analyst in (roles.RESEARCH, roles.FINANCIAL):
                        self.assertTrue(all(not blocks for blocks in context[analyst]['sections'].values()))
                        self.assertIsInstance(context[analyst]['insight'],dict)
                    key,row = next((k,r) for k,r in records.items() if r.get('text')=='Diluted EPS was $0.50 (GAAP).')
                    text, filename, number = row['text'], row['source_id'].rsplit(':',1)[-1], row['line']
                else:
                    key = next(k for k,text in context['evidence'].items() if text=='Diluted EPS was $0.50 (GAAP).')
                    text = context['evidence'][key]
                    filename = context['insight_source_files'][key.rsplit('.L',1)[0]]
                    number = int(key.rsplit('.L',1)[1])
                output = {'sections': {part:[{'text':'Unsupported invented margin was 999999.', 'evidence_ids':[key]}]
                                       for part in parts}, 'limitations': []}
                output['insight'] = {'part': insight_locations(roles.PARTS,roles.OWNERS)[role],
                    'text':f'Insight: {text} [{filename}, line {number}]','evidence_ids':[key]}
                if role == roles.RESEARCH:
                    output['limitations'] = list(context['limitations'])
                elif role == roles.FINANCIAL:
                    output['calculator_requests'] = []
                    output['limitations'] = [k for k,r in context['evidence'].items()
                                             if isinstance(r,dict) and role in r.get('required_for',[])]
                else:
                    for blocks in output['sections'].values():
                        blocks[0]['text']='The selected filing evidence informs the recommendation.'
                    output.update(thesis='The filing informs the thesis.',recommendation='Hold',deciding_evidence_id=None,
                                  reason='No valuation metric is available.',thesis_evidence_ids=[key],reason_evidence_ids=[key])
                # This is precisely the schema sent as response_format by the
                # production NodeClient, not a simplified test-only contract.
                roles.validate_shape(output, body['response_format']['json_schema']['schema'])
                self.assertIn('insight',schema['required'])
                self.assertEqual(schema['properties']['insight']['required'],['part','text','evidence_ids'])
                self.assertEqual(schema['properties']['insight']['properties']['part']['enum'],[output['insight']['part']])
                self.assertIn(key,schema['properties']['insight']['properties']['evidence_ids']['items']['enum'])
                missing = copy.deepcopy(output); missing.pop('insight')
                with self.assertRaises(roles.RoleError): roles.validate_shape(missing,schema)
                duplicate = copy.deepcopy(output); duplicate['insight']['evidence_ids'].append(key)
                with self.assertRaises(roles.RoleError): roles.validate_shape(duplicate,schema)
                self.assertEqual(body['messages'][0]['content'],roles.load_instructions(role)+'\n\n'+INSIGHT_OUTPUT_CONTRACT)
            elif '$defs' in schema and 'semantic_check' in schema['$defs']:
                output={'checks':{key:{'supported':True,'reason':'Period and definition reviewed.','correction':None}
                                  for key in schema['properties']['checks']['required']}}
            else:
                output={'checks':{part:{'passed':True,'issues':[],**{
                    field:True for field in ('insight_present','insight_not_headline_restatement')
                    if field in check['properties']}}
                    for part,check in schema['properties']['checks']['properties'].items()}}
            response = Mock(ok=True)
            response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(output)}}]}
            return response

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), patch.object(team.requests,'post',side_effect=model):
            folder=Path(tmp); saved_fixture(folder)
            manager=team.TeamManager(folder,team.NodeClient('test-key',folder))
            for role in (roles.RESEARCH,roles.FINANCIAL,roles.ADVISOR): manager.specialist(role)
            for role in (roles.RESEARCH,roles.FINANCIAL,roles.ADVISOR):
                part=insight_locations(roles.PARTS,roles.OWNERS)[role]
                manager.specialist(role,{part:['Review this part once.']})
            compiled=manager.compile_report(manager.review()).read_text(encoding='utf-8')
            for role in (roles.RESEARCH,roles.FINANCIAL,roles.ADVISOR):
                for n in (1,2):
                    stem=role.lower().replace(' ','_')+'_'+str(n)
                    raw=json.loads((folder/(stem+'_output.json')).read_text(encoding='utf-8'))
                    instructions=json.loads((folder/(stem+'_instructions.json')).read_text(encoding='utf-8'))
                    self.assertEqual(instructions['instructions'],roles.load_instructions(role)+'\n\n'+INSIGHT_OUTPUT_CONTRACT)
                    self.assertIn(raw['insight']['text'],compiled)
                    self.assertFalse(any('Insight:' in b['text'] for blocks in raw['sections'].values() for b in blocks))
                    section=compiled.split('## '+raw['insight']['part']+'\n',1)[1].split('\n## ',1)[0]
                    visible=[line for line in section.splitlines() if line and not line.startswith('<!--')]
                    self.assertEqual(visible[-1],raw['insight']['text'])
            self.assertEqual(sum(line.startswith('Insight:') for line in compiled.splitlines()),3)
            result=Verifier(manager.client).run(load_verifier_sources(folder),folder)
            self.assertTrue(result.is_file())
            marks=json.loads((folder/'verification_marks.json').read_text(encoding='utf-8'))
            self.assertEqual(len([c for c in marks if c.get('insight')]),3)
            self.assertTrue(all(c['mark']=='correct' for c in marks if c.get('insight')))
            self.assertEqual([manager.client.counts[r] for r in (roles.RESEARCH,roles.FINANCIAL,roles.ADVISOR)],[2,2,2])

    def test_partial_revision_excludes_insight_field_and_preserves_existing_model_line(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder=Path(tmp); saved_fixture(folder); client=ScriptedClient()
            manager=team.TeamManager(folder,client)
            manager.specialist(roles.RESEARCH)
            before=copy.deepcopy(manager.results[roles.RESEARCH]['insight'])
            manager.specialist(roles.RESEARCH,{roles.PARTS[0]:['Revise ordinary analysis only.']})
            self.assertNotIn('insight',client.calls[-1][3]['properties'])
            self.assertEqual(manager.results[roles.RESEARCH]['insight'],before)

    def test_unavailable_text_cannot_be_fabricated_and_null_fails_manager(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder=Path(tmp); saved_fixture(folder)
            manager=team.TeamManager(folder,ScriptedClient())
            schema=manager.specialist_schema(roles.ADVISOR,{},[roles.PARTS[-1]])
            self.assertEqual(schema['properties']['insight'],{'enum':[None]})
            for role in (roles.RESEARCH,roles.FINANCIAL,roles.ADVISOR): manager.specialist(role)
            manager.results[roles.ADVISOR]['insight']=None
            review=manager.review()
            self.assertFalse(review['checks'][roles.PARTS[-1]]['passed'])

    def test_invalid_upstream_insight_is_filtered_only_in_advisor_projection_not_repaired(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder=Path(tmp); saved_fixture(folder)
            manager=team.TeamManager(folder,ScriptedClient())
            for role in (roles.RESEARCH,roles.FINANCIAL): manager.specialist(role)
            for blocks in manager.results[roles.RESEARCH]['sections'].values(): blocks.clear()
            bad=manager.results[roles.RESEARCH]['insight']
            bad['text']='Insight: Invented amount was $888 million. [10-K.txt, line 2]'
            original=copy.deepcopy(bad)
            projection=manager.analyst_context()
            self.assertNotIn('insight',projection[roles.RESEARCH])
            self.assertNotIn(original['evidence_ids'][0],projection[roles.RESEARCH]['cited_evidence'])
            self.assertEqual(manager.results[roles.RESEARCH]['insight'],original)


if __name__=='__main__': unittest.main()
