"""Offline arithmetic/provenance regressions for quoted inputs in ordinary prose."""

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from desk_verifier import Verifier, SemanticResponseError
from quoted_calculations import parse_quoted_calculation


class SemanticClient:
    def __init__(self, reject=False, omit=False):
        self.calls = []
        self.reject, self.omit = reject, omit

    def call(self, role, task, context, schema):
        self.calls.append((role, task, context, schema))
        checks = json.loads(task.split("Code checks: ", 1)[1])
        result = {c["id"]: {"supported": not self.reject, "reason": "Period/definition check.", "correction": None}
                  for c in checks}
        if self.omit and result:
            del result[next(iter(result))]
        return {"checks": result}


def source_fixture(current=150, prior=125, stated="24", alias=""):
    accn = "0000000001-25-000001"
    tag = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    current_period = ("2024-07-01", "2025-06-30")
    prior_period = ("2023-07-01", "2024-06-30")
    def fact(value, period):
        return {"val": value * 1000000, "start": period[0], "end": period[1], "tag": tag,
                "unit": "USD", "form": "10-K", "accn": accn, "filing_url": "https://www.sec.gov/test"}
    facts = {"company": {"ticker": "SYNTH", "name": "Synthetic Issuer"}, "currency": "USD", "concepts": {
        "revenue": {"label": "Revenue", "current": fact(current, current_period), "prior": fact(prior, prior_period)}}}
    sentence = (f"The issuer's total revenue growth was approximately {stated}%{alias}, calculated from "
                f'"${current:,} million" for the 52 weeks ended June 30, 2025 and "${prior:,} million" '
                f"for the 52 weeks ended June 30, 2024 (10-K {accn}; saved 10-K.txt, total revenue line 5, "
                f"units line 2, period headers lines 3-4; XBRL tag {tag}).")
    return {"compiled_report": "Filings read: synthetic 10-K\n\n## Valuation Insights\n\n" + sentence + "\n",
            "saved_filing_texts": {"10-K.txt": "Statements of income\n(amounts in millions)\n52 Weeks Ended\n"
                f"June 30, 2025 | June 30, 2024\nTotal revenue | {current:,} | {prior:,}\n"},
            "saved_facts": {"facts.json": facts}}


class QuotedArithmeticTests(unittest.TestCase):
    def test_growth_and_input_order_are_determined_from_periods_not_size(self):
        cases = [(150, 125, Decimal(20)), (80, 100, Decimal(-20)), (125, 125, Decimal(0))]
        for current, prior, expected in cases:
            with self.subTest(current=current, prior=prior):
                parsed = parse_quoted_calculation(source_fixture(current, prior)["compiled_report"].splitlines()[-1])
                self.assertEqual(parsed.recomputed, expected)
        parsed = parse_quoted_calculation('Sales growth was 24%, using "125 million" for June 30, 2024 '
                                          'and "150 million" for June 30, 2025.')
        self.assertEqual(parsed.recomputed, Decimal(20))
        self.assertEqual(parsed.expression, "(150 / 125 - 1) * 100")

    def test_different_scales_normalize_before_arithmetic(self):
        parsed = parse_quoted_calculation('Total revenue growth was 24%, using "$0.15 billion" for June 30, 2025 '
                                          'and "$125 million" for June 30, 2024.')
        self.assertEqual(parsed.recomputed, Decimal(20))
        self.assertEqual(parsed.expression, "(150000000.00 / 125000000 - 1) * 100")

    def test_ratio_percent_and_multiple_units(self):
        for rate, expected in (("30%", Decimal(25)), ("0.3x", Decimal("0.25")), ("0.3", Decimal("0.25"))):
            with self.subTest(rate=rate):
                parsed = parse_quoted_calculation(f'The ratio of income to revenue was {rate}, defined as income divided by revenue, '
                                                  'using "$30 million" and "$120 million".')
                self.assertEqual(parsed.recomputed, expected)
                self.assertTrue(parsed.explicit_definition)

    def test_zero_denominator_currency_and_definition_ambiguity_remain_unsupported(self):
        zero = parse_quoted_calculation('Total revenue growth was 10%, from "$0" to "$5".')
        self.assertIsNone(zero.recomputed)
        self.assertTrue(any("zero" in e for e in zero.errors))
        currency = parse_quoted_calculation('The ratio was 2x, using "USD 100" and "EUR 50".')
        self.assertTrue(any("currencies" in e for e in currency.errors))
        self.assertTrue(any("definition" in e for e in currency.errors))


