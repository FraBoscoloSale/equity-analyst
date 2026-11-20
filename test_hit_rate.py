"""Fictional offline runs and reader rows; no real data, network or model."""
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import hit_rate as h

NOW = datetime.fromisoformat('2026-01-07T18:00:00-05:00')
ROWS = [{'date': '2026-01-01', 'close': 100, 'instrument': 'UNIT.K'},
        {'date': '2026-01-02', 'close': 110, 'instrument': 'UNIT.K'}]


class HitRateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.reader = Mock(return_value=ROWS)
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('No network/model'))
        guard.start(); self.addCleanup(guard.stop)
        guard = patch.object(h.closes_reader, 'dotenv_values', side_effect=AssertionError('No real workbook'))
        guard.start(); self.addCleanup(guard.stop)

    def run_record(self, name='unit', direction='up', view='2026-01-02T09:00:00-05:00', **extra):
        folder = self.root/name; folder.mkdir()
        record = {'status': 'completed', 'ticker': name.upper(), 'filing_time': '2026-01-02T08:00:00-05:00',
                  'view': {'time': view, 'direction': direction}, 'reaction': None, **extra}
        path = folder/'run.json'; path.write_text(json.dumps(record), encoding='utf-8')
        return path

    def table(self):
        return h.table(self.root, reader=self.reader, as_of=NOW)

    def test_one_row_per_completed_run(self):
        self.run_record('first'); self.run_record('second')
        self.run_record('unfinished', status='running')
        result = self.table()
        self.assertEqual(len([line for line in result.splitlines() if line.startswith(('FIRST |', 'SECOND |'))]), 2)
        self.assertNotIn('UNFINISHED |', result)
        for line in result.splitlines()[4:-1]:
            self.assertEqual(len(line.split(' | ')), 7)

    def test_reaction_unavailable_remains_unavailable(self):
        path = self.run_record(); before = path.read_bytes()
        self.reader.return_value = h.NA
        result = self.table()
        self.assertIn(' | not available | excluded | not available', result)
        self.assertEqual(path.read_bytes(), before)

    def test_new_reaction_added_only_in_memory_preserves_all_history(self):
        path = self.run_record(); before = path.read_bytes(); mtime = path.stat().st_mtime_ns
        self.reader.return_value = h.NA
        self.assertNotIn('UNIT.K; pre', self.table())
        self.reader.return_value = ROWS
        result = self.table()
        self.assertIn('UNIT.K; pre 2026-01-01=100; first 2026-01-02=110', result)
        self.assertIn(' | up | up (10.0%;', result)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertIsNone(json.loads(path.read_text())['reaction'])

    def test_agree(self):
        self.run_record()
        self.assertIn(' | agree | -', self.table())

    def test_disagree(self):
        self.run_record(direction='down')
        self.assertIn(' | disagree | -', self.table())

    def test_after_excluded(self):
        self.run_record(view='2026-01-02T16:00:01-05:00')
        result = self.table()
        self.assertIn(' | excluded | after', result)
        self.assertTrue(result.endswith('Agreements: 0 | Eligible runs with a reaction: 0 | Hit rate: not computed\n'))

    def test_equal_first_close_is_not_after(self):
        self.run_record(view='2026-01-02T16:00:00-05:00')
        self.assertIn(' | agree | -', self.table())

    def test_not_computed_excluded(self):
        self.run_record(direction='not computed')
        self.assertIn(' | not computed | up (', self.table())
        self.assertIn('Eligible runs with a reaction: 0', self.table())

    def test_unavailable_reaction_excluded(self):
        self.run_record(); self.reader.return_value = ROWS[:1]
        self.assertIn('Eligible runs with a reaction: 0', self.table())

    def test_exact_final_agreement_denominator(self):
        self.run_record('agree'); self.run_record('disagree', direction='down')
        self.run_record('late', view='2026-01-03T08:00:00-05:00')
        self.run_record('unknown', direction='not computed'); self.run_record('missing')
        self.reader.side_effect = lambda ticker, *args: h.NA if ticker == 'MISSING' else ROWS
        self.assertEqual(self.table().splitlines()[-1], 'Agreements: 1 | Eligible runs with a reaction: 2 | Hit rate: 50.00%')

    def test_no_model_network_and_cli_reads_supplied_folder(self):
        self.run_record()
        with patch.object(h.closes_reader, 'daily_closes_for_ticker', return_value=ROWS) as reader, patch.object(h, 'datetime') as clock:
            clock.now.return_value = NOW
            # Timestamp parsing still uses the real datetime class.
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.combine.side_effect = datetime.combine
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(h.main([str(self.root)]), 0)
            self.assertIn('Agreements: 1 | Eligible runs with a reaction: 1', out.getvalue())
            reader.assert_called_once()

    def test_historical_view_sidecar_fields_and_bytes_unchanged(self):
        path = self.run_record(); run = json.loads(path.read_text()); view = run.pop('view')
        path.write_text(json.dumps(run)); view_path = path.parent/'view.json'; view_path.write_text(json.dumps(view))
        before = {p: p.read_bytes() for p in (path, view_path)}
        self.table()
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(json.loads(view_path.read_text()), view)

    def test_run_start_and_file_mtime_never_substitute_view_time(self):
        path = self.run_record(view=None, run_time='2026-01-02T09:00:00-05:00')
        self.assertIn('Eligible runs with a reaction: 0', self.table())
        self.assertIsNone(json.loads(path.read_text())['view']['time'])

    def test_when_issued_rejected(self):
        self.run_record(); self.reader.return_value = [dict(row, instrument='UNITWI.K') for row in ROWS]
        self.assertIn('Eligible runs with a reaction: 0', self.table())

    def test_first_close_strictly_after_filing(self):
        path = self.run_record(view='2026-01-03T09:00:00-05:00')
        data = json.loads(path.read_text()); data['filing_time'] = '2026-01-02T16:00:00-05:00'; path.write_text(json.dumps(data))
        self.reader.return_value = ROWS+[{'date': '2026-01-05', 'close': 120, 'instrument': 'UNIT.K'}]
        self.assertIn('first 2026-01-05=120', self.table())

    def test_reader_future_close_not_used_before_its_time(self):
        self.run_record()
        text = h.table(self.root, reader=self.reader, as_of=datetime.fromisoformat('2026-01-02T15:00:00-05:00'))
        self.assertIn('Eligible runs with a reaction: 0', text)

    def test_saved_reaction_not_replaced(self):
        evidence = {'instrument': 'UNIT.K', 'before': {'date': '2026-01-01', 'close': 100},
                    'after': {'date': '2026-01-02', 'close': 110}, 'direction': 'up'}
        path = self.run_record(reaction=evidence); before = path.read_bytes()
        self.assertIn(' | agree | -', self.table())
        self.reader.return_value = [ROWS[0], dict(ROWS[1], close=120)]
        self.assertIn('Eligible runs with a reaction: 0', self.table())
        self.assertEqual(path.read_bytes(), before)

    def test_flat_direction_and_reaction_agree(self):
        self.run_record(direction='flat')
        self.reader.return_value = [ROWS[0], dict(ROWS[1], close=100)]
        self.assertIn(' | flat | flat (0%;', self.table())
        self.assertIn(' | agree | -', self.table())

    def test_concurrent_historical_change_refuses_results(self):
        path = self.run_record()
        def changed_history(*args):
            record = json.loads(path.read_text())
            record['view']['direction'] = 'down'
            path.write_text(json.dumps(record))  # Simulated outside writer, not the command.
            return ROWS
        self.reader.side_effect = changed_history
        with self.assertRaisesRegex(ValueError, 'historical run/view bytes changed'):
            self.table()


if __name__ == '__main__':
    unittest.main()
