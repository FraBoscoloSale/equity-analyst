"""Offline source-contract tests: prose values must resolve explicit citations."""

import copy
import json
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from desk_verifier import Verifier, SemanticResponseError
from sourced_claims import render_claim


class SemanticClient:
    def __init__(self,reject=False,omit=False): self.calls=[]; self.reject=reject; self.omit=omit
    def call(self,role,task,context,schema):
        self.calls.append((role,task,context,schema))
        checks={key:{'supported':not self.reject,'reason':'Period and financial definition reviewed.','correction':None}
                for key in schema['properties']['checks']['required']}
        if self.omit and checks: checks.pop(next(iter(checks)))
        return {'checks':checks}


def fixture():
    numbers={'revenue':(180,150),'operating_income':(45,35),'net_income':(30,20),'equity':(120,80),
             'long_term_debt':(20,21),'current_debt':(5,4),'cash_from_operations':(55,40),'capex':(15,10),
             'diluted_eps':(2.5,2)}
    concepts={}
    lines=['CONSOLIDATED STATEMENTS OF INCOME','(amounts in millions, except per share data)',
           '52 Weeks Ended','June 30, 2025 | June 30, 2024']
    row_numbers={}
    for name,(current,prior) in numbers.items():
        row_numbers[name]=len(lines)+1
        lines.append(f'{name} | {current} | {prior}')
        obs={}
        for period,value,end,start in [('current',current,'2025-06-30','2024-07-01'),('prior',prior,'2024-06-30','2023-07-01')]:
            obs[period]={'val':value if name=='diluted_eps' else value*1000000,'unit':'USD/shares' if name=='diluted_eps' else 'USD',
                         'tag':'us-gaap:'+name,'end':end,'accn':'0000000001-25-000001','form':'10-K'}
            if name not in ('equity','long_term_debt','current_debt'): obs[period]['start']=start
        concepts[name]={'label':name,**obs}
    facts={'company':{'ticker':'SYNTH','name':'Synthetic Issuer'},'concepts':concepts}
    release=['CONSOLIDATED STATEMENTS OF INCOME','(amounts in millions, except per share data)',
             '13 Weeks Ended','June 30, 2025 | June 30, 2024','Total revenue | 60 | 50','Diluted EPS | 0.70 | 0.60']
    sources={'compiled_report':'Filings read: synthetic\n','saved_filing_texts':{'10-K.txt':'\n'.join(lines),
              'earnings_release.txt':'\n'.join(release)},'saved_facts':{'facts.json':facts}}
    def operand(name,concept,period='current'):
        row=concepts[concept][period]
        quoted=str(numbers[concept][0 if period=='current' else 1])
        scale='1' if concept=='diluted_eps' else '1000000'
        return {'name':name,'quoted':quoted,'scale':scale,'unit':row['unit'],
                'period':{k:row[k] for k in ('start','end') if k in row},'definition':concept,
                'source':{'fact':{'file':'facts.json','pointer':f'/concepts/{concept}/{period}','record':copy.deepcopy(row)},
                          'filing':{'file':'10-K.txt','line':row_numbers[concept], 'quote':lines[row_numbers[concept]-1],
                                    'number_index':0 if period=='current' else 1,
                                    'context':[{'line':i+1,'quote':lines[i]} for i in (1,2,3)]}}}
    return sources,operand


def payload(identity,statement,inputs,bindings):
    mentions=[]
    for literal,expression,unit,scale,decimals in bindings:
        match=next(m for m in re.finditer(r'(?<![\w.])'+re.escape(literal)+r'(?![\w.])',statement)
                   if not any(m.start()==old['start'] for old in mentions))
        mentions.append({'start':match.start(),'end':match.end(),'expression':expression,'unit':unit,
                         'scale':str(scale),'decimals':decimals})
    return {'id':identity,'record':{'kind':'sourced_claim','statement':statement,'inputs':inputs,'mentions':mentions}}


