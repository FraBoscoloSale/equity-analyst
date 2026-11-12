"""Made-up-data rule contract. No SEC/model request or real export read is allowed."""
import copy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch

import spinoff_seeker as s


def evidence(value, quote=None, filed='2026-10-01'):
    return {'value': value, 'quote': quote or str(value), 'section': 'Separation',
            'filing_date': filed, 'form': '10-12B/A', 'filer_cik': '101',
            'accession': '0000000101-26-000001',
            'index_url': 'https://www.sec.gov/Archives/edgar/data/101/one-index.html',
            'document_url': 'https://www.sec.gov/Archives/edgar/data/101/one.htm'}


def candidate(number=101, distribution='2026-10-01'):
    c = s.new_candidate(str(number), f'Registrant {number}')
    values = {'parent': f'Parent {number}', 'parent_cik': str(number+1000),
              'distribution_ratio': 'one share for every ten shares',
              'record_date': '2026-09-20', 'distribution_date': distribution,
              'ticker': f'X{number}', 'regular_trading_date': '2026-10-02',
              'jurisdiction': 'United States', 'child_assets': '10 million',
              'parent_assets': '100 million', 'child_assets_date': '2026-06-30',
              'parent_assets_date': '2026-06-30', 'child_assets_currency': 'USD',
              'parent_assets_currency': 'USD'}
    c['facts'] = {key: evidence(value) for key,value in values.items()}
    c['findings'] = [{'kind': 'spinoff', **evidence('parent distributes business to its shareholders')},
                     {'kind': 'distribution_completed', **evidence('completed distribution')},
                     {'kind': 'regular_way', **evidence('regular-way trading began')}]
    c['information_statement'] = True
    c['registration_complete'] = True
    c['filings'] = [{'form':'10-Q','filing_date':'2026-10-05','cik':str(number)}]
    c['insider_buying'] = 1000.0
    c['insider_evidence'] = [evidence('1000')]
    c['insider_complete'] = True
    return c


def prices(*args):
    return [{'date': day, 'close': value} for day,value in
            [('2026-10-02',10),('2026-10-05',9),('2026-10-06',8),
             ('2026-10-07',8),('2026-10-08',7)]]


class SeekerRules(unittest.TestCase):
    def setUp(self):
        self.block_network = patch('requests.sessions.Session.request', side_effect=AssertionError('Live request forbidden'))
        self.block_network.start()
        self.addCleanup(self.block_network.stop)
        self.block_reader = patch('closes_reader.daily_closes', side_effect=AssertionError('Real export read forbidden'))
        self.block_reader.start()
        self.addCleanup(self.block_reader.stop)
        # Ranking assertions below use a fixed fictional configuration, independent
        # of the user's editable factor weights (including weight-zero exercises).
        with tempfile.TemporaryDirectory() as tmp:
            factor_file=Path(tmp)/'factors.txt'
            factor_file.write_text('Window: 90 days post-distribution\nFactors:\n'
                '1. Small Relative to Parent\n - Direction: Smaller is better\n - Source: Greenblatt, Chapter 3\n - Weight: 1\n'
                '2. Weak First-Week Price Performance\n - Direction: Weaker is better\n - Source: Greenblatt, Chapter 3\n - Weight: 1\n'
                '3. Insider Buying / Management Alignment\n - Direction: Higher insider buying is better\n - Source: Greenblatt, Chapter 3\n - Weight: 1\n',encoding='utf-8')
            self.factors=s.load_factors(factor_file)

    def test_start_date_and_distribution_cutoff_are_inclusive(self):
        self.assertTrue(s.in_scope(candidate(), date(2026,10,1)))
        self.assertFalse(s.in_scope(candidate(), date(2026,10,2)))
        c=candidate(); del c['facts']['distribution_date']
        self.assertTrue(s.in_scope(c, date(2026,10,2)))
        with self.assertRaises(ValueError): s.iso_date('10/01/2026')

    def test_old_registrations_are_discoverable_and_all_required_forms_supported(self):
        text='Last Data Received: October 09, 2026\nCIK|Company Name|Form Type|Date Filed|Filename\n'
        for i,form in enumerate(('10-12B','10-12B/A','10-12G','10-12G/A','CERT','8-K','10-K')):
            text+=f'{101+i}|Registrant {i}|{form}|2025-01-02|edgar/data/{101+i}/0000000101-25-000001.txt\n'
        rows=s.master_rows(text)
        self.assertEqual({row['form'] for row in rows}, {'10-12B','10-12B/A','10-12G','10-12G/A','CERT','8-K','10-K'})
        self.assertEqual(next(s.quarters(date(1994,7,1),date(2026,10,10))), (1994,3))
        self.assertEqual(s.index_date(text,{}),'2026-10-09')
        self.assertEqual(s.index_date('',{}),'')

    def test_fact_extraction_requires_literal_value_quote_and_actual_section(self):
        doc={'text':'Separation\nOur parent is Parent 101. Distribution date: October 1, 2026.',
             **evidence('unused')}
        c=s.new_candidate('101','Registrant 101')
        payload={'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Separation'},
                          'distribution_date':{'value':'October 1, 2026','quote':'Distribution date: October 1, 2026.','section':'Separation'},
                          'parent_cik':{'value':'1101','quote':'invented','section':'Separation'}},'findings':[]}
        s.ingest(c,doc,payload)
        self.assertEqual(c['facts']['parent']['value'],'Parent 101')
        self.assertEqual(c['facts']['distribution_date']['value'],'2026-10-01')
        self.assertNotIn('parent_cik',c['facts'])
        self.assertIn('parent_cik', ' '.join(c['issues']))
        payload['facts']['parent']['section']='Invented heading'
        other=s.new_candidate('102','Registrant 102');s.ingest(other,doc,payload)
        self.assertNotIn('parent',other['facts'])

    def test_numeric_substrings_and_nonheading_sections_do_not_establish_facts(self):
        c=s.new_candidate('101','Registrant 101')
        doc={'text':'Separation\nChild assets were 100 million.','filer_cik':'101',**evidence('unused')}
        for claim in ({'value':'10 million','quote':'Child assets were 100 million.','section':'Separation'},
                      {'value':'100 million','quote':'Child assets were 100 million.','section':'assets were'}):
            s.ingest(c,doc,{'facts':{'child_assets':claim},'findings':[]})
        self.assertNotIn('child_assets',c['facts'])

    def test_parent_document_must_explicitly_identify_target_and_us_means_incorporation(self):
        c=s.new_candidate('101','Registrant 101')
        doc={'text':'Separation\nDistribution date: October 1, 2026.\nAddress is in United States.',**evidence('unused'),'filer_cik':'1101'}
        s.ingest(c,doc,{'facts':{'distribution_date':{'value':'October 1, 2026','quote':'Distribution date: October 1, 2026.','section':'Separation'}},'findings':[]})
        self.assertNotIn('distribution_date',c['facts'])
        doc['filer_cik']='101'
        s.ingest(c,doc,{'facts':{'jurisdiction':{'value':'United States','quote':'Address is in United States.','section':'Separation'}},'findings':[]})
        self.assertNotIn('jurisdiction',c['facts'])

    def test_parent_cik_must_be_explicitly_associated_with_parent_not_guessed(self):
        c=s.new_candidate('101','Registrant 101')
        quote='Parent 101 [https://www.sec.gov/Archives/edgar/data/1101/filing.htm]'
        doc={'text':'Separation\nOur parent is Parent 101.\n'+quote,**evidence('unused')}
        s.ingest(c,doc,{'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Separation'},
                 'parent_cik':{'value':'1101','quote':quote,'section':'Separation'}},'findings':[]})
        self.assertEqual(c['facts']['parent_cik']['value'],'1101')
        c=s.new_candidate('101','Registrant 101')
        doc={'text':'Separation\nRegistrant CIK 1101.',**evidence('unused')}
        s.ingest(c,doc,{'facts':{'parent_cik':{'value':'1101','quote':'Registrant CIK 1101.','section':'Separation'}},'findings':[]})
        self.assertNotIn('parent_cik',c['facts'])

    def test_negated_or_nonfinal_claims_do_not_establish_positive_findings(self):
        for kind,quote in (('spinoff','A parent does not distribute a business to shareholders.'),
                           ('fund','The registrant is not an investment fund.'),
                           ('existing_shares','The registrant is not registering existing shares.'),
                           ('distribution_completed','Distribution will be completed.'),
                           ('listing_approved','Listing approval is pending.')):
            c=s.new_candidate('101','Registrant 101')
            doc={'text':'Separation\n'+quote,**evidence('unused'),'form':'CERT' if kind=='listing_approved' else '10-12B'}
            s.ingest(c,doc,{'facts':{},'findings':[{'kind':kind,'quote':quote,'section':'Separation'}]})
            self.assertEqual(c['findings'],[],(kind,quote))

    def test_latest_observation_is_not_a_completed_close_before_four_eastern(self):
        c=candidate();reader=Mock(return_value=prices())
        s.evaluate(c,date(2026,10,8),self.factors,reader,price_as_of=date(2026,10,7))
        reader.assert_called_once_with('X101','2026-10-02','2026-10-07')
        self.assertNotIn('first_week',c['prices'])

    def test_conflicting_and_conditional_facts_stay_blank_not_estimated(self):
        c=s.new_candidate('101','Registrant 101')
        for value in ('2026-10-01','2026-10-02'):
            doc={'text':f'Separation\nDistribution date: {value}.',**evidence('unused')}
            s.ingest(c,doc,{'facts':{'distribution_date':{'value':value,'quote':f'Distribution date: {value}.','section':'Separation'}},'findings':[]})
        self.assertNotIn('distribution_date', c['facts'])
        self.assertEqual(len(c['observations']['distribution_date']),2)
        doc={'text':'Separation\nDistribution is expected on October 1, 2026.',**evidence('unused')}
        c=s.new_candidate('102','Registrant 102')
        s.ingest(c,doc,{'facts':{'distribution_date':{'value':'October 1, 2026','quote':'Distribution is expected on October 1, 2026.','section':'Separation'}},'findings':[]})
        self.assertNotIn('distribution_date',c['facts'])

    def test_each_required_registry_fact_has_filing_and_section_or_missing(self):
        c=candidate(); del c['facts']['record_date']
        s.evaluate(c,date(2026,10,10),self.factors,prices)
        for field in s.REGISTRY_FIELDS:
            row=c['registry_facts'][field]
            if field=='record_date':
                self.assertEqual(row['value'],'');self.assertIn('missing',row['reason'])
            else:
                self.assertTrue(row['index_url']);self.assertTrue(row['section'])

    def test_fund_and_existing_share_registrations_are_dropped_with_evidence(self):
        for kind in ('fund','existing_shares'):
            c=candidate();c['findings']=[{'kind':kind,**evidence('Explicit exclusion')}]
            s.evaluate(c,date(2026,10,10),self.factors,prices)
            self.assertTrue(c['dropped']);self.assertTrue(c['drop_evidence']['index_url'])
            self.assertIn(kind,c['drop_reason'])

    def test_missing_information_statement_requires_successful_inventory(self):
        c=candidate();c['information_statement']=False;c['findings']=[]
        c['registration_index']=evidence('No information statement in filing inventory')
        s.evaluate(c,date(2026,10,10),self.factors,prices)
        self.assertTrue(c['dropped']);self.assertIn('information statement',c['drop_reason'])
        c['registration_complete']=False
        s.evaluate(c,date(2026,10,10),self.factors,prices)
        self.assertFalse(c['dropped']);self.assertTrue(c['unranked_reasons'])

    def test_initial_distribution_language_without_information_statement_is_dropped_until_later_proof(self):
        c=candidate();c['information_statement']=False
        c['registration_index']=evidence('No information statement in inventory',filed='2026-10-01')
        s.evaluate(c,date(2026,10,10),self.factors,prices)
        self.assertTrue(c['dropped'])
        c['findings'].append({'kind':'spinoff',**evidence('Parent distributes business to shareholders',filed='2026-10-02')})
        s.evaluate(c,date(2026,10,10),self.factors,prices)
        self.assertFalse(c['dropped'])

    def test_later_parent_distribution_evidence_rescues_each_exclusion(self):
        for kind in ('fund','existing_shares','no_information_statement'):
            c=candidate();c['information_statement']=False
            c['findings']=[{'kind':kind,**evidence('exclusion',filed='2026-09-01')},
                           {'kind':'spinoff',**evidence('parent distributes business to shareholders',filed='2026-10-01')}]
            s.evaluate(c,date(2026,10,10),self.factors,prices)
            self.assertFalse(c['dropped'])

    def test_stages_and_window_boundaries(self):
        for elapsed,expected in ((0,'trading'),(1,'in its window'),(90,'in its window'),(91,'past its window')):
            c=candidate();c['filings'][0]['filing_date']='2026-09-30'
            s.evaluate(c,date(2026,10,1)+s.timedelta(days=elapsed),self.factors,prices)
            self.assertEqual(c['stage'],expected)
        c=candidate();c['filings']=[];c['findings']=[c['findings'][0]];del c['facts']['distribution_date']
        s.evaluate(c,date(2026,10,10),self.factors,prices);self.assertEqual(c['stage'],'registered')
        c['findings'].append({'kind':'listing_approved',**evidence('approved'),'form':'CERT'})
        s.evaluate(c,date(2026,10,10),self.factors,prices);self.assertEqual(c['stage'],'listing approved')

    def test_own_periodic_or_ownership_form_proves_trading_but_parent_form_does_not(self):
        for form in ('10-Q','10-K','4'):
            c=candidate();c['findings']=[c['findings'][0]];del c['facts']['distribution_date']
            c['filings']=[{'form':form,'cik':c['cik'],'filing_date':'2026-10-05'}]
            s.evaluate(c,date(2026,10,10),self.factors,prices);self.assertEqual(c['stage'],'trading')
            c['filings'][0]['cik']='1101'
            s.evaluate(c,date(2026,10,10),self.factors,prices);self.assertEqual(c['stage'],'registered')

    def test_us_status_requires_filing_evidence(self):
        c=candidate();del c['facts']['jurisdiction']
        s.evaluate(c,date(2026,10,10),self.factors,prices)
        self.assertFalse(c['eligible']);self.assertIn('U.S.', ' '.join(c['unranked_reasons']))

    def test_reader_used_only_for_regular_way_closes_strictly_after_distribution(self):
        c=candidate();reader=Mock(return_value=prices())
        s.evaluate(c,date(2026,10,10),self.factors,reader)
        reader.assert_called_once_with('X101','2026-10-02','2026-10-10')
        self.assertEqual(c['prices']['first'],{'date':'2026-10-02','close':10.0})
        self.assertAlmostEqual(c['measures']['weak first-week price performance'],-.3)

    def test_default_reader_resolves_export_and_retains_instrument_price_evidence(self):
        from test_closes_reader import export_file
        c=candidate()
        c['facts']['ticker']=evidence('X101')
        original=copy.deepcopy(c['facts']['ticker'])
        self.block_reader.stop()
        try:
            import closes_reader
            with export_file(['X101.K']), patch('closes_reader.daily_closes', wraps=closes_reader.daily_closes) as exact:
                s.evaluate(c,date(2026,10,10),self.factors)
                exact.assert_called_once_with('X101.K','2026-10-02','2026-10-10')
        finally:
            self.block_reader.start()
        self.assertEqual(c['facts']['ticker'],original)
        for label in ('first','first_week','latest'):
            self.assertEqual(c['prices'][label]['instrument'],'X101.K')
        self.assertAlmostEqual(c['measures']['weak first-week price performance'],-.3)
        self.assertFalse(c['unranked_reasons'])
        s.rank([c],self.factors)
        text=s.report([c],self.factors,'2026-10-10T12:00:00-04:00',[],[])
        self.assertIn('export instrument X101.K',text)
        self.assertEqual(json.loads(json.dumps(c))['prices']['first']['instrument'],'X101.K')

    def test_when_issued_identifiers_or_missing_regular_way_proof_are_never_priced(self):
        for ticker in ('X101.WI','X101-WI','X101 WI','X101.W','X101w','X101.wi','X101/wi','X101-wi'):
            c=candidate();c['facts']['ticker']=evidence(ticker);reader=Mock()
            s.evaluate(c,date(2026,10,10),self.factors,reader);reader.assert_not_called()
        c=candidate();c['findings']=[f for f in c['findings'] if f['kind']!='regular_way'];reader=Mock()
        s.evaluate(c,date(2026,10,10),self.factors,reader);reader.assert_not_called()

    def test_no_substitution_when_first_close_or_first_week_missing(self):
        for rows in ('not available',[],prices()[1:],prices()[:4]):
            c=candidate();s.evaluate(c,date(2026,10,10),self.factors,lambda *args:rows)
            self.assertTrue(c['unranked_reasons'])
        c=candidate();del c['facts']['record_date']
        s.evaluate(c,date(2026,10,10),self.factors,prices)
        self.assertIn('record_date', ' '.join(c['unranked_reasons']))

    def test_factors_come_from_file_and_unknown_configuration_fails(self):
        self.assertEqual(self.factors['window_days'],90)
        self.assertEqual([f['weight'] for f in self.factors['factors']],[1,1,1])
        self.assertTrue(all('Chapter 3' in f['source'] for f in self.factors['factors']))
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'factors.txt';p.write_text('Window: 7 days post-distribution\n1. Unknown\n - Direction: Higher is better\n - Source: Manual\n - Weight: 2\n')
            with self.assertRaises(ValueError): s.load_factors(p)

    def test_size_uses_equal_dates_and_currency_and_no_missing_data_estimate(self):
        c=candidate();s.evaluate(c,date(2026,10,10),self.factors,prices)
        self.assertAlmostEqual(c['measures']['small relative to parent'],.1)
        for field in ('parent_assets','child_assets_date','parent_assets_currency'):
            c=candidate();del c['facts'][field]
            s.evaluate(c,date(2026,10,10),self.factors,prices)
            self.assertNotIn('small relative to parent',c['measures'])

    def test_rank_only_window_names_with_all_inputs_and_print_each_factor_points(self):
        rows=[]
        for i in range(3):
            c=candidate(101+i);s.evaluate(c,date(2026,10,10),self.factors,prices);rows.append(c)
        rows[0]['measures'].update({'small relative to parent':.01,'weak first-week price performance':-.4,'insider buying / management alignment':2000})
        rows[1]['measures'].update({'small relative to parent':.2,'weak first-week price performance':.1,'insider buying / management alignment':10})
        rows[2]['stage']='past its window'
        ranked=s.rank(rows,self.factors)
        self.assertEqual([c['cik'] for c in ranked],['101','102'])
        self.assertEqual(ranked[0]['total_points'],3)
        self.assertEqual(ranked[1]['total_points'],0)
        self.assertEqual(len(ranked[0]['factor_points']),3)
        self.assertNotIn('rank',rows[2])
        rows[0]['measures']['small relative to parent']=.2
        rows[0]['measures']['weak first-week price performance']=.1
        rows[0]['measures']['insider buying / management alignment']=10
        self.assertTrue(all(c['total_points']==0 for c in s.rank(rows,self.factors)))

    def test_report_dates_prices_rank_gaps_drops_and_exact_final_line(self):
        c=candidate();s.evaluate(c,date(2026,10,10),self.factors,prices);s.rank([c],self.factors)
        dropped=candidate(102);dropped.update(dropped=True,drop_reason='fund',drop_evidence=evidence('fund'))
        pending=candidate(103);pending.update(stage='registered',eligible=False,unranked_reasons=['missing distribution_date'])
        text=s.report([c,dropped,pending],self.factors,'2026-10-10T12:00:00-04:00',['2026-10-09'],['Coverage gap'])
        self.assertIn('2026-10-10',text.splitlines()[0]);self.assertIn('2026-10-08',text.splitlines()[0]);self.assertIn('2026-10-09',text.splitlines()[0])
        self.assertIn('2026-10-02',text);self.assertIn('10.0',text)
        self.assertIn('Registered',text);self.assertIn('fund',text);self.assertIn('one-index.html',text)
        self.assertIn('Coverage gap',text);self.assertIn('missing distribution_date',text)
        for factor in self.factors['factors']: self.assertIn(factor['name'],text)
        self.assertEqual(text.splitlines()[-1],'screens, not recommendations')

    def test_registry_preserves_unseen_records_and_timestamped_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);c=candidate();s.evaluate(c,date(2026,10,10),self.factors,prices)
            old=candidate(999)
            s.save_run(root,{'999':old},[c],self.factors,'2026-10-10T12:00:00-04:00',[],[])
            first=list(root.glob('report_*.md'))[0];before=first.read_bytes()
            s.save_run(root,json.loads((root/'registry.json').read_text())['candidates'],[c],self.factors,'2026-10-10T12:00:01-04:00',[],[])
            saved=json.loads((root/'registry.json').read_text())
            self.assertEqual(set(saved['candidates']),{'101','999'})
            self.assertEqual(first.read_bytes(),before);self.assertEqual(len(list(root.glob('report_*.md'))),2)


