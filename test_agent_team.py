"""Offline integration tests of the real Flow with scripted model responses."""

import copy
import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import agent_team as team
import research_roles as roles
from desk_verifier import (Verifier, fact_records, load_verifier_sources, semantic_response_schema,
                           validate_semantic_response, load_semantic_response, SemanticResponseError)
from test_research_roles import setup_run


class ScriptedClient:
    def __init__(self, key=None, output=None, fail_parts=()):
        self.calls = []
        self.fail_parts = fail_parts

    def call(self, role, task, context, schema):
        self.calls.append((role, task, copy.deepcopy(context), copy.deepcopy(schema)))
        if role == "Verifier":
            checks = json.loads(task.split("Code checks: ", 1)[1])
            return {"checks": {c["id"]: {"supported": True, "reason": "Period and definition match.",
                                "correction": None} for c in checks if c.get("semantic")}}
        if role == roles.MANAGER:
            checks = {p: {"passed": p not in self.fail_parts,
                      "issues": [] if p not in self.fail_parts else [{"code": "missing_analysis", "text": "Clarify this part."}]}
                      for p in roles.PARTS}
            for part, check in checks.items():
                for field in ('insight_present', 'insight_not_headline_restatement'):
                    if field in schema['properties']['checks']['properties'][part]['properties']:
                        check[field] = True
            return {'checks': checks}
        properties = schema["properties"]
        parts = properties["sections"]["properties"]
        if role == roles.RESEARCH:
            key = next(k for k, row in context["evidence"].items() if row == "The business sells subscriptions.")
            refs, text = [key], "The business sells subscriptions."
            limitations = list(context["limitations"])
        elif role == roles.FINANCIAL:
            refs, text = ["subject.operating_margin", "subject.pe", "quarter.revenue"], "The supplied evidence informs valuation."
            limitations = [k for k, r in context["evidence"].items() if isinstance(r, dict) and role in r.get("required_for", [])]
        else:
            refs, text, limitations = ["subject.pe"], "The supplied evidence informs the recommendation.", []
        result = {"sections": {p: [{"text": text, "evidence_ids": list(refs)}] for p in parts}, "limitations": limitations}
        if role in (roles.RESEARCH, roles.FINANCIAL):
            wanted = 'The business sells subscriptions.' if role == roles.RESEARCH else 'Revenue includes subscriptions.'
            insight_id = next(k for k, value in context['evidence'].items() if value == wanted)
            filename = context['insight_source_files'][insight_id.rsplit('.L', 1)[0]]
            number = insight_id.rsplit('.L', 1)[1]
        else:
            insight_id, row = next((k, r) for output in context.values() for k, r in output['cited_evidence'].items()
                                   if r.get('text') == 'The business sells subscriptions.')
            wanted, filename, number = row['text'], row['source_id'].rsplit(':', 1)[-1], row['line']
        designated = team.insight_locations(roles.PARTS, roles.OWNERS)[role]
        if designated in result['sections']:
            result['insight'] = {'part': designated, 'text': f'Insight: {wanted} [{filename}, line {number}]', 'evidence_ids': [insight_id]}
        if role == roles.FINANCIAL:
            result["calculator_requests"] = []
        if "thesis" in properties:
            result.update(thesis="Unverified thesis with 999 percent upside.", recommendation="Hold",
                          deciding_evidence_id="subject.pe", reason="The earnings multiple decides the rating.",
                          thesis_evidence_ids=["subject.pe"], reason_evidence_ids=["subject.pe"])
        roles.validate_shape(result, schema)
        return result


def saved_fixture(path):
    setup_run(path)
    roles.write_json(path / "input_facts.json", {"price": 30, "source": "CLI --price"})


