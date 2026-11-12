"""Offline integration checks; the closes reader is always mocked, never run."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_team as team
import agent_team_ui as ui
from test_agent_team import ScriptedClient
from test_research_roles import setup_run


class FilingValuationTests(unittest.TestCase):
    def test_flow_uses_only_selected_close_and_finishes_when_unavailable(self):
        for close in (42.5, 'not available'):
            with self.subTest(close=close), tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
                folder = Path(tmp)
                observed_prices = []

                def tools(args, settings, on_step):
                    self.assertIsNone(args.price)  # Legacy --price cannot enter valuation.
                    setup_run(folder)
                    filings = json.loads((folder / 'filings.json').read_text())
                    filings['8-K'].update(accepted='2025-07-01 16:15:00',
                                         accepted_source='https://www.sec.gov/test-index.html')
                    team.roles.write_json(folder / 'filings.json', filings)
                    for step in range(1, 8):
                        on_step(step, 'input', {'ticker': args.ticker, 'price': args.price})
                        if step == 5:
                            observed_prices.append(args.price)
                            facts = json.loads((folder / 'facts.json').read_text())
                            team.roles.write_json(folder / 'metrics.json', team.desk.calculate(facts, args.price))
                        on_step(step, 'output', {'run_folder': str(folder)})
                    return folder

                client = ScriptedClient()
                with patch.object(team.desk, 'run_seven_steps', side_effect=tools), \
                     patch.object(team, 'last_close_before', return_value=close) as reader, \
                     patch.object(team, 'NodeClient', return_value=client):
                    flow = team.DeskFlow({'API_KEY': 'test-key', 'SEC_USER_AGENT': 'test@example.test'})
                    result = flow.kickoff(inputs={'ticker': 'TEST', 'price': 999})
                reader.assert_called_once_with('TEST', '2025-07-01 16:15:00')
                expected = close if isinstance(close, float) else None
                self.assertEqual(observed_prices, [expected])
                self.assertTrue(Path(result).is_file())
                run = json.loads((folder / 'run.json').read_text())
                saved = json.loads((folder / 'input_facts.json').read_text())
                self.assertEqual(run['price'], expected)
                self.assertEqual(saved['price'], expected)
                self.assertEqual(saved['accepted_source'], 'https://www.sec.gov/test-index.html')
                financial = next(call[2] for call in client.calls if call[0] == team.roles.FINANCIAL)
                research = next(call[2] for call in client.calls if call[0] == team.roles.RESEARCH)
                self.assertNotIn('price', research['evidence'])
                if expected is not None:
                    self.assertEqual(financial['evidence']['price']['val'], expected)
                    self.assertIn('CLOSES_FILE:', financial['evidence']['price']['price_source'])
                else:
                    self.assertEqual(financial['evidence']['price']['kind'], 'missing_input')
                    self.assertIsNone(financial['evidence']['subject.pe']['value'])
                    self.assertEqual(saved['status'], 'not available')
                self.assertNotIn('daily_closes', json.dumps(client.calls))

    def test_override_retains_original_accepted_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            setup_run(folder)
            team.roles.write_json(folder / 'filings.json', {'8-K': {
                'accepted': '2025-07-01 16:15:00', 'accepted_source': 'https://www.sec.gov/test-index.html'}})
            with patch.object(team, 'last_close_before', return_value=25) as reader:
                saved = team.filing_valuation(folder, 'TEST', '2025-07-02T09:00:00')
            reader.assert_called_once_with('TEST', '2025-07-02T09:00:00')
            self.assertEqual(saved['accepted'], '2025-07-01 16:15:00')
            self.assertEqual(saved['effective_filing_time'], '2025-07-02T09:00:00')
            self.assertEqual(saved['price'], 25)

    def test_missing_filing_does_not_call_reader_or_keep_manual_price(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            setup_run(folder)
            team.roles.write_json(folder / 'filings.json', {})
            with patch.object(team, 'last_close_before') as reader:
                saved = team.filing_valuation(folder, 'TEST')
            reader.assert_not_called()
            self.assertIsNone(saved['price'])
            self.assertEqual(saved['status'], 'not available')

    def test_browser_filing_override_validation(self):
        command = ui.command_for({'ticker': 'TEST', 'filed': '2025-07-02T09:00:00'})
        self.assertEqual(command[-2:], ['--filed', '2025-07-02T09:00:00'])
        for filed in ('2025-02-30T12:00:00', '2025-07-02', '2025-07-02T09:00:00Z', 123):
            with self.subTest(filed=filed), self.assertRaises(ui.InputError):
                ui.command_for({'ticker': 'TEST', 'filed': filed})


if __name__ == '__main__':
    unittest.main()