class SourceRules(unittest.TestCase):
    def setUp(self):
        self.guard=patch('requests.sessions.Session.request',side_effect=AssertionError('Live network forbidden'))
        self.guard.start();self.addCleanup(self.guard.stop)

    def test_transport_headers_throttle_retry_redirect_and_host_rejection(self):
        response=Mock(status_code=200,content=b'body',headers={},url='https://www.sec.gov/x')
        response.raise_for_status.return_value=None
        session=Mock();session.get.return_value=response
        clock=Mock(return_value=1);sleep=Mock()
        client=s.SEC('unit-test contact@example.invalid',session=session,clock=clock,sleep=sleep)
        client.get('https://www.sec.gov/x');client.get('https://data.sec.gov/y')
        self.assertGreaterEqual(sleep.call_args.args[0],.3)
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs['headers']['User-Agent'],'unit-test contact@example.invalid')
            self.assertFalse(call.kwargs['allow_redirects'])
        for url in ('http://www.sec.gov/x','https://invalid.example/x'):
            with self.assertRaises(ValueError):client.get(url)
        retry=Mock(status_code=429,headers={'Retry-After':'2'});redirect=Mock(status_code=302,headers={'Location':'https://data.sec.gov/z'})
        session.get.side_effect=[retry,redirect,response]
        client.get('https://www.sec.gov/retry')
        self.assertTrue(any(call.args[0]>=2 for call in sleep.call_args_list))
        self.assertEqual(session.get.call_args.kwargs['headers']['User-Agent'],'unit-test contact@example.invalid')
        session.get.side_effect=[Mock(status_code=302,headers={'Location':'https://invalid.example/x'})]
        with self.assertRaises(ValueError):client.get('https://www.sec.gov/redirect')

    def test_historical_submission_shards_and_parent_cik_are_used(self):
        client=Mock();client.json.side_effect=[{'name':'Registrant 101','filings':{'recent':{'form':['10-Q'],'accessionNumber':['0000000101-26-000002'],'filingDate':['2026-10-05'],'primaryDocument':['q.htm']},'files':[{'name':'CIK0000000101-submissions-001.json'}]}},
                {'form':['8-K'],'accessionNumber':['0000000101-26-000001'],'filingDate':['2026-10-01'],'primaryDocument':['k.htm']}]
        info,rows=s.submissions(client,'101')
        self.assertEqual({r['form'] for r in rows},{'10-Q','8-K'})
        self.assertIn('submissions-001.json',client.json.call_args.args[0])
        c=candidate();lookup=Mock(return_value=({},[{'form':'8-K','cik':'1101'}]))
        with patch.object(s,'submissions',lookup): self.assertEqual(s.parent_filings(client,c)[0]['cik'],'1101')
        lookup.assert_called_once_with(client,'1101')

    def test_filing_index_folder_and_relevant_documents_are_read(self):
        client=Mock()
        client.text.side_effect=['<html><div class="infoHead">Accepted</div><div class="info">2026-10-01 16:00:00</div><table class="tableFile"><tr><td>1</td><td>Information Statement</td><td><a href="/Archives/edgar/data/101/a/ex.htm">ex.htm</a></td><td>EX-99.1</td></tr></table></html>',
                                '<h1>Information Statement</h1><h2>Separation</h2><p>Parent distributes a business.</p>']
        client.json.return_value={'directory':{'item':[{'name':'ex.htm'}]}}
        row={'cik':'101','name':'Registrant 101','form':'10-12B/A','accession':'0000000101-26-000001','filing_date':'2026-10-01'}
        bundle=s.filing_documents(client,row)
        self.assertTrue(bundle['information_statement']);self.assertTrue(bundle['complete'])
        self.assertIn('index.json',client.json.call_args.args[0]);self.assertEqual(len(bundle['documents']),1)
        self.assertIn('Separation',bundle['documents'][0]['text'])

    def test_saying_no_information_statement_is_not_an_information_statement(self):
        client=Mock()
        client.text.side_effect=['<table class="tableFile"><tr><td>1</td><td>Registration</td><td><a href="doc.htm">doc.htm</a></td><td>10-12B</td></tr></table>',
                                '<h1>Registration</h1><p>No information statement is included in this filing.</p>']
        client.json.return_value={'directory':{'item':[{'name':'doc.htm'}]}}
        bundle=s.filing_documents(client,{'cik':'101','form':'10-12B','filing_date':'2026-09-01','accession':'0000000101-26-000001'})
        self.assertFalse(bundle['information_statement']);self.assertTrue(bundle['complete'])

    def test_form4_buys_only_actual_p_acquisitions_no_grants_no_amendment_double_count(self):
        def xml(code,shares='10',price='5'):
            return f'<ownershipDocument><issuer><issuerCik>101</issuerCik></issuer><nonDerivativeTransaction><transactionDate><value>2026-10-02</value></transactionDate><transactionCoding><transactionCode>{code}</transactionCode></transactionCoding><transactionAmounts><transactionShares><value>{shares}</value></transactionShares><transactionPricePerShare><value>{price}</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction></ownershipDocument>'
        self.assertEqual(s.form4_purchases(xml('P'),'101',date(2026,10,1),date(2026,10,10)),50)
        self.assertEqual(s.form4_purchases(xml('A'),'101',date(2026,10,1),date(2026,10,10)),0)
        self.assertEqual(s.form4_purchases(xml('S'),'101',date(2026,10,1),date(2026,10,10)),0)
        self.assertIsNone(s.form4_purchases(xml('P',price=''),'101',date(2026,10,1),date(2026,10,10)))
        self.assertIsNone(s.form4_purchases(xml('P'),'999',date(2026,10,1),date(2026,10,10)))
        c=candidate();issues=[]
        s.collect_insiders(Mock(),c,[{'form':'4/A','filing_date':'2026-10-03'}],date(2026,10,10),issues)
        self.assertFalse(c['insider_complete']);self.assertIsNone(c['insider_buying'])
        self.assertIn('amendment',' '.join(issues))

    def test_extractor_gets_filing_text_only_and_no_vendor_price_history(self):
        client=Mock();client.return_value={'facts':{},'findings':[]}
        extractor=s.Extractor('not-a-real-key',completion=client)
        doc={'text':'Separation\nA parent distributes a business.','form':'10-12B'}
        extractor.extract(doc)
        encoded=json.dumps(client.call_args.args)
        self.assertIn('A parent distributes a business',encoded)
        self.assertNotIn('Price Close',encoded);self.assertNotIn('daily_closes',encoded)
        self.assertNotIn('CLOSES_FILE',encoded)

    def test_full_pipeline_with_parent_distribution_filing_and_made_up_sources(self):
        reg={'cik':'101','name':'Registrant 101','form':'10-12B','filing_date':'2026-09-01','accession':'0000000101-26-000001'}
        cert={**reg,'form':'CERT','filing_date':'2026-09-02','accession':'0000000101-26-000002'}
        parent={**reg,'cik':'1101','name':'Parent 101','form':'8-K','filing_date':'2026-10-03','accession':'0000001101-26-000001'}
        owned={**reg,'form':'4','filing_date':'2026-10-06','accession':'0000000101-26-000003'}
        periodic={**reg,'form':'10-Q','filing_date':'2026-10-05','accession':'0000000101-26-000004'}
        child_facts={key: value for key,value in candidate()['facts'].items() if key not in {'distribution_date','record_date','regular_trading_date','ticker'}}
        def claims(facts):
            payload={};sentences=[]
            for key,row in facts.items():
                literal=row['value']
                sentence=f'Registrant 101 {key}: {literal}.'
                if key=='jurisdiction':sentence=f'Registrant 101 is incorporated in {literal}.'
                if key=='parent_cik':sentence=f'Registrant 101 parent Parent 101 CIK: {literal}.'
                if key=='ticker':sentence=f'Registrant 101 began regular-way trading under ticker {literal}.'
                payload[key]={'value':literal,'quote':sentence,'section':'Separation'};sentences.append(sentence)
            return payload,sentences
        reg_claims,reg_text=claims(child_facts)
        parent_claims,parent_text=claims({key:candidate()['facts'][key] for key in ('distribution_date','record_date','regular_trading_date','ticker')})
        texts={'10-12B':'Information Statement\nSeparation\n'+'\n'.join(reg_text)+'\nParent 101 distributes Registrant 101 business to its shareholders.',
               '8-K':'Separation\n'+'\n'.join(parent_text)+'\nParent 101 completed the distribution of Registrant 101.\nRegistrant 101 began regular-way trading.',
               'CERT':'Separation\nExchange approved the listing of Registrant 101.',
               '4':'<ownershipDocument><issuer><issuerCik>101</issuerCik></issuer><nonDerivativeTransaction><transactionDate><value>2026-10-02</value></transactionDate><transactionCoding><transactionCode>P</transactionCode></transactionCoding><transactionAmounts><transactionShares><value>10</value></transactionShares><transactionPricePerShare><value>5</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction></ownershipDocument>'}
        routes={};json_routes={};payloads={}
        for row in (reg,cert,parent,owned):
            base=f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{row['accession'].replace('-','')}/"
            filename='doc.xml' if row['form']=='4' else 'doc.htm'
            routes[base+row['accession']+'-index.html']=f'<table class="tableFile"><tr><td>1</td><td>Primary document</td><td><a href="{filename}">{filename}</a></td><td>{row["form"]}</td></tr></table>'
            routes[base+filename]='<pre>'+texts[row['form']]+'</pre>' if filename.endswith('htm') else texts[row['form']]
            json_routes[base+'index.json']={'directory':{'item':[{'name':filename}]}}
            findings=[];facts={}
            if row['form']=='10-12B':
                facts=reg_claims;findings=[{'kind':'spinoff','quote':'Parent 101 distributes Registrant 101 business to its shareholders.','section':'Separation'}]
            elif row['form']=='8-K':
                facts=parent_claims;findings=[{'kind':'distribution_completed','quote':'Parent 101 completed the distribution of Registrant 101.','section':'Separation'},
                      {'kind':'regular_way','quote':'Registrant 101 began regular-way trading.','section':'Separation'}]
            elif row['form']=='CERT':findings=[{'kind':'listing_approved','quote':'Exchange approved the listing of Registrant 101.','section':'Separation'}]
            payloads[base+filename]={'facts':facts,'findings':findings}
        def submission(rows,name):
            return {'name':name,'filings':{'recent':{'form':[r['form'] for r in rows],'accessionNumber':[r['accession'] for r in rows],
                'filingDate':[r['filing_date'] for r in rows],'primaryDocument':['doc.htm']*len(rows)},'files':[]}}
        json_routes['https://data.sec.gov/submissions/CIK0000000101.json']=submission([reg,cert,owned,periodic],'Registrant 101')
        json_routes['https://data.sec.gov/submissions/CIK0000001101.json']=submission([parent],'Parent 101')
        master='Last Data Received: October 09, 2026\n'+''.join(f"{r['cik']}|{r['name']}|{r['form']}|{r['filing_date']}|edgar/data/{r['cik']}/{r['accession']}.txt\n" for r in (reg,cert))
        client=Mock();client.get.return_value=Mock(content=master.encode(),headers={})
        client.text.side_effect=lambda url:routes[url];client.json.side_effect=lambda url:json_routes[url]
        extractor=Mock();extractor.extract.side_effect=lambda doc,target:copy.deepcopy(payloads[doc['document_url']])
        progress=s.Progress()
        with tempfile.TemporaryDirectory() as tmp,patch('closes_reader.daily_closes',side_effect=AssertionError('Real reader forbidden')):
            path=s.seek(date(2025,10,10),date(2026,10,1),date(2026,10,10),tmp,client,extractor,prices,
                        run_at='2026-10-10T17:00:00-04:00',progress=progress)
            c=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['101']
            self.assertEqual(c['rank'],1,c['issues'])
            self.assertEqual(c['facts']['distribution_date']['filer_cik'],'1101')
            self.assertEqual(c['stage'],'in its window')
            self.assertEqual(len(c['factor_points']),3)
            self.assertEqual(path.read_text().splitlines()[-1],'screens, not recommendations')
            self.assertTrue(any('0000001101' in call.args[0] for call in client.json.call_args_list))
            self.fixture_stats={'plausible_candidates':progress.counts['candidates_total'],
                                'documents_inspected':len({call.args[0] for call in client.text.call_args_list if call.args[0] in payloads}),
                                'model_calls':extractor.extract.call_count}

    def test_missing_index_is_explicit_coverage_gap_not_empty_success(self):
        client=Mock();client.get.side_effect=RuntimeError('Synthetic failure')
        issues=[];regs,certs,dates=s.discover(client,date(2026,10,1),date(2026,10,10),issues)
        self.assertEqual(dict(regs),{});self.assertTrue(issues);self.assertEqual(dates,[])


