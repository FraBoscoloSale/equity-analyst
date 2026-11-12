"""Offline browser-service tests; never contact SEC or a model provider."""

import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import agent_team_ui as ui


def artifacts(root):
    folder = root / 'research_data' / 'saved_run'
    folder.mkdir(parents=True)
    report = ('Filings read: saved filings\n'
              '| Number | Mark | Correction | Filing line / XBRL tag |\n'
              '| --- | --- | --- | --- |\n'
              '| 12 | correct | | saved.txt, line 5 |\n\n## Overview\nVerified evidence.\n')
    (folder/'verified_report.md').write_text(report, encoding='utf-8')
    (folder/'compiled_report.md').write_text('Compiled report', encoding='utf-8')
    data = {'verifier_semantic_results.json': {'complete':True, 'checks':{'number_1':{
                'supported':True, 'reason':'Period and definition agree.', 'correction':None}},
                'aliases':{'number_1':'number_1'}},
            'verifier_semantic_batches.json': {'expected_ids':['number_1'], 'aliases':{'number_1':'number_1'}},
            'verification_marks.json': [{'original':'12', 'mark':'correct', 'correction':'', 'source':'saved.txt, line 5'}],
            'review_final.json': {'checks':{str(i):{'passed':i != 0} for i in range(5)}}}
    for filename, value in data.items():
        (folder/filename).write_text(json.dumps(value),encoding='utf-8')
    return folder/'verified_report.md', report


