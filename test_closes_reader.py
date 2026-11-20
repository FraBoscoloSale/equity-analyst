"""Offline closes-reader checks using a temporary export, never the real .env."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager

from openpyxl import Workbook

import closes_reader as reader


@contextmanager
def export_file(instruments):
    """A made-up export; never read or write the configured real workbook."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "closes.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Instrument", "Date", "Price Close"])
        for instrument in instruments:
            for day, close in ((2, 10), (5, 9), (6, 8), (7, 8), (8, 7)):
                sheet.append([instrument, datetime(2026, 10, day), close])
        workbook.save(path)
        workbook.close()
        before = path.read_bytes()
        with patch.object(reader, "dotenv_values", return_value={"CLOSES_FILE": str(path)}):
            yield path
        assert path.read_bytes() == before, "Export was modified"


class ClosesReaderTests(unittest.TestCase):
    @contextmanager
    def volume_export(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'volumes.xlsx'
            workbook = Workbook()
            for row in rows:
                workbook.active.append(row)
            workbook.save(path)
            workbook.close()
            before = path.read_bytes()
            with patch.object(reader, 'dotenv_values', return_value={'CLOSES_FILE': str(path)}):
                yield
            self.assertEqual(path.read_bytes(), before)

    def test_numeric_volume(self):
        with self.volume_export([
            ['Instrument', 'Date', 'Price Close', 'Volume'],
            ['EXAMPLE.K', datetime(2026, 10, 27), 177.61, 393514],
        ]):
            self.assertEqual(reader.daily_close_volumes('EXAMPLE', '2026-10-27', '2026-10-27'),
                             [{'date': '2026-10-27', 'close': 177.61, 'volume': 393514.0,
                               'instrument': 'EXAMPLE.K'}])

    def test_later_header_introduces_volume_and_updates_column_order(self):
        with self.volume_export([
            ['Instrument', 'Date', 'Price Close'],
            ['FIRST.O', datetime(2026, 10, 27), 100],
            ['Instrument', 'Date', 'Price Close', 'Volume'],
            ['EXAMPLE.K', datetime(2026, 10, 27), 20, 1000],
            [' Volume ', ' Price Close ', ' Instrument ', ' Date '],
            [2000, 22, 'EXAMPLE.K', datetime(2026, 10, 28)],
        ]):
            rows = reader.daily_close_volumes('EXAMPLE', '2026-10-27', '2026-10-28')
            self.assertEqual([(r['close'], r['volume']) for r in rows], [(20.0, 1000.0), (22.0, 2000.0)])
            self.assertEqual(reader.daily_closes('FIRST.O', '2026-10-27', '2026-10-28'),
                             [{'date': '2026-10-27', 'close': 100.0}])

    def test_missing_and_invalid_volume_remain_unavailable(self):
        for volume in (None, 'invalid', -1, True, 'NaN', 'inf'):
            with self.subTest(volume=volume), self.volume_export([
                ['Instrument', 'Date', 'Price Close', 'Volume'],
                ['EXAMPLE.K', datetime(2026, 10, 27), 20, volume],
            ]):
                rows = reader.daily_close_volumes('EXAMPLE', '2026-10-27', '2026-10-27')
                self.assertIsNone(rows[0]['volume'])
                self.assertEqual(rows[0]['close'], 20.0)

    def test_close_only_export_preserves_closes_and_missing_volume(self):
        with export_file(['EXAMPLE.K']):
            closes = reader.daily_closes('EXAMPLE.K', '2026-10-02', '2026-10-10')
            rows = reader.daily_close_volumes('EXAMPLE', '2026-10-02', '2026-10-10')
            self.assertEqual(closes, [{'date': row['date'], 'close': row['close']} for row in rows])
            self.assertTrue(all(row['volume'] is None for row in rows))

    def test_sec_ticker_resolves_generic_sole_vendor_suffix(self):
        for ticker, instrument in (("MSGR", "MSGR.K"), ("EXAMPLE", "EXAMPLE.O"),
                                   ("CLASS.B", "CLASS.B.N"), ("PLAIN", "PLAIN")):
            with self.subTest(ticker=ticker), export_file([instrument]):
                self.assertEqual(reader.resolve_export_instrument(ticker), instrument)
                rows = reader.daily_closes_for_ticker(ticker, "2026-10-02", "2026-10-10")
                self.assertEqual(rows[0], {"date": "2026-10-02", "close": 10.0,
                                           "instrument": instrument})
                if ticker != instrument:
                    self.assertEqual(reader.daily_closes(ticker, "2026-10-02", "2026-10-10"),
                                     reader.NOT_AVAILABLE)

    def test_zero_or_multiple_instruments_never_guess_even_with_exact_match(self):
        for instruments in (["OTHER.K"], ["TEST.K", "TEST.O"], ["TEST", "TEST.K"]):
            with self.subTest(instruments=instruments), export_file(instruments):
                self.assertEqual(reader.resolve_export_instrument("TEST"), reader.NOT_AVAILABLE)
                self.assertEqual(reader.daily_closes_for_ticker("TEST", "2026-10-02", "2026-10-10"),
                                 reader.NOT_AVAILABLE)

    def test_when_issued_variants_never_selected(self):
        variants = ["TEST.WI", "TEST.W", "TESTw.K", "TEST.WI.K", "TEST-WI.K",
                    "TEST_w.K", "TEST WI", "TESTwhenissued.K"]
        with export_file(variants):
            self.assertEqual(reader.resolve_export_instrument("TEST"), reader.NOT_AVAILABLE)
            for instrument in variants:
                self.assertEqual(reader.resolve_export_instrument(instrument), reader.NOT_AVAILABLE)
        with export_file(variants + ["TEST.K"]):
            self.assertEqual(reader.resolve_export_instrument("TEST"), "TEST.K")

    def test_duplicate_rows_are_one_instrument(self):
        with export_file(["TEST.K", "TEST.K"]):
            self.assertEqual(reader.resolve_export_instrument(" test "), "TEST.K")

    def test_ambiguity_is_checked_across_all_sheets(self):
        with export_file(['TEST.K']) as path:
            # Build a separate temporary workbook to preserve the fixture's
            # read-only assertion on its original export.
            other = path.with_name('multiple_sheets.xlsx')
            workbook = Workbook()
            for sheet, instrument in ((workbook.active, 'TEST.K'),
                                      (workbook.create_sheet(), 'TEST.O')):
                sheet.append(['Instrument', 'Date', 'Price Close'])
                sheet.append([instrument, datetime(2026,10,2), 10])
            workbook.save(other)
            workbook.close()
            before=other.read_bytes()
            with patch.object(reader, 'dotenv_values', return_value={'CLOSES_FILE':str(other)}):
                self.assertEqual(reader.resolve_export_instrument('TEST'),reader.NOT_AVAILABLE)
            self.assertEqual(other.read_bytes(),before)

    def test_repeated_header_between_export_blocks_is_ignored_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "closes.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Instrument", "Date", "Price Close"])
            sheet.append(["FIRST.O", datetime(2026, 10, 27), 100])
            sheet.append([" Instrument ", " Date ", " Price Close "])
            sheet.append(["SECOND.K", datetime(2026, 10, 28), 22])
            sheet.append(["SECOND.K", datetime(2026, 10, 27), 20])
            workbook.save(path)
            workbook.close()
            before = path.read_bytes()
            with patch.object(reader, "dotenv_values", return_value={"CLOSES_FILE": str(path)}):
                self.assertEqual(reader.daily_closes("FIRST.O", "2026-10-27", "2026-10-28"),
                                 [{"date": "2026-10-27", "close": 100.0}])
                self.assertEqual(reader.daily_closes("second.k", "2026-10-27", "2026-10-28"),
                                 [{"date": "2026-10-27", "close": 20.0},
                                  {"date": "2026-10-28", "close": 22.0}])
                self.assertEqual(reader.last_close_before("SECOND.K", "2026-10-28T16:00:00"), 20.0)
                self.assertEqual(reader.daily_closes("SECOND.K", "2026-10-29", "2026-10-30"),
                                 "not available")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