class ExtractionPerformanceRules(unittest.TestCase):
    def setUp(self):
        self.guard=patch('requests.sessions.Session.request',side_effect=AssertionError('Live request forbidden'))
        self.guard.start();self.addCleanup(self.guard.stop)

    def doc(self,text,form='10-12B'):
        return {'text':text,**evidence('unused'),'form':form}

    def test_irrelevant_cover_and_earnings_documents_are_read_without_model_calls(self):
        for form,text in (('10-12B','Registration\nThe registrant is registering existing shares.'),
                          ('8-K','Results\nRevenue increased in the quarter.'),
                          ('8-K','Results\nAssets and liabilities are reported separately.'),
                          ('8-K','Dividends\nThe distribution of cash dividends was completed.'),
                          ('10-12B','Agreement\nCredit covenant and collateral terms.')):
            c=s.new_candidate('101','Registrant 101');extractor=Mock();issues=[]
            bundle={'documents':[self.doc(text,form)],'complete':True,'information_statement':False,'index':evidence('Inventory')}
            with patch.object(s,'filing_documents',return_value=bundle):
                s.inspect_filing(Mock(),extractor,c,{'form':form,'accession':'one'},issues)
            extractor.extract.assert_not_called()
            self.assertFalse(issues)
        self.assertTrue(any(f['kind']=='existing_shares' for f in
            s.deterministic_findings(self.doc('Registration\nThe registrant is registering existing shares.'))))

    def test_old_ambiguous_and_later_positive_registration_documents_are_not_excluded_by_age(self):
        for text in ('Information Statement\nDetails of the business.',
                     'Separation\nA parent distributes the business to its shareholders.',
                     'Registration\nNature of the registrant is unclear.'):
            self.assertTrue(s.needs_extraction(self.doc(text)))
        self.assertTrue(s.needs_extraction(self.doc('Separation\nDistribution of the business was completed.','8-K')))
        self.assertTrue(s.needs_extraction(self.doc('Transaction\nThe parent distributed shares of its subsidiary to stockholders.','8-K')))

    def test_successful_chunk_cache_is_persistent_and_target_sensitive(self):
        completion=Mock(return_value={'facts':{},'findings':[]})
        with tempfile.TemporaryDirectory() as tmp:
            first=s.Extractor('fake-key',completion=completion,cache_dir=Path(tmp))
            doc=self.doc('Information Statement\nA parent distributes a business to shareholders.')
            first.extract(doc,target='Registrant 101');first.extract(doc,target='Registrant 101')
            second=s.Extractor('fake-key',completion=completion,cache_dir=Path(tmp))
            second.extract(doc,target='Registrant 101')
            self.assertEqual(completion.call_count,1)
            second.extract(doc,target='Registrant 102');self.assertEqual(completion.call_count,2)
            doc['text']+='\nAn amendment.'
            second.extract(doc,target='Registrant 101');self.assertEqual(completion.call_count,3)

    def test_duplicate_document_in_index_is_only_read_once(self):
        client=Mock();tr='<tr><td>1</td><td>Information Statement</td><td><a href="doc.htm">doc.htm</a></td><td>EX-99.1</td></tr>'
        client.text.side_effect=['<table class="tableFile">'+tr+tr+'</table>','<h1>Information Statement</h1>']
        client.json.return_value={'directory':{'item':[{'name':'doc.htm'}]}}
        bundle=s.filing_documents(client,{'cik':'101','form':'10-12B','filing_date':'2026-10-01','accession':'0000000101-26-000001'})
        self.assertEqual(len(bundle['documents']),1);self.assertEqual(client.text.call_count,2)

    def test_cached_claims_are_revalidated_and_attached_to_current_filing(self):
        text='Information Statement\nSeparation\nOur parent is Parent 101.'
        payload={'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Separation'}},'findings':[]}
        completion=Mock(return_value=payload);extractor=s.Extractor('fake-key',completion=completion)
        c=s.new_candidate('101','Registrant 101')
        for accession in ('0000000101-26-000001','0000000101-26-000002'):
            doc={**self.doc(text),'accession':accession}
            bundle={'documents':[doc],'complete':True,'information_statement':True,'index':evidence('Inventory')}
            payload=extractor.extract(doc,target=c['name'])
            s.ingest(c,doc,payload)
        self.assertEqual(completion.call_count,1)
        self.assertEqual(c['facts']['parent']['accession'],'0000000101-26-000002')
        self.assertEqual(c['facts']['parent']['section'],'Separation')
        self.assertEqual(len(c['observations']['parent']),2)

    def test_success_resets_consecutive_failure_counter(self):
        completion=Mock(side_effect=[request_timeout(),request_timeout(),{'facts':{},'findings':[]},request_timeout()])
        extractor=s.Extractor('fake-key',completion=completion)
        for i in range(4):
            doc=self.doc('Information Statement\n'+str(i))
            if i==2:extractor.extract(doc,target='Registrant 101')
            else:
                with self.assertRaises(s.requests.Timeout):extractor.extract(doc,target='Registrant 101')
        self.assertEqual(extractor.failures,1);self.assertEqual(completion.call_count,4)

    def test_failed_extractions_are_not_cached_and_circuit_opens_after_three_failures(self):
        completion=Mock(side_effect=request_timeout())
        with tempfile.TemporaryDirectory() as tmp:
            extractor=s.Extractor('fake-key',completion=completion,cache_dir=Path(tmp))
            for i in range(4):
                with self.assertRaises((s.ExtractionUnavailable, s.requests.RequestException)):
                    extractor.extract(self.doc('Information Statement\n'+str(i)),target='Registrant 101')
            self.assertEqual(completion.call_count,3)
            self.assertEqual(list(Path(tmp).glob('*.json')),[])
            completion.side_effect=None;completion.return_value={'facts':{},'findings':[]}
            fresh=s.Extractor('fake-key',completion=completion,cache_dir=Path(tmp))
            fresh.extract(self.doc('Information Statement\n0'),target='Registrant 101')
            self.assertEqual(completion.call_count,4)

    def test_hard_deadline_kills_worker_and_emits_wait_progress(self):
        now=[0.0];events=[]
        process=Mock()
        def communicate(*args,**kwargs):
            if 'timeout' not in kwargs:return ('','')
            now[0]+=kwargs['timeout']
            raise subprocess.TimeoutExpired('synthetic-worker',kwargs['timeout'])
        process.communicate.side_effect=communicate
        with patch.object(s.subprocess,'Popen',return_value=process):
            with self.assertRaises(s.ExtractionUnavailable):
                s.model_request({},timeout=25,clock=lambda:now[0],notify=events.append)
        process.kill.assert_called_once()
        self.assertTrue(any('waiting' in event for event in events))
        self.assertLessEqual(now[0],25)

    def test_failed_chunk_stops_rest_of_document_and_records_gap(self):
        c=s.new_candidate('101','Registrant 101');extractor=Mock();extractor.extract.side_effect=request_timeout()
        doc=self.doc('Information Statement\nDistribution Terms\nThe distribution ratio involves two classes of shares and allocation adjustments.\n'+('Unrelated business details. '*5000))
        issues=[]
        with patch.object(s,'filing_documents',return_value={'documents':[doc],'complete':True,'information_statement':True,'index':evidence('inventory')}):
            s.inspect_filing(Mock(),extractor,c,{'form':'10-12B','accession':'one'},issues)
        self.assertEqual(extractor.extract.call_count,1)
        self.assertTrue(any('remaining' in issue for issue in issues))
        self.assertFalse(c['facts'])

    def test_progress_counts_and_extraction_audit_do_not_expose_key_or_body(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp,redirect_stdout(io.StringIO()) as output:
            progress=s.Progress(Path(tmp)/'events.jsonl')
            extractor=s.Extractor('SECRET_TEST_VALUE',completion=Mock(return_value={'facts':{},'findings':[]}),progress=progress)
            extractor.extract(self.doc('Information Statement\nFILING_TEXT_SENTINEL'),target='Registrant 101')
            extractor.extract(self.doc('Information Statement\nFILING_TEXT_SENTINEL'),target='Registrant 101')
            log=(Path(tmp)/'events.jsonl').read_text()
            self.assertEqual(progress.counts['model_calls'],1)
            self.assertEqual(progress.counts['cache_hits'],1)
            self.assertIn('model',output.getvalue());self.assertIn('cache',output.getvalue())
            self.assertNotIn('SECRET_TEST_VALUE',log);self.assertNotIn('FILING_TEXT_SENTINEL',log)


def request_timeout():
    return s.requests.Timeout('Synthetic timeout')


class RegistrantTriageRules(unittest.TestCase):
    def setUp(self):
        self.guard=patch('requests.sessions.Session.request',side_effect=AssertionError('Live request forbidden'))
        self.guard.start();self.addCleanup(self.guard.stop)

    def row(self,cik='101',day='2026-09-01',form='10-12B'):
        return {'cik':cik,'name':'Registrant '+cik,'filing_date':day,'form':form,'accession':f'{int(cik):010d}-26-{int(day[-2:]):06d}'}

    def bundle(self,row,text,information=False,complete=True):
        doc={**evidence('unused'),'text':text,'form':row['form'],'filing_date':row['filing_date'],'filer_cik':row['cik'],'accession':row['accession']}
        return {'documents':[doc],'complete':complete,'information_statement':information,'inventory':[],
                'index':{**evidence('Inventory'),'filing_date':row['filing_date'],'accession':row['accession']}}

    def test_seeds_are_period_registrations_cert_linked_carryins_and_registry_not_entire_archive(self):
        from collections import defaultdict
        regs=defaultdict(list,{'101':[self.row()], '102':[self.row('102','2001-09-01')],
                               '103':[self.row('103','2025-08-01')]})
        certs=defaultdict(list,{'103':[self.row('103','2026-09-02','CERT')]})
        self.assertEqual(s.period_seeds(regs,certs,{},date(2025,11,10)),{'101','103'})
        self.assertEqual(s.period_seeds(regs,certs,{'104':{}},date(2025,11,10)),{'101','103','104'})
        self.assertNotIn('102',s.period_seeds(regs,certs,{},date(2025,11,10)))
        self.assertNotIn('105',s.period_seeds(regs,certs,{'105':{'dropped':True}},date(2025,11,10)))

    def test_new_registration_starts_new_chain_without_dragging_in_unrelated_old_registrations(self):
        rows=[self.row(day='2001-09-01'),self.row(day='2002-09-02',form='10-12B/A'),self.row(day='2026-09-03')]
        selected=s.registration_chain(rows,date(2025,11,10))
        self.assertEqual([r['filing_date'] for r in selected],['2026-09-03'])
        rows[-1]['form']='10-12B/A'
        self.assertEqual(len(s.registration_chain(rows,date(2025,11,10))),3)

    def test_no_information_statement_is_rejected_before_models_or_submissions(self):
        row=self.row();c=s.new_candidate('101','Registrant 101')
        with patch.object(s,'filing_documents',return_value=self.bundle(row,'Registration\nClass of shares being registered.')) as documents:
            queued,issues=s.screen_registrant(Mock(),c,[row],[],s.Progress())
        self.assertFalse(queued);self.assertFalse(c['plausible']);self.assertEqual(c['pre_screen_status'],'dropped')
        self.assertEqual(c['pre_screen_evidence']['accession'],row['accession'])
        self.assertIn('information statement',c['pre_screen_reason'])
        self.assertEqual(documents.call_args.kwargs['purpose'],'triage')

    def test_fund_information_statement_is_not_itself_a_spinoff_signal(self):
        row=self.row();c=s.new_candidate('101','Registrant 101')
        text='Information Statement\nThe registrant is an investment fund.\nCash distributions to shareholders are paid monthly.'
        with patch.object(s,'filing_documents',return_value=self.bundle(row,text,True)):
            queue,issues=s.screen_registrant(Mock(),c,[row],[],s.Progress())
        self.assertFalse(queue);self.assertFalse(c['plausible']);self.assertIn('fund',c['pre_screen_reason'])

    def test_trust_beneficial_interest_registration_is_dropped_with_filing_proof(self):
        row=self.row(form='10-12G')
        text=('Information Statement\nExample Asset Trust (the “Trust”) issues common units of '
              'fractional undivided beneficial interest (“Shares”), which represent ownership in the Trust.')
        bundle=self.bundle(row,text,True)
        c=s.new_candidate('101','Example Asset Trust')
        with patch.object(s,'filing_documents',return_value=bundle):
            queue,issues=s.screen_registrant(Mock(),c,[row],[],s.Progress())
        self.assertFalse(queue);self.assertFalse(c['plausible'])
        self.assertEqual(c['pre_screen_status'],'dropped')
        s.evaluate(c,date(2026,10,10),s.load_factors(s.ROOT/'spinoff_factors.txt'),Mock())
        self.assertTrue(c['dropped']);self.assertEqual(c['drop_reason'],'fund')
        self.assertEqual(c['drop_evidence']['accession'],row['accession'])
        self.assertEqual(c['drop_evidence']['section'],'Information Statement')
        self.assertIn('beneficial interest',c['drop_evidence']['quote'])

    def test_marketing_agreement_date_and_labeled_date_are_not_spinoff_dates(self):
        row=self.row(form='10-12G')
        for heading,quote in (
            ('Distribution and Marketing Agreement','On September 18, 2026, the Sponsor entered into a distribution and marketing agreement.'),
            ('Securities Distribution','On September 18, 2026, the Company signed a securities distribution agreement.'),
            ('Distribution Agreement','Distribution date: September 18, 2026.')):
            with self.subTest(heading=heading):
                c=s.new_candidate('101','Registrant 101')
                doc=self.bundle(row,heading+'\n'+quote,True)['documents'][0]
                claim={'value':'September 18, 2026','quote':quote,'section':heading}
                s.ingest(c,doc,{'facts':{'distribution_date':claim},'findings':[]})
                s.deterministic_document(c,doc)
                self.assertNotIn('distribution_date',c['facts'])

    def test_securities_marketing_arrangement_cannot_create_spinoff_finding_or_signal(self):
        row=self.row(form='10-12G')
        text=('Information Statement\nDistribution and Marketing Agreement\n'
              'The Sponsor entered into a securities distribution agreement to market shares of the Trust to shareholders. '
              'The Sponsor has an investment business.')
        bundle=self.bundle(row,text,True);doc=bundle['documents'][0]
        c=s.new_candidate('101','Registrant 101')
        self.assertFalse(s.spin_evidence(doc))
        s.ingest(c,doc,{'facts':{},'findings':[{'kind':'spinoff',
            'quote':'The Sponsor entered into a securities distribution agreement to market shares of the Trust to shareholders.',
            'section':'Distribution and Marketing Agreement'}]})
        self.assertFalse(c['findings'])
        with patch.object(s,'filing_documents',return_value=bundle):
            queue,issues=s.screen_registrant(Mock(),c,[row],[],s.Progress())
        self.assertFalse(queue);self.assertFalse(c['plausible'])

    def test_later_operating_business_distribution_rescues_trust_registration(self):
        row=self.row(form='10-12G');later=self.row(day='2026-10-02',form='8-K')
        c=s.new_candidate('101','Registrant 101')
        original=self.bundle(row,'Information Statement\nThe Trust issues shares of beneficial interest in the Trust.',True)
        with patch.object(s,'filing_documents',return_value=original):
            s.screen_registrant(Mock(),c,[row],[],s.Progress())
        factors=s.load_factors(s.ROOT/'spinoff_factors.txt')
        s.evaluate(c,date(2026,10,10),factors,Mock())
        self.assertTrue(c['dropped'])
        quote='The parent distributed the operating business of Registrant 101 to its shareholders on October 1, 2026.'
        rescue=self.bundle(later,'Separation\n'+quote,True)
        with patch.object(s,'filing_documents',return_value=original):
            queue,issues=s.screen_registrant(Mock(),c,[row],[(later,rescue)],s.Progress())
        self.assertTrue(c['plausible']);self.assertTrue(queue)
        s.ingest(c,rescue['documents'][0],{'facts':{},'findings':[{'kind':'spinoff','quote':quote,'section':'Separation'}]})
        s.evaluate(c,date(2026,10,10),factors,Mock())
        self.assertFalse(c['dropped'])

    def test_agreement_can_evidence_actual_parent_distribution_on_same_date(self):
        row=self.row(form='8-K')
        quote=('On September 18, 2026, under the distribution agreement, the parent distributed '
               'the operating business of Registrant 101 to its shareholders.')
        doc=self.bundle(row,'Distribution Agreement\n'+quote,True)['documents'][0]
        c=s.new_candidate('101','Registrant 101')
        s.ingest(c,doc,{'facts':{'distribution_date':{'value':'September 18, 2026',
            'quote':quote,'section':'Distribution Agreement'}},'findings':[]})
        self.assertEqual(s.value(c,'distribution_date'),'2026-09-18')

    def test_unrelated_parent_distribution_does_not_validate_marketing_agreement_date(self):
        row=self.row(form='8-K')
        quote=('On September 18, 2026, the Sponsor entered into a distribution and marketing agreement. '
               'On October 1, 2026, the parent distributed the operating business to its shareholders.')
        doc=self.bundle(row,'Separation\n'+quote,True)['documents'][0]
        c=s.new_candidate('101','Registrant 101')
        s.ingest(c,doc,{'facts':{'distribution_date':{'value':'September 18, 2026',
            'quote':quote,'section':'Separation'}},'findings':[]})
        self.assertNotIn('distribution_date',c['facts'])

    def test_saved_marketing_date_is_invalidated_without_erasing_historical_evidence(self):
        c=s.new_candidate('101','Registrant 101')
        bad={**evidence('2026-09-18'),'quote':'On September 18, 2026, the Sponsor entered into a distribution and marketing agreement.',
             'section':'Distribution and Marketing Agreement'}
        c['facts']['distribution_date']=copy.deepcopy(bad)
        c['observations']['distribution_date']=[copy.deepcopy(bad)]
        s.evaluate(c,date(2026,10,10),s.load_factors(s.ROOT/'spinoff_factors.txt'),Mock())
        self.assertNotIn('distribution_date',c['facts'])
        self.assertTrue(c['observations']['distribution_date'][0].get('invalid_reason'))
        self.assertIn('missing distribution_date',c['unranked_reasons'])

    def test_explicit_trust_exclusion_survives_incomplete_inventory_in_offline_pipeline(self):
        from collections import defaultdict
        row=self.row(form='10-12G');c=s.new_candidate('101','Registrant 101')
        bundle=self.bundle(row,'Information Statement\nThe Trust issues shares of beneficial interest in the Trust.',True,False)
        extractor=Mock();reader=Mock()
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(defaultdict(list,{'101':[row]}),defaultdict(list),[])),\
             patch.object(s,'period_distribution_filings',return_value=[]),\
             patch.object(s,'filing_documents',return_value=bundle):
            s.seek(date(2025,11,10),None,date(2026,10,10),tmp,Mock(),extractor,reader,run_at='2026-10-10T17:00:00-04:00')
            saved=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['101']
        self.assertTrue(saved['dropped']);self.assertEqual(saved['drop_evidence']['accession'],row['accession'])
        extractor.extract.assert_not_called();reader.assert_not_called()

    def test_later_cash_dividend_does_not_rescue_nonspinoff(self):
        row=self.row();later=self.row('1101','2026-09-03','8-K');c=s.new_candidate('101','Registrant 101')
        bundle=self.bundle(later,'Distributions\nRegistrant 101 has an investment business. The distribution of cash dividends to its shareholders was completed.')
        with patch.object(s,'filing_documents',return_value=self.bundle(row,'Registration\nThe registrant is an investment fund.')):
            queue,issues=s.screen_registrant(Mock(),c,[row],[(later,bundle)],s.Progress())
        self.assertFalse(queue);self.assertFalse(c['plausible']);self.assertEqual(c['pre_screen_status'],'dropped')

    def test_triage_uses_index_and_folder_but_does_not_download_unrelated_contract_exhibits(self):
        row=self.row();client=Mock()
        client.text.side_effect=['<table class="tableFile"><tr><td>1</td><td>Registration</td><td><a href="primary.htm">primary.htm</a></td><td>10-12B</td></tr><tr><td>2</td><td>Credit agreement</td><td><a href="credit.htm">credit.htm</a></td><td>EX-10.1</td></tr><tr><td>3</td><td>Information Statement</td><td><a href="info.htm">info.htm</a></td><td>EX-99.1</td></tr></table>',
                                 '<h1>Registration</h1>','<h1>Information Statement</h1><p>Parent distributes its business to shareholders.</p>']
        client.json.return_value={'directory':{'item':[{'name':name} for name in ('primary.htm','credit.htm','info.htm')]}}
        bundle=s.filing_documents(client,row,purpose='triage')
        self.assertTrue(bundle['information_statement']);self.assertTrue(bundle['complete'])
        self.assertEqual(len(bundle['inventory']),3);self.assertEqual(len(bundle['documents']),2)
        self.assertFalse(any(call.args[0].endswith('credit.htm') for call in client.text.call_args_list))

    def test_unlisted_potential_information_statement_in_folder_prevents_absence_claim(self):
        client=Mock()
        client.text.side_effect=['<table class="tableFile"><tr><td>1</td><td>Registration</td><td><a href="primary.htm">primary.htm</a></td><td>10-12B</td></tr></table>','<h1>Registration</h1>']
        client.json.return_value={'directory':{'item':[{'name':'primary.htm'},{'name':'information_statement.htm'}]}}
        bundle=s.filing_documents(client,self.row(),purpose='triage')
        self.assertFalse(bundle['complete'])

    def test_ambiguous_information_statement_stays_unresolved_without_model(self):
        row=self.row();c=s.new_candidate('101','Registrant 101')
        with patch.object(s,'filing_documents',return_value=self.bundle(row,'Information Statement\nBusiness and financial information.',True)):
            queue,issues=s.screen_registrant(Mock(),c,[row],[],s.Progress())
        self.assertFalse(queue);self.assertEqual(c['pre_screen_status'],'unresolved')
        self.assertFalse(c['facts'])

    def test_later_positive_amendment_rescues_fund_and_later_parent_filing_rescues_missing_statement(self):
        initial=self.row();later=self.row(day='2026-09-02',form='10-12B/A')
        positive='Information Statement\nSeparation\nParent 101 will distribute shares of its business to its shareholders.'
        c=s.new_candidate('101','Registrant 101')
        with patch.object(s,'filing_documents',side_effect=[self.bundle(initial,'Registration\nThe registrant is an investment fund.'),self.bundle(later,positive,True)]):
            queue,issues=s.screen_registrant(Mock(),c,[initial,later],[],s.Progress())
        self.assertTrue(c['plausible']);self.assertEqual(len(queue),2)
        parent=self.row('1101','2026-09-03','8-K')
        parent_bundle=self.bundle(parent,'Separation\nParent 101 distributed the business of Registrant 101 to its shareholders.')
        c=s.new_candidate('101','Registrant 101')
        with patch.object(s,'filing_documents',return_value=self.bundle(initial,'Registration\nShares are being registered.')):
            queue,issues=s.screen_registrant(Mock(),c,[initial],[(parent,parent_bundle)],s.Progress())
        self.assertTrue(c['plausible']);self.assertEqual(len(queue),2)

    def test_positive_fictional_candidate_passes_gate(self):
        row=self.row();c=s.new_candidate('101','Registrant 101')
        bundle=self.bundle(row,'Information Statement\nSeparation\nParent 101 distributes the business of Registrant 101 to its shareholders.',True)
        with patch.object(s,'filing_documents',return_value=bundle):queue,issues=s.screen_registrant(Mock(),c,[row],[],s.Progress())
        extractor=Mock(return_value={'facts':{},'findings':[]});extractor.extract.return_value={'facts':{},'findings':[]}
        for filing,prepared in queue:s.inspect_filing(Mock(),extractor,c,filing,issues,bundle=prepared)
        extractor.extract.assert_called_once()

    def test_distribution_to_holders_of_parent_stock_is_a_positive_routing_signal(self):
        row=self.row();c=s.new_candidate('101','Registrant 101')
        bundle=self.bundle(row,"Information Statement\nThe parent will spin-off its business by distributing shares to holders of the parent's common stock.",True)
        with patch.object(s,'filing_documents',return_value=bundle):
            queue,issues=s.screen_registrant(Mock(),c,[row],[],s.Progress())
        self.assertTrue(c['plausible']);self.assertTrue(queue)
        self.assertFalse(c['facts'])

    def test_incomplete_inventory_never_proves_missing_information_statement(self):
        row=self.row();c=s.new_candidate('101','Registrant 101')
        with patch.object(s,'filing_documents',return_value=self.bundle(row,'Registration\nDocument unavailable.',complete=False)):
            queue,issues=s.screen_registrant(Mock(),c,[row],[],s.Progress())
        self.assertEqual(c['pre_screen_status'],'unresolved');self.assertTrue(issues);self.assertFalse(queue)

    def test_full_pipeline_rejects_irrelevant_registrant_without_any_model_or_history_scan(self):
        from collections import defaultdict
        row=self.row();regs=defaultdict(list,{'101':[row]});certs=defaultdict(list)
        extractor=Mock();reader=Mock(side_effect=AssertionError('No prices for rejected registrant'))
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(regs,certs,['2026-10-09'])),\
             patch.object(s,'period_distribution_filings',return_value=[]),\
             patch.object(s,'filing_documents',return_value=self.bundle(row,'Registration\nThe registrant is registering existing shares.')),\
             patch.object(s,'submissions',side_effect=AssertionError('No historical filings before plausible')):
            s.seek(date(2025,11,10),date(1994,7,1),date(2026,10,10),tmp,Mock(),extractor,reader,run_at='2026-10-10T17:00:00-04:00')
            saved=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['101']
        extractor.extract.assert_not_called();reader.assert_not_called();self.assertTrue(saved['dropped'])

    def test_all_registrants_are_screened_before_first_extraction_or_history_expansion(self):
        from collections import defaultdict
        rows=[self.row('101'),self.row('102')];seen=[]
        def documents(client,row,purpose=None):
            seen.append(row['cik'])
            return self.bundle(row,f"Information Statement\nSeparation\nParent distributes the business of Registrant {row['cik']} to its shareholders.",True)
        def history(client,cik):
            self.assertEqual(set(seen),{'101','102'})
            return {'name':'Registrant '+cik},[]
        extractor=Mock()
        def extract(doc,target):
            self.assertEqual(set(seen),{'101','102'})
            return {'facts':{},'findings':[]}
        extractor.extract.side_effect=extract
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(defaultdict(list,{r['cik']:[r] for r in rows}),defaultdict(list),[])),\
             patch.object(s,'period_distribution_filings',return_value=[]),patch.object(s,'filing_documents',side_effect=documents),\
             patch.object(s,'submissions',side_effect=history),patch.object(s,'parent_filings',return_value=[]):
            s.seek(date(2025,11,10),date(1994,7,1),date(2026,10,10),tmp,Mock(),extractor,Mock(),run_at='2026-10-10T17:00:00-04:00')
        self.assertEqual(extractor.extract.call_count,2)

    def test_period_parent_distribution_index_rescues_preperiod_registration_without_date_guess(self):
        from collections import defaultdict
        reg=self.row('103','2001-09-01');parent=self.row('1101','2026-09-03','8-K')
        original=self.bundle(reg,'Registration\nShares are registered.')
        proof=self.bundle(parent,'Separation\nParent distributes the business of Registrant 103 to its shareholders.')
        extractor=Mock();extractor.extract.return_value={'facts':{},'findings':[]}
        def documents(client,row,purpose=None):return proof if row['cik']=='1101' else original
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(defaultdict(list,{'103':[reg]}),defaultdict(list),[])),\
             patch.object(s,'period_distribution_filings',return_value=[parent]),patch.object(s,'filing_documents',side_effect=documents),\
             patch.object(s,'submissions',return_value=({'name':'Registrant 103'},[])),patch.object(s,'parent_filings',return_value=[]):
            s.seek(date(2025,11,10),date(1994,7,1),date(2026,10,10),tmp,Mock(),extractor,Mock(),run_at='2026-10-10T17:00:00-04:00')
            saved=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['103']
        self.assertTrue(saved['plausible']);self.assertGreater(extractor.extract.call_count,0)
        self.assertNotIn('distribution_date',saved['facts'])
        self.assertIn('103',saved['routing_evidence'][0]['quote'])

    def test_period_distribution_search_paginates_without_shortening_start_or_using_model(self):
        from urllib.parse import urlparse,parse_qs
        def hit(cik,number):
            return {'_source':{'adsh':f'{int(cik):010d}-26-{number:06d}','ciks':[cik],'form':'8-K','file_date':'2026-09-03'}}
        client=Mock();client.json.side_effect=[{'hits':{'total':{'value':2},'hits':[hit('1101',1)]}},
                                             {'hits':{'total':{'value':2},'hits':[hit('1102',2)]}}]
        rows=s.period_distribution_filings(client,date(2025,11,10),date(2026,10,10),[])
        self.assertEqual(len(rows),2)
        params=[parse_qs(urlparse(call.args[0]).query) for call in client.json.call_args_list]
        self.assertEqual([p['startdt'] for p in params],[['2025-11-10'],['2025-11-10']])
        self.assertEqual(params[1]['from'],['1'])

    def test_saturated_distribution_index_splits_dates_instead_of_truncating(self):
        from urllib.parse import urlparse,parse_qs
        client=Mock();client.json.side_effect=[{'hits':{'total':{'value':10000},'hits':[]}},
            {'hits':{'total':{'value':0},'hits':[]}}, {'hits':{'total':{'value':0},'hits':[]}}]
        issues=[]
        s.period_distribution_filings(client,date(2026,10,9),date(2026,10,10),issues)
        bounds=[parse_qs(urlparse(call.args[0]).query) for call in client.json.call_args_list]
        self.assertEqual([(p['startdt'][0],p['enddt'][0]) for p in bounds],
                         [('2026-10-09','2026-10-10'),('2026-10-09','2026-10-09'),('2026-10-10','2026-10-10')])
        self.assertFalse(issues)


