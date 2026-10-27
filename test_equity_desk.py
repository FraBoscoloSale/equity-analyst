"""Offline regression tests for financial alignment and EDGAR source selection."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import equity_desk as desk


COMPANY = {"ticker": "TEST", "cik": "0000000001", "name": "Synthetic Issuer"}


def record(value, end="2025-06-30", start="2024-07-01", **kwargs):
    row = {"val": value, "end": end, "form": "10-K", "fy": 2025, "fp": "FY",
           "filed": "2025-08-01", "accn": "0000000001-25-000001", **kwargs}
    if start:
        row["start"] = start
    return row


def fixture():
    facts = {}
    for key, (_, kind, tags) in desk.CONCEPTS.items():
        unit = "USD/shares" if key == "diluted_eps" else "USD"
        current = {"revenue": 120, "operating_income": 30, "net_income": 20,
                   "diluted_eps": 2, "equity": 60, "long_term_debt": 25,
                   "current_debt": 5, "cash_from_operations": 35, "capex": 10}[key]
        prior = 100 if key == "revenue" else 40 if key == "equity" else current / 2
        facts[tags[0]] = {"units": {unit: [record(current, start="2024-07-01" if kind == "duration" else None),
            record(prior, end="2024-06-30", start="2023-07-01" if kind == "duration" else None)]}}
    return {"facts": {"us-gaap": facts}}


class FinancialTests(unittest.TestCase):
    def test_fresh_tag_wins_and_every_fact_uses_revenue_period(self):
        raw = fixture()
        taxonomy = raw["facts"]["us-gaap"]
        fresh = taxonomy.pop(desk.CONCEPTS["revenue"][2][0])
        taxonomy["Revenues"] = fresh
        taxonomy[desk.CONCEPTS["revenue"][2][0]] = {"units": {"USD": [
            record(999, end="2023-06-30", start="2022-07-01", filed="2026-01-01")]}}
        # A quarterly duration with the same end and a newer filing is not a full year.
        taxonomy["OperatingIncomeLoss"]["units"]["USD"].append(record(888, start="2025-04-01", filed="2026-01-01"))
        facts = desk.extract_facts(raw, COMPANY)
        self.assertEqual(facts["concepts"]["revenue"]["current"]["tag"], "us-gaap:Revenues")
        self.assertEqual(facts["concepts"]["operating_income"]["current"]["val"], 30)
        self.assertEqual(facts["concepts"]["revenue"]["prior"]["val"], 100)
        for concept in facts["concepts"].values():
            self.assertEqual(concept["current"]["end"], "2025-06-30")
            self.assertEqual(concept["prior"]["end"], "2024-06-30")

    def test_prior_year_can_use_predecessor_tag_with_provenance(self):
        raw = fixture()
        tax = raw["facts"]["us-gaap"]
        revenue = tax[desk.CONCEPTS["revenue"][2][0]]["units"]["USD"]
        tax["Revenues"] = {"units": {"USD": [revenue.pop()]}}
        facts = desk.extract_facts(raw, COMPANY)
        self.assertEqual(facts["concepts"]["revenue"]["prior"]["tag"], "us-gaap:Revenues")

    def test_all_calculations_and_current_debt_included(self):
        facts = desk.extract_facts(fixture(), COMPANY)
        with redirect_stdout(io.StringIO()):
            metrics = desk.calculate(facts, 30)
        expected = {"revenue_growth": .2, "operating_margin": .25,
                    "return_on_average_equity": .4, "debt_to_equity": .5,
                    "free_cash_flow": 25, "pe": 15}
        for key, value in expected.items():
            self.assertAlmostEqual(metrics[key]["value"], value)
        facts["concepts"]["current_debt"]["current"] = None
        facts["concepts"]["diluted_eps"]["current"]["val"] = -1
        with redirect_stdout(io.StringIO()):
            metrics = desk.calculate(facts, 30)
        self.assertIsNone(metrics["debt_to_equity"]["value"])
        self.assertIsNone(metrics["pe"]["value"])

    def test_comparative_fy_does_not_select_wrong_prior_year(self):
        raw = fixture()
        # Both current and prior are labelled FY2025 in the same 10-K.
        facts = desk.extract_facts(raw, COMPANY)
        self.assertEqual(facts["prior_period"]["end"], "2024-06-30")
        self.assertEqual(facts["concepts"]["revenue"]["prior"]["fy"], 2025)


class EvidenceTests(unittest.TestCase):
    def test_quarter_per_diluted_share_is_printed_verbatim(self):
        quarter_line = (
            "Net income for the fourth quarter was $2.998 billion, $6.75 per diluted share, "
            "compared to $2.610 billion, $5.87 per diluted share, last year."
        )
        # Annual figures can share the same HTML paragraph/plain-text line.
        for suffix in ("", " Net income for the full fiscal year was $9.226 billion, $20.76 per diluted share."):
            with self.subTest(suffix=suffix):
                source_line = quarter_line + suffix
                result = desk.release_lines(source_line)
                self.assertEqual([item["text"] for item in result["diluted_eps"]], [source_line])
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "release.txt"
                    path.write_text(source_line, encoding="utf-8")
                    printed = io.StringIO()
                    with redirect_stdout(printed):
                        desk.show_release({"release": str(path)}, Path(tmp))
                eps_output = printed.getvalue().split("Quarter diluted EPS", 1)[1]
                self.assertIn(source_line, eps_output)
                self.assertNotIn("No reliable quarter-labelled match", eps_output)

    def test_per_diluted_share_keeps_quarter_scope_and_rejects_guidance(self):
        for wording in ("per diluted share", "per diluted common share"):
            with self.subTest(wording=wording):
                actual = f"Net income was $120 million, $1.25 {wording}."
                text = "\n".join([
                    "Results for the first quarter ended March 31:", actual,
                    f"Next quarter we expect $1.50 {wording}.",
                    "Twelve Months Ended December 31",
                    f"Net income was $400 million, $4.25 {wording}.",
                ])
                result = desk.release_lines(text)
                self.assertEqual([item["text"] for item in result["diluted_eps"]], [actual])

    def test_quarter_totals_exclude_segments_guidance_and_annual(self):
        text = "\n".join([
            "Results for the quarter ended June 30, 2025:",
            "Revenue was $12 billion.", "Diluted earnings per share was $2.10 (GAAP).",
            "Cloud revenue was $8 billion.", "Revenue in Services was $3 billion.",
            "Outlook: next quarter total revenue will be $15 billion.",
            "Twelve Months Ended June 30,", "Revenue | $45,000 | $40,000",
            "Diluted Earnings per Share | $8.00 | $7.00"])
        result = desk.release_lines(text)
        self.assertEqual([r["text"] for r in result["revenue"]], ["Revenue was $12 billion."])
        self.assertEqual([r["text"] for r in result["diluted_eps"]], ["Diluted earnings per share was $2.10 (GAAP)."])

    def test_table_preserves_quarter_labels_values_and_hidden_xbrl_removed(self):
        text = desk.plain_text('<html><ix:hidden>secret</ix:hidden><p>Three Months Ended June 30</p>'
            '<table><tr><th></th><th>2025</th><th>2024</th></tr>'
            '<tr><td>Total revenue</td><td>$120</td><td>$100</td></tr>'
            '<tr><td>Diluted EPS</td><td>$2</td><td>$1</td></tr></table></html>')
        result = desk.release_lines(text)
        self.assertNotIn("secret", text)
        self.assertEqual(len(result["revenue"]), 1)
        self.assertIn("$120", result["revenue"][0]["text"])
        self.assertEqual(len(result["diluted_eps"]), 1)

    def test_index_accepted_time_is_used_verbatim(self):
        client = Mock()
        client.text.return_value = '<div class="infoHead">Accepted</div><div class="info">2025-07-01 16:03:02</div>'
        filing = {"accessionNumber": "0000000001-25-000001", "primaryDocument": "filing.htm",
                  "acceptanceDateTime": "2025-07-01T20:03:02Z"}
        result = desk.index_metadata(client, COMPANY, filing)
        self.assertEqual(result["accepted"], "2025-07-01 16:03:02")

    def test_item_202_must_be_in_document_and_history_pages_are_searched(self):
        def filing(accn, form, items="", day="2025-08-01"):
            return {"accessionNumber": accn, "form": form, "items": items,
                    "filingDate": day, "primaryDocument": accn + ".htm"}
        rows = [filing("0000000001-25-000005", "10-K"),
                filing("0000000001-25-000004", "10-Q"),
                filing("0000000001-25-000003", "8-K", "2.02"),
                filing("0000000001-25-000002", "8-K", "5.02")]
        older = [filing("0000000001-25-000001", "8-K", "2.02", "2025-07-01")]
        def columns(data):
            return {key: [row[key] for row in data] for key in data[0]}
        client = Mock()
        client.json.side_effect = [{"filings": {"recent": columns(rows), "files": [{"name": "older.json"}]}}, columns(older)]
        client.text.side_effect = lambda url: "<p>Item 2.02 Results of Operations</p>" if "000001.htm" in url else "<p>Item 5.02</p>"
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), patch.object(
            desk, "index_metadata", side_effect=lambda client, company, filing: {**filing, "accepted": "from index", "accepted_source": "index"}):
            selected = desk.select_filings(client, COMPANY, Path(tmp))
        self.assertEqual(selected["8-K"]["accessionNumber"], "0000000001-25-000001")
        self.assertEqual(client.json.call_count, 2)

    def test_user_agent_is_on_every_prepared_request(self):
        client = desk.SECClient("Research Contact contact@example.test")
        for url in ("https://www.sec.gov/files/company_tickers.json", "https://data.sec.gov/submissions/CIK0000000001.json"):
            request = client.session.prepare_request(desk.requests.Request("GET", url))
            self.assertEqual(request.headers["User-Agent"], "Research Contact contact@example.test")


if __name__ == "__main__":
    unittest.main()
