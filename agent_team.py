"""Fixed CrewAI Flow over the Lab 03 SEC desk. Run `python agent_team.py --help`."""

import argparse
import copy
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("CREWAI_STORAGE_DIR", str(Path(__file__).resolve().parent / ".crewai"))

import requests
from crewai.flow import Flow, listen, start
from dotenv import dotenv_values
from pydantic import BaseModel

import equity_desk as desk
import research_roles as roles
from desk_verifier import (Verifier, load_verifier_sources, load_semantic_response,
                           validate_semantic_response, SemanticResponseError)
from model_budget import MODEL_INPUT_CHAR_LIMIT, request_characters

ROOT = Path(__file__).resolve().parent
VERIFIER = "Verifier"


class NodeClient:
    """Exactly one HTTP model request per invocation; no implicit agent loop/retry."""

    def __init__(self, key, output):
        self.key, self.output = key, Path(output)
        self.counts = {}
        self.verifier_plan = None

    def prepare_verifier_batches(self, batches):
        if self.verifier_plan is not None or self.counts.get(VERIFIER, 0):
            raise roles.RoleError("Verifier batch plan can only be registered once, before its model calls.")
        plan = tuple(tuple(ids) for ids in batches)
        flat = [key for ids in plan for key in ids]
        if not plan or len(flat) != len(set(flat)):
            raise roles.RoleError("Verifier batch plan must cover each required ID exactly once.")
        self.verifier_plan = plan

    def call(self, role, task, context, schema):
        count = self.counts.get(role, 0) + 1
        if role == VERIFIER and self.verifier_plan is not None:
            if count > len(self.verifier_plan) or tuple(schema["properties"]["checks"]["required"]) != self.verifier_plan[count - 1]:
                raise roles.RoleError("Verifier attempted an unscheduled or out-of-order semantic batch.")
        elif count > 2:
            raise roles.RoleError(f"{role}: maximum of two calls per run reached.")
        self.counts[role] = count
        print(role, flush=True)
        if role == roles.MANAGER and "supplied_evidence" in context:
            context = roles.compact_strategy_context(context)
        envelope = {"task": task, "context": context, "expected_output": schema}
        stem = f"{role.lower().replace(' ', '_')}_{count}"
        roles.write_json(self.output / f"{stem}_input.json", envelope)
        instructions = ((ROOT / "roles/verifier.txt").read_text(encoding="utf-8")
                        if role == VERIFIER else roles.load_instructions(role))
        body = {"model": roles.MODEL, "stream": False, "max_tokens": 16384 if role == VERIFIER else 8192,
                "messages": [{"role": "system", "content": instructions},
                             {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "node_response", "schema": schema}}}
        if request_characters(instructions, task, context, schema) > MODEL_INPUT_CHAR_LIMIT:
            raise roles.RoleError(f"{role}: source context exceeds the model budget; input saved in {stem}_input.json.")
        try:
            response = requests.post(roles.ENDPOINT, headers={"Authorization": f"Bearer {self.key}"},
                                     json=body, timeout=(15, 300))
            if not response.ok:
                raise roles.RoleError(f"{role}: Purdue API returned HTTP {response.status_code}; no automatic retry.")
            raw = response.json()
            choice = raw["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise roles.RoleError(f"{role}: incomplete response ({choice.get('finish_reason')}).")
            result = (load_semantic_response(choice["message"]["content"]) if role == VERIFIER
                      else json.loads(choice["message"]["content"]))
            if role == VERIFIER:
                validate_semantic_response(result, schema["properties"]["checks"]["required"])
            roles.validate_shape(result, schema)
        except requests.RequestException as exc:
            roles.write_json(self.output / f"{stem}_output.json", {"error": type(exc).__name__})
            raise roles.RoleError(f"{role}: API connection failed ({type(exc).__name__}); no automatic retry.") from None
        except roles.RoleError as exc:
            roles.write_json(self.output / f"{stem}_output.json", {"error": str(exc)})
            raise
        except SemanticResponseError as exc:
            roles.write_json(self.output / f"{stem}_output.json", {"error": str(exc)})
            raise
        except (ValueError, KeyError, IndexError, TypeError):
            roles.write_json(self.output / f"{stem}_output.json", {"error": "Malformed model response"})
            raise roles.RoleError(f"{role}: malformed model response; saved input retained.") from None
        roles.write_json(self.output / f"{stem}_output.json", result)
        return result


class TeamManager(roles.StrategyManager):
    """Reuse Lab 03 evidence boundaries and review contracts, with bounded revisions."""

    def specialist(self, role, feedback=None):
        if role == roles.ADVISOR:
            context = copy.deepcopy(self.analyst_context())
            supplied = {key: copy.deepcopy(row) for result in context.values()
                        for key, row in result["cited_evidence"].items()}
        else:
            keys = self.catalog.research_ids if role == roles.RESEARCH else roles.financial_evidence_ids(self.catalog)
            supplied = self.catalog.select(keys)
            context = {"company": self.run["company"], "evidence": supplied,
                       "sources": self.catalog.source_records(supplied)}
            if role == roles.FINANCIAL:
                context.update({key: copy.deepcopy(self.financial[key]) for key in
                                ("step4_facts", "step5_metrics", "step7_peers")})
                context["step6_quarter"] = list(self.catalog.quarter_ids)
            context = (roles.compact_research_context(context) if role == roles.RESEARCH
                       else roles.compact_financial_context(context))
        self.supplied[role] = copy.deepcopy(supplied)
        schema = roles.analyst_schema(role, supplied)
        parts = [p for p in roles.PARTS if roles.OWNERS[p] == role and (feedback is None or p in feedback)]
        schema["properties"]["sections"] = roles.object_schema({p: schema["properties"]["sections"]["properties"][p]
                                                               for p in parts})
        # A retry contains only failed parts. Preserve all other part text and fields.
        if feedback is not None and role == roles.ADVISOR and roles.PARTS[4] not in parts:
            for field in ("thesis", "thesis_evidence_ids", "recommendation", "reason", "reason_evidence_ids", "deciding_evidence_id"):
                del schema["properties"][field]
                schema["required"].remove(field)
        task = "Write only these assigned report parts: " + "; ".join(parts) + ". Use only the assigned context."
        if feedback:
            context["revision"] = {p: {"previous": self.results[role]["sections"][p], "issues": feedback[p]} for p in parts}
            task += " Revise only these failed parts once. Do not return or change any other part."
        result = self.client.call(role, task, context, schema)
        if role == roles.FINANCIAL:
            # Calculator work is Python, never a hidden second model call.
            for request in result["calculator_requests"]:
                key = "extra." + request["id"]
                if not re.fullmatch(r"[a-z][a-z_]{0,63}", request["id"]) or key in self.catalog.records:
                    raise roles.RoleError("Financial Analyst requested an invalid or duplicate calculation ID.")
                calculation = roles.calculate_request(request, self.facts)
                self.metrics[key] = calculation
                self.catalog.add_calculation(key, calculation)
                for block in result["sections"][roles.PARTS[2]]:
                    block["evidence_ids"].append(key)
            supplied = self.catalog.select(roles.financial_evidence_ids(self.catalog))
            self.supplied[role] = supplied
            roles.write_json(self.output / "additional_calculations.json", self.metrics)
        if feedback is not None:
            merged = copy.deepcopy(self.results[role])
            merged["sections"].update(result.pop("sections"))
            merged.update(result)
            result = merged
        self.results[role] = self.enrich(role, result)
        self.errors.update(roles.validate_content(role, result, supplied))

    def finish(self):
        # The model checks content, but Python chooses owners and bounds retries.
        initial = self.review()
        roles.write_json(self.output / "review_initial.json", initial)
        failures = {p: c["issues"] for p, c in initial["checks"].items() if not c["passed"]}
        for role in (roles.RESEARCH, roles.FINANCIAL, roles.ADVISOR):
            feedback = {p: failures[p] for p in roles.PARTS if p in failures and roles.OWNERS[p] == role}
            if feedback:
                self.specialist(role, feedback)
        final = self.review() if failures else initial
        roles.write_json(self.output / "review_final.json", final)
        roles.write_json(self.output / "role_outputs.json", self.results)
        roles.write_json(self.output / "evidence_catalog.json", self.catalog.snapshot())
        roles.write_json(self.output / "role_evidence_scopes.json", {r: sorted(s) for r, s in self.supplied.items()})
        return self.compile_report(final)

    def compile_report(self, review):
        """Compile in code after checks/revisions; records travel inside the report."""
        manifest = sorted(f"{s['file']} ({s['form']} {s['accession']})" for s in self.sources.values())
        lines = ["Filings read: " + "; ".join(manifest), "", f"# {self.run['company']['ticker']} Equity Research", ""]

        def prose(text):
            # Escape model-authored control markers; models cannot hide numbers from verification.
            lines.extend(["<!-- desk-prose -->", text.replace("<!--", "&lt;!--"), "<!-- /desk-prose -->", ""])

        def evidence(key, role, seen):
            if key not in self.supplied[role]:
                prose(f"Unsupported evidence reference: {key}")
                return
            for ref, row in self.catalog.select([key]).items():
                if ref in seen:
                    continue
                seen.add(ref)
                record = {"id": ref, "record": row, "source": self.catalog.sources[row["source_id"]],
                          "inputs": {r: self.catalog.records[r] for r in row.get("references", [])}}
                # Render one record here; its dependencies get their own auditable block.
                shallow = copy.deepcopy(row)
                shallow["references"] = []
                original = self.catalog.records[ref]
                self.catalog.records[ref] = shallow
                try:
                    rendered = self.catalog.render(ref)
                finally:
                    self.catalog.records[ref] = original
                lines.extend(["<!-- desk-record " + json.dumps(record, ensure_ascii=True) + " -->",
                              *rendered, "<!-- /desk-record -->", ""])

        for part in roles.PARTS:
            role, seen = roles.OWNERS[part], set()
            result = self.results[role]
            lines.extend(["## " + part, ""])
            if not review["checks"][part]["passed"]:
                lines.extend(["**Review unresolved after the one permitted revision.**", ""])
                for issue in review["checks"][part]["issues"]:
                    prose("Review finding: " + issue)
            if part == roles.PARTS[4]:
                prose("Recommendation: " + result["recommendation"])
                lines.extend(["<!-- advisor-thesis -->", "Thesis: " + result["thesis"].replace("<!--", "&lt;!--"),
                              "<!-- /advisor-thesis -->", ""])
                prose(result["reason"])
                for key in result["reason_evidence_ids"] + ([result["deciding_evidence_id"]] if result["deciding_evidence_id"] else []):
                    evidence(key, role, seen)
            for block in result["sections"][part]:
                prose(block["text"])
                for key in block["evidence_ids"]:
                    evidence(key, role, seen)
            for key in result["limitations"]:
                evidence(key, role, seen)
        path = self.output / "compiled_report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        (self.output / "report.md").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return path


class DeskState(BaseModel):
    ticker: str = ""
    peers: list[str] = []
    price: float | None = None
    run_folder: str = ""
    compiled_report: str = ""
    verified_report: str = ""


class DeskFlow(Flow[DeskState]):
    """Only these edges exist. No model output can choose a successor."""

    def __init__(self, settings, **kwargs):
        super().__init__(**kwargs)
        self.settings = settings
        self.manager = None

    @start()
    def seven_steps(self):
        print("The seven steps", flush=True)
        events = []
        def record(step, phase, payload):
            events.append((step, phase, payload))
            if phase == "output":
                folder = Path(payload["run_folder"])
                for n, event_phase, event_payload in events:
                    roles.write_json(folder / f"step_{n}_{event_phase}.json", event_payload)
                events.clear()
        args = SimpleNamespace(ticker=self.state.ticker, peers=self.state.peers, price=self.state.price, step=None)
        output = desk.run_seven_steps(args, self.settings, on_step=record)
        self.state.run_folder = str(output)
        roles.write_json(output / "seven_steps_input.json", {"ticker": args.ticker, "peers": args.peers, "price": args.price})
        roles.write_json(output / "seven_steps_output.json", {"run_folder": str(output), "steps": list(range(1, 8))})
        # The supplied price remains explicitly an input, never a filing-derived price.
        roles.write_json(output / "input_facts.json", {"price": self.state.price, "source": "CLI --price",
                                                     "run_time": json.loads((output / "run.json").read_text(encoding="utf-8"))["run_time"]})
        self.manager = TeamManager(output, NodeClient(self.settings["API_KEY"], output))

    @listen(seven_steps)
    def research_analyst(self):
        self.manager.specialist(roles.RESEARCH)

    @listen(research_analyst)
    def financial_analyst(self):
        self.manager.specialist(roles.FINANCIAL)

    @listen(financial_analyst)
    def investment_advisor(self):
        self.manager.specialist(roles.ADVISOR)

    @listen(investment_advisor)
    def strategy_manager(self):
        path = self.manager.finish()
        self.state.compiled_report = str(path)

    @listen(strategy_manager)
    def verifier(self):
        output = Path(self.state.run_folder)
        source = load_verifier_sources(output)
        result = Verifier(self.manager.client).run(source, output)
        self.state.verified_report = str(result)
        return str(result)


def fresh_folder(company, parent=None):
    name = re.sub(r"[^\w.-]+", "_", company).strip("_.")[:100] or "verification"
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
    path = (parent or ROOT / "research_data") / f"{name}_{stamp}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def verify_only(saved, key):
    # This path never constructs DeskFlow, TeamManager, or the seven-step tools.
    source = load_verifier_sources(saved)
    facts = source["saved_facts"].get("facts.json", {})
    company = facts.get("company", {}).get("name") or facts.get("company", {}).get("ticker") or Path(saved).name
    output = fresh_folder(company)
    for name, text in source["saved_filing_texts"].items():
        (output / name).write_text(text, encoding="utf-8")
    for name, data in source["saved_facts"].items():
        roles.write_json(output / name, data)
    (output / "compiled_report.md").write_text(source["compiled_report"], encoding="utf-8")
    roles.write_json(output / "verification_origin.json", {"saved_run": str(Path(saved).resolve())})
    return Verifier(NodeClient(key, output)).run(source, output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?")
    parser.add_argument("--peers", nargs="*", default=[])
    parser.add_argument("--price", type=float)
    parser.add_argument("--verify-only", type=Path, metavar="SAVED_RUN")
    args = parser.parse_args(argv)
    if args.verify_only and (args.ticker or args.peers or args.price is not None):
        parser.error("--verify-only accepts only a saved run path; it does not run the desk.")
    if not args.verify_only and not args.ticker:
        parser.error("a ticker is required for a normal run")
    if args.price is not None and (not math.isfinite(args.price) or args.price <= 0):
        parser.error("--price must be a finite positive number")
    settings = dotenv_values(ROOT / ".env")
    if not (settings.get("API_KEY") or "").strip():
        raise desk.DeskError("Required Lab 03 input missing: API_KEY in the root .env (Purdue GenAI Studio key).")
    if args.verify_only:
        result = verify_only(args.verify_only, settings["API_KEY"].strip())
    else:
        if not (settings.get("SEC_USER_AGENT") or "").strip():
            raise desk.DeskError("Required Lab 03 input missing: SEC_USER_AGENT in the root .env.")
        peers = list(dict.fromkeys(p.strip().upper() for entry in args.peers for p in entry.split(",") if p.strip()))
        for optional, supplied in (("--peers: peer comparison will be unavailable", bool(peers)),
                                   ("--price: annual P/E will be unavailable", args.price is not None)):
            if not supplied:
                print("Missing optional input " + optional, flush=True)
        flow = DeskFlow(settings)
        result = flow.kickoff(inputs={"ticker": args.ticker.upper(), "peers": peers, "price": args.price})
    print(f"Verified report: {result}", flush=True)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (desk.DeskError, roles.RoleError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