def with_records(sources,records):
    source=copy.deepcopy(sources)
    for i,row in enumerate(records,1):
        source['compiled_report']+=f'\n#### Claim {i}: Financial observation\n\n<!-- desk-record '+json.dumps(row)+' -->\n'+render_claim(row)+'<!-- /desk-record -->\n'
    return source


class SourcedClaimTests(unittest.TestCase):
    @staticmethod
    def focused_checks(client):
        return [c for call in client.calls for c in json.loads(call[1].split('Code checks: ',1)[1])
                if c.get('source_binding',{}).get('kind') in ('direct','calculation')]

    def run_case(self,sources,records,client=None):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        folder=Path(tmp.name); client=client or SemanticClient()
        original=copy.deepcopy(sources)
        report=Verifier(client).run(with_records(sources,records),folder)
        self.assertEqual(original,sources)
        checks=json.loads((folder/'verification_marks.json').read_text(encoding='utf-8'))
        return report.read_text(encoding='utf-8'),checks,client

    def revenue(self):
        source,op=fixture()
        row=payload('revenue','Annual total revenue was "$180 million".',[op('a','revenue')],
                    [('180','a','USD',1000000,0)])
        return source,row

    def test_correctly_cited_statement_is_correct_with_semantic_coverage(self):
        source,row=self.revenue()
        report,checks,client=self.run_case(source,[row])
        result=[c for c in checks if c.get('claim_statement')]
        self.assertEqual([c['mark'] for c in result],['correct'])
        self.assertIn(row['record']['statement'],report)
        self.assertTrue(all(c.get('semantic_reason') for c in result))
        self.assertEqual(set(client.calls[0][2]),{'compiled_report','saved_filing_texts','saved_facts'})

    def test_same_number_without_source_stays_unsupported_even_when_model_approves(self):
        source,row=self.revenue()
        row['record']['inputs'][0]['source']={}
        _,checks,_=self.run_case(source,[row])
        self.assertEqual([c['mark'] for c in checks if c.get('claim_statement')],['unsupported'])

    def test_wrong_value_with_real_citation_is_not_accepted(self):
        source,row=self.revenue()
        row['record']['statement']=row['record']['statement'].replace('180','181')
        _,checks,_=self.run_case(source,[row])
        result=next(c for c in checks if c.get('claim_statement'))
        self.assertEqual(result['mark'],'wrong')
        self.assertEqual(result['correction'],'180')

    def test_forged_citation_record_and_wrong_period_stay_unsupported(self):
        for kind in ('value','period','pointer','quote'):
            source,row=self.revenue(); op=row['record']['inputs'][0]
            if kind=='value': op['source']['fact']['record']['val']=181000000
            elif kind=='period': op['period']['end']='2023-06-30'
            elif kind=='pointer': op['source']['fact']['pointer']='/concepts/missing/current'
            else: op['source']['filing']['quote']='revenue | 181 | 150'
            with self.subTest(kind=kind):
                _,checks,_=self.run_case(source,[row])
                self.assertEqual([c['mark'] for c in checks if c.get('claim_statement')],['unsupported'])

    def test_ratio_is_recomputed_and_rounding_does_not_replace_clean_value(self):
        source,op=fixture()
        row=payload('growth','Total revenue growth was 20.00%, using "$180 million" and "$150 million".',
                    [op('a','revenue'),op('b','revenue','prior')],
                    [('20.00','(a/b-1)*100','%',1,2),('180','a','USD',1000000,0),('150','b','USD',1000000,0)])
        report,checks,_=self.run_case(source,[row])
        results=[c for c in checks if c.get('claim_statement')]
        self.assertTrue(all(c['mark']=='correct' for c in results))
        self.assertIn('(a/b-1)*100',results[0]['arithmetic'])
        self.assertIn('20.00%',report)

    def test_model_semantic_rejection_or_missing_check_cannot_pass(self):
        source,row=self.revenue()
        _,checks,_=self.run_case(source,[row],SemanticClient(reject=True))
        self.assertEqual([c['mark'] for c in checks if c.get('claim_statement')],['unsupported'])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SemanticResponseError):
                Verifier(SemanticClient(omit=True)).run(with_records(source,[row]),Path(tmp))
            self.assertFalse((Path(tmp)/'verified_report.md').exists())

    def test_million_and_billion_equivalents_keep_conversion_and_both_sources_in_model_request(self):
        source,op=fixture()
        row=payload('cash_flow','Free cash flow was "$40 million" ($0.040 billion).',
                    [op('a','cash_from_operations'),op('b','capex')],
                    [('40','a-b','USD',1000000,0),('0.040','a-b','USD',1000000000,3)])
        _,checks,client=self.run_case(source,[row])
        results=[c for c in checks if c.get('claim_statement')]
        self.assertEqual([c['mark'] for c in results],['correct','correct'])
        targets=self.focused_checks(client)
        self.assertEqual(len(targets),2)
        for target in targets:
            binding=target['source_binding']
            self.assertEqual(binding['expression'],'a-b')
            self.assertEqual([op['name'] for op in binding['operands']],['a','b'])
            self.assertEqual(Decimal(binding['display']['base_value']),Decimal(40000000))
            self.assertEqual(Decimal(binding['computed_base_value']),Decimal(40000000))
            for operand in binding['operands']:
                filing=operand['source']['filing']
                self.assertEqual(filing['number_index'],0)
                self.assertTrue(filing['context'])
                self.assertEqual(operand['period']['end'],'2025-06-30')
                self.assertFalse(operand['source_errors'])
        self.assertEqual(targets[1]['source_binding']['display']['scale'],'1000000000')
        self.assertEqual(targets[1]['source_binding']['display']['unit_label'],'billion USD')
        self.assertIn('billion USD',targets[1]['source_binding']['arithmetic'])

    def test_wrong_billion_equivalent_cannot_pass_with_valid_sources(self):
        source,op=fixture()
        row=payload('cash_flow','Free cash flow was "$40 million" ($0.041 billion).',
                    [op('a','cash_from_operations'),op('b','capex')],
                    [('40','a-b','USD',1000000,0),('0.041','a-b','USD',1000000000,3)])
        _,checks,_=self.run_case(source,[row])
        result=next(c for c in checks if c.get('claim_statement') and c['original']=='0.041')
        self.assertEqual(result['mark'],'wrong')
        self.assertEqual(result['correction'],'0.040')

    def test_prior_table_input_keeps_only_its_selected_period_column_and_definition(self):
        source,_=fixture()
        lines=source['saved_filing_texts']['earnings_release.txt'].splitlines()
        def quarterly(name,index):
            return {'name':name,'quoted':('60','50')[index],'scale':'1000000','unit':'USD',
                    'definition':'Total revenue','period':{'end':('2025-06-30','2024-06-30')[index],'duration':'13 weeks'},
                    'source':{'filing':{'file':'earnings_release.txt','line':5,'quote':lines[4],
                      'number_index':index,'context':[{'line':i+1,'quote':lines[i]} for i in (1,2,3)]}}}
        row=payload('quarter_growth','Revenue growth was 20.00%, using "$60 million" and "$50 million".',
                    [quarterly('a',0),quarterly('b',1)],
                    [('20.00','(a/b-1)*100','%',1,2),('60','a','USD',1000000,0),('50','b','USD',1000000,0)])
        _,checks,client=self.run_case(source,[row])
        self.assertTrue(all(c['mark']=='correct' for c in checks if c.get('claim_statement')))
        target=next(c for c in self.focused_checks(client) if c['original']=='50')
        self.assertEqual(json.loads(target['period']),[{'end':'2024-06-30','duration':'13 weeks'}])
        self.assertTrue(target['definition'].startswith('Total revenue;'))
        binding=target['source_binding']
        self.assertEqual(binding['expression'],'b')
        self.assertEqual(len(binding['operands']),1)
        operand=binding['operands'][0]
        self.assertEqual(operand['name'],'b')
        self.assertEqual(operand['source']['filing']['number_index'],1)
        self.assertEqual(operand['period']['end'],'2024-06-30')
        self.assertIn('June 30, 2024',operand['source']['filing']['context'][-1]['quote'])
        growth=next(c for c in self.focused_checks(client) if c['original']=='20.00')
        self.assertEqual([o['name'] for o in growth['source_binding']['operands']],['a','b'])
        # A valid-looking citation that selects the wrong column is not accepted,
        # even if the model returns supported=true for every check.
        row['record']['inputs'][1]['source']['filing']['number_index']=0
        _,checks,_=self.run_case(source,[row])
        self.assertTrue(all(c['mark']=='unsupported' for c in checks if c.get('claim_statement')))

    def test_direct_operand_uses_its_own_filing_row_not_the_first_operand(self):
        source,op=fixture()
        row=payload('input_quote','Operating cash flow was "$55 million" and capex was "$15 million".',
                    [op('a','cash_from_operations'),op('b','capex')],
                    [('55','a','USD',1000000,0),('15','b','USD',1000000,0)])
        _,_,client=self.run_case(source,[row])
        target=next(c for c in self.focused_checks(client) if c['original']=='15')
        self.assertTrue(target['quote'].startswith('capex |'))
        self.assertEqual(target['source_binding']['operands'][0]['definition'],'capex')
        self.assertNotIn('cash_from_operations',target['quoted_input'])

    def test_semantic_rejection_of_valid_unit_conversion_still_blocks_it(self):
        class RejectConversion(SemanticClient):
            def call(self,role,task,context,schema):
                result=super().call(role,task,context,schema)
                for check in json.loads(task.split('Code checks: ',1)[1]):
                    if check.get('source_binding',{}).get('display',{}).get('scale')=='1000000000':
                        result['checks'][check['id']]={'supported':False,'reason':'Financial definition rejected.','correction':None}
                return result
        source,op=fixture()
        row=payload('cash_flow','Free cash flow was "$0.040 billion".',
                    [op('a','cash_from_operations'),op('b','capex')],
                    [('0.040','a-b','USD',1000000000,3)])
        _,checks,_=self.run_case(source,[row],RejectConversion())
        self.assertEqual(next(c for c in checks if c.get('claim_statement'))['mark'],'unsupported')

    def test_incorrect_calculated_result_is_corrected_from_quoted_inputs(self):
        source,op=fixture()
        row=payload('growth','Revenue growth was 24.00%.',[op('a','revenue'),op('b','revenue','prior')],
                    [('24.00','(a/b-1)*100','%',1,2)])
        _,checks,_=self.run_case(source,[row])
        result=next(c for c in checks if c.get('claim_statement'))
        self.assertEqual(result['mark'],'wrong')
        self.assertEqual(result['correction'],'20.00')
        self.assertIn('180000000/150000000',result['arithmetic'])

    def test_actual_eleven_clean_statements_parse_through_generic_contract(self):
        fixture_path=Path(__file__).parent/'tests/fixtures/sourced_clean_claims.json'
        data=json.loads(fixture_path.read_text(encoding='utf-8'))
        rows=data.pop('records')
        data['compiled_report']='Filings read: saved evidence fixture\n'
        report,checks,client=self.run_case(data,rows)
        self.assertEqual(len(rows),11)
        for row in rows:
            results=[c for c in checks if c.get('claim_statement') and c['claim_id']==row['id']]
            self.assertTrue(results)
            self.assertTrue(all(c['mark']=='correct' for c in results),results)
            self.assertIn(row['record']['statement'],report)
        # Every required semantic ID has an exact result before report output.
        requested=[key for call in client.calls for key in call[3]['properties']['checks']['required']]
        self.assertEqual(len(requested),len(set(requested)))
        self.assertTrue(all(c.get('semantic_reason') for c in checks if c.get('semantic')))

    def test_direct_source_value_cannot_be_rounded_to_pass(self):
        source,row=self.revenue()
        op=row['record']['inputs'][0]
        source['saved_facts']['facts.json']['concepts']['revenue']['current']['val']=180400000
        lines=source['saved_filing_texts']['10-K.txt'].splitlines()
        line=op['source']['filing']['line']-1
        lines[line]=lines[line].replace('180','180.4')
        source['saved_filing_texts']['10-K.txt']='\n'.join(lines)
        op['quoted']='180.4'
        op['source']['filing']['quote']=lines[line]
        op['source']['fact']['record']['val']=180400000
        _,checks,_=self.run_case(source,[row])
        result=next(c for c in checks if c.get('claim_statement'))
        self.assertEqual(result['mark'],'wrong')
        self.assertEqual(result['correction'],'180.4')

    def test_unbound_extra_value_is_not_hidden_inside_a_bound_record(self):
        source,row=self.revenue()
        row['record']['statement']+=' Uncited target price: 999.'
        _,checks,_=self.run_case(source,[row])
        self.assertTrue(any(c['original']=='999' and c['mark']=='unsupported' for c in checks))
        self.assertFalse(any(c['mark']=='correct' for c in checks if c.get('claim_statement')))

    def test_all_eleven_claim_shapes_use_one_generic_contract(self):
        source,op=fixture()
        rows=[]
        def add(statement,inputs,bindings): rows.append(payload('observation_'+str(len(rows)+1),statement,inputs,bindings))
        add('Annual revenue was "$180 million".',[op('a','revenue')],[('180','a','USD',1000000,0)])
        add('Revenue growth was 20.00%.',[op('a','revenue'),op('b','revenue','prior')],[('20.00','(a/b-1)*100','%',1,2)])
        add('Operating income was "$45 million" and operating margin was 25.00%.',[op('a','operating_income'),op('b','revenue')],
            [('45','a','USD',1000000,0),('25.00','a/b*100','%',1,2)])
        add('Net income was "$30 million".',[op('a','net_income')],[('30','a','USD',1000000,0)])
        add('GAAP diluted EPS was "$2.50 per share".',[op('a','diluted_eps')],[('2.50','a','USD/shares',1,2)])
        add('Return on average equity was 30.00%.',[op('a','net_income'),op('b','equity'),op('c','equity','prior')],
            [('30.00','a/((b+c)/2)*100','%',1,2)])
        add('Debt to equity was 0.2083 times.',[op('a','long_term_debt'),op('b','current_debt'),op('c','equity')],
            [('0.2083','(a+b)/c','times',1,4)])
        add('Free cash flow was "$40 million".',[op('a','cash_from_operations'),op('b','capex')],[('40','a-b','USD',1000000,0)])
        release=source['saved_filing_texts']['earnings_release.txt'].splitlines()
        def quarter(name,line,index,quoted,unit='USD'):
            return {'name':name,'quoted':quoted,'scale':'1' if unit=='USD/shares' else '1000000','unit':unit,
                    'period':{'end':'2025-06-30' if index==0 else '2024-06-30','duration':'13 weeks'},
                    'definition':'Quarter GAAP diluted EPS' if unit=='USD/shares' else 'Quarter total revenue',
                    'source':{'filing':{'file':'earnings_release.txt','line':line,'quote':release[line-1],'number_index':index,
                                       'context':[{'line':i+1,'quote':release[i]} for i in (1,2,3)]}}}
        add('Latest-quarter total revenue was "$60 million".',[quarter('a',5,0,'60')],[('60','a','USD',1000000,0)])
        add('Same-quarter revenue growth was 20.00%.',[quarter('a',5,0,'60'),quarter('b',5,1,'50')],
            [('20.00','(a/b-1)*100','%',1,2)])
        add('Latest-quarter GAAP diluted EPS was "$0.70 per share".',[quarter('a',6,0,'0.70','USD/shares')],
            [('0.70','a','USD/shares',1,2)])
        report,checks,_=self.run_case(source,rows)
        for row in rows:
            results=[c for c in checks if c.get('claim_statement') and c['claim_id']==row['id']]
            self.assertTrue(results)
            self.assertTrue(all(c['mark']=='correct' for c in results),results)
            self.assertIn(row['record']['statement'],report)
        self.assertEqual(len({c['claim_id'] for c in checks if c.get('claim_statement')}),11)


if __name__=='__main__': unittest.main()