class FlowTests(unittest.TestCase):
    def test_real_flow_fixed_order_mandatory_verifier_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            def tools(args, settings, on_step):
                saved_fixture(folder)
                for i in range(1, 8):
                    on_step(i, "input", {"ticker": args.ticker})
                    on_step(i, "output", {"run_folder": str(folder)})
                return folder
            client = ScriptedClient()
            with patch.object(team.desk, "run_seven_steps", side_effect=tools) as tools_call, \
                 patch.object(team, "NodeClient", return_value=client):
                flow = team.DeskFlow({"API_KEY": "test-key", "SEC_USER_AGENT": "test@example.test"})
                path = Path(flow.kickoff(inputs={"ticker": "TEST", "peers": [], "price": 30}))
            tools_call.assert_called_once()
            self.assertEqual([c[0] for c in client.calls[:4]], [roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR, roles.MANAGER])
            self.assertEqual(client.calls[-1][0], "Verifier")
            self.assertTrue(path.is_file())
            self.assertLessEqual(max(Counter(c[0] for c in client.calls).values()), 2)
            for step in range(1, 8):
                for phase in ("input", "output"):
                    self.assertTrue((folder / f"step_{step}_{phase}.json").is_file())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], (folder / "compiled_report.md").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(lines[1], "| Number | Mark | Correction | Filing line / XBRL tag |")
            context = client.calls[-1][2]
            self.assertEqual(set(context), {"compiled_report", "saved_filing_texts", "saved_facts"})
            self.assertNotIn("999 percent", context["compiled_report"])
            self.assertIn("999 percent", path.read_text(encoding="utf-8"))

    def test_failed_parts_only_one_revision_per_owner_and_unchanged_other_parts(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            saved_fixture(folder)
            client = ScriptedClient(fail_parts=roles.PARTS[:2])
            manager = team.TeamManager(folder, client)
            for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
                manager.specialist(role)
            financial = copy.deepcopy(manager.results[roles.FINANCIAL])
            manager.finish()
            research = [c for c in client.calls if c[0] == roles.RESEARCH]
            self.assertEqual(len(research), 2)
            self.assertEqual(set(research[1][2]["revision"]), set(roles.PARTS[:2]))
            self.assertEqual(manager.results[roles.FINANCIAL], financial)
            self.assertEqual(Counter(c[0] for c in client.calls)[roles.MANAGER], 2)
            self.assertLessEqual(max(Counter(c[0] for c in client.calls).values()), 2)

    def test_verify_only_never_constructs_team_or_tools_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            root = Path(tmp)
            saved = root / "original"
            saved.mkdir()
            saved_fixture(saved)
            (saved / "compiled_report.md").write_text("Filings read: test\n\nNo numeric claims.\n", encoding="utf-8")
            before = {p.name: p.read_bytes() for p in saved.iterdir()}
            client = ScriptedClient()
            with patch.object(team, "ROOT", root), patch.object(team, "NodeClient", return_value=client), \
                 patch.object(team, "TeamManager") as manager, patch.object(team, "DeskFlow") as flow, \
                 patch.object(team.desk, "run_seven_steps") as tools:
                first = team.verify_only(saved, "test-key")
                second = team.verify_only(saved, "test-key")
            self.assertNotEqual(first.parent, second.parent)
            self.assertTrue(first.is_file() and second.is_file())
            manager.assert_not_called()
            flow.assert_not_called()
            tools.assert_not_called()
            self.assertEqual([c[0] for c in client.calls], ["Verifier", "Verifier"])
            self.assertEqual(before, {p.name: p.read_bytes() for p in saved.iterdir()})

    def test_single_part_revision_does_not_return_or_change_the_other_part(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            saved_fixture(folder)
            client = ScriptedClient(fail_parts=[roles.PARTS[0]])
            manager = team.TeamManager(folder, client)
            for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
                manager.specialist(role)
            untouched = copy.deepcopy(manager.results[roles.RESEARCH]["sections"][roles.PARTS[1]])
            manager.finish()
            retry = [c for c in client.calls if c[0] == roles.RESEARCH][1]
            self.assertEqual(set(retry[2]["revision"]), {roles.PARTS[0]})
            self.assertEqual(set(retry[3]["properties"]["sections"]["properties"]), {roles.PARTS[0]})
            self.assertEqual(manager.results[roles.RESEARCH]["sections"][roles.PARTS[1]], untouched)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        saved_fixture(self.folder)
        (self.folder / "compiled_report.md").write_text("Filings read: test\n", encoding="utf-8")
        self.sources = load_verifier_sources(self.folder)

    def fact_payload(self, value=None):
        row = fact_records(self.sources["saved_facts"])["subject.revenue.current"]
        if value is not None: row["val"] = value
        row["kind"] = "xbrl_fact"
        payload = {"id": "subject.revenue.current", "record": row}
        visible = (f"{row['ticker']} {row['label']}: {row['val']} {row['unit']} "
                   f"[{row['form']} {row['accn']}; {row['tag']}; {row['start']} to {row['end']}; {row['filing_url']}]\n")
        return payload, visible

    def semantic_sources(self, count):
        source = copy.deepcopy(self.sources)
        quotes = [f"Revenue was ${i} million." for i in range(1, count + 1)]
        source["saved_filing_texts"]["earnings_release.txt"] = "\n".join(quotes)
        for quote in quotes:
            payload = {"record": {"kind": "text", "text": quote},
                       "source": {"file": "earnings_release.txt", "period": "2025-06-30"}}
            source["compiled_report"] += '<!-- desk-record ' + json.dumps(payload) + ' -->\n> ' + quote + '\n<!-- /desk-record -->\n'
        return source

    def test_all_78_checks_are_required_across_deterministic_batches(self):
        source = self.semantic_sources(78)
        client = team.NodeClient("test-key", self.folder)
        received_ids = []
        def respond(*args, **kwargs):
            envelope = json.loads(kwargs["json"]["messages"][1]["content"])
            expected = envelope["expected_output"]["properties"]["checks"]
            ids = expected["required"]
            received_ids.extend(ids)
            self.assertEqual(set(expected["properties"]), set(ids))
            self.assertIs(expected["additionalProperties"], False)
            result = {"checks": {key: {"supported": True, "reason": "Period and definition match.", "correction": None}
                                 for key in ids}}
            response = Mock(ok=True)
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
            return response
        with patch.object(team.requests, "post", side_effect=respond) as post:
            report = Verifier(client).run(source, self.folder)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(received_ids, [f"number_{i}" for i in range(1, 79)])
        marks = json.loads((self.folder / "verification_marks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(marks), 78)
        self.assertTrue(all(c["semantic_reason"] == "Period and definition match." for c in marks))
        self.assertEqual(report.read_text(encoding="utf-8").splitlines()[0], source["compiled_report"].splitlines()[0])

    def test_missing_number_13_still_fails_without_retry_or_verified_report(self):
        source = self.semantic_sources(78)
        client = team.NodeClient("test-key", self.folder)
        def respond(*args, **kwargs):
            envelope = json.loads(kwargs["json"]["messages"][1]["content"])
            result = {"checks": {key: {"supported": True, "reason": "Period and definition match.", "correction": None}
                                 for key in envelope["expected_output"]["properties"]["checks"]["required"] if key != "number_13"}}
            response = Mock(ok=True)
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
            return response
        with patch.object(team.requests, "post", side_effect=respond) as post:
            with self.assertRaisesRegex(SemanticResponseError, "expected 64, received 63; missing: number_13"):
                Verifier(client).run(source, self.folder)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(client.counts, {"Verifier": 1})
        for name in ("verified_report.md", "verification_marks.json", "verifier_output.json"):
            self.assertFalse((self.folder / name).exists())
        diagnostic = json.loads((self.folder / "verifier_semantic_error.json").read_text(encoding="utf-8"))
        self.assertIn("number_13", diagnostic["error"])
        self.assertEqual(len(diagnostic["expected_ids"]), 78)
        self.assertTrue((self.folder / "verifier_code_checks.json").is_file())

    def test_xbrl_exact_value_and_source_identity(self):
        verifier = Verifier(None)
        payload, visible = self.fact_payload()
        _, checks = verifier.check_record(payload, visible, self.sources)
        self.assertEqual(checks[0]["mark"], "correct")
        payload["record"]["val"] += 1
        revised, checks = verifier.check_record(payload, visible, self.sources)
        self.assertEqual(checks[0]["mark"], "wrong")
        self.assertIn("us-gaap:", revised)
        payload["record"]["end"] = "1900-01-01"
        _, checks = verifier.check_record(payload, visible, self.sources)
        self.assertEqual(checks[0]["mark"], "unsupported")

    def test_visible_number_tampering_does_not_trust_citation_json(self):
        payload, visible = self.fact_payload()
        visible = visible.replace(f": {payload['record']['val']} ", ": 999999 ")
        _, checks = Verifier(None).check_record(payload, visible, self.sources)
        self.assertEqual(checks[0]["mark"], "wrong")

    def test_line_normalization_and_model_cannot_override_missing_line(self):
        payload = {"record": {"kind": "text", "text": "Revenue was   $40 million."},
                   "source": {"file": "earnings_release.txt", "period": "2025-06-30"}}
        visible = "> Revenue was $40 million.\n"
        _, checks = Verifier(None).check_record(payload, visible, self.sources)
        self.assertEqual(checks[0]["mark"], "correct")
        payload["record"]["text"] = "Revenue was $999 million."
        _, checks = Verifier(None).check_record(payload, "> Revenue was $999 million.\n", self.sources)
        self.assertEqual(checks[0]["mark"], "unsupported")

    def test_ratio_recomputation_definition_and_unsourced_operands(self):
        facts = fact_records(self.sources["saved_facts"])
        refs = ["subject.operating_income.current", "subject.revenue.current"]
        a, b = [facts[r]["val"] for r in refs]
        payload = {"id": "subject.operating_margin", "record": {"kind": "calculation", "value": a / b,
                   "arithmetic": f"{a} / {b}", "definition": "Operating income / total revenue", "inputs": refs},
                   "inputs": {r: {**facts[r], "kind": "xbrl_fact"} for r in refs}}
        visible = f"subject.operating_margin: {a} / {b} = {a / b} fraction. Definition: Operating income / total revenue"
        verifier = Verifier(None)
        _, checks = verifier.check_record(payload, visible, self.sources)
        self.assertEqual(checks[0]["mark"], "correct")
        payload["record"]["value"] = 999
        revised, checks = verifier.check_record(payload, visible, self.sources)
        self.assertEqual(checks[0]["mark"], "wrong")
        self.assertIn(str(a / b), revised)
        payload["record"]["definition"] = ""
        _, checks = verifier.check_record(payload, visible, self.sources)
        self.assertEqual(checks[0]["mark"], "unsupported")

    def test_model_semantic_failure_and_supported_correction(self):
        quote = "Revenue was $40 million."
        payload = {"record": {"kind": "text", "text": quote},
                   "source": {"file": "earnings_release.txt", "period": "2025-06-30"}}
        self.sources["compiled_report"] += '<!-- desk-record ' + json.dumps(payload) + ' -->\n> ' + quote + '\n<!-- /desk-record -->\n'
        client = ScriptedClient()
        def call(role, task, context, schema):
            checks = json.loads(task.split("Code checks: ", 1)[1])
            return {"checks": {c["id"]: {"supported": False, "reason": "Wrong definition asserted.",
                     "correction": {"file": "earnings_release.txt", "line": "Diluted EPS was $0.50 (GAAP).",
                                    "value": "0.50", "period": "2025-06-30", "definition": "GAAP diluted EPS"}}
                    for c in checks if c.get("semantic")}}
        client.call = call
        path = Verifier(client).run(self.sources, self.folder)
        marks = json.loads((self.folder / "verification_marks.json").read_text(encoding="utf-8"))
        self.assertEqual(marks[0]["mark"], "wrong")
        self.assertIn("Diluted EPS was $0.50", path.read_text(encoding="utf-8"))

    def test_missing_sources_explicit_and_private_artifacts_not_loaded(self):
        (self.folder / "role_outputs.json").write_text('"PRIVATE"', encoding="utf-8")
        (self.folder / "companyfacts.json").write_text('"RAW HUGE FILE"', encoding="utf-8")
        source = load_verifier_sources(self.folder)
        self.assertNotIn("PRIVATE", json.dumps(source))
        self.assertNotIn("RAW HUGE FILE", json.dumps(source))
        (self.folder / "facts.json").unlink()
        with self.assertRaisesRegex(ValueError, "Required Lab 03 input missing: facts.json"):
            load_verifier_sources(self.folder)

    def test_unsupported_prose_numbers_are_removed_and_thesis_is_skipped(self):
        self.sources["compiled_report"] += "\nRevenue was 999.\n<!-- advisor-thesis -->Thesis: 888.<!-- /advisor-thesis -->\n"
        path = Verifier(ScriptedClient()).run(self.sources, self.folder)
        marks = json.loads((self.folder / "verification_marks.json").read_text(encoding="utf-8"))
        self.assertEqual([c["original"] for c in marks], ["999"])
        self.assertEqual(marks[0]["mark"], "unsupported")
        body = path.read_text(encoding="utf-8").split("|---|---|---|---|", 1)[1]
        self.assertIn("Thesis: 888.", body)
        self.assertIn("N/A [unsupported", body)

    def test_model_cannot_approve_a_filing_line_that_failed_code_checks(self):
        quote = "Revenue was $999 million."
        payload = {"record": {"kind": "text", "text": quote},
                   "source": {"file": "earnings_release.txt", "period": "2025-06-30"}}
        self.sources["compiled_report"] += '<!-- desk-record ' + json.dumps(payload) + ' -->\n> ' + quote + '\n<!-- /desk-record -->\n'
        Verifier(ScriptedClient()).run(self.sources, self.folder)
        marks = json.loads((self.folder / "verification_marks.json").read_text(encoding="utf-8"))
        self.assertEqual(marks[0]["mark"], "unsupported")

    def test_invalid_quote_can_be_corrected_from_the_actual_saved_line(self):
        quote = "Revenue was $999 million."
        payload = {"record": {"kind": "text", "text": quote},
                   "source": {"file": "earnings_release.txt", "period": "2025-06-30"}}
        self.sources["compiled_report"] += '<!-- desk-record ' + json.dumps(payload) + ' -->\n> ' + quote + '\n<!-- /desk-record -->\n'
        client = Mock()
        def respond(role, task, context, schema):
            self.assertIn("Revenue was $40 million.", context["saved_filing_texts"]["earnings_release.txt"])
            checks = json.loads(task.split("Code checks: ", 1)[1])
            return {"checks": {c["id"]: {"supported": False, "reason": "The saved line says forty million.",
                    "correction": {"file": "earnings_release.txt", "line": "Revenue was $40 million.",
                    "value": "40", "period": "2025-06-30", "definition": "Quarter revenue, millions"}}
                    for c in checks}}
        client.call.side_effect = respond
        path = Verifier(client).run(self.sources, self.folder)
        marks = json.loads((self.folder / "verification_marks.json").read_text(encoding="utf-8"))
        self.assertEqual(marks[0]["mark"], "unsupported")
        self.assertEqual(marks[0]["correction"], "40")
        self.assertIn("Revenue was $40 million.", path.read_text(encoding="utf-8"))


class SemanticResponseTests(unittest.TestCase):
    def test_complete_required_id_schema_and_local_validator_agree(self):
        ids = [f"number_{i}" for i in range(1, 79)]
        schema = semantic_response_schema(ids)
        answer = {"checks": {key: {"supported": True, "reason": "Checked the cited line.", "correction": None} for key in ids}}
        roles.validate_shape(answer, schema)
        self.assertEqual(set(validate_semantic_response(answer, ids)), set(ids))
        del answer["checks"]["number_13"]
        with self.assertRaises(roles.RoleError):
            roles.validate_shape(answer, schema)
        with self.assertRaisesRegex(SemanticResponseError, "missing: number_13"):
            validate_semantic_response(answer, ids)

    def test_wrong_id_cannot_replace_a_required_id_even_at_the_same_count(self):
        answer = {"checks": {"number_99": {"supported": True, "reason": "Checked.", "correction": None}}}
        with self.assertRaisesRegex(SemanticResponseError, "missing: number_13; unexpected: number_99"):
            validate_semantic_response(answer, ["number_13"])

    def test_malformed_and_legacy_array_results_are_rejected(self):
        good = {"supported": True, "reason": "Checked.", "correction": None}
        invalid = [None, [], {"checks": []}, {"checks": [{"id": "number_13", **good}]},
                   {"checks": {"number_13": {**good, "supported": "true"}}},
                   {"checks": {"number_13": {**good, "supported": 1}}},
                   {"checks": {"number_13": {**good, "reason": " "}}},
                   {"checks": {"number_13": {"supported": True, "reason": "Checked."}}},
                   {"checks": {"number_13": {**good, "correction": {"value": "13"}}}}]
        for answer in invalid:
            with self.subTest(answer=answer), self.assertRaises(SemanticResponseError):
                validate_semantic_response(answer, ["number_13"])

    def test_duplicate_raw_json_keys_are_rejected_before_they_can_be_overwritten(self):
        value = '{"supported":true,"reason":"Checked.","correction":null}'
        content = '{"checks":{"number_13":' + value + ',"number_13":' + value + '}}'
        with self.assertRaisesRegex(SemanticResponseError, "duplicate JSON key: number_13"):
            load_semantic_response(content)


class TransportTests(unittest.TestCase):
    def test_verifier_rejects_duplicate_keys_in_the_actual_http_response(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            client = team.NodeClient("test-key", tmp)
            response = Mock(ok=True)
            value = '{"supported":true,"reason":"Checked.","correction":null}'
            content = '{"checks":{"number_13":' + value + ',"number_13":' + value + '}}'
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
            with patch.object(team.requests, "post", return_value=response) as post:
                with self.assertRaisesRegex(SemanticResponseError, "duplicate JSON key: number_13"):
                    client.call("Verifier", "Check the number.", {}, semantic_response_schema(["number_13"]))
            self.assertEqual(post.call_count, 1)
            self.assertIn("duplicate JSON key", (Path(tmp) / "verifier_1_output.json").read_text(encoding="utf-8"))

    def test_http_failure_is_saved_and_never_automatically_retried(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            client = team.NodeClient("SECRET", tmp)
            response = Mock(ok=False, status_code=500)
            with patch.object(team.requests, "post", return_value=response) as post:
                with self.assertRaisesRegex(roles.RoleError, "HTTP 500"):
                    client.call(roles.RESEARCH, "work", {}, roles.object_schema({"value": roles.STRING}))
                self.assertEqual(post.call_count, 1)
            self.assertTrue((Path(tmp) / "research_analyst_1_output.json").is_file())

    def test_one_request_envelope_role_files_call_cap_and_no_key_saved(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            client = team.NodeClient("SECRET", tmp)
            response = Mock(ok=True)
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": '{"value":"done"}'}}]}
            schema = roles.object_schema({"value": roles.STRING})
            with patch.object(team.requests, "post", return_value=response) as post:
                client.call(roles.RESEARCH, "work", {"assigned": "only"}, schema)
                self.assertEqual(post.call_count, 1)
                request = post.call_args.kwargs["json"]
                self.assertEqual(request["messages"][0]["content"], roles.load_instructions(roles.RESEARCH))
                self.assertEqual(set(json.loads(request["messages"][1]["content"])), {"task", "context", "expected_output"})
                client.call(roles.RESEARCH, "revision", {}, schema)
                with self.assertRaisesRegex(roles.RoleError, "maximum of two"):
                    client.call(roles.RESEARCH, "forbidden", {}, schema)
                self.assertEqual(post.call_count, 2)
            self.assertNotIn("SECRET", "".join(p.read_text(encoding="utf-8") for p in Path(tmp).iterdir()))


if __name__ == "__main__":
    unittest.main()