class QuotedVerificationTests(unittest.TestCase):
    def run_verifier(self, source, client=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        before = json.dumps(source, sort_keys=True)
        client = client or SemanticClient()
        path = Verifier(client).run(source, folder)
        self.assertEqual(before, json.dumps(source, sort_keys=True))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "Verifier")
        self.assertEqual(set(client.calls[0][2]), {"compiled_report", "saved_filing_texts", "saved_facts"})
        marks = json.loads((folder / "verification_marks.json").read_text(encoding="utf-8"))
        return path.read_text(encoding="utf-8"), marks, client

    def test_wrong_growth_shows_correction_arithmetic_inputs_and_real_sources(self):
        report, marks, client = self.run_verifier(source_fixture())
        result = next(c for c in marks if c["original"] == "24")
        self.assertEqual(result["mark"], "wrong")
        self.assertEqual(result["correction"], "20%")
        self.assertIn("(150 / 125 - 1) * 100 = 20%", result["source"])
        self.assertIn('"$150 million"', result["source"])
        self.assertIn('"$125 million"', result["source"])
        self.assertIn("10-K.txt, line 5", result["source"])
        self.assertIn("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", result["source"])
        self.assertIn("| 24 | wrong | 20% | Arithmetic from quoted inputs:", report)
        self.assertNotIn("approximately 24%", report)
        requested = json.loads(client.calls[0][1].split("Code checks: ", 1)[1])
        self.assertEqual(len(requested), 2)
        for check in requested:
            calculation=check['calculation_context']
            self.assertIn('(150 / 125 - 1) * 100 = 20%', calculation['arithmetic'])
            self.assertEqual([operand['period'] for operand in calculation['operands']],['2025-06-30','2024-06-30'])
            self.assertEqual([operand['fact']['val'] for operand in calculation['operands']],[150000000,125000000])
            self.assertTrue(all(operand['scale']=='1000000' for operand in calculation['operands']))
        self.assertIn("June 30, 2025 | June 30, 2024", client.calls[0][2]["saved_filing_texts"]["10-K.txt"])

    def test_reported_invented_growth_regression_and_exact_fraction_alias(self):
        source = source_fixture(303154, 275235, "14.143695387578", " (exactly 778568/55047%)")
        report, marks, _ = self.run_verifier(source)
        result = next(c for c in marks if c["original"] == "14.143695387578")
        self.assertEqual(result["mark"], "wrong")
        self.assertEqual(result["correction"], "10.143695387578%")
        self.assertIn("(303154 / 275235 - 1) * 100 = 10.143695387578%", result["source"])
        self.assertIn('"$303,154 million"', result["source"])
        self.assertIn('"$275,235 million"', result["source"])
        alias = next(c for c in marks if c["original"] == "778568/55047")
        self.assertEqual(alias["correction"], "558380/55047%")
        self.assertIn("exactly 558380/55047%", report)

    def test_correct_growth_is_not_replaced_but_its_inputs_still_get_semantic_checks(self):
        report, marks, client = self.run_verifier(source_fixture(stated="20"))
        result = next(c for c in marks if c.get("quoted_calculation"))
        self.assertEqual(result["mark"], "correct")
        self.assertIn("approximately 20%", report)
        self.assertEqual(len(client.calls[0][3]["properties"]["checks"]["required"]), 2)

    def test_wrapped_prose_is_recomputed_inside_report_markers(self):
        source = source_fixture()
        sentence = source["compiled_report"].splitlines()[-1].replace('calculated from ', 'calculated from\n').replace(' and "$', ' and\n"$')
        source["compiled_report"] = 'Filings read: synthetic\n\n<!-- desk-prose -->\n' + sentence + '\n<!-- /desk-prose -->\n'
        report, marks, _ = self.run_verifier(source)
        result = next(c for c in marks if c.get("quoted_calculation"))
        self.assertEqual(result["mark"], "wrong")
        self.assertIn("(150 / 125 - 1) * 100 = 20%", result["source"])
        self.assertNotIn("approximately 24%", report)

    def test_a_ratio_in_prose_is_recomputed_from_its_two_sourced_inputs(self):
        source = source_fixture()
        income = copy.deepcopy(source['saved_facts']['facts.json']['concepts']['revenue']['current'])
        income.update(val=30000000, tag='us-gaap:OperatingIncomeLoss')
        source['saved_facts']['facts.json']['concepts']['operating_income'] = {'label': 'Operating income', 'current': income, 'prior': None}
        source['saved_filing_texts']['10-K.txt'] += 'Operating income | 30\n'
        sentence = ('The operating margin ratio was 30%, defined as operating income divided by total revenue, '
                    'using "$30 million" for June 30, 2025 and "$150 million" for June 30, 2025 '
                    '(saved 10-K.txt, lines 5-6; accession 0000000001-25-000001).')
        source['compiled_report'] = 'Filings read: synthetic\n\n' + sentence + '\n'
        report, marks, _ = self.run_verifier(source)
        result = next(c for c in marks if c.get('quoted_calculation'))
        self.assertEqual(result['mark'], 'wrong')
        self.assertEqual(result['correction'], '20%')
        self.assertIn('(30 / 150) * 100 = 20%', result['source'])
        self.assertIn('| 30 | wrong | 20% |', report)

    def test_missing_or_forged_source_never_gets_approved_by_a_model(self):
        for change in (lambda s: s["compiled_report"].replace("10-K.txt", "missing.txt").replace("us-gaap:", "invented:"),
                       lambda s: s["compiled_report"].replace("RevenueFromContractWithCustomerExcludingAssessedTax", "InventedTag"),
                       lambda s: s["compiled_report"].replace("June 30, 2025", "June 30, 2023")):
            source = source_fixture()
            source["compiled_report"] = change(source)
            with self.subTest(report=source["compiled_report"]):
                _, marks, _ = self.run_verifier(source)
                result = next(c for c in marks if c.get("quoted_calculation"))
                self.assertEqual(result["mark"], "unsupported")
                self.assertIn("Arithmetic from quoted inputs:", result["source"])
                self.assertNotEqual(result["correction"], "")

    def test_semantic_rejection_propagates_to_the_ratio_without_losing_arithmetic(self):
        report, marks, _ = self.run_verifier(source_fixture(), SemanticClient(reject=True))
        result = next(c for c in marks if c.get("quoted_calculation"))
        self.assertEqual(result["mark"], "unsupported")
        self.assertEqual(result["correction"], "20%")
        self.assertIn("(150 / 125 - 1) * 100", result["source"])
        self.assertIn("conditional", report)

    def test_incomplete_semantic_response_still_aborts_without_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            client = SemanticClient(omit=True)
            with self.assertRaises(SemanticResponseError):
                Verifier(client).run(source_fixture(), folder)
            self.assertEqual(len(client.calls), 1)
            self.assertFalse((folder / "verified_report.md").exists())

    def test_other_numbers_in_calculation_sentence_are_still_rejected(self):
        source = source_fixture()
        source["compiled_report"] = source["compiled_report"].rstrip() + " An unrelated valuation is 999.\n"
        report, marks, _ = self.run_verifier(source)
        extra = next(c for c in marks if c["original"] == "999")
        self.assertEqual(extra["mark"], "unsupported")
        self.assertIn("N/A [unsupported: no cited filing source]", report)

    def test_advisor_thesis_remains_excluded(self):
        source = source_fixture()
        sentence = source["compiled_report"].splitlines()[-1]
        source["compiled_report"] = "Filings read: synthetic\n<!-- advisor-thesis -->\n" + sentence + "\n<!-- /advisor-thesis -->\n"
        report, marks, client = self.run_verifier(source)
        self.assertEqual(marks, [])
        self.assertIn(sentence, report)
        self.assertNotIn(sentence, client.calls[0][2]["compiled_report"])


if __name__ == "__main__":
    unittest.main()