class RunServiceTests(unittest.TestCase):
    def execute(self, root, lines, code=0, payload=None):
        service=ui.RunService(root)
        job_id='a'*32
        service.active=job_id
        service.jobs[job_id]={'id':job_id,'status':'running','nodes':[], 'error':'','report':'','counts':{},'unresolved_parts':0,
                              'kind':'seeker' if payload and 'start' in payload else 'team'}
        process=Mock()
        process.stdout=iter(lines)
        process.wait.return_value=code
        context=Mock()
        context.__enter__=Mock(return_value=process)
        context.__exit__=Mock(return_value=False)
        with patch.object(ui.subprocess,'Popen',return_value=context) as popen:
            command=ui.command_for(payload or {'ticker':'TEST','peers':'PEER,OTHER','price':'40.25'},root)
            service.execute(job_id,command)
        self.assertIsNone(service.active)
        self.assertFalse(popen.call_args.kwargs['shell'])
        self.assertEqual(popen.call_args.args[0],command)
        return service.snapshot(job_id)

    def test_seeker_valid_date_launches_existing_unbuffered_cli(self):
        command=ui.command_for({'start':'2025-11-10'})
        self.assertEqual(command,[ui.sys.executable,'-u',str(ui.ROOT/'spinoff_seeker.py'),
                                  '--start','2025-11-10'])

    def test_seeker_rejects_invalid_dates_and_mixed_team_inputs(self):
        for payload in ({'start':''},{'start':'11/10/2025'},{'start':'2025-2-01'},
                        {'start':'2025-02-30'},{'start':None},{'start':True},
                        {'start':'2025-11-10; whoami'},{'start':'2025-11-10','ticker':'TEST'}):
            with self.subTest(payload=payload),self.assertRaises(ui.InputError):
                ui.command_for(payload)

    def test_seeker_progress_and_printed_report_return_without_team_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'spinoff_runs';folder.mkdir()
            path=folder/'report_example.md'
            report='## In their window, ordered by rank\n\nNo ranked names.\n\nscreens, not recommendations\n'
            path.write_text(report,encoding='utf-8')
            result=self.execute(root,['[start] Distribution cutoff 2025-11-10\n',
                '[triage] Inspecting saved test registrant\n',str(path)+'\n'],payload={'start':'2025-11-10'})
            self.assertEqual(result['status'],'completed');self.assertEqual(result['report'],report)
            self.assertEqual(result['nodes'],['[start] Distribution cutoff 2025-11-10',
                                             '[triage] Inspecting saved test registrant'])
            self.assertEqual(result['counts'],{})

    def test_seeker_refuses_failed_missing_or_outside_report(self):
        for mode in ('failed','no_path','outside','missing','empty'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);folder=root/'spinoff_runs';folder.mkdir()
                path=(root if mode=='outside' else folder)/'report_example.md'
                if mode!='missing':path.write_text('' if mode=='empty' else 'Test report',encoding='utf-8')
                lines=[] if mode=='no_path' else [str(path)+'\n']
                result=self.execute(root,lines,code=1 if mode=='failed' else 0,payload={'start':'2025-11-10'})
                self.assertEqual(result['status'],'failed');self.assertEqual(result['report'],'')

    def test_form_uses_existing_cli_and_preserves_optional_inputs(self):
        command=ui.command_for({'ticker':' test ','peers':'peer,other PEER','price':'40.25'})
        self.assertEqual(command,[ui.sys.executable,'-u',str(ui.ROOT/'agent_team.py'),'TEST',
                                 '--peers','PEER,OTHER','--price','40.25'])
        empty=ui.command_for({'ticker':'TEST','peers':'','price':''})
        self.assertEqual(empty,[ui.sys.executable,'-u',str(ui.ROOT/'agent_team.py'),'TEST'])

    def test_invalid_inputs_and_attempted_bypass_are_rejected(self):
        payloads=[{}, {'ticker':'--verify-only'}, {'ticker':'TEST; whoami'},
                  {'ticker':'TEST','peers':'../secret'}, {'ticker':'TEST','price':'NaN'},
                  {'ticker':'TEST','price':'inf'}, {'ticker':'TEST','price':0},
                  {'ticker':'TEST','price':True}, {'ticker':'TEST','skip_verifier':True},
                  {'ticker':'TEST','verify_only':'a_path'}, {'ticker':'TEST','peers':['PEER']}]
        for payload in payloads:
            with self.subTest(payload=payload),self.assertRaises(ui.InputError):
                ui.command_for(payload)

    def test_progress_and_verified_report_with_marks_are_returned_only_after_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); report_path, report=artifacts(root)
            names=list(ui.NODES[:5])+['Research Analyst','Financial Analyst','Investment Advisor','Strategy Manager','Verifier: code checks','Verifier']
            result=self.execute(root,[n+'\n' for n in names]+[f'Verified report: {report_path}\n'])
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['report'],report)
            self.assertEqual(result['counts'],{'correct':1})
            self.assertEqual(result['unresolved_parts'],1)
            self.assertEqual(result['nodes'][-1],'Verifier')
            self.assertEqual(result['nodes'].count('Verifier'),1)
            self.assertEqual(result['nodes'].count('Research Analyst'),2)

    def test_nonzero_exit_is_failure_even_when_report_path_is_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path,_=artifacts(root)
            result=self.execute(root,[f'Verified report: {path}\n','Error: Required evidence is missing.\n'],1)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(result['report'],'')
            self.assertIn('Required evidence is missing',result['error'])

    def test_zero_exit_without_verification_artifacts_or_full_coverage_is_not_success(self):
        for mode in ('no_path','missing_report','incomplete','missing_check','missing_marks','wrong_path'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); path,_=artifacts(root)
                if mode=='missing_report': path.unlink()
                if mode in ('incomplete','missing_check'):
                    p=path.parent/'verifier_semantic_results.json'
                    data=json.loads(p.read_text())
                    if mode=='incomplete': data['complete']=False
                    else: data['checks']={}
                    p.write_text(json.dumps(data))
                if mode=='missing_marks': (path.parent/'verification_marks.json').unlink()
                if mode=='wrong_path': path=root/'verified_report.md'
                lines=[] if mode=='no_path' else [f'Verified report: {path}\n']
                result=self.execute(root,lines)
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['report'],'')

    def test_credentials_are_redacted_from_errors_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'.env').write_text('API_KEY=secret-example-key\nSEC_USER_AGENT=private-contact@example.invalid\n',encoding='utf-8')
            path,_=artifacts(root)
            path.write_text('Filings read: saved\nsecret-example-key private-contact@example.invalid',encoding='utf-8')
            for result in (self.execute(root,[f'Verified report: {path}\n']),
                           self.execute(root,['Error: secret-example-key private-contact@example.invalid\n'],1)):
                serialized=json.dumps(result)
                self.assertNotIn('secret-example-key',serialized)
                self.assertNotIn('private-contact@example.invalid',serialized)
                self.assertIn('[redacted]',serialized)

    def test_progress_detects_interleaved_frames_and_tool_steps(self):
        self.assertEqual(ui.node_from_line('│       Research Analyst'), 'Research Analyst')
        self.assertEqual(ui.node_from_line('Step 3/7: Download filings'), 'Step 3/7: Download filings')
        self.assertIsNone(ui.node_from_line('an arbitrary provider log'))

    def test_active_run_cannot_be_submitted_twice(self):
        service=ui.RunService()
        with patch.object(ui.threading,'Thread') as thread:
            first=service.submit({'ticker':'TEST'})
            with self.assertRaisesRegex(ui.InputError,'already active'):
                service.submit({'ticker':'OTHER'})
            thread.return_value.start.assert_called_once()
        self.assertEqual(service.snapshot()['id'],first)


class BrowserRenderingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the offline JavaScript check')
    def test_existing_page_submits_seeker_and_renders_both_workflows(self):
        # Execute the actual page script with a small DOM and mocked local HTTP
        # responses. No browser, server process, seeker or network is launched.
        script=r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor() { this.children = []; this.firstChild = {textContent:''}; this.events = {}; this.hidden = false; }
  get childElementCount() { return this.children.length; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, fn) { this.events[name] = fn; }
}
const html = fs.readFileSync(process.argv[3], 'utf8');
const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Element()]));
assert(html.includes('>Run seeker</button>'));
const document = {getElementById: id => { assert(elements[id], 'Missing HTML element '+id); return elements[id]; },
  createElement: () => new Element(), createTextNode: text => ({textContent:text})};
let kind = 'seeker', polls = 0, submissions = [];
const reply = data => ({ok:true,json:async () => data});
const context = vm.createContext({document, setTimeout: fn => fn(), fetch:async (path, options) => {
  if (path === '/api/session') return reply({token:'test-token',run:null});
  if (options.method === 'POST') {
    const payload = JSON.parse(options.body); submissions.push(payload);
    kind = payload.start ? 'seeker' : 'team'; polls = 0;
    return reply({id:'a'.repeat(32)});
  }
  const job = {kind, nodes:kind === 'seeker' ? ['[discovery] Inspecting filings'] : ['Research Analyst'],
    counts:kind === 'seeker' ? {} : {correct:1}, unresolved_parts:0};
  if (++polls === 1) return reply({...job,status:'running',report:''});
  assert.equal(elements.nodes.children[0].textContent, job.nodes[0]);
  assert(elements['run-button'].disabled && elements['seeker-button'].disabled);
  return reply({...job,status:'completed',report:kind === 'seeker' ? '# Seeker result\nscreens, not recommendations' : '# Verified result'});
}});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
async function settle() { for (let i=0;i<5;i++) await new Promise(resolve => setImmediate(resolve)); }
(async () => {
  await settle();
  elements.start.value = '2025-11-10';
  await elements['seeker-form'].events.submit({preventDefault(){}}); await settle();
  assert.deepEqual(submissions[0], {start:'2025-11-10'});
  assert.equal(elements['report-title'].textContent, 'Seeker report');
  assert.equal(elements.report.children[0].children[0].textContent, 'Seeker result');
  assert.equal(elements.results.hidden, false); assert.equal(elements.counts.hidden, true);
  assert.equal(elements.findings.hidden, true);
  elements.ticker.value='TEST'; elements.peers.value='PEER'; elements.filed.value='2026-10-01T16:30:00';
  await elements['run-form'].events.submit({preventDefault(){}}); await settle();
  assert.deepEqual(submissions[1], {ticker:'TEST',peers:'PEER',filed:'2026-10-01T16:30:00'});
  assert.equal(elements['report-title'].textContent, 'Verified report');
  assert.equal(elements.report.children[0].children[0].textContent, 'Verified result');
  assert.equal(elements.counts.hidden, false); assert.equal(elements.counts.children[0].textContent,'1 correct');
  assert.equal(elements['run-button'].disabled, false); assert.equal(elements['seeker-button'].disabled, false);
})().catch(error => { console.error(error); process.exitCode=1; });
'''
        result=subprocess.run([shutil.which('node'),'-',str(ui.ASSETS/'app.js'),str(ui.ASSETS/'index.html')],
                              input=script,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.service=ui.RunService()
        self.server=ui.LocalServer(('127.0.0.1',0),self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.host=f'127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def request(self,method,path,body=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request(method,path,body=body,headers=headers or {})
            response=connection.getresponse(); data=response.read(); status=response.status
            return status,data
        finally:
            connection.close()

    def test_one_page_assets_session_and_credential_files_are_not_served(self):
        for path in ('/','/app.js','/style.css'):
            status,data=self.request('GET',path)
            self.assertEqual(status,200)
            self.assertTrue(data)
        for path in ('/.env','/roles/research_analyst.txt','/../.env','/research_data/private'):
            status,_=self.request('GET',path)
            self.assertEqual(status,404)
        status,data=self.request('GET','/api/session')
        payload=json.loads(data)
        self.assertEqual(status,200)
        self.assertEqual(set(payload),{'token','run'})

    def test_form_starts_existing_service_and_allows_progress_polling(self):
        payload=json.dumps({'ticker':'TEST','peers':'PEER','price':None})
        headers={'Content-Type':'application/json','X-Run-Token':self.server.form_token,
                 'Origin':f'http://{self.host}'}
        with patch.object(self.service,'execute'):
            status,data=self.request('POST','/api/runs',payload,headers)
        self.assertEqual(status,202)
        job_id=json.loads(data)['id']
        status,data=self.request('GET','/api/runs/'+job_id)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(data)['status'],'running')

    def test_remote_origins_hosts_and_missing_form_token_cannot_start_runs(self):
        for headers in ({}, {'X-Run-Token':self.server.form_token,'Origin':'https://external.invalid'},
                        {'X-Run-Token':self.server.form_token,'Host':'external.invalid'}):
            status,_=self.request('POST','/api/runs',json.dumps({'ticker':'TEST'}),headers)
            self.assertEqual(status,403)
        self.assertIsNone(self.service.active)

    def test_seeker_progress_is_pollable_while_running_and_report_after_completion(self):
        headers={'Content-Type':'application/json','X-Run-Token':self.server.form_token}
        progress_ready=threading.Event();finish=threading.Event();done=threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'spinoff_runs';folder.mkdir()
            path=folder/'report_example.md';report='# Seeker report\nscreens, not recommendations\n'
            path.write_text(report,encoding='utf-8');self.service.root=root
            def output():
                yield '[discovery] Inspecting filings\n'
                progress_ready.set()
                if not finish.wait(5):raise AssertionError('Test did not release mocked seeker')
                yield str(path)+'\n'
            process=Mock();process.stdout=output();process.wait.return_value=0
            process.__enter__=Mock(return_value=process);process.__exit__=Mock(return_value=False)
            execute=self.service.execute
            def worker(*args):
                try:execute(*args)
                finally:done.set()
            with patch.object(ui.subprocess,'Popen',return_value=process) as popen,patch.object(self.service,'execute',side_effect=worker):
                try:
                    status,data=self.request('POST','/api/runs',json.dumps({'start':'2025-11-10'}),headers)
                    self.assertEqual(status,202);job_id=json.loads(data)['id']
                    self.assertTrue(progress_ready.wait(5))
                    status,data=self.request('GET','/api/runs/'+job_id);job=json.loads(data)
                    self.assertEqual(job['status'],'running');self.assertEqual(job['report'],'')
                    self.assertEqual(job['kind'],'seeker')
                    self.assertIn('[discovery] Inspecting filings',job['nodes'])
                    popen.assert_called_once()
                    self.assertEqual(popen.call_args.args[0],[ui.sys.executable,'-u',str(root/'spinoff_seeker.py'),'--start','2025-11-10'])
                finally:finish.set();done.wait(5)
            status,data=self.request('GET','/api/runs/'+job_id);job=json.loads(data)
            self.assertEqual(job['status'],'completed');self.assertEqual(job['report'],report)

    def test_invalid_seeker_date_never_starts_process(self):
        headers={'Content-Type':'application/json','X-Run-Token':self.server.form_token}
        with patch.object(ui.subprocess,'Popen') as popen:
            status,_=self.request('POST','/api/runs',json.dumps({'start':'2025-02-30'}),headers)
        self.assertEqual(status,400);popen.assert_not_called()


if __name__=='__main__':
    unittest.main()