class DiscoveryRangeRules(unittest.TestCase):
    def setUp(self):
        blocker=patch('requests.sessions.Session.request',side_effect=AssertionError('Live request forbidden'))
        blocker.start();self.addCleanup(blocker.stop)
        blocker=patch('closes_reader.daily_closes',side_effect=AssertionError('Real export read forbidden'))
        blocker.start();self.addCleanup(blocker.stop)

    def test_default_scans_previous_quarter_through_run_quarter_only(self):
        floor=s.discovery_start(date(2025,11,10))
        self.assertEqual(floor,date(2025,7,1))
        client=Mock();client.get.return_value=Mock(content=b'Last Data Received: November 10, 2026\n',headers={})
        s.discover(client,floor,date(2026,11,10),[])
        urls=[call.args[0] for call in client.get.call_args_list]
        self.assertEqual(len(urls),6)
        self.assertIn('/2025/QTR3/',urls[0]);self.assertIn('/2026/QTR4/',urls[-1])
        self.assertEqual(s.discovery_start(date(2025,1,10)),date(2024,10,1))

    def test_cli_uses_derived_floor_and_preserves_requested_start(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'datetime',wraps=datetime) as clock,\
             patch.object(s,'dotenv_values',return_value={'SEC_USER_AGENT':'fixture','API_KEY':'fixture'}),\
             patch.object(s,'SEC'),patch.object(s,'Extractor'),patch.object(s,'seek',return_value=Path(tmp)/'report.txt') as seek:
            clock.now.return_value=datetime(2026,11,10,17,tzinfo=timezone.utc)
            s.main(['--start','2025-11-10','--output',tmp])
        self.assertEqual(seek.call_args.args[:3],(date(2025,11,10),date(2025,7,1),date(2026,11,10)))

    def test_prestart_registration_can_establish_poststart_distribution(self):
        from collections import defaultdict
        helper=RegistrantTriageRules();row=helper.row(day='2025-10-15')
        text='Information Statement\nSeparation\nParent distributed its business to shareholders on 2025-11-20.'
        bundle=helper.bundle(row,text,True)
        extractor=Mock();extractor.extract.return_value={'facts':{'distribution_date':{'value':'2025-11-20','quote':text.splitlines()[-1],'section':'Separation'}},'findings':[]}
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(defaultdict(list,{'101':[row]}),defaultdict(list),[])) as discover,\
             patch.object(s,'period_distribution_filings',return_value=[]),patch.object(s,'filing_documents',return_value=bundle),\
             patch.object(s,'submissions',return_value=({'name':'Registrant 101'},[])),patch.object(s,'parent_filings',return_value=[]):
            s.seek(date(2025,11,10),None,date(2026,11,10),tmp,Mock(),extractor,Mock(),run_at='2026-11-10T17:00:00-05:00')
            saved=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['101']
        self.assertEqual(discover.call_args.args[1],date(2025,7,1))
        self.assertEqual(s.value(saved,'distribution_date'),'2025-11-20')
        self.assertEqual(saved['facts']['distribution_date']['accession'],row['accession'])
        extractor.extract.assert_called_once()

    def test_only_bounded_carryin_registrations_are_added_as_seeds(self):
        helper=RegistrantTriageRules()
        regs={'101':[helper.row(day='2025-10-15')],'102':[helper.row('102','2001-09-01')]}
        self.assertEqual(s.period_seeds(regs,{}, {},date(2025,11,10),registration_start=date(2025,7,1)),{'101'})

    def test_period_amendment_or_cert_fetches_only_its_own_missing_registration_chain(self):
        from collections import defaultdict
        helper=RegistrantTriageRules();original=helper.row(day='2025-06-01');amendment=helper.row(day='2025-11-12',form='10-12B/A')
        unrelated=helper.row(day='2001-09-01');future=helper.row(day='2027-01-01')
        for regs,certs in [(defaultdict(list,{'101':[amendment]}),{}),
                           (defaultdict(list),{'101':[helper.row(day='2025-11-13',form='CERT')]})]:
            with self.subTest(regs=bool(regs)),patch.object(s,'submissions',return_value=({},[unrelated,original,amendment,future])) as history:
                s.complete_registration_chains(Mock(),regs,{'101'},date(2025,11,10),date(2026,11,10),[],s.Progress())
                history.assert_called_once()
                self.assertEqual(history.call_args.args[1],'101')
                self.assertEqual([r['filing_date'] for r in regs['101']],['2025-06-01','2025-11-12'])

    def test_complete_bounded_original_needs_no_history_expansion(self):
        helper=RegistrantTriageRules();regs={'101':[helper.row(day='2025-10-15')]}
        with patch.object(s,'submissions',side_effect=AssertionError('Unnecessary history scan')):
            s.complete_registration_chains(Mock(),regs,{'101'},date(2025,11,10),date(2026,11,10),[],s.Progress())

    def test_failed_older_anchor_lookup_cannot_prove_missing_information_statement(self):
        from collections import defaultdict
        helper=RegistrantTriageRules();row=helper.row(day='2025-11-12',form='10-12B/A')
        extractor=Mock()
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(defaultdict(list,{'101':[row]}),defaultdict(list),[])),\
             patch.object(s,'period_distribution_filings',return_value=[]),\
             patch.object(s,'filing_documents',return_value=helper.bundle(row,'Separation\nParent distributes its business to its shareholders.')),\
             patch.object(s,'submissions',side_effect=RuntimeError('offline fixture failure')):
            s.seek(date(2025,11,10),None,date(2026,11,10),tmp,Mock(),extractor,Mock(),run_at='2026-11-10T17:00:00-05:00')
            saved=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['101']
        self.assertFalse(saved['dropped']);self.assertFalse(saved['facts'])
        self.assertTrue(any('registration-history coverage unavailable' in issue for issue in saved['issues']))
        extractor.extract.assert_not_called()

    def test_explicit_index_override_still_works_without_changing_distribution_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'datetime',wraps=datetime) as clock,\
             patch.object(s,'dotenv_values',return_value={'SEC_USER_AGENT':'fixture','API_KEY':'fixture'}),\
             patch.object(s,'SEC'),patch.object(s,'Extractor'),patch.object(s,'seek',return_value=Path(tmp)/'report.txt') as seek:
            clock.now.return_value=datetime(2026,11,10,17,tzinfo=timezone.utc)
            s.main(['--start','2025-11-10','--index-start','2025-04-01','--output',tmp])
        self.assertEqual(seek.call_args.args[:3],(date(2025,11,10),date(2025,4,1),date(2026,11,10)))

    def test_period_parent_archive_link_can_route_older_child_without_global_catalog(self):
        helper=RegistrantTriageRules();row=helper.row('1101','2026-09-03','8-K')
        bundle=helper.bundle(row,'Separation\nParent distributes its business to shareholders.\nhttps://www.sec.gov/Archives/edgar/data/103/old-registration.htm')
        with patch.object(s,'period_distribution_filings',return_value=[row]),patch.object(s,'filing_documents',return_value=bundle):
            linked=s.carry_in_bundles(Mock(),{},date(2025,11,10),date(2026,11,10),[],s.Progress())
        self.assertIn('103',linked)
        self.assertEqual(linked['103'][0][0]['accession'],row['accession'])


