"""Offline continuation: saved data and completed responses are never refetched."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import agent_team as team
import research_roles as roles
from test_agent_team import ScriptedClient, saved_fixture


class ResumeTests(unittest.TestCase):
    def test_resume_reuses_completed_agent_and_data_without_collection(self):
        agents = (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR)
        for completed_agents in (0, 1, 2, 3):
            with self.subTest(completed_agents=completed_agents), tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
                folder = Path(tmp)
                saved_fixture(folder)
                roles.write_json(folder / 'seven_steps_output.json', {'steps': list(range(1, 8))})
                scripted = ScriptedClient()

                def model(*args, **kwargs):
                    body = kwargs['json']
                    envelope = json.loads(body['messages'][1]['content'])
                    # The transport announces the node in its saved input filename.
                    role = next(r for r in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR, roles.MANAGER)
                                if roles.load_instructions(r) in body['messages'][0]['content'])
                    output = scripted.call(role, envelope['task'], envelope['context'], envelope['expected_output'])
                    return Mock(ok=True, json=lambda: {'choices': [{'finish_reason': 'stop',
                                'message': {'content': json.dumps(output)}}]})

                with patch.object(team.requests, 'post', side_effect=model):
                    initial = team.TeamManager(folder, team.NodeClient('offline', folder))
                    for role in agents[:completed_agents]:
                        initial.specialist(role)
                    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
                    scripted.calls.clear()

                    def verified(source, output):
                        path = output / 'verified_report.md'
                        path.write_text('Offline verified fixture', encoding='utf-8')
                        return path

                    with patch.object(team.desk, 'run_seven_steps', side_effect=AssertionError('collection')), \
                         patch.object(team, 'filing_valuation', side_effect=AssertionError('price collection')), \
                         patch.object(team.Verifier, 'run', side_effect=verified):
                        result = team.resume_saved(folder, 'offline')
                    self.assertEqual(result, folder / 'verified_report.md')
                    self.assertTrue((folder / 'view.json').is_file())
                    called = [c[0] for c in scripted.calls]
                    self.assertEqual(called.count(roles.RESEARCH), 0 if completed_agents >= 1 else 1)
                    self.assertEqual(called.count(roles.FINANCIAL), 0 if completed_agents >= 2 else 1)
                    # This fixture deliberately needs a bounded Advisor revision.
                    self.assertEqual(called.count(roles.ADVISOR), 1 if completed_agents >= 3 else 2)
                    for name, data in before.items():
                        self.assertEqual((folder / name).read_bytes(), data, name)

    def test_resume_requires_completed_data_and_never_collects(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            roles.write_json(folder / 'seven_steps_output.json', {'steps': [1, 2]})
            with patch.object(team.requests, 'post') as post, self.assertRaisesRegex(roles.RoleError, 'seven'):
                team.resume_saved(folder, 'offline')
            post.assert_not_called()

    def test_resume_cli_routes_without_sec_credentials_or_flow(self):
        with patch.object(team, 'dotenv_values', return_value={'API_KEY': 'offline'}), \
             patch.object(team, 'resume_saved', return_value=Path('saved/verified_report.md')) as resume, \
             patch.object(team, 'DeskFlow', side_effect=AssertionError('must not run collection')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(team.main(['--resume', 'saved']), 0)
            resume.assert_called_once_with(Path('saved'), 'offline')

    def test_resume_refuses_failed_saved_response_instead_of_retrying(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            roles.write_json(folder / 'research_analyst_1_output.json', {'error': 'failed'})
            with patch.object(team.requests, 'post') as post, self.assertRaisesRegex(roles.RoleError, 'refusing'):
                team.ResumeClient('offline', folder).call(roles.RESEARCH, 'task', {}, {})
            post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
