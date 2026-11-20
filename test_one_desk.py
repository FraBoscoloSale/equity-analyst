"""Fictional offline pipeline, real report/worksheet checks, no live data."""
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import one_desk as d
import agent_team as team
import research_roles as roles
import spinoff_worksheet as w
import verify_spinoff_worksheet as numeric
from test_agent_team import ScriptedClient, saved_fixture
from test_spinoff_worksheet import structured_response, financial_html

NOW = datetime.fromisoformat('2026-01-02T10:00:00-05:00')


class FictionalPipeline(d.Pipeline):
    def __init__(self, fail=None, empty=False):
        self.events, self.fail, self.empty = [], fail, empty
        self.folder = None
        self.judgment_calls = 0

    def event(self, name):
        self.events.append(name)
        if self.fail == name:
            raise ValueError('exact fictional failure')

    def seeker(self, folder, client, start, now):
        self.event('seeker')
        candidate = {'eligible': True, 'rank': 1, 'facts': {'ticker': {'value': 'TEST'}},
                     'filings': [{'form': '10-12B', 'accession': '0000000001-26-000001'}]}
        d.write_json(folder/'registry.json', {'candidates': {'1': candidate}})
        (folder/'report_fictional.md').write_text('Run date: '+now.isoformat()+' | price date: not available\n'
                '## In their window, ordered by rank\n'
                +('' if self.empty else '1. Fictional | CIK 1 | total points 0\n')
                +'\n## Other stages\nscreens, not recommendations\n', encoding='utf-8')
        archive = folder/'sec_sources'; archive.mkdir()
        raw = b'Fictional saved Information Statement'
        (archive/'fictional.bin').write_bytes(raw)
        d.write_json(archive/'fictional.json', {'url': 'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/form10.htm',
                                               'sha256': d.digest(archive/'fictional.bin')})

    def team(self, ticker, folder, client):
        self.event('team')
        self.folder = folder
        saved_fixture(folder)
        cik = d.read_json(folder/'run.json')['company']['cik']
        (folder/'10-K.html').write_text(financial_html(start='2025-01-01', end='2025-12-31', missing=('shares_outstanding',)).replace(
            '<xbrli:identifier>101', '<xbrli:identifier>'+str(cik)), encoding='utf-8')
        with (folder/'10-K.txt').open('a', encoding='utf-8') as handle:
            handle.write('\nCONSOLIDATED STATEMENTS OF INCOME AND CASH FLOWS\n'
                         'after_tax_earnings | 100\ndepreciation | 20\ncapital_spending | 10\nshares_outstanding | 10\n')
        scripted = ScriptedClient()
        def model(*args, **kwargs):
            body = kwargs['json']; envelope = json.loads(body['messages'][1]['content'])
            role = 'Verifier' if 'checks' in envelope['expected_output']['properties'] and 'tasks' not in envelope['context'] else None
            if role is None:
                role = next(r for r in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR, roles.MANAGER)
                            if roles.load_instructions(r) in body['messages'][0]['content'])
            # Manager also has checks, but its saved persona identifies it exactly.
            if roles.load_instructions(roles.MANAGER) == body['messages'][0]['content']:
                role = roles.MANAGER
            result = scripted.call(role, envelope['task'], envelope['context'], envelope['expected_output'])
            return Mock(ok=True, json=lambda: {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]})
        with patch.object(team.requests, 'post', side_effect=model):
            manager = team.TeamManager(folder, team.NodeClient('offline', folder))
            for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
                manager.specialist(role)
            manager.finish()
            return team.Verifier(manager.client).run(team.load_verifier_sources(folder), folder)

    def worksheet(self, folder, client, now):
        self.event('worksheet')
        assert folder == self.folder and (folder/'facts.json').is_file()
        def judge(persona, contract):
            self.judgment_calls += 1
            return structured_response(contract)
        return w.run_worksheet(folder, d.read_json(folder/'registry.json'), folder/'spinoff_worksheet.txt',
                    folder/'investment_advisor.txt', judge, client, now=NOW,
                    reader=lambda *args: 'not available', volume_reader=lambda *args: 'not available',
                    currency_reader=lambda *args: 'not available')

    def team_hook(self, folder):
        self.event('team hook')
        super().team_hook(folder)

    def hook(self, path, folder):
        self.event('final hook' if path.name == 'desk_report.md' else 'worksheet hook')
        super().hook(path, folder)


class OneDeskTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        project = self.root/'project'; (project/'roles').mkdir(parents=True)
        for filename in ('spinoff_worksheet.txt', 'spinoff_factors.txt', 'roles/investment_advisor.txt', 'roles/verifier.txt'):
            shutil.copyfile(d.ROOT/filename, project/filename)
        self.project = project
        p = patch.object(d, 'ROOT', project); p.start(); self.addCleanup(p.stop)
        p = patch('requests.sessions.Session.request', side_effect=AssertionError('Network/model forbidden')); p.start(); self.addCleanup(p.stop)

    def run_desk(self, selection='TEST', pipeline=None, root=None):
        self.pipeline = pipeline or FictionalPipeline()
        self.stdout = io.StringIO()
        with redirect_stdout(self.stdout):
            return d.run(selection, root=root or self.root/'runs', settings={'SEC_USER_AGENT': 'fictional contact'},
                         pipeline=self.pipeline, now=NOW)

    def test_ticker_fixed_order_one_folder_real_worksheet_and_final_hook(self):
        final = self.run_desk()
        self.assertEqual(self.pipeline.events, ['team', 'worksheet', 'team hook', 'worksheet hook', 'final hook'])
        self.assertEqual(self.pipeline.judgment_calls, 1)
        self.assertEqual(len(list((self.root/'runs').iterdir())), 1)
        self.assertEqual(final.parent.name, 'TEST_2026-01-02_10-00-00_000000')
        manifest = d.read_json(final.parent/'desk_manifest.json')
        sheet = (final.parent/manifest['worksheet']).read_text(encoding='utf-8')
        self.assertIn('Parent: not applicable', sheet)
        data = final.read_bytes()
        report = (final.parent/'verified_report.md').read_bytes()
        self.assertTrue(data.startswith(b'Filings read: '))
        self.assertIn(b'10-K.txt (10-K ', data.splitlines()[0])
        self.assertIn(report+b'\n'+d.view_section(final.parent/manifest['worksheet']), data)
        for part in d.ORDER:
            self.assertIn('[desk] '+part+' elapsed seconds:', self.stdout.getvalue())

    def test_screen_selects_latest_completed_report_and_renames_same_folder(self):
        final = self.run_desk('screen')
        self.assertEqual(self.pipeline.events[0:3], ['seeker', 'team', 'worksheet'])
        self.assertEqual(len(list((self.root/'runs').iterdir())), 1)
        self.assertTrue(final.parent.name.startswith('TEST_'))
        self.assertIn(b'form10.htm (10-12B 0000000001-26-000001)', final.read_bytes().splitlines()[0])

    def test_original_desk_report_routes_through_desk_verifier(self):
        final = self.run_desk()
        with patch.object(d, 'verify_desk_report', wraps=d.verify_desk_report) as check:
            numeric.verify(final)
        check.assert_called_once_with(final)

    def test_clean_renamed_desk_report_routes_identically_and_passes(self):
        final = self.run_desk()
        copy = final.with_name('arbitrary_saved_output.md')
        copy.write_bytes(final.read_bytes())
        with patch.object(d, 'verify_desk_report', wraps=d.verify_desk_report) as check:
            numeric.verify(copy)
        check.assert_called_once_with(copy)

    def test_normal_worksheet_is_not_a_desk_copy(self):
        final = self.run_desk()
        manifest = d.read_json(final.parent/'desk_manifest.json')
        sheet = final.parent/manifest['worksheet']
        self.assertFalse(numeric.desk_report_copy(sheet))
        with patch.object(d, 'verify_desk_report', side_effect=AssertionError('Wrong routing')):
            numeric.verify(sheet, final.parent/'spinoff_worksheet.txt')

    def test_renamed_desk_numeric_error_identifies_changed_line_and_source(self):
        final = self.run_desk()
        lines = final.read_text(encoding='utf-8').splitlines(keepends=True)
        index = next(i for i, line in enumerate(lines) if numeric.tokens(line) and i > 1)
        token = numeric.tokens(lines[index])[0]
        original = token[0]
        changed = str(numeric.dec(original)+1)
        lines[index] = lines[index][:token.start()]+changed+lines[index][token.end():]
        copy = final.with_name('unrelated_name.md')
        copy.write_text(''.join(lines), encoding='utf-8')
        with self.assertRaises(numeric.Failure) as cm:
            numeric.verify(copy)
        message = str(cm.exception)
        self.assertIn('line '+str(index+1)+':', message)
        self.assertIn('amount '+changed, message)
        self.assertIn('claimed source:', message)
        self.assertIn(original, message)

    def test_filename_prefix_alone_does_not_route_as_desk(self):
        final = self.run_desk()
        copy = final.with_name('desk_report_unrelated.md')
        copy.write_text('Unsupported 999\n', encoding='utf-8')
        self.assertFalse(numeric.desk_report_copy(copy))
        with self.assertRaises(numeric.Failure):
            numeric.verify(copy)

    def test_empty_screen_stops_before_team_and_keeps_evidence(self):
        pipeline = FictionalPipeline(empty=True)
        with self.assertRaisesRegex(d.PartFailure, 'Selection: Empty screen'):
            self.run_desk('screen', pipeline)
        self.assertEqual(pipeline.events, ['seeker'])
        self.assertTrue(list((self.root/'runs').glob('*/report_fictional.md')))

    def test_failure_in_each_part_stops_later_parts(self):
        for failure in ('seeker', 'team', 'worksheet', 'team hook', 'worksheet hook', 'final hook'):
            with self.subTest(failure=failure):
                pipeline = FictionalPipeline(fail=failure)
                with self.assertRaisesRegex(d.PartFailure, 'exact fictional failure'):
                    self.run_desk('screen', pipeline, self.root/failure)
                self.assertEqual(pipeline.events[-1], failure)
                self.assertFalse(any(d.read_json(p).get('status') == 'completed' for p in (self.root/failure).glob('*/desk_manifest.json')))

    def test_replay_no_network_model_or_live_export_and_byte_identical(self):
        final = self.run_desk()
        before = {p: p.read_bytes() for p in final.parent.rglob('*') if p.is_file()}
        with patch.object(team.requests, 'post', side_effect=AssertionError('Model forbidden')), \
             patch('closes_reader.dotenv_values', side_effect=AssertionError('Live export forbidden')), \
             patch.object(d, 'SavedSEC', side_effect=AssertionError('SEC forbidden')), redirect_stdout(io.StringIO()):
            self.assertEqual(d.replay(final.parent), final)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(set(before), {p for p in final.parent.rglob('*') if p.is_file()})

    def test_replay_missing_evidence_or_changed_final_report_fails(self):
        final = self.run_desk()
        original = final.read_bytes()
        final.write_bytes(original+b'Unsupported 999\n')
        with self.assertRaisesRegex(ValueError, 'differs'):
            d.verify_desk_report(final)
        final.write_bytes(original)
        (final.parent/'10-K.txt').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing or changed'):
            d.verify_desk_report(final)

    def test_same_execution_order_on_two_runs(self):
        self.run_desk(root=self.root/'first'); first = self.pipeline.events[:]
        self.run_desk(root=self.root/'second')
        self.assertEqual(first, self.pipeline.events)

    def test_shared_sec_cache_never_fetches_saved_url_twice(self):
        folder = self.root/'cache'; folder.mkdir()
        client = d.SavedSEC(folder, 'fictional contact')
        url = 'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/form10.htm'
        response = requests.Response(); response.status_code = 200; response.url = url; response._content = b'fictional filing'
        client.transport.session = Mock(); client.transport.session.get.return_value = response
        self.assertEqual(client.text(url), 'fictional filing')
        self.assertEqual(client.text(url), 'fictional filing')
        self.assertEqual(client.transport.session.get.call_count, 1)
        other = d.SavedSEC(folder, 'fictional contact')
        self.assertEqual(other.text(url), 'fictional filing')

    def test_existing_team_data_step_accepts_exact_folder_and_shared_client(self):
        from types import SimpleNamespace
        import equity_desk as desk
        folder = self.root/'exact'; folder.mkdir()
        args = SimpleNamespace(ticker='TEST', peers=[], price=None, step=1, run_folder=folder, sec_client=Mock())
        with patch.object(desk, 'resolve', return_value={'ticker':'TEST','cik':'1','name':'Fictional'}), \
             patch.object(desk, 'SECClient', side_effect=AssertionError('Duplicate client')), redirect_stdout(io.StringIO()):
            self.assertEqual(desk.run_seven_steps(args, {'SEC_USER_AGENT':'fictional'}), folder)
        self.assertEqual(list(folder.glob('*/run.json')), [])

    def test_reader_replay_uses_saved_rows_with_original_instrument_and_currency_rules(self):
        import closes_reader as reader
        folder = self.root/'reader'; folder.mkdir()
        def export(include_volume=False, include_currency=False):
            row = ('TEST.K', NOW.date(), 25)
            if include_volume: row += (4000,)
            if include_currency: row += ('USD',)
            return iter([row])
        def observe():
            return (reader.daily_closes_for_ticker('TEST', '2026-01-02', '2026-01-02'),
                    reader.daily_close_volumes('TEST.K', '2026-01-02', '2026-01-02'),
                    reader.instrument_currency('TEST.K'))
        with patch.object(reader, '_export_rows', side_effect=export):
            with d.reader_snapshot(folder):
                before = observe()
        with patch.object(reader, 'dotenv_values', side_effect=AssertionError('Export forbidden')):
            with d.reader_snapshot(folder, replay=True):
                self.assertEqual(observe(), before)
        self.assertEqual(before[0][0]['instrument'], 'TEST.K')
        self.assertEqual(before[1][0]['volume'], 4000)
        self.assertEqual(before[2], 'USD')

    def test_numeric_hook_adapter_finds_and_checks_actual_final_report(self):
        final = self.run_desk()
        self.assertIn(final.resolve(), numeric.output_paths({'stdout':'Desk report: '+str(final)}, self.root))
        numeric.hook({'hook_event_name':'PostToolUse', 'cwd':str(self.root),
                      'tool_response':{'stdout':'Desk report: '+str(final)}}, final.parent/'spinoff_worksheet.txt')

    def test_flow_passes_existing_folder_and_client_without_creating_another_run(self):
        folder = self.root/'flow'; folder.mkdir()
        client = Mock()
        with patch.object(team.desk, 'run_seven_steps', return_value=folder) as collect, \
             patch.object(team, 'TeamManager'), patch.object(team, 'NodeClient'), redirect_stdout(io.StringIO()):
            flow = team.DeskFlow({'API_KEY':'offline'}, run_folder=folder, sec_client=client)
            flow.state.ticker = 'TEST'
            flow.seven_steps()
        args = collect.call_args.args[0]
        self.assertEqual(args.run_folder, folder)
        self.assertIs(args.sec_client, client)

    def saved_screen_fixture(self, ticker='TEST'):
        folder = self.root/'saved_screen'; folder.mkdir(exist_ok=True)
        report = folder/'report_saved.md'
        spec = 'd'*64
        report.write_text('Run date: '+NOW.isoformat()+' | price date: not available\n'
            +'Factor file SHA256: '+spec+'.\n## In their window, ordered by rank\n'
            +'1. Fictional first | CIK 1 | total points 1\n'
            +'2. Fictional second | CIK 2 | total points 0\n\n'
            +'## Other stages\nscreens, not recommendations\n', encoding='utf-8')
        d.write_json(folder/'registry.json', {'updated_at':NOW.isoformat(), 'factors':{'sha256':spec},
            'candidates':{'1':{'eligible':True, 'rank':1, 'facts':{'ticker':{'value':ticker}}},
                          '2':{'eligible':True, 'rank':2, 'facts':{'ticker':{'value':'OTHER'}}}}})
        return report

    def test_saved_screen_selects_top_uses_same_order_and_preserves_all_outputs(self):
        final = self.run_desk()
        live_order = self.stdout.getvalue().splitlines()[0]
        report = self.saved_screen_fixture()
        files = [p for p in self.root.rglob('*') if p.is_file()]
        before = {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in files}
        output = io.StringIO()
        with patch.object(d.Pipeline, 'seeker', side_effect=AssertionError('Seeker forbidden')), \
             patch.object(d.Pipeline, 'team', side_effect=AssertionError('Team forbidden')), \
             patch.object(d.Pipeline, 'worksheet', side_effect=AssertionError('Worksheet forbidden')), \
             patch.object(d, 'SavedSEC', side_effect=AssertionError('SEC forbidden')), \
             patch.object(team.requests, 'post', side_effect=AssertionError('Model forbidden')), \
             patch('closes_reader.dotenv_values', side_effect=AssertionError('Live reader forbidden')), \
             patch.object(numeric, 'verify', wraps=numeric.verify) as hook, redirect_stdout(output):
            self.assertEqual(d.saved_input_screen(report, final.parent), final)
        self.assertEqual(output.getvalue().splitlines()[0], live_order)
        self.assertIn('not a fresh live screen', output.getvalue())
        self.assertIn('Selected ticker: TEST', output.getvalue())
        phases = [line.removeprefix('[desk] ') for line in output.getvalue().splitlines() if line in ['[desk] '+p for p in d.ORDER]]
        self.assertEqual(phases, list(d.ORDER))
        for part in d.ORDER:
            self.assertIn(part+' elapsed seconds:', output.getvalue())
        checked = [Path(call.args[0]) for call in hook.call_args_list]
        self.assertIn(final, checked)
        self.assertIn(final.parent/'verified_report.md', checked)
        self.assertTrue(any(p.name.startswith('spinoff_worksheet_') for p in checked))
        self.assertEqual(before, {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in files})
        self.assertEqual(set(files), {p for p in self.root.rglob('*') if p.is_file()})

    def test_saved_screen_selection_mismatch_fails_without_substitution_or_validation(self):
        final = self.run_desk()
        report = self.saved_screen_fixture('DIFFERENT')
        original = final.read_bytes()
        with patch.object(numeric, 'verify') as hook, redirect_stdout(io.StringIO()), \
             self.assertRaisesRegex(d.PartFailure, 'Selected ticker DIFFERENT does not match'):
            d.saved_input_screen(report, final.parent)
        hook.assert_not_called()
        self.assertEqual(final.read_bytes(), original)

    def test_saved_screen_requires_matching_registry_and_completed_report(self):
        report = self.saved_screen_fixture()
        registry_path = report.parent/'registry.json'
        original = registry_path.read_bytes()
        for field in ('date', 'factors', 'incomplete'):
            with self.subTest(field=field), redirect_stdout(io.StringIO()):
                registry_path.write_bytes(original)
                registry = d.read_json(registry_path)
                text = report.read_text(encoding='utf-8')
                if field == 'date': registry['updated_at'] = '2025-01-01T00:00:00-05:00'
                elif field == 'factors': registry['factors']['sha256'] = 'e'*64
                else: report.write_text(text.replace('screens, not recommendations', 'in progress'), encoding='utf-8')
                d.write_json(registry_path, registry)
                with self.assertRaises(d.PartFailure):
                    d.saved_input_screen(report, self.root/'not_used')
                report.write_text(text, encoding='utf-8')

    def test_saved_screen_still_refuses_changed_saved_desk_evidence(self):
        final = self.run_desk()
        report = self.saved_screen_fixture()
        final.write_bytes(final.read_bytes()+b'unsupported changed output\n')
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(d.PartFailure, 'Final report:.*differs'):
            d.saved_input_screen(report, final.parent)

    def test_saved_screen_cli_is_explicit_and_normal_screen_and_replay_routes_unchanged(self):
        with patch.object(d, 'saved_input_screen') as saved, patch.object(d, 'run') as live, \
             patch.object(d, 'replay') as replay, redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(['screen', '--saved-screen', 'screen/report.md', '--reuse-desk', 'desk']), 0)
            saved.assert_called_once_with(Path('screen/report.md'), Path('desk'))
            live.assert_not_called(); replay.assert_not_called()
            self.assertEqual(d.main(['screen']), 0)
            live.assert_called_once_with('screen', root=d.ROOT/'runs/desk', start=None)
            self.assertEqual(d.main(['--replay', 'desk']), 0)
            replay.assert_called_once_with(Path('desk'))

    def test_saved_screen_cli_rejects_incomplete_or_mixed_mode_options(self):
        from contextlib import redirect_stderr
        for args in (['screen','--saved-screen','report'], ['screen','--reuse-desk','desk'],
                     ['TEST','--saved-screen','report','--reuse-desk','desk'],
                     ['screen','--saved-screen','report','--reuse-desk','desk','--start','2026-01-01'],
                     ['--replay','desk','--saved-screen','report','--reuse-desk','desk']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                d.main(args)
            self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