class AnchorRoutingRules(unittest.TestCase):
    def setUp(self):
        self.helper=RegistrantTriageRules()
        blocker=patch('requests.sessions.Session.request',side_effect=AssertionError('Live request forbidden'))
        blocker.start();self.addCleanup(blocker.stop)
        blocker=patch('closes_reader.daily_closes',side_effect=AssertionError('Real export read forbidden'))
        blocker.start();self.addCleanup(blocker.stop)

    def test_mixed_seeds_are_all_screened_before_only_positive_chain_recovers_anchor(self):
        from collections import defaultdict
        rows={cik:self.helper.row(cik,'2026-09-01',form) for cik,form in
              [('101','10-12B/A'),('102','CERT'),('103','10-12B/A'),('104','8-K'),('105','10-12B')]}
        original=self.helper.row('103','2025-06-02')
        texts={'101':'Registration\nClass of shares being registered.',
               '102':'Certification\nExchange certifies approval for listing.',
               '103':'Separation\nParent distributes the business of Registrant 103 to its shareholders.',
               '104':'Results\nRegistrant 104 reports quarterly earnings.',
               '105':'Registration\nThe registrant is registering existing shares.'}
        regs=defaultdict(list,{cik:[rows[cik]] for cik in ('101','103','105')})
        certs=defaultdict(list,{'102':[rows['102']]});seen=set();events=[]
        def documents(client,row,purpose=None):
            seen.add(row['cik'])
            if row['filing_date']=='2025-06-02':
                return self.helper.bundle(row,'Information Statement\nParent distributes the business of Registrant 103 to its shareholders.',True)
            return self.helper.bundle(row,texts[row['cik']])
        def history(client,cik):
            self.assertTrue({'101','102','103','105'}.issubset(seen))
            self.assertEqual(cik,'103')
            events.append(cik)
            return {'name':'Registrant 103'},[original,rows['103']]
        extractor=Mock();extractor.extract.return_value={'facts':{},'findings':[]}
        progress=s.Progress()
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(regs,certs,[])),\
             patch.object(s,'carry_in_bundles',return_value={'104':[(rows['104'],self.helper.bundle(rows['104'],texts['104']))]}),\
             patch.object(s,'filing_documents',side_effect=documents),patch.object(s,'submissions',side_effect=history),\
             patch.object(s,'parent_filings',return_value=[]):
            (Path(tmp)/'registry.json').write_text(json.dumps({'candidates':{'106':s.new_candidate('106','Unrelated identity')}}))
            s.seek(date(2025,11,10),None,date(2026,11,10),tmp,Mock(),extractor,Mock(),run_at='2026-11-10T17:00:00-05:00',progress=progress)
            saved=json.loads((Path(tmp)/'registry.json').read_text())['candidates']
        self.assertEqual(progress.counts['initial_seeds'],6)
        self.assertEqual(progress.counts['deterministic_rejected'],1)
        self.assertEqual(progress.counts['older_anchor_candidates'],1)
        self.assertEqual(progress.counts['candidates_total'],1)
        self.assertEqual(set(events),{'103'})
        self.assertTrue(saved['103']['plausible']);self.assertFalse(saved['101']['dropped'])
        self.assertIn(original['accession'],[r['accession'] for r in saved['103']['filings']])
        self.assertGreater(extractor.extract.call_count,0)

    def test_unrelated_8k_archive_link_does_not_route_history(self):
        row=self.helper.row('1101','2026-09-03','8-K')
        bundle=self.helper.bundle(row,'Results\nRegistrant 103 reports quarterly earnings.\nhttps://www.sec.gov/Archives/edgar/data/103/earnings.htm')
        with patch.object(s,'period_distribution_filings',return_value=[row]),patch.object(s,'filing_documents',return_value=bundle):
            self.assertFalse(s.carry_in_bundles(Mock(),{},date(2025,11,10),date(2026,11,10),[],s.Progress()))

    def test_cert_with_positive_distribution_evidence_can_recover_prestart_registration(self):
        from collections import defaultdict
        cert=self.helper.row('103','2026-09-03','CERT');original=self.helper.row('103','2025-06-02')
        cert_bundle=self.helper.bundle(cert,'Certification\nExchange approves listing following the parent distributing the business of Registrant 103 to its shareholders.')
        extractor=Mock();extractor.extract.return_value={'facts':{},'findings':[]}
        progress=s.Progress()
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(defaultdict(list),defaultdict(list,{'103':[cert]}),[])),\
             patch.object(s,'period_distribution_filings',return_value=[]),\
             patch.object(s,'filing_documents',side_effect=lambda client,row,purpose=None:cert_bundle if row['form']=='CERT' else self.helper.bundle(row,'Information Statement\nParent distributes its business to shareholders.',True)),\
             patch.object(s,'submissions',return_value=({'name':'Registrant 103'},[original])),patch.object(s,'parent_filings',return_value=[]):
            s.seek(date(2025,11,10),None,date(2026,11,10),tmp,Mock(),extractor,Mock(),run_at='2026-11-10T17:00:00-05:00',progress=progress)
            saved=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['103']
        self.assertEqual(progress.counts['older_anchor_candidates'],1)
        self.assertTrue(saved['plausible']);self.assertTrue(saved['information_statement'])
        self.assertIn(original['accession'],[r['accession'] for r in saved['filings']])

    def test_incidental_cik_outside_distribution_passage_is_not_a_spinoff_chain(self):
        row=self.helper.row('1101','2026-09-03','8-K')
        text=('Separation\nParent distributes its business to shareholders. '+
              'https://www.sec.gov/Archives/edgar/data/103/registration.htm\n'+('Other information. '*100)+
              'https://www.sec.gov/Archives/edgar/data/104/earnings.htm')
        with patch.object(s,'period_distribution_filings',return_value=[row]),\
             patch.object(s,'filing_documents',return_value=self.helper.bundle(row,text)):
            linked=s.carry_in_bundles(Mock(),{},date(2025,11,10),date(2026,11,10),[],s.Progress())
        self.assertIn('103',linked);self.assertNotIn('104',linked)

    def test_parent_distribution_passage_can_rescue_unknown_child_and_recover_prestart_form10(self):
        from collections import defaultdict
        parent=self.helper.row('1101','2026-09-03','8-K');original=self.helper.row('103','2025-06-01')
        passage='Parent distributed the business of Registrant 103 to its shareholders on 2026-09-02: https://www.sec.gov/Archives/edgar/data/103/registration.htm'
        proof=self.helper.bundle(parent,'Separation\n'+passage)
        extractor=Mock();extractor.extract.side_effect=lambda doc,target:{'facts':{},'findings':[{'kind':'spinoff','quote':passage,'section':'Separation'}] if doc['form']=='8-K' else []}
        progress=s.Progress()
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=(defaultdict(list),defaultdict(list),[])),\
             patch.object(s,'period_distribution_filings',return_value=[parent]),\
             patch.object(s,'filing_documents',side_effect=lambda client,row,purpose=None:proof if row['cik']=='1101' else self.helper.bundle(row,'Registration\nShares registered.')),\
             patch.object(s,'submissions',return_value=({'name':'Registrant 103'},[original])) as history,\
             patch.object(s,'parent_filings',return_value=[]):
            s.seek(date(2025,11,10),None,date(2026,11,10),tmp,Mock(),extractor,Mock(),run_at='2026-11-10T17:00:00-05:00',progress=progress)
            saved=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['103']
        self.assertEqual(progress.counts['older_anchor_candidates'],1)
        self.assertTrue(saved['plausible']);self.assertFalse(saved['dropped'])
        self.assertEqual(saved['filings'][0]['filing_date'],'2025-06-01')
        self.assertEqual(saved['routing_evidence'][0]['accession'],parent['accession'])
        self.assertFalse(saved['facts'])
        self.assertTrue(all(call.args[1]=='103' for call in history.call_args_list))


