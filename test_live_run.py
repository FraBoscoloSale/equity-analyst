"""Offline live-run plumbing: fictional issuer, clocks, filings and model stubs."""
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_team as team
import equity_desk as desk
import live_run as live
from test_agent_team import saved_fixture
from test_equity_desk import COMPANY, fixture, record

FINAL = datetime.fromisoformat('2026-01-02T14:03:04+00:00')
ADVISOR = {'direction': 'up', 'recommendation': 'Hold', 'thesis': 'Original filing-supported view.',
           'reason': 'Original reason.', 'deciding_evidence_id': 'metric'}


class LiveRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.saved = self.root/'saved'; self.saved.mkdir()
        self.run = {'company': COMPANY, 'run_time': '2026-01-01T00:00:00+00:00',
                    'filing_time': {'accepted': '2026-01-02 08:00:00',
                                    'effective_filing_time': '2026-01-02 08:30:00',
                                    'accepted_source': 'saved SEC index', 'timezone': 'America/New_York'}}
        (self.saved/'run.json').write_text(json.dumps(self.run))
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('No network/model'))
        guard.start(); self.addCleanup(guard.stop)

    def test_default_output_still_research_data(self):
        args = SimpleNamespace(ticker='TEST', peers=[], price=None, step=1)
        with patch.object(desk, 'ROOT', self.root), patch.object(desk, 'SECClient'), \
             patch.object(desk, 'resolve', return_value=COMPANY), redirect_stdout(io.StringIO()):
            folder = desk.run_seven_steps(args, {'SEC_USER_AGENT': 'offline@example.test'})
        self.assertEqual(folder.parent, self.root/'research_data')
        self.assertTrue((folder/'run.json').is_file())

    def test_requested_output_root_used_by_existing_folder_creation(self):
        requested = self.root/'runs'/'live'
        args = SimpleNamespace(ticker='TEST', peers=[], price=None, step=1, output_root=requested)
        with patch.object(desk, 'SECClient'), patch.object(desk, 'resolve', return_value=COMPANY), redirect_stdout(io.StringIO()):
            folder = desk.run_seven_steps(args, {'SEC_USER_AGENT': 'offline@example.test'})
        self.assertEqual(folder.parent, requested)

    def test_cli_passes_output_root_through_flow_state(self):
        requested = self.root/'runs'/'live'
        flow = Mock(); flow.kickoff.return_value = 'fictional_verified_report.md'
        with patch.object(team, 'dotenv_values', return_value={'API_KEY': 'offline', 'SEC_USER_AGENT': 'offline@example.test'}), \
             patch.object(team, 'DeskFlow', return_value=flow), redirect_stdout(io.StringIO()):
            self.assertEqual(team.main(['TEST', '--output-root', str(requested)]), 0)
        self.assertEqual(flow.kickoff.call_args.kwargs['inputs']['output_root'], str(requested))

    def test_view_uses_actual_finalization_clock_and_keeps_accepted_time(self):
        with patch.object(live, 'datetime') as clock:
            clock.now.return_value = FINAL
            path = live.freeze_view(self.saved, ADVISOR)
        view = json.loads(path.read_text())
        self.assertEqual(view['time'], FINAL.isoformat())
        self.assertNotEqual(view['time'], self.run['run_time'])
        self.assertEqual(view['filing_accepted_time'], '2026-01-02 08:00:00')
        self.assertEqual(view['filing_time'], '2026-01-02 08:30:00')
        self.assertEqual(view['ticker'], COMPANY['ticker'])
        self.assertEqual(view['reaction'], 'not available')
        self.assertEqual(view['original_view']['thesis'], ADVISOR['thesis'])

    def test_direction_enum_exact_and_no_recommendation_inference(self):
        for direction in live.DIRECTIONS:
            folder = self.root/direction; folder.mkdir()
            (folder/'run.json').write_text(json.dumps(self.run))
            view = live.freeze_view(folder, dict(ADVISOR, direction=direction), FINAL)
            self.assertEqual(json.loads(view.read_text())['direction'], direction)
        for invalid in ('Buy', 'UP', '', None):
            with self.assertRaises(ValueError):
                live.freeze_view(self.saved, dict(ADVISOR, direction=invalid), FINAL)

    def test_view_is_atomic_create_once_and_refuses_overwrite(self):
        path = live.freeze_view(self.saved, ADVISOR, FINAL); before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            live.freeze_view(self.saved, dict(ADVISOR, direction='down'))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.saved.glob('.publish_*.tmp')))

    def test_manager_freezes_at_finalization_and_refuses_second_attempt_before_calls(self):
        manager = object.__new__(team.TeamManager)
        manager.output = self.saved
        manager.results = {team.roles.ADVISOR: ADVISOR}
        manager.supplied = {}; manager.catalog = Mock(); manager.catalog.snapshot.return_value = {}
        manager.review = Mock(return_value={'checks': {part: {'passed': True} for part in team.roles.PARTS}})
        manager.compile_report = Mock(return_value=self.saved/'compiled_report.md')
        with patch.object(live, 'datetime') as clock:
            clock.now.return_value = FINAL
            manager.finish()
        self.assertEqual(json.loads((self.saved/'view.json').read_text())['time'], FINAL.isoformat())
        with self.assertRaises(FileExistsError):
            manager.finish()
        manager.review.assert_called_once()

    def test_verification_and_worksheet_links_do_not_change_history(self):
        path = live.freeze_view(self.saved, ADVISOR, FINAL); before = path.read_bytes()
        for kind, filename in [('verified_report', 'verified_report.md'), ('worksheet', 'worksheet.md')]:
            artifact = self.saved/filename; artifact.write_text('Fictional completed artifact.')
            link = live.reference(self.saved, kind, artifact)
            self.assertEqual(json.loads(link.read_text())['path'], str(artifact.resolve()))
            self.assertEqual(live.reference(self.saved, kind, artifact), link)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list((self.saved/'live_references').glob('*.json'))), 2)

    def test_verify_only_outside_research_data_preserves_original_view(self):
        saved_fixture(self.saved)
        (self.saved/'compiled_report.md').write_text('Filings read: fictional\nNo numeric claims.\n')
        view = live.freeze_view(self.saved, ADVISOR, FINAL); before = view.read_bytes()
        original_files = {p.name: p.read_bytes() for p in self.saved.iterdir() if p.is_file()}
        def verified(source, output):
            path = output/'verified_report.md'; path.write_text('Fictional verification.'); return path
        verifier = Mock(); verifier.run.side_effect = verified
        requested = self.root/'runs'/'live'
        with patch.object(team, 'Verifier', return_value=verifier), patch.object(team, 'NodeClient'), \
             patch.object(team, 'DeskFlow') as flow:
            result = team.verify_only(self.saved, 'offline', requested)
        self.assertEqual(result.parent.parent, requested)
        self.assertEqual(view.read_bytes(), before)
        self.assertEqual(original_files, {name: (self.saved/name).read_bytes() for name in original_files})
        self.assertEqual((self.saved/'verified_report.md').read_bytes(), result.read_bytes())
        self.assertTrue(list((self.saved/'live_references').glob('verified_report_*.json')))
        flow.assert_not_called()

    def test_filing_history_cannot_be_rewritten_after_freeze(self):
        live.freeze_view(self.saved, ADVISOR, FINAL)
        before = (self.saved/'run.json').read_bytes()
        with self.assertRaises(FileExistsError):
            team.filing_valuation(self.saved, 'TEST', '2026-01-03T10:00:00')
        self.assertEqual((self.saved/'run.json').read_bytes(), before)

    def test_advisor_direction_is_structured_in_existing_call(self):
        manager = object.__new__(team.TeamManager)
        schema = manager.specialist_schema(team.roles.ADVISOR, {}, list(team.roles.PARTS[3:]))
        self.assertEqual(schema['properties']['direction']['enum'], list(live.DIRECTIONS))
        self.assertIn('direction', schema['required'])
        with self.assertRaises(team.roles.RoleError):
            team.roles.validate_shape({'direction': 'Buy'}, schema)

    def test_new_saved_sec_inputs_preserve_existing_mapped_facts(self):
        raw = fixture()
        raw['facts']['us-gaap']['Depreciation'] = {'units': {'USD': [record(20)]}}
        client = Mock(); client.json.return_value = raw
        expected = desk.extract_facts(raw, COMPANY)
        result = desk.collect_facts(client, COMPANY, self.saved)
        extra = result.pop('worksheet_inputs')
        self.assertEqual(result, expected)
        self.assertTrue(any(row['tag'] == 'us-gaap:Depreciation' and row['val'] == 20 and
                            row['unit'] == 'USD' and row['scale'] == 0 for row in extra))
        saved = json.loads((self.saved/'facts.json').read_text())
        self.assertEqual(saved['concepts'], expected['concepts'])

    def test_quarterly_and_conflicting_annual_extra_records_not_invented(self):
        raw = {'facts': {'us-gaap': {'Depreciation': {'units': {'USD': [
            record(5, start='2025-04-01'), record(20), record(21)]}}}}}
        self.assertEqual(desk.worksheet_input_records(raw, COMPANY), [])


if __name__ == '__main__':
    unittest.main()
