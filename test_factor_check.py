"""Fictional, offline completed reports; no user data, reader, network or model."""
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import factor_check as f


SPEC = '''Window: 90 days post-distribution
Factors:
1. Small Relative to Parent
   - Direction: Smaller is better
   - Source: Fictional prespecified rule
   - Weight: 1
2. Insider Buying / Management Alignment
   - Direction: Higher insider buying is better
   - Source: Fictional prespecified rule
   - Weight: 1
'''


class FactorCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.rules = self.root/'factors.txt'; self.rules.write_text(SPEC, encoding='utf-8')
        self.runs = self.root/'runs'; self.runs.mkdir()
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('No network'))
        guard.start(); self.addCleanup(guard.stop)
        self.baseline = self.report('baseline', '2026-01-01', '2025-12-31', (100, 100))
        self.report('entry', '2026-01-03', '2026-01-02', (100, 100))
        self.exit = self.report('exit', '2026-01-05', '2026-01-04', (110, 90))

    def report(self, name, stamp, price_date, prices, values=(('0.1', '2'), ('0.5', '0'))):
        digest = hashlib.sha256(self.rules.read_bytes()).hexdigest()
        lines = [f'Run date: {stamp}T10:00:00-05:00 | price date: {price_date}',
                 f'Window: 90 calendar days post-distribution. Factor file SHA256: {digest}.',
                 '## In their window, ordered by rank']
        for i, (price, measure) in enumerate(zip(prices, values), 1):
            lines += [f'{i}. Fictional {i} | CIK {i} | total points 0',
                      f'- Small Relative to Parent: value {measure[0]}; points 0; weight 1.0; Smaller is better; Fictional prespecified rule',
                      f'- Insider Buying / Management Alignment: value {measure[1]}; points 0; weight 1.0; Higher insider buying is better; Fictional prespecified rule']
            if price is not None:
                lines += [f'- latest official close: {price} | price date {price_date} | export instrument UNIT{i}.K']
        lines += ['## Other stages', 'screens, not recommendations']
        path = self.runs/f'report_{name}.md'; path.write_text('\n'.join(lines)+'\n', encoding='utf-8')
        return path

    def check(self):
        return f.check(self.runs, self.rules)

    def test_factor_file_read_not_hard_coded(self):
        self.assertIn('Valid comparisons: 2\n', self.check())
        previous_digest = hashlib.sha256(self.rules.read_bytes()).hexdigest()
        changed = SPEC.replace('Weight: 1', 'Weight: 0', 1)
        self.rules.write_text(changed, encoding='utf-8')
        digest = hashlib.sha256(self.rules.read_bytes()).hexdigest()
        text = self.baseline.read_text()
        text = text.replace(previous_digest, digest)
        text = text.replace('weight 1.0; Smaller', 'weight 0.0; Smaller')
        self.baseline.write_text(text)
        result = self.check()
        self.assertIn('weight: 0.0 | valid comparisons: 0', result)
        self.assertIn('Valid comparisons: 1\n', result)

    def test_missing_factor_excluded_from_count(self):
        self.baseline.write_text(self.baseline.read_text().replace('value 0.5;', 'value not known;'))
        self.assertIn('Valid comparisons: 1\n', self.check())

    def test_missing_return_excluded_from_count(self):
        self.exit = self.report('exit', '2026-01-05', '2026-01-04', (110, None))
        self.assertIn('Valid comparisons: 0\n', self.check())

    def test_exact_valid_count_printed_by_cli(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = f.main([str(self.runs), '--factors', str(self.rules)])
        self.assertEqual(code, 0)
        self.assertEqual([line for line in output.getvalue().splitlines() if line.startswith('Valid comparisons:')], ['Valid comparisons: 2'])

    def test_saved_runs_views_and_timestamps_never_written(self):
        paths = list(self.runs.iterdir())+[self.rules]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        self.check()
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})
        self.assertEqual(set(self.runs.iterdir()), set(paths)-{self.rules})

    def test_conflicting_saved_view_cannot_replace_original(self):
        original = self.baseline.read_bytes()
        (self.runs/'report_conflict.md').write_bytes(original.replace(b'value 0.1;', b'value 0.9;'))
        with self.assertRaisesRegex(ValueError, 'Conflicting saved historical view'):
            self.check()
        self.assertEqual(self.baseline.read_bytes(), original)

    def test_returns_never_choose_or_change_factors(self):
        rules_before, view_before = self.rules.read_bytes(), self.baseline.read_bytes()
        positive = self.check()
        self.report('exit', '2026-01-05', '2026-01-04', (90, 110))
        negative = self.check()
        self.assertIn('concordant: 1 | discordant: 0', positive)
        self.assertIn('concordant: 0 | discordant: 1', negative)
        self.assertEqual(rules_before, self.rules.read_bytes())
        self.assertEqual(view_before, self.baseline.read_bytes())
        self.assertEqual(positive.splitlines()[0], negative.splitlines()[0])

    def test_outcomes_cannot_be_prices_observed_before_view(self):
        self.exit.unlink()
        (self.runs/'report_entry.md').unlink()
        self.report('past', '2025-12-30', '2025-12-29', (90, 110))
        self.assertIn('Valid comparisons: 0\n', self.check())

    def test_wrong_pre_return_spec_hash_excludes_view(self):
        text = self.baseline.read_text()
        self.baseline.write_text(text.replace(hashlib.sha256(self.rules.read_bytes()).hexdigest(), '0'*64))
        self.assertIn('Valid comparisons: 0\n', self.check())

    def test_factor_ties_excluded(self):
        self.baseline.write_text(self.baseline.read_text().replace('value 0.5;', 'value 0.1;'))
        self.assertIn('Valid comparisons: 1\n', self.check())

    def test_duplicate_reports_not_independent_comparisons(self):
        (self.runs/'report_duplicate.md').write_bytes(self.baseline.read_bytes())
        result = self.check()
        self.assertIn('Valid comparisons: 2\n', result)
        self.assertIn('not independent trials', result)
        self.assertIn('no statistical significance is claimed', result)

    def test_no_comparisons_disclosed_for_empty_folder(self):
        empty = self.root/'empty'; empty.mkdir()
        self.assertIn('Valid comparisons: 0\n', f.check(empty, self.rules))


if __name__ == '__main__':
    unittest.main()