class PassageExtractionRules(unittest.TestCase):
    def setUp(self):
        self.helper=RegistrantTriageRules()
        blocker=patch('requests.sessions.Session.request',side_effect=AssertionError('Live request forbidden'))
        blocker.start();self.addCleanup(blocker.stop)
        blocker=patch('closes_reader.daily_closes',side_effect=AssertionError('Real export read forbidden'))
        blocker.start();self.addCleanup(blocker.stop)

    def prepared(self,day,text,form='10-12G/A'):
        row=self.helper.row(day=day,form=form)
        return row,self.helper.bundle(row,text,True)

    def test_latest_amendment_first_and_unchanged_fact_is_not_reextracted(self):
        c=s.new_candidate('101','Registrant 101');seen=[]
        prepared=[self.prepared(day,'Information Statement\nSeparation\nOur parent is Parent 101.') for day in ('2026-09-01','2026-09-02','2026-09-03')]
        extractor=Mock()
        def extract(doc,target):
            seen.append(doc['filing_date'])
            return {'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Separation'}},'findings':[]}
        extractor.extract.side_effect=extract
        with patch.object(s,'inspect_filing',wraps=s.inspect_filing) as inspect:
            s.extract_registration_chain(Mock(),extractor,c,prepared,[])
        self.assertEqual(inspect.call_args_list[0].args[3]['filing_date'],'2026-09-03')
        self.assertEqual(seen,[])
        self.assertEqual(c['facts']['parent']['accession'],prepared[-1][0]['accession'])

    def test_older_filing_supplies_missing_fact_with_its_actual_evidence(self):
        c=s.new_candidate('101','Registrant 101')
        older=self.prepared('2026-09-01','Information Statement\nDistribution Terms\nRecord date: 2026-08-20.','10-12G')
        latest=self.prepared('2026-09-03','Information Statement\nSeparation\nOur parent is Parent 101.')
        extractor=Mock();extractor.extract.return_value={'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Separation'}},'findings':[]}
        s.extract_registration_chain(Mock(),extractor,c,[older,latest],[])
        fact=c['facts']['record_date']
        self.assertEqual(fact['value'],'2026-08-20');self.assertEqual(fact['section'],'Distribution Terms')
        self.assertEqual(fact['accession'],older[0]['accession']);self.assertTrue(fact['document_url']);self.assertTrue(fact['index_url'])

    def test_later_supported_amendment_supersedes_earlier_fact_but_keeps_observations(self):
        c=s.new_candidate('101','Registrant 101')
        for day,literal,form in [('2026-09-01','2026-08-20','10-12G'),('2026-09-03','2026-08-21','10-12G/A')]:
            row,bundle=self.prepared(day,'Distribution Terms\nRecord date: '+literal+'.',form)
            s.ingest(c,bundle['documents'][0],{'facts':{'record_date':{'value':literal,'quote':'Record date: '+literal+'.','section':'Distribution Terms'}},'findings':[]})
        self.assertEqual(s.value(c,'record_date'),'2026-08-21')
        self.assertEqual(c['facts']['record_date']['filing_date'],'2026-09-03')
        self.assertEqual(len(c['observations']['record_date']),2)

    def test_long_information_statement_sends_only_relevant_passage(self):
        text='Information Statement\nSeparation\nOur parent is Parent 101.\nRisk Factors\n'+('IRRELEVANT_SENTINEL competition and weather exposure. '*5000)
        row,bundle=self.prepared('2026-09-03',text)
        c=s.new_candidate('101','Registrant 101');extractor=Mock()
        extractor.extract.return_value={'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Separation'}},'findings':[]}
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_not_called()
        self.assertEqual(c['facts']['parent']['section'],'Separation')

    def test_explicit_registry_labels_are_read_without_a_model(self):
        text='Distribution Terms\nParent: Parent 101\nParent CIK: 1101\nDistribution ratio: one share for every ten shares\nRecord date: 2026-08-20\nDistribution date: 2026-09-02'
        row,bundle=self.prepared('2026-09-03',text)
        c=s.new_candidate('101','Registrant 101');extractor=Mock()
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_not_called()
        for field in s.REGISTRY_FIELDS:
            fact=c['facts'][field]
            self.assertEqual(fact['section'],'Distribution Terms');self.assertEqual(fact['accession'],row['accession'])
            self.assertIn(s.compact(fact['quote']),s.compact(text));self.assertTrue(fact['document_url']);self.assertTrue(fact['index_url'])

    def test_conditional_dates_and_unknown_labels_stay_missing(self):
        row,bundle=self.prepared('2026-09-03','Distribution Terms\nDistribution date: expected 2026-09-02\nRecord date: not determined')
        c=s.new_candidate('101','Registrant 101');extractor=Mock();extractor.extract.return_value={'facts':{},'findings':[]}
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        self.assertFalse(c['facts'])

    def test_conditional_context_above_date_label_is_not_parsed_as_a_final_date(self):
        row,bundle=self.prepared('2026-09-03','Distribution Terms\nThese dates are expected and subject to approval.\nDistribution date: 2026-09-02')
        c=s.new_candidate('101','Registrant 101');extractor=Mock();extractor.extract.return_value={'facts':{},'findings':[]}
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        self.assertNotIn('distribution_date',c['facts'])

    def test_same_date_contradictions_still_stay_blank(self):
        c=s.new_candidate('101','Registrant 101')
        for literal in ('2026-08-20','2026-08-21'):
            row,bundle=self.prepared('2026-09-03','Distribution Terms\nRecord date: '+literal)
            s.ingest(c,bundle['documents'][0],{'facts':{'record_date':{'value':literal,'quote':'Record date: '+literal,'section':'Distribution Terms'}},'findings':[]})
        self.assertNotIn('record_date',c['facts'])

    def test_distant_relevant_passages_are_not_lost_when_field_patterns_overlap(self):
        row,bundle=self.prepared('2026-09-03','Distribution Terms\nRecord date: 2026-08-20\n'+('Unrelated prose. '*800)+'\nOur parent is Parent 101.')
        doc=bundle['documents'][0]
        request,_=s.filing_fallback(s.new_candidate('101','Registrant 101'),[doc],row)
        self.assertIn('Record date: 2026-08-20',request['text'])
        self.assertIn('Our parent is Parent 101.',request['text'])
        self.assertLess(len(request['text']),6000)

    def test_unchanged_distribution_history_does_not_call_model_for_every_version(self):
        c=s.new_candidate('101','Registrant 101')
        text='Information Statement\nSeparation\nOur parent is Parent 101. Parent 101 distributes the business to its shareholders.'
        prepared=[self.prepared(day,text) for day in ('2026-09-01','2026-09-02','2026-09-03')]
        extractor=Mock();extractor.extract.return_value={'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Separation'}},
            'findings':[{'kind':'spinoff','quote':'Parent 101 distributes the business to its shareholders.','section':'Separation'}]}
        s.extract_registration_chain(Mock(),extractor,c,prepared,[])
        extractor.extract.assert_not_called()
        self.assertEqual(c['facts']['parent']['filing_date'],'2026-09-03')
        self.assertEqual({finding['filing_date'] for finding in c['findings']},{'2026-09-03'})

    def test_same_filing_conflicting_labels_are_not_first_match_wins(self):
        row,bundle=self.prepared('2026-09-03','Distribution Terms\nRecord date: 2026-08-20\nRecord date: 2026-08-21')
        c=s.new_candidate('101','Registrant 101');extractor=Mock();extractor.extract.return_value={'facts':{},'findings':[]}
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        self.assertNotIn('record_date',c['facts']);self.assertEqual(len(c['observations']['record_date']),2)

    def test_later_own_amendment_can_supersede_older_parent_filing_fact(self):
        c=s.new_candidate('101','Registrant 101')
        quote='Registrant 101 record date: 2026-08-20.'
        doc={**evidence('unused'),'filer_cik':'1101','form':'8-K','filing_date':'2026-09-01','text':'Distribution Terms\n'+quote}
        s.ingest(c,doc,{'facts':{'record_date':{'value':'2026-08-20','quote':quote,'section':'Distribution Terms'}},'findings':[]})
        row,bundle=self.prepared('2026-09-03','Distribution Terms\nRecord date: 2026-08-21')
        s.ingest(c,bundle['documents'][0],{'facts':{'record_date':{'value':'2026-08-21','quote':'Record date: 2026-08-21','section':'Distribution Terms'}},'findings':[]})
        self.assertEqual(s.value(c,'record_date'),'2026-08-21')
        self.assertEqual(len(c['observations']['record_date']),2)

    def test_headings_and_terms_split_over_html_lines_still_route_a_passage(self):
        row,bundle=self.prepared('2026-09-03','Record Date\n2026-08-20\nFinancial Statements\nTotal\nassets were 10 million USD.')
        doc=bundle['documents'][0]
        request,_=s.filing_fallback(s.new_candidate('101','Registrant 101'),[doc],row)
        self.assertIn('record_date',request['requested_fields'])
        self.assertIn('child_assets',request['requested_fields'])

    def test_incidental_risk_references_do_not_reextract_a_supported_summary_fact(self):
        text='Separation\nOur parent is Parent 101.\nRisk Factors\n'+('Our parent faces competition and weather exposure. '*1000)
        row,bundle=self.prepared('2026-09-03',text)
        c=s.new_candidate('101','Registrant 101');extractor=Mock()
        extractor.extract.return_value={'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Separation'}},'findings':[]}
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_not_called()

    def test_lower_priority_passage_is_still_used_when_summary_cannot_establish_fact(self):
        text='Summary\nOur parent relationship is described below.\nRisk Factors\nOur parent is Parent 101.'
        row,bundle=self.prepared('2026-09-03',text)
        c=s.new_candidate('101','Registrant 101');extractor=Mock()
        extractor.extract.side_effect=lambda doc,target:{'facts':{'parent':{'value':'Parent 101','quote':'Our parent is Parent 101.','section':'Risk Factors'}} if doc['text'].startswith('Risk Factors') else {},'findings':[]}
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_not_called();self.assertEqual(s.value(c,'parent'),'Parent 101')


