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