class SimpleFilingExtractionRules(unittest.TestCase):
    def setUp(self):
        self.helper=RegistrantTriageRules()
        for target in ('requests.sessions.Session.request','closes_reader.daily_closes'):
            guard=patch(target,side_effect=AssertionError('Live access forbidden'))
            guard.start();self.addCleanup(guard.stop)

    def prepared(self,day,text,form='10-12G/A'):
        row=self.helper.row(day=day,form=form)
        return row,self.helper.bundle(row,text,True)

    def complete_text(self):
        lines=['Information Statement','Separation']
        for field,claim in candidate()['facts'].items():
            if field=='jurisdiction':lines.append('The company is incorporated in United States.')
            elif field=='ticker':lines.append('Regular-way ticker: X101')
            elif field=='regular_trading_date':lines.append('Regular-way trading date: 2026-10-02')
            else:lines.append(field.replace('_',' ').capitalize()+': '+claim['value'])
        lines.extend(['Our parent Parent 101 distributed the business of Registrant 101 to its shareholders.',
                      'The distribution was completed on 2026-10-01.'])
        return '\n'.join(lines)

    def test_explicit_information_statement_requires_zero_model_calls(self):
        row,bundle=self.prepared('2026-10-03',self.complete_text())
        c=s.new_candidate('101','Registrant 101');extractor=Mock()
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_not_called()
        self.assertEqual(set(c['facts']),s.FACT_FIELDS)
        self.assertTrue(any(f['kind']=='spinoff' for f in c['findings']))
        for field in s.FACT_FIELDS:
            claim=c['facts'][field]
            self.assertEqual(claim['accession'],row['accession']);self.assertEqual(claim['section'],'Separation')
            self.assertTrue(claim['document_url']);self.assertTrue(claim['index_url'])

    def test_two_unresolved_facts_in_different_documents_share_one_filing_request(self):
        row,bundle=self.prepared('2026-10-03','Information Statement\nDistribution Terms\nThe distribution ratio is described as two Class A shares for every three parent interests.')
        extra={**bundle['documents'][0],'document_url':'https://www.sec.gov/Archives/edgar/data/101/extra.htm',
               'text':'Information Statement\nRecord Date\nHolders of record at the stated cutoff on October 1, 2026 participate.'}
        bundle['documents'].append(extra)
        c=s.new_candidate('101','Registrant 101');extractor=Mock();extractor.extract.return_value={'facts':{},'findings':[]}
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_called_once()
        request=extractor.extract.call_args.args[0]
        self.assertIn('distribution_ratio',request['requested_fields']);self.assertIn('record_date',request['requested_fields'])
        self.assertIn('two Class A shares',request['text']);self.assertIn('October 1, 2026',request['text'])

    def test_latest_complete_amendment_prevents_earlier_processing(self):
        latest=self.prepared('2026-10-03',self.complete_text())
        older=self.prepared('2026-09-01','Information Statement\nSeparation\nOur parent is Older Parent.','10-12G')
        c=s.new_candidate('101','Registrant 101');extractor=Mock();progress=s.Progress()
        with patch.object(s,'inspect_filing',wraps=s.inspect_filing) as inspect:
            s.extract_registration_chain(Mock(),extractor,c,[older,latest],[],progress)
        self.assertEqual(inspect.call_count,1);self.assertEqual(inspect.call_args.args[3]['filing_date'],'2026-10-03')
        self.assertEqual(progress.counts['documents_seen'],1);extractor.extract.assert_not_called()

    def test_failed_combined_fallback_leaves_missing_facts_blank_and_finishes(self):
        row,bundle=self.prepared('2026-10-03','Information Statement\nDistribution Terms\nThe distribution ratio involves two classes of stock and allocation adjustments.\nRecord Date\nThe record date depends on the cutoff specified in an unavailable attachment.')
        c=s.new_candidate('101','Registrant 101');extractor=Mock();extractor.extract.side_effect=request_timeout()
        issues=[]
        s.inspect_filing(Mock(),extractor,c,row,issues,bundle=bundle)
        extractor.extract.assert_called_once();self.assertNotIn('distribution_ratio',c['facts']);self.assertNotIn('record_date',c['facts'])
        self.assertTrue(issues)

    def malformed_response(self, bad, mixed=False):
        row,bundle=self.prepared('2026-10-03','Information Statement\nDistribution Terms\nThe distribution ratio is two Class A shares for every three parent interests.\nRecord Date\nThe cutoff is October 1, 2026.')
        good={'value':'two Class A shares for every three parent interests',
              'quote':'The distribution ratio is two Class A shares for every three parent interests.',
              'section':'Distribution Terms','source_id':'0'}
        facts={'record_date':bad}
        if mixed:facts['distribution_ratio']=good
        extractor=Mock();extractor.extract.return_value={'facts':facts,'findings':[bad]}
        c=s.new_candidate('101','Registrant 101');issues=[]
        s.inspect_filing(Mock(),extractor,c,row,issues,bundle=bundle)
        self.assertFalse(s.value(c,'record_date'))
        self.assertFalse(c['findings'])
        self.assertIn('malformed',' '.join(c['issues']+issues).lower())
        if mixed:self.assertEqual(s.value(c,'distribution_ratio'),good['value'])
        return c

    def test_null_extraction_claim_is_rejected_without_crashing(self):
        self.malformed_response(None)

    def test_wrong_type_or_malformed_object_claim_is_rejected_without_crashing(self):
        for bad in ('unexpected',17,True,[],{}, {'kind':[],'quote':'text','section':'Record Date'},
                    {'value':'October 1, 2026','quote':None,'section':[]},
                    {'value':'October 1, 2026','quote':'text','section':'Record Date','source_id':{}}):
            with self.subTest(bad=bad):self.malformed_response(bad,mixed=True)

    def test_valid_claim_survives_null_claim_in_same_extraction_response(self):
        c=self.malformed_response(None,mixed=True)
        self.assertTrue(c['facts']['distribution_ratio']['accession'])

    def test_unrelated_exhibits_and_earnings_do_not_request_semantic_analysis(self):
        row,bundle=self.prepared('2026-10-03',self.complete_text())
        bundle['documents'].append({**bundle['documents'][0],'document_type':'EX-10.1','description':'Credit agreement',
                                    'text':'Credit Agreement\nParent obligations and distribution restrictions.\n'+('Debt terms. '*1000)})
        extractor=Mock();c=s.new_candidate('101','Registrant 101')
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_not_called()

    def test_latest_amendment_is_still_read_when_prior_registry_is_complete(self):
        row,bundle=self.prepared('2026-10-03','Information Statement\nDistribution Terms\nRecord date: 2026-09-21')
        c=candidate();c['observations']={field:[copy.deepcopy(claim)] for field,claim in c['facts'].items()}
        with patch.object(s,'inspect_filing',wraps=s.inspect_filing) as inspect:
            s.extract_registration_chain(Mock(),Mock(),c,[(row,bundle)],[])
        inspect.assert_called_once();self.assertEqual(s.value(c,'record_date'),'2026-09-21')

    def test_identical_failed_fallback_is_not_repeated_across_earlier_amendments(self):
        text='Information Statement\nDistribution Terms\nThe distribution ratio involves two classes of shares and allocation adjustments.'
        prepared=[self.prepared(day,text) for day in ('2026-10-01','2026-10-02','2026-10-03')]
        extractor=Mock();extractor.extract.side_effect=request_timeout();c=s.new_candidate('101','Registrant 101');issues=[]
        s.extract_registration_chain(Mock(),extractor,c,prepared,issues)
        extractor.extract.assert_called_once();self.assertNotIn('distribution_ratio',c['facts']);self.assertTrue(issues)

    def test_combined_fallback_keeps_each_documents_actual_citation(self):
        row,bundle=self.prepared('2026-10-03','Information Statement\nDistribution Terms\nThe distribution ratio is two Class A shares for every three parent interests.')
        extra={**bundle['documents'][0],'document_url':'https://www.sec.gov/Archives/edgar/data/101/extra.htm',
               'text':'Information Statement\nRecord Date\nThe cutoff is October 1, 2026.'}
        bundle['documents'].append(extra)
        c=s.new_candidate('101','Registrant 101');extractor=Mock()
        extractor.extract.return_value={'facts':{
            'distribution_ratio':{'value':'two Class A shares for every three parent interests','quote':'The distribution ratio is two Class A shares for every three parent interests.','section':'Distribution Terms','source_id':'0'},
            'record_date':{'value':'October 1, 2026','quote':'The cutoff is October 1, 2026.','section':'Record Date','source_id':'1'}},'findings':[]}
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_called_once()
        self.assertEqual(c['facts']['record_date']['document_url'],extra['document_url'])
        self.assertEqual(c['facts']['distribution_ratio']['document_url'],bundle['documents'][0]['document_url'])
        self.assertEqual(c['facts']['record_date']['accession'],row['accession'])

    def test_unrelated_parent_8k_is_not_parsed_or_sent_to_model(self):
        row=self.helper.row('1101','2026-10-03','8-K')
        bundle=self.helper.bundle(row,'Distribution Terms\nParent distributed the business of Registrant 102 to its shareholders.\nDistribution date: 2026-10-01')
        c=s.new_candidate('101','Registrant 101');extractor=Mock()
        s.inspect_filing(Mock(),extractor,c,row,[],bundle=bundle)
        extractor.extract.assert_not_called();self.assertFalse(c['facts']);self.assertFalse(c['findings'])


class ExistingInputGapRules(unittest.TestCase):
    def setUp(self):
        self.factors=s.load_factors(Path(__file__).parent/'spinoff_factors.txt')
        for target in ('requests.sessions.Session.request','closes_reader.daily_closes'):
            guard=patch(target,side_effect=AssertionError('Live access forbidden'))
            guard.start();self.addCleanup(guard.stop)

    def test_jurisdiction_stops_before_incorporation_date_and_repairs_bad_prior_observation(self):
        c=s.new_candidate('101','Registrant 101')
        quote='The Company was incorporated in the state of Nevada on April 28, 2026.'
        doc={**evidence('unused'),'form':'10-12B/A','text':'Corporate Information\n'+quote}
        bad={**evidence('Nevada on',quote=quote),'section':'Corporate Information'}
        c['observations']['jurisdiction']=[bad]
        s.deterministic_document(c,doc)
        self.assertEqual(s.value(c,'jurisdiction'),'Nevada')
        self.assertTrue(s.us_jurisdiction(s.value(c,'jurisdiction')))
        self.assertTrue(any(row['value']=='Nevada on' for row in c['observations']['jurisdiction']))

    def test_foreign_incorporation_is_not_replaced_by_parents_us_incorporation(self):
        c=s.new_candidate('101','Registrant 101')
        text='Corporate Information\nOur Parent was incorporated in Nevada on April 28, 2026.\nThe Company was incorporated under the laws of the Cayman Islands on June 14, 2023.'
        s.deterministic_document(c,{**evidence('unused'),'text':text})
        self.assertEqual(s.value(c,'jurisdiction'),'the Cayman Islands')
        self.assertFalse(s.us_jurisdiction(s.value(c,'jurisdiction')))

    def test_registered_exchange_cover_ticker_is_usable_without_trading_start_date(self):
        c=candidate();c['facts'].pop('ticker');c['facts'].pop('regular_trading_date')
        c['findings']=[f for f in c['findings'] if f['kind']!='regular_way']
        text='Securities registered pursuant to Section 12(b) of the Act:\nTitle of each class\nTrading\nSymbol(s)\nName of Each Exchange\non Which Registered\nClass A Common Stock\nX101\nNew York Stock Exchange'
        s.deterministic_document(c,{**evidence('unused'),'text':text,'form':'8-K'})
        self.assertEqual(s.value(c,'ticker'),'X101')
        reader=Mock(return_value=prices())
        s.evaluate(c,date(2026,10,10),self.factors,reader)
        reader.assert_called_once_with('X101','2026-10-02','2026-10-10')
        self.assertEqual(c['prices']['first']['date'],'2026-10-02')
        self.assertNotIn('missing regular_trading_date',c['unranked_reasons'])

    def test_unavailable_export_leaves_price_factor_missing_without_substitution(self):
        c=candidate();reader=Mock(return_value='not available')
        s.evaluate(c,date(2026,10,10),self.factors,reader)
        self.assertFalse(c['prices']);self.assertNotIn('weak first-week price performance',c['measures'])
        self.assertEqual(s.rank([c],self.factors),[])

    def test_regular_trading_on_distribution_day_does_not_block_first_later_close(self):
        c=candidate();c['facts']['regular_trading_date']=evidence('2026-10-01')
        s.evaluate(c,date(2026,10,10),self.factors,Mock(return_value=prices()))
        self.assertEqual(c['prices']['first']['date'],'2026-10-02')

    def test_export_is_not_trimmed_to_a_later_filed_commencement_date(self):
        c=candidate();c['facts']['regular_trading_date']=evidence('2026-10-05')
        s.evaluate(c,date(2026,10,10),self.factors,Mock(return_value=prices()))
        self.assertNotIn('first',c['prices'])

    def test_rendered_form4_does_not_invalidate_available_raw_xml(self):
        row={'form':'4','filing_date':'2026-10-05','cik':'101','accession':'0000000101-26-000005'}
        raw='<ownershipDocument><issuer><issuerCik>101</issuerCik></issuer><nonDerivativeTable></nonDerivativeTable></ownershipDocument>'
        base={**evidence('unused'),'form':'4','document_type':'4'}
        bundle={'complete':True,'documents':[{**base,'document_url':'https://www.sec.gov/view.xml','text':'<html><body>Rendered Form 4</body></html>'},
                                           {**base,'document_url':'https://www.sec.gov/raw.xml','text':raw}]}
        c=candidate();issues=[]
        with patch.object(s,'filing_documents',return_value=bundle):s.collect_insiders(Mock(),c,[row],date(2026,10,10),issues)
        self.assertTrue(c['insider_complete']);self.assertEqual(c['insider_buying'],0)
        self.assertFalse(issues);self.assertEqual(len(c['insider_evidence']),1)

    def test_rendered_only_form4_is_still_missing_not_zero(self):
        row={'form':'4','filing_date':'2026-10-05','cik':'101','accession':'0000000101-26-000005'}
        doc={**evidence('unused'),'form':'4','document_type':'4','text':'<html><body>Rendered Form 4</body></html>'}
        c=candidate();issues=[]
        with patch.object(s,'filing_documents',return_value={'complete':True,'documents':[doc]}):s.collect_insiders(Mock(),c,[row],date(2026,10,10),issues)
        self.assertFalse(c['insider_complete']);self.assertIsNone(c['insider_buying']);self.assertTrue(issues)

    def test_equivalent_share_class_ratio_wording_keeps_latest_source_without_false_conflict(self):
        c=s.new_candidate('101','Registrant 101')
        values=['one share of our Class A Common Stock for every two shares',
                'one share of the Company Class A common stock for every two shares of Parent Class A Common Stock']
        for i,literal in enumerate(values):
            doc={**evidence('unused'),'form':'8-K' if i else '10-12B/A','filing_date':f'2026-10-0{i+1}',
                 'text':'Distribution Ratio\n'+literal}
            s.ingest(c,doc,{'facts':{'distribution_ratio':{'value':literal,'quote':literal,'section':'Distribution Ratio'}},'findings':[]})
        self.assertEqual(s.value(c,'distribution_ratio'),values[-1]);self.assertEqual(len(c['observations']['distribution_ratio']),2)
        self.assertEqual(c['facts']['distribution_ratio']['form'],'8-K')

    def test_different_distribution_ratios_still_remain_conflicting(self):
        c=s.new_candidate('101','Registrant 101')
        for literal in ('one share for every two shares','one share for every ten shares'):
            doc={**evidence('unused'),'text':'Distribution Ratio\n'+literal}
            s.ingest(c,doc,{'facts':{'distribution_ratio':{'value':literal,'quote':literal,'section':'Distribution Ratio'}},'findings':[]})
        self.assertNotIn('distribution_ratio',c['facts'])

    def test_outstanding_share_and_numeric_wording_do_not_create_ratio_conflicts(self):
        self.assertEqual(s.ratio_key('one share of Spinco for every two outstanding shares'),s.ratio_key('1 share for every 2 shares'))

    def test_report_counts_describe_screening_records_not_confirmed_spinoffs(self):
        rows=[{'dropped':True},{'unranked_reasons':['missing distribution_date']},{'rank':1}]
        text=s.record_counts(rows)
        self.assertIn('3 screening records',text);self.assertIn('1 dropped',text)
        self.assertIn('1 unresolved/unranked',text);self.assertIn('1 ranked',text)
        self.assertNotIn('in requested distribution range',text)


class ParentFinancialLookup(unittest.TestCase):
    # Inherit the offline transport guards, not a live EDGAR client.
    setUp=SeekerRules.setUp
    def financial_html(self, cik='1101', day='2026-09-30', currency='USD', segmented=False):
        return f'''<html><body><xbrli:context id="now"><xbrli:entity>
        <xbrli:identifier>{cik}</xbrli:identifier>{'<xbrli:segment>division</xbrli:segment>' if segmented else ''}
        </xbrli:entity><xbrli:period><xbrli:instant>{day}</xbrli:instant></xbrli:period></xbrli:context>
        <xbrli:unit id="money"><xbrli:measure>iso4217:{currency}</xbrli:measure></xbrli:unit>
        <h2>FICTIONAL CONSOLIDATED BALANCE SHEETS</h2><p>(in thousands)</p>
        <table><tr><th>Total assets</th><td><ix:nonFraction name="us-gaap:Assets"
        contextRef="now" unitRef="money" scale="3">240,500</ix:nonFraction></td></tr></table>
        </body></html>'''

    def client(self, html=None):
        info={'name':'Fictional Parent','filings':{'recent':{
            'form':['8-K','10-Q','10-K'], 'reportDate':['2026-10-01','2026-09-30','2026-06-30'],
            'filingDate':['2026-10-02','2026-11-05','2026-08-20'],
            'accessionNumber':['0000001101-26-000003','0000001101-26-000002','0000001101-26-000001'],
            'primaryDocument':['distribution.htm','quarter.htm','annual.htm']},'files':[]}}
        client=Mock();client.json.return_value=info;client.text.return_value=html or self.financial_html()
        return client

    def incomplete(self):
        c=candidate()
        for field in ('parent_assets','parent_assets_date','parent_assets_currency'):c['facts'].pop(field)
        c['facts']['child_assets_date']=evidence('2026-09-30')
        return c

    def test_parent_periodic_lookup_uses_evidenced_cik_and_only_matching_primary(self):
        c=self.incomplete();client=self.client();issues=[]
        s.collect_parent_assets(client,c,self.factors,date(2026,11,11),issues)
        client.json.assert_called_once_with('https://data.sec.gov/submissions/CIK0000001101.json')
        client.text.assert_called_once_with('https://www.sec.gov/Archives/edgar/data/1101/000000110126000002/quarter.htm')
        self.assertEqual(s.value(c,'parent_assets'),'240500000')
        self.assertEqual(s.value(c,'parent_assets_date'),'2026-09-30')
        self.assertEqual(s.value(c,'parent_assets_currency'),'USD')
        claim=c['facts']['parent_assets']
        self.assertEqual(claim['accession'],'0000001101-26-000002')
        self.assertEqual(claim['section'],'FICTIONAL CONSOLIDATED BALANCE SHEETS')
        self.assertEqual(claim['units'],'thousands')
        self.assertIn('240,500',claim['quote'])
        self.assertIn('scale="3"',claim['amount_quote'])
        self.assertIn('2026-09-30',claim['date_quote'])
        self.assertIn('iso4217:USD',claim['currency_quote'])
        self.assertEqual(issues,[])
        s.evaluate(c,date(2026,11,11),self.factors,prices)
        self.assertAlmostEqual(c['measures']['small relative to parent'],10000000/240500000)

    def test_parent_lookup_requires_factor_child_date_and_established_parent_cik(self):
        for field in ('parent_cik','child_assets_date'):
            c=self.incomplete();c['facts'].pop(field);client=self.client()
            s.collect_parent_assets(client,c,self.factors,date(2026,11,11),[])
            client.json.assert_not_called();client.text.assert_not_called()
        client=self.client()
        s.collect_parent_assets(client,self.incomplete(),{'factors':[]},date(2026,11,11),[])
        client.json.assert_not_called()

    def test_parent_assets_currency_or_date_mismatch_never_computes_ratio(self):
        for day,currency in [('2026-09-30','EUR'),('2026-06-30','USD')]:
            c=self.incomplete();client=self.client(self.financial_html(day=day,currency=currency));issues=[]
            s.collect_parent_assets(client,c,self.factors,date(2026,11,11),issues)
            s.evaluate(c,date(2026,11,11),self.factors,prices)
            self.assertNotIn('small relative to parent',c['measures'])
            if day!='2026-09-30':self.assertFalse(s.value(c,'parent_assets'))

    def test_parent_assets_missing_or_segment_or_wrong_entity_stays_missing(self):
        for html in ('<html>No assets disclosed</html>',self.financial_html(segmented=True),self.financial_html(cik='999')):
            c=self.incomplete();issues=[]
            s.collect_parent_assets(self.client(html),c,self.factors,date(2026,11,11),issues)
            self.assertFalse(s.value(c,'parent_assets'));self.assertTrue(issues)

    def test_parent_lookup_failure_is_explicit_and_does_not_retry(self):
        client=self.client();client.text.side_effect=RuntimeError('offline failure');c=self.incomplete();issues=[]
        s.collect_parent_assets(client,c,self.factors,date(2026,11,11),issues)
        self.assertFalse(s.value(c,'parent_assets'));self.assertTrue(issues)
        self.assertEqual(client.text.call_count,1)

    def test_parent_lookup_reads_only_overlapping_metadata_shard_and_supports_10k(self):
        client=self.client();info=client.json.return_value
        group=copy.deepcopy(info['filings']['recent']);group['form'][1]='10-K'
        info['filings']['recent']={}
        info['filings']['files']=[
            {'name':'CIK0000001101-submissions-001.json','filingFrom':'2001-01-01','filingTo':'2002-01-01'},
            {'name':'CIK0000001101-submissions-002.json','filingFrom':'2026-01-01','filingTo':'2026-11-06'}]
        client.json.side_effect=[info,group]
        c=self.incomplete();s.collect_parent_assets(client,c,self.factors,date(2026,11,11),[])
        self.assertEqual(client.json.call_count,2)
        self.assertEqual(client.json.call_args.args[0],'https://data.sec.gov/submissions/CIK0000001101-submissions-002.json')
        self.assertEqual(c['facts']['parent_assets']['form'],'10-K')
        self.assertEqual(client.text.call_count,1)

    def test_latest_matching_amended_periodic_is_used_without_reading_other_filings(self):
        client=self.client();g=client.json.return_value['filings']['recent']
        for key,val in {'form':'10-Q/A','reportDate':'2026-09-30','filingDate':'2026-11-08',
                        'accessionNumber':'0000001101-26-000004','primaryDocument':'amended.htm'}.items():g[key].append(val)
        c=self.incomplete();s.collect_parent_assets(client,c,self.factors,date(2026,11,11),[])
        self.assertEqual(c['facts']['parent_assets']['accession'],'0000001101-26-000004')
        self.assertEqual(client.text.call_count,1)
        self.assertTrue(client.text.call_args.args[0].endswith('/amended.htm'))

    def test_no_matching_report_means_no_filing_download_or_substituted_date(self):
        client=self.client();client.json.return_value['filings']['recent']['reportDate'][1]='2026-06-30'
        c=self.incomplete();issues=[]
        s.collect_parent_assets(client,c,self.factors,date(2026,11,11),issues)
        client.text.assert_not_called()
        self.assertFalse(s.value(c,'parent_assets'));self.assertTrue(issues)

    def test_missing_units_and_conflicting_total_assets_remain_missing(self):
        html=self.financial_html()
        for bad in (html.replace('iso4217:USD','USD'), html.replace('scale="3"','scale="unknown"'),
                    html.replace('</body>',self.financial_html().replace('240,500','340,500')+'</body>')):
            c=self.incomplete();issues=[]
            s.collect_parent_assets(self.client(bad),c,self.factors,date(2026,11,11),issues)
            self.assertFalse(s.value(c,'parent_assets'));self.assertTrue(issues)

    def test_complete_same_date_parent_evidence_needs_no_additional_lookup(self):
        c=candidate();client=self.client()
        s.collect_parent_assets(client,c,self.factors,date(2026,11,11),[])
        client.json.assert_not_called();client.text.assert_not_called()


class FinancialPrerequisites(unittest.TestCase):
    setUp=SeekerRules.setUp
    financial_html=ParentFinancialLookup.financial_html

    def ownership(self, distribution=True, owner='1101'):
        footnote=('On October 1, 2026, FP distributed shares of Registrant 101 to its stockholders.'
                  if distribution else 'FP sold shares of Registrant 101 to another investor.')
        return f'''<ownershipDocument><issuer><issuerCik>101</issuerCik><issuerName>Registrant 101</issuerName></issuer>
        <reportingOwner><reportingOwnerId><rptOwnerCik>{owner}</rptOwnerCik><rptOwnerName>Fictional Parent Corp.</rptOwnerName></reportingOwnerId></reportingOwner>
        <nonDerivativeTransaction><transactionCoding><transactionCode>J</transactionCode><footnoteId id="F2"/></transactionCoding>
        <transactionAmounts><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
        <footnotes><footnote id="F1">Fictional Parent Corp. (referred to herein as "FP") owned Registrant 101.</footnote>
        <footnote id="F2">{footnote}</footnote></footnotes></ownershipDocument>'''

    def ownership_doc(self, **kw):
        return {**evidence('unused'),'form':'4','document_type':'4','text':self.ownership(**kw),
                'document_url':'https://www.sec.gov/Archives/edgar/data/101/ownership.xml'}

    def test_explicit_parent_distribution_xml_records_cik_with_structured_evidence(self):
        c=s.new_candidate('101','Registrant 101')
        s.ownership_parent(c,self.ownership_doc())
        self.assertEqual(s.value(c,'parent_cik'),'1101')
        fact=c['facts']['parent_cik']
        self.assertIn('reportingOwnerId',fact['section']);self.assertIn('F2',fact['section'])
        self.assertIn('<rptOwnerCik>1101</rptOwnerCik>',fact['quote'])
        self.assertIn('FP distributed shares',fact['quote'])
        self.assertEqual(fact['accession'],'0000000101-26-000001')

    def test_reporting_owner_alone_or_unrelated_distribution_is_not_parent_proof(self):
        for raw in (self.ownership(False),self.ownership().replace('FP distributed','Another company distributed'),
                    self.ownership().replace('<issuerCik>101','<issuerCik>999'),
                    self.ownership().replace('shares of Registrant 101 to','shares of Another Subsidiary to'),
                    self.ownership().replace('FP distributed','FP will distribute')):
            c=s.new_candidate('101','Registrant 101')
            s.ownership_parent(c,{**self.ownership_doc(),'text':raw})
            self.assertFalse(s.value(c,'parent_cik'))

    def test_explicit_owner_rename_alias_can_link_distribution_footnote_to_owner_cik(self):
        c=s.new_candidate('101','Registrant 101')
        doc=self.ownership_doc()
        doc['text']=doc['text'].replace('Fictional Parent Corp. (referred to herein as "FP")',
            'Former Parent Corp. (to be renamed Fictional Parent Corp. and referred to herein as "FP")')
        s.ownership_parent(c,doc)
        self.assertEqual(s.value(c,'parent_cik'),'1101')
        self.assertIn('to be renamed Fictional Parent Corp.',c['facts']['parent_cik']['quote'])

    def test_conflicting_parent_ciks_are_retained_but_left_blank(self):
        c=s.new_candidate('101','Registrant 101')
        s.ownership_parent(c,self.ownership_doc())
        s.ownership_parent(c,self.ownership_doc(owner='1201'))
        self.assertFalse(s.value(c,'parent_cik'))
        self.assertEqual(len(c['observations']['parent_cik']),2)

    def test_existing_parent_archive_link_records_explicit_cik_without_name_lookup(self):
        c=s.new_candidate('101','Registrant 101')
        doc={**evidence('unused'),'text':'Separation\nOur parent is Fictional Parent Corp.\nFiling links\nLink: Fictional Parent Corp. [https://www.sec.gov/Archives/edgar/data/1101/one.htm]'}
        s.deterministic_document(c,doc)
        self.assertEqual(s.value(c,'parent_cik'),'1101')
        self.assertEqual(c['facts']['parent_cik']['section'],'Filing links')

    def test_child_periodic_assets_preserve_all_evidence_and_normalize_scale(self):
        c=s.new_candidate('101','Registrant 101');client=Mock();issues=[]
        client.text.return_value=self.financial_html(cik='101').replace('CONSOLIDATED','COMBINED')
        rows=[{'cik':'101','form':'10-Q','filing_date':'2026-11-05','report_date':'2026-09-30',
               'accession':'0000000101-26-000002','primary_document':'quarter.htm'}]
        s.collect_child_assets(client,c,rows,self.factors,date(2026,11,11),issues)
        self.assertEqual(s.value(c,'child_assets'),'240500000')
        self.assertEqual(s.value(c,'child_assets_date'),'2026-09-30')
        self.assertEqual(s.value(c,'child_assets_currency'),'USD')
        claim=c['facts']['child_assets']
        self.assertEqual(claim['units'],'thousands');self.assertEqual(claim['scale'],'3')
        self.assertEqual(claim['section'],'FICTIONAL COMBINED BALANCE SHEETS')
        self.assertIn('240,500',claim['quote']);self.assertIn('iso4217:USD',claim['currency_quote'])
        self.assertEqual(claim['accession'],rows[0]['accession']);self.assertFalse(issues)
        self.assertEqual(client.text.call_count,1)

    def test_parent_lookup_waits_for_complete_child_asset_prerequisites(self):
        for missing in ('parent_cik','child_assets','child_assets_date','child_assets_currency'):
            c=candidate();c['facts'].pop(missing)
            for field in ('parent_assets','parent_assets_date','parent_assets_currency'):c['facts'].pop(field)
            client=Mock();s.collect_parent_assets(client,c,self.factors,date(2026,11,11),[])
            client.json.assert_not_called();client.text.assert_not_called()

    def test_incomplete_child_financial_evidence_does_not_trigger_parent_lookup(self):
        row={'cik':'101','form':'10-Q','filing_date':'2026-11-05','report_date':'2026-09-30',
             'accession':'0000000101-26-000002','primary_document':'quarter.htm'}
        for raw in (self.financial_html(cik='101').replace('iso4217:USD','unknown'),
                    self.financial_html(cik='101',day='2026-06-30'),
                    self.financial_html(cik='101',segmented=True)):
            c=s.new_candidate('101','Registrant 101');c['facts']['parent_cik']=evidence('1101')
            client=Mock();client.text.return_value=raw;issues=[]
            s.collect_child_assets(client,c,[row],self.factors,date(2026,11,11),issues)
            s.collect_parent_assets(client,c,self.factors,date(2026,11,11),issues)
            self.assertFalse(s.value(c,'child_assets'))
            client.json.assert_not_called();self.assertEqual(client.text.call_count,1)
            self.assertTrue(issues)

    def test_offline_seek_persists_child_and_parent_evidence_then_computes_size(self):
        reg={'cik':'101','name':'Registrant 101','form':'10-12B','filing_date':'2026-09-01','accession':'0000000101-26-000001'}
        own={'cik':'101','form':'10-Q','filing_date':'2026-11-05','accession':'0000000101-26-000002','primary_document':'quarter.htm'}
        four={**reg,'form':'4','filing_date':'2026-10-03','accession':'0000000101-26-000003'}
        parent={**own,'cik':'1101','accession':'0000001101-26-000002'}
        regtext='Information Statement\nSeparation\nOur parent is Fictional Parent Corp.\nOur parent distributes shares of Registrant 101 business to its stockholders.\nDistribution date: October 1, 2026.\nRecord date: September 20, 2026.\nDistribution ratio: one share for every ten shares.\nThe company is incorporated in Delaware.\nRegular-way ticker: X101.\nRegular-way trading date: October 2, 2026.'
        regdoc={**evidence('unused'),'text':regtext,'document_type':'10-12B','form':'10-12B','filing_date':reg['filing_date']}
        def bundle(row):
            doc=regdoc if row['form'] in s.REGISTRATIONS else {**self.ownership_doc(),'accession':four['accession']}
            return {'documents':[doc],'complete':True,'information_statement':row['form'] in s.REGISTRATIONS,'index':evidence('Inventory')}
        def submissions_info(cik, rows):
            return {'name':'Registrant 101' if cik=='101' else 'Fictional Parent Corp.','filings':{'recent':{
                'form':[r['form'] for r in rows],'filingDate':[r['filing_date'] for r in rows],
                'accessionNumber':[r['accession'] for r in rows],'primaryDocument':[r.get('primary_document','doc.xml') for r in rows],
                'reportDate':['2026-09-30' if r['form']=='10-Q' else '' for r in rows]},'files':[]}}
        client=Mock()
        client.json.side_effect=lambda url:submissions_info('101',[reg,own,four]) if 'CIK0000000101' in url else submissions_info('1101',[parent])
        client.text.side_effect=lambda url:self.financial_html(cik='101').replace('240,500','24,050') if '/101/' in url else self.financial_html()
        extractor=Mock();extractor.extract.return_value={'facts':{},'findings':[]}
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'discover',return_value=({'101':[reg]}, {}, ['2026-11-11'])),patch.object(s,'carry_in_bundles',return_value={}),patch.object(s,'filing_documents',side_effect=lambda client,row,**kwargs:bundle(row)):
            s.seek(date(2025,11,10),None,date(2026,11,11),tmp,client,extractor,prices,run_at='2026-11-11T18:00:00-05:00')
            c=json.loads((Path(tmp)/'registry.json').read_text())['candidates']['101']
            self.assertEqual(s.value(c,'parent_cik'),'1101')
            self.assertEqual(s.value(c,'child_assets'),'24050000')
            self.assertEqual(s.value(c,'parent_assets'),'240500000')
            self.assertAlmostEqual(c['measures']['small relative to parent'],0.1)
            self.assertEqual(c['facts']['child_assets']['accession'],own['accession'])
            self.assertEqual(c['facts']['parent_assets']['accession'],parent['accession'])
            self.assertIn('distributed shares',c['facts']['parent_cik']['quote'])
            self.assertEqual(client.text.call_count,2)


if __name__=='__main__':unittest.main()
