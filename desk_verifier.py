"""Independent verification: inputs are report, saved filing texts, and saved facts only."""

import ast
import copy
import html
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quoted_calculations import parse_quoted_calculation, correction_text, AMOUNT, DATE
from semantic_batches import plan_batches
from sourced_claims import check_claim, finish_claim
from insight_contract import parse_insight


NUMBER = re.compile(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?(?!\w|\.\d)")
RECORD = re.compile(r"<!-- desk-record (.*?) -->\s*\n(.*?)<!-- /desk-record -->", re.S)
PROSE = re.compile(r"<!-- desk-prose -->\s*\n(.*?)<!-- /desk-prose -->", re.S)
THESIS = re.compile(r"<!-- advisor-thesis -->.*?<!-- /advisor-thesis -->", re.S)
LEGACY_THESIS = re.compile(r"(?ms)^Thesis:.*?(?=^Deciding number:|^## |\Z)")
XBRL = re.compile(r"^(.+?):\s+(-?[\d.,]+(?:[eE][+-]?\d+)?)\s+(\S+)\s+\[([^;]+);\s*([^;]+);\s*(instant|\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2});\s*(.*?)\]$")


class SemanticResponseError(ValueError):
    """Incomplete or invalid model verification must never produce a verified report."""


def semantic_response_schema(ids):
    correction = {"type": ["object", "null"], "properties": {
        name: {"type": "string"} for name in ("file", "line", "value", "period", "definition")},
        "required": ["file", "line", "value", "period", "definition"], "additionalProperties": False}
    check = {"type": "object", "properties": {
        "supported": {"type": "boolean"}, "reason": {"type": "string", "minLength": 1},
        "correction": correction}, "required": ["supported", "reason", "correction"],
        "additionalProperties": False}
    return {"type": "object", "properties": {"checks": {
        "type": "object", "properties": {key: {"$ref": "#/$defs/semantic_check"} for key in ids},
        "required": list(ids), "additionalProperties": False}}, "required": ["checks"],
        "additionalProperties": False, "$defs": {"semantic_check": check}}


def load_semantic_response(content):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SemanticResponseError(f"Verifier semantic response contains a duplicate JSON key: {key}.")
            result[key] = value
        return result
    return json.loads(content, object_pairs_hook=unique_object)


def validate_semantic_response(answer, expected_ids):
    """Validate coverage independently of whether the API enforces its JSON schema."""
    if not isinstance(answer, dict) or set(answer) != {"checks"} or not isinstance(answer["checks"], dict):
        raise SemanticResponseError("Verifier semantic response must contain a checks object keyed by every requested number ID.")
    checks = answer["checks"]
    expected = set(expected_ids)
    missing, unexpected = sorted(expected - checks.keys()), sorted(checks.keys() - expected)
    if missing or unexpected:
        raise SemanticResponseError(
            f"Verifier semantic check coverage mismatch: expected {len(expected)}, received {len(checks)}; "
            f"missing: {', '.join(missing) or 'none'}; unexpected: {', '.join(unexpected) or 'none'}.")
    for key, check in checks.items():
        if (not isinstance(check, dict) or set(check) != {"supported", "reason", "correction"}
                or type(check["supported"]) is not bool or not isinstance(check["reason"], str)
                or not check["reason"].strip()):
            raise SemanticResponseError(f"Verifier semantic check {key} must contain a boolean supported, a nonempty reason, and a correction.")
        correction = check["correction"]
        if correction is not None and (not isinstance(correction, dict)
                or set(correction) != {"file", "line", "value", "period", "definition"}
                or any(not isinstance(value, str) for value in correction.values())):
            raise SemanticResponseError(f"Verifier semantic check {key} has an invalid correction.")
    return checks


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def normalized(text):
    text = html.unescape(unicodedata.normalize("NFKC", text))
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"[*_`#>|]", "", text)
    return re.sub(r"\s+", "", text).casefold()


def decimal(value):
    if isinstance(value, bool):
        raise ValueError("A boolean is not a financial number")
    result = Decimal(str(value).replace(",", ""))
    if not result.is_finite():
        raise ValueError("A non-finite number is not supported")
    return result


def fact_records(saved):
    records = {}
    def add(data, prefix):
        for name, concept in data.get("concepts", {}).items():
            for period in ("current", "prior"):
                row = concept.get(period)
                if row:
                    records[f"{prefix}.{name}.{period}"] = {**row, "ticker": data["company"]["ticker"],
                                                           "label": concept["label"]}
    subject = saved.get("facts.json", {})
    add(subject, "subject")
    for filename, data in saved.items():
        if filename.endswith("_facts.json") and filename != "input_facts.json":
            add(data, "peer." + data.get("company", {}).get("ticker", filename[:-11]))
    price = saved.get("input_facts.json", {})
    if price.get("price") is not None:
        records["price"] = {"val": price["price"], "unit": subject.get("currency"),
                            "source": "CLI --price", "label": "User-supplied share price"}
    return records


def load_verifier_sources(folder):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"Required Lab 03 saved run missing: {folder}")
    report_path = next((folder / name for name in ("compiled_report.md", "report.md") if (folder / name).is_file()), None)
    if report_path is None:
        raise ValueError(f"Required Lab 03 input missing: compiled_report.md or report.md in {folder}")
    if not (folder / "facts.json").is_file():
        raise ValueError(f"Required Lab 03 input missing: facts.json in {folder}")
    # An explicit allowlist prevents role requests, outputs, instructions and metrics
    # from becoming Verifier inputs. Ratios must be recomputed, not trusted caches.
    texts = {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.txt")
             if p.name in {"10-K.txt", "10-Q.txt", "8-K.txt", "earnings_release.txt"}}
    if not texts:
        raise ValueError(f"Required Lab 03 input missing: saved filing texts in {folder}")
    facts = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*facts.json")
             if p.name == "facts.json" or p.name.endswith("_facts.json")}
    report = report_path.read_text(encoding="utf-8")
    if not report.splitlines() or not report.splitlines()[0].startswith("Filings read:"):
        raise ValueError("Required Lab 03 filings line is missing from the compiled report.")
    return {"compiled_report": report, "saved_filing_texts": texts, "saved_facts": facts}


def find_fact(claim, records):
    fields = ("tag", "start", "end", "unit", "accn", "ticker")
    matches = [r for r in records.values() if r.get("tag") and all(r.get(k) == claim.get(k) for k in fields)]
    return matches[0] if matches and len({str(r["val"]) for r in matches}) == 1 else None


def source_label(row):
    return (f"{row.get('ticker', '')} {row.get('tag', '')}; {row.get('start', 'instant')} to {row.get('end', '')}; "
            f"{row.get('accn', '')}; {row.get('filing_url', '')}")


def evaluate(expression):
    """Decimal arithmetic on a small whitelist, without eval or executable model code."""
    expression = re.sub(r"(?<=\d),(?=\d)", "", expression)
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 64:
        raise ValueError("Oversized arithmetic")
    literals = []
    def walk(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = decimal(ast.get_source_segment(expression, node))
            literals.append(value)
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            if isinstance(node.op, ast.USub):
                literals[-1] = -literals[-1]
                return -value
            return value
        if isinstance(node, ast.BinOp):
            a, b = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add): return a + b
            if isinstance(node.op, ast.Sub): return a - b
            if isinstance(node.op, ast.Mult): return a * b
            if isinstance(node.op, ast.Div): return a / b
        raise ValueError("Only arithmetic on numeric literals is permitted")
    return walk(tree.body), literals


def canonical_calculation(key, refs, facts):
    """Rebuild known desk formulas using the saved source values, including price."""
    prefix, _, metric = key.rpartition(".")
    definitions = {
        "revenue_growth": (["revenue.current", "revenue.prior"], "{0} / {1} - 1"),
        "operating_margin": (["operating_income.current", "revenue.current"], "{0} / {1}"),
        "return_on_average_equity": (["net_income.current", "equity.current", "equity.prior"], "{0} / (({1} + {2}) / 2)"),
        "debt_to_equity": (["long_term_debt.current", "current_debt.current", "equity.current"], "({0} + {1}) / {2}"),
        "free_cash_flow": (["cash_from_operations.current", "capex.current"], "{0} - {1}"),
        "pe": (["price", "diluted_eps.current"], "{0} / {1}"),
    }
    if metric not in definitions:
        return None
    suffixes, template = definitions[metric]
    expected = [r if r == "price" else prefix + "." + r for r in suffixes]
    if refs != expected or any(r not in facts for r in refs):
        return None
    return template.format(*(str(facts[r]["val"]) for r in refs))


class Verifier:
    def __init__(self, client):
        self.client = client
        self.checks = []

    def add(self, original, mark, reason, correction="", source="", **extra):
        row = {"id": f"number_{len(self.checks) + 1}", "original": str(original), "mark": mark,
               "reason": reason, "correction": str(correction), "source": source, **extra}
        self.checks.append(row)
        return row

    def check_quoted_calculation(self, line, sources):
        calculation = parse_quoted_calculation(line)
        if calculation is None:
            return None
        saved = fact_records(sources["saved_facts"])
        tags = set(re.findall(r"\b(?:us-gaap|ifrs-full):[A-Za-z][A-Za-z0-9]*", line))
        accessions = set(re.findall(r"\b\d{10}-\d{2}-\d{6}\b", line))
        files = [name for name in sources["saved_filing_texts"] if name in line]
        cited_lines = set()
        for match in re.finditer(r"\blines?\s+(\d+)(?:\s*[-–]\s*(\d+))?", line, re.I):
            start, end = int(match[1]), int(match[2] or match[1])
            # Bounds prevent an invented citation range from allocating huge sets.
            if 0 < start <= end and end - start <= 1000:
                cited_lines.update(range(start, end + 1))
        input_checks = []
        canonical_inputs = []
        for operand in calculation.inputs:
            candidates = [row for row in saved.values() if row.get("tag")
                          and decimal(row["val"]) == operand.value
                          and (not operand.period or row.get("end") == operand.period)
                          and (not tags or row["tag"] in tags)
                          and (not accessions or row.get("accn") in accessions)]
            identities = {(r.get("ticker"), r.get("tag"), r.get("start"), r.get("end"), r.get("accn"), r.get("unit")) for r in candidates}
            canonical = candidates[0] if len(identities) == 1 else None
            canonical_inputs.append(canonical)
            found = []
            for filename in files:
                for n, text in enumerate(sources["saved_filing_texts"][filename].splitlines(), 1):
                    if cited_lines and n not in cited_lines:
                        continue
                    if any(decimal(m.group()) == decimal(operand.number) for m in NUMBER.finditer(text)):
                        found.append((filename, n, text))
            problems = []
            if not operand.period and calculation.kind == "growth":
                problems.append("Quoted growth input has no specified period.")
            if tags and canonical is None:
                problems.append("Quoted input has no exact saved XBRL value/tag/period/accession match.")
            if files and not found:
                problems.append("Quoted input does not occur at its cited saved filing line.")
            if not files and not (tags and canonical):
                problems.append("Quoted input has no explicit, validated filing or XBRL citation.")
            if canonical and operand.currency:
                currency = {"€": "EUR", "£": "GBP", "¥": "JPY"}.get(operand.currency, operand.currency)
                if currency != "$" and currency != canonical.get("unit"):
                    problems.append("Quoted currency differs from its saved fact unit.")
            filename, n, quote = found[0] if found else (None, None, "")
            if canonical and filename and filename in {"10-K.txt", "10-Q.txt", "8-K.txt"} and canonical.get("form") != filename[:-4]:
                problems.append("Quoted fact form differs from the cited filing.")
            source = f'{filename}, line {n}: {quote}' if found else source_label(canonical) if canonical else "Unverified quoted input"
            if canonical:
                source += "; " + source_label(canonical)
            check = self.add(operand.number, "unsupported" if problems else "correct",
                             "; ".join(problems) or "Quoted input matches saved source; period/definition review required for filing lines.",
                             source=source, semantic=bool(found), file=filename, quote=quote,
                             period=operand.period or "as specified by the quoted input",
                             definition=calculation.definition + "; financial measure asserted in: " + line[:line.find(operand.text)],
                             quoted_input=operand.text)
            input_checks.append(check)
        errors = list(calculation.errors)
        if all(canonical_inputs):
            if len({r["unit"] for r in canonical_inputs}) != 1:
                errors.append("Saved input currencies/units differ.")
            if calculation.kind == "growth":
                from datetime import date
                periods = [r for r in canonical_inputs if r.get("start")]
                if len(periods) != 2:
                    errors.append("Growth needs two duration facts, not instants.")
                else:
                    days = [(date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days for r in periods]
                    if abs(days[0] - days[1]) > 14:
                        errors.append("Quoted growth input periods are not comparable durations.")
            elif len({r.get("end") for r in canonical_inputs}) != 1:
                errors.append("Quoted ratio inputs have different periods.")
        if any(c["mark"] != "correct" for c in input_checks):
            errors.append("Quoted input provenance is unsupported; arithmetic is conditional on those inputs.")
        arithmetic = calculation.expression + " = " + correction_text(calculation)
        quoted = "; ".join(f"{operand.text} (period {operand.period or 'unspecified'}): {c['source']}"
                           for operand, c in zip(calculation.inputs, input_checks))
        result_checks = []
        for check in input_checks:
            check['calculation_context'] = {'definition': calculation.definition,
                'arithmetic': arithmetic, 'operands': [
                    {'quoted_input': operand.text, 'period': operand.period, 'scale': str(operand.scale),
                     'currency': operand.currency, 'source': source_check['source'],
                     'fact': copy.deepcopy(canonical)}
                    for operand, source_check, canonical in zip(calculation.inputs, input_checks, canonical_inputs)]}
        for result in calculation.results:
            equal = calculation.recomputed is not None and abs(result.value - calculation.recomputed) <= result.tolerance
            check = self.add(result.text, "unsupported" if errors else "correct" if equal else "wrong",
                             "; ".join(errors) or "Recomputed from the quoted inputs in code.",
                             correction_text(calculation, exact="/" in result.text), "Arithmetic from quoted inputs: " + arithmetic + "; " + quoted,
                             arithmetic=arithmetic, quoted_calculation=True)
            result_checks.append(check)
        return calculation, result_checks, input_checks

    def unclaimed_calculation_numbers(self, line, calculation):
        """A recognized calculation must not hide other unsupported prose numbers."""
        spans = [m.span() for m in AMOUNT.finditer(line)] + [(r.start, r.end) for r in calculation.results]
        spans += [m.span() for m in DATE.finditer(line)]
        for pattern in (r"https?://\S+", r"\b\d{10}-\d{2}-\d{6}\b", r"\b(?:10-[KQ]|8-K)\b",
                        r"\blines?\s+\d+(?:\s*[-–]\s*\d+)?", r"\b\d+[- ](?:week|month|year)s?\b"):
            spans += [m.span() for m in re.finditer(pattern, line, re.I)]
        remainder = list(line)
        for a, b in spans:
            remainder[a:b] = " " * (b - a)
        checks = []
        for match in NUMBER.finditer("".join(remainder)):
            check = self.add(match.group(), "unsupported", "Additional prose number has no validated source.")
            checks.append((match.start(), match.end(), check))
        return checks

    def check_record(self, payload, visible, sources):
        row = payload["record"]
        kind = row["kind"]
        saved = fact_records(sources["saved_facts"])
        if kind == "xbrl_fact":
            canonical = find_fact(row, saved)
            displayed = next((XBRL.match(line) for line in visible.splitlines() if XBRL.match(line)), None)
            if not canonical:
                fallback = saved.get(payload.get("id"), {})
                if fallback.get("tag") and fallback.get("label") == row.get("label") and fallback.get("ticker") == row.get("ticker"):
                    check = self.add(row.get("val"), "unsupported", "Citation identity failed; corrected from the named saved fact.",
                                     fallback["val"], source_label(fallback))
                else:
                    check = self.add(row.get("val"), "unsupported", "No exact saved fact for this tag, period, unit, accession and ticker.")
            else:
                valid = (displayed is not None and decimal(displayed[2]) == decimal(canonical["val"])
                         and decimal(row["val"]) == decimal(canonical["val"])
                         and displayed[3] == canonical["unit"] and displayed[5].strip() == canonical["tag"]
                         and displayed[6] == canonical.get("start", "instant") and displayed[7] == canonical["end"]
                         and canonical["accn"] in displayed[4])
                check = self.add(displayed[2] if displayed else row.get("val"), "correct" if valid else "wrong",
                                 "Exact XBRL fact comparison.", canonical["val"], source_label(canonical))
            return self.replacement(check, visible)
        if kind == "text":
            filename = payload["source"].get("file")
            quote = row.get("text", "")
            # Compare the visible quotation as well as its citation, never just metadata.
            displayed = "\n".join(line[2:] for line in visible.splitlines() if line.startswith("> "))
            text = sources["saved_filing_texts"].get(filename, "")
            exists = bool(quote) and normalized(quote) in normalized(text) and normalized(displayed) == normalized(quote)
            # A changed numeric literal can invalidate a quotation while leaving
            # one identifiable source line. Give that real line to the semantic
            # check so it can correct the claim rather than merely reject it.
            skeleton = normalized(NUMBER.sub("", displayed or quote))
            candidates = list(dict.fromkeys(line for line in text.splitlines()
                              if skeleton and normalized(NUMBER.sub("", line)) == skeleton))
            candidate = candidates[0] if not exists and len(candidates) == 1 else None
            checks = []
            for match in NUMBER.finditer(displayed or quote):
                check = self.add(match.group(), "correct" if exists else "unsupported",
                                       "Quoted line occurs in the saved filing; semantic check pending." if exists else
                                       "Quoted line does not occur in the specified saved filing.",
                                       source=f"{filename}: {quote}", file=filename, quote=displayed or quote,
                                       period="as stated in quoted line and saved column headers",
                                       filing_report_date=payload["source"].get("period"), definition="as stated in quoted line",
                                       semantic=True)
                if candidate:
                    check["candidate_quote"] = candidate
                checks.append(check)
            return visible, checks
        if kind == "user_input":
            canonical = saved.get("price")
            valid = canonical and decimal(canonical["val"]) == decimal(row["val"])
            check = self.add(row.get("val"), "correct" if valid else "unsupported",
                             "User price is an input, not an observed filing number.",
                             canonical["val"] if canonical else "", "input_facts.json: CLI --price")
            return self.replacement(check, visible)
        if kind == "calculation" and row.get("value") is not None:
            definition = row.get("definition", "")
            refs = row.get("inputs", [])
            quoted_inputs = payload.get("inputs", {})
            reasons = []
            for ref in refs:
                operand = quoted_inputs.get(ref, {})
                canonical = saved.get(ref) if operand.get("kind") == "user_input" else find_fact(operand, saved)
                if not canonical or decimal(operand.get("val", "NaN")) != decimal(canonical["val"]):
                    reasons.append("Quoted operand has no exact saved fact/input: " + ref)
            if not definition:
                reasons.append("Ratio has no definition; alternative filing definitions cannot be distinguished.")
            canonical_expression = canonical_calculation(payload.get("id", ""), refs, saved)
            try:
                value, literals = evaluate(row["arithmetic"])
                remaining = [decimal(quoted_inputs[r]["val"]) for r in refs]
                for literal in literals:
                    if remaining and literal == remaining[0]:
                        remaining.pop(0)
                    elif literal not in (Decimal(1), Decimal(2)):
                        reasons.append("Arithmetic operand does not equal a quoted input.")
                if remaining:
                    reasons.append("Not all quoted inputs occur in arithmetic.")
                # Floating-point result display may differ only in its final binary digits.
                equal = abs(value - decimal(row["value"])) <= max(abs(value), Decimal(1)) * Decimal("1e-14")
                visible_match = re.search(r":\s*(.+?) = ([-\d.,eE+]+) (\S+)\. Definition: (.*)", visible)
                equal = (equal and visible_match is not None and decimal(visible_match[2]) == decimal(row["value"])
                         and normalized(visible_match[1]) == normalized(row["arithmetic"])
                         and normalized(visible_match[4]) == normalized(definition))
                if canonical_expression:
                    canonical_value, _ = evaluate(canonical_expression)
                    equal = equal and abs(canonical_value - value) <= max(abs(canonical_value), Decimal(1)) * Decimal("1e-14")
            except (ValueError, KeyError, SyntaxError, ArithmeticError):
                value, equal = None, False
                reasons.append("Arithmetic cannot be recomputed from quoted, sourced inputs.")
            # Recover a wrong displayed ratio only when every quoted source is proven.
            source = "; ".join(source_label(saved[r]) if saved.get(r, {}).get("tag") else "CLI --price input"
                               for r in refs if r in saved)
            if value is not None:
                source += "; arithmetic: " + row["arithmetic"] + " = " + str(value)
                source += "; quoted inputs: " + ", ".join(f"{ref}={quoted_inputs[ref]['val']}" for ref in refs if ref in quoted_inputs)
            check = self.add(row["value"], "unsupported" if reasons else "correct" if equal else "wrong",
                             "; ".join(reasons) or "Recomputed ratio from quoted inputs; definition: " + definition,
                             value if not reasons else "", source)
            if canonical_expression and definition:
                try:
                    correct_value, _ = evaluate(canonical_expression)
                    check["correction"] = str(correct_value)
                    check["source"] = source + "; arithmetic: " + canonical_expression + "; definition: " + definition
                except ArithmeticError:
                    pass
            return self.replacement(check, visible)
        return visible, []

    @staticmethod
    def replacement(check, visible):
        if check["mark"] == "correct":
            return visible, [check]
        value = check["correction"] or "N/A (not established by the saved filings)"
        return f"Corrected number: {value}. Source: {check['source'] or 'No supporting filing line or XBRL fact found.'}\n", [check]

    def run(self, sources, output):
        print("Verifier: code checks", flush=True)
        output = Path(output)
        self.checks = []
        write_json(output / "verifier_input.json", sources)
        original = sources["compiled_report"]
        report = THESIS.sub("[Investment Advisor thesis skipped by Verifier]", original)
        # Old Lab 03 reports have an explicit 'Thesis:' paragraph rather than markers.
        if not THESIS.search(original):
            report = LEGACY_THESIS.sub("[Investment Advisor thesis skipped by Verifier]\n\n", report, count=1)
        replacements, covered, semantic_blocks, quoted_blocks, sourced_blocks = [], [], [], [], []
        for match in RECORD.finditer(report):
            payload={}
            try:
                payload = json.loads(match[1])
                if payload['record']['kind']=='sourced_claim':
                    group=check_claim(self,payload,match[2],sources,normalized)
                    rendered,checks=match[2],group['checks']
                    sourced_blocks.append((match.start(),match.end(),group))
                else:
                    rendered, checks = self.check_record(payload, match[2], sources)
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                check = self.add("malformed numeric citation", "unsupported", f"Invalid report citation: {type(exc).__name__}")
                rendered, checks = self.replacement(check, match[2])
            replacements.append((match.start(), match.end(), rendered))
            covered.append((match.start(), match.end()))
            if any(c.get("semantic") for c in checks) and payload.get('record',{}).get('kind')!='sourced_claim':
                semantic_blocks.append((match.start(), match.end(), match[2], checks))
        # Legacy report citations are supported without reading any role work.
        # Source-record and legacy citations can coexist; neither format suppresses
        # verification of the other format elsewhere in the same report.
        self.legacy_checks(report, sources, replacements, covered, semantic_blocks)
        # Ordinary Markdown paragraphs can wrap across lines, including inside
        # desk-prose markers. Recognize quoted calculations before the unsupported
        # literal fallback, while retaining all other source/number checks.
        for paragraph in re.finditer(r"(?m)^(?!<!--|#)[^\n]+(?:\n(?![ \t]*$|<!--|#)[^\n]+)*", report):
            a, b = paragraph.span()
            if any(lo < b and a < hi for lo, hi in covered):
                continue
            if paragraph.group().startswith(("Filings read:", "Run time:", "[Investment Advisor thesis")):
                continue
            quoted = self.check_quoted_calculation(paragraph.group(), sources)
            if quoted is not None:
                calculation, result_checks, input_checks = quoted
                extras = self.unclaimed_calculation_numbers(paragraph.group(), calculation)
                quoted_blocks.append((a, b, paragraph.group(), calculation, result_checks, input_checks, extras))
                covered.append((a, b))
            else:
                insight = parse_insight(paragraph.group())
                if insight:
                    filename, number = insight['file'], int(insight['line'])
                    source_lines = sources['saved_filing_texts'].get(filename, '').splitlines()
                    actual = source_lines[number - 1] if 0 < number <= len(source_lines) else ''
                    bound = bool(actual) and normalized(insight['claim']) in normalized(actual)
                    payload = {'record': {'kind': 'text', 'text': insight['claim']},
                               'source': {'file': filename}}
                    _, checks = self.check_record(payload, '> ' + insight['claim'] + '\n', sources)
                    for check in checks:
                        check['insight'] = True
                        check['quoted_input'] = paragraph.group()
                        check['definition'] = 'Measure stated in this Insight claim: ' + insight['claim']
                        check['source'] = f"{filename}, line {number}: {actual or '[cited line missing]'}"
                        if not bound:
                            check['mark'] = 'unsupported'
                            check['reason'] += ' Insight claim does not occur at the specified saved filing line.'
                    covered.append((a, b))
                    semantic_blocks.append((a, b, paragraph.group(), checks))
        # Any remaining model-authored financial numeric literal has no source.
        # Dates, metadata and source URLs are removed only in known metadata lines.
        for line_match in re.finditer(r"(?m)^.+$", report):
            a, b = line_match.span()
            if any(lo <= a < hi for lo, hi in covered):
                continue
            line = line_match.group()
            if line.startswith(("Filings read:", "Run time:", "<!--", "[Investment Advisor thesis")):
                continue
            if re.fullmatch(r'#{1,6}\s+[^\d$%]+\d+\s*:\s*[^\d$%]+',line):
                continue  # A heading index is document structure, not a financial claim.
            clean = re.sub(r"https?://\S+|\b(?:10-[KQ]|8-K)\b|\b\d{10}-\d{2}-\d{6}\b", "", line)
            matches = list(NUMBER.finditer(clean))
            if matches:
                for m in matches:
                    self.add(m.group(), "unsupported", "Number has no explicit filing/XBRL/calculator source.")
                revised = NUMBER.sub("N/A [unsupported: no cited filing source]", clean)
                replacements.append((a, b, revised))
        write_json(output / "verifier_code_checks.json", self.checks)
        # Only the three allowed inputs enter the semantic model call. Include code
        # results in task (derived from those same inputs), never other role work.
        semantic = [c for c in self.checks if c.get("semantic")]
        # The same cited line may appear in several report parts. One semantic
        # finding can apply to identical occurrences without extra model calls.
        aliases, unique = {}, {}
        for check in semantic:
            identity = tuple(check.get(k) for k in ("file", "quote", "original", "period", "definition")) + (
                json.dumps(check.get('source_binding'),sort_keys=True),
                json.dumps(check.get('calculation_context'),sort_keys=True))
            unique.setdefault(identity, check)
            aliases[check["id"]] = unique[identity]["id"]
        model_checks = [{k: c[k] for k in ("id", "original", "mark", "reason", "file", "quote", "candidate_quote", "period", "definition", "semantic", "quoted_input", "source_binding", "calculation_context")
                         if k in c} for c in unique.values()]
        expected_ids = [c["id"] for c in model_checks]
        returned = {}
        batch_index = None
        try:
            instructions = (Path(__file__).resolve().parent / "roles/verifier.txt").read_text(encoding="utf-8")
            batches = plan_batches(model_checks, sources, re.sub(r"<!-- desk-record .*? -->", "", report),
                                   instructions, semantic_response_schema, normalized, SemanticResponseError)
            write_json(output / "verifier_semantic_batches.json", {
                "expected_ids": expected_ids, "aliases": aliases,
                "batches": [{"batch": index, "expected_ids": batch["ids"], "input_characters": batch["input_characters"]}
                            for index, batch in enumerate(batches, 1)]})
            prepare = getattr(self.client, "prepare_verifier_batches", None)
            if callable(prepare):
                prepare([batch["ids"] for batch in batches])
            for batch_index, batch in enumerate(batches, 1):
                print(f"Verifier: semantic batch {batch_index}/{len(batches)} ({len(batch['ids'])} checks)", flush=True)
                stem = output / f"verifier_semantic_batch_{batch_index:03d}"
                write_json(Path(str(stem) + "_input.json"), {
                    "task": batch["task"], "context": batch["context"], "expected_output": batch["schema"]})
                answer = self.client.call("Verifier", batch["task"], batch["context"], batch["schema"])
                validated = validate_semantic_response(answer, batch["ids"])
                if returned.keys() & validated.keys():
                    raise SemanticResponseError("Duplicate semantic result across batches.")
                returned.update(validated)
                write_json(Path(str(stem) + "_output.json"), answer)
            validate_semantic_response({"checks": returned}, expected_ids)
            if any(key not in returned for key in aliases.values()):
                raise SemanticResponseError("Required number occurrence has no semantic result.")
            write_json(output / "verifier_semantic_results.json", {"checks": returned, "aliases": aliases, "complete": True})
        except SemanticResponseError as exc:
            write_json(output / "verifier_semantic_error.json", {"error": str(exc), "batch": batch_index,
                       "expected_ids": expected_ids, "completed_ids": list(returned)})
            raise
        for check in semantic:
            result = returned[aliases[check["id"]]]
            check["semantic_reason"] = result["reason"]
            if not result["supported"]:
                if check.get('sourced_claim_result'):
                    check['mark']='unsupported'
                    check['reason']+=' Semantic period/definition binding did not pass.'
                    continue
                code_passed = check["mark"] == "correct"
                if code_passed:
                    check["mark"] = "unsupported"
                check["correction"] = ""
                proposed = result["correction"]
                if proposed and proposed["period"] and proposed["definition"]:
                    text = sources["saved_filing_texts"].get(proposed["file"], "")
                    try:
                        number_occurs = any(decimal(m.group()) == decimal(proposed["value"]) for m in NUMBER.finditer(proposed["line"]))
                    except (ValueError, ArithmeticError):
                        number_occurs = False
                    if proposed["line"] and normalized(proposed["line"]) in normalized(text) and number_occurs:
                        check["correction"] = proposed["value"]
                        check["source"] = f"{proposed['file']}: {proposed['line']} (period: {proposed['period']}; definition: {proposed['definition']})"
                        if code_passed:
                            check["mark"] = "wrong"
            # A semantic pass can never override a failed deterministic source check.
        for a, b, visible, checks in semantic_blocks:
            failed = [c for c in checks if c["mark"] != "correct"]
            if failed:
                corrected = ["Verified correction to cited statement:"]
                for c in checks:
                    if c["mark"] == "correct":
                        continue
                    corrected.append(f"{c['original']} -> {c['correction'] or 'N/A (not established by the saved filings)'}. Source: {c['source']}")
                replacements = [(lo, hi, val) for lo, hi, val in replacements if (lo, hi) != (a, b)]
                replacements.append((a, b, "\n".join(corrected) + "\n"))
        for a, b, line, calculation, result_checks, input_checks, extras in quoted_blocks:
            if any(check["mark"] != "correct" for check in input_checks):
                for check in result_checks:
                    check["mark"] = "unsupported"
                    check["reason"] += " Input period/definition/source review did not pass."
                    check["source"] += "; calculation is conditional on unsupported quoted inputs"
            revised = line
            edits = [(lo, hi, "N/A [unsupported: no cited filing source]") for lo, hi, _ in extras]
            for result, check in zip(calculation.results, result_checks):
                if check["mark"] != "correct":
                    value = correction_text(calculation, exact="/" in result.text)
                    suffix = "%" if result.percent else " multiple"
                    replacement = value[:-len(suffix)] if value.endswith(suffix) else value
                    edits.append((result.start, result.end, replacement))
            for lo, hi, replacement in sorted(edits, reverse=True):
                revised = revised[:lo] + replacement + revised[hi:]
            failed = [check for check in result_checks if check["mark"] != "correct"]
            if failed:
                revised += "\n\nVerification arithmetic: " + failed[0]["source"]
                if any(check["mark"] != "correct" for check in input_checks):
                    revised = "**Unsupported quoted-input calculation (conditional arithmetic only):** " + revised
                replacements.append((a, b, revised))
            elif extras:
                replacements.append((a, b, revised))
        for a,b,group in sourced_blocks:
            rendered=finish_claim(group)
            replacements=[(lo,hi,value) for lo,hi,value in replacements if (lo,hi)!=(a,b)]
            replacements.append((a,b,rendered))
        verified = report
        for a, b, replacement in sorted(replacements, reverse=True):
            verified = verified[:a] + replacement + verified[b:]
        # Restore the thesis verbatim: it was never checked or sent to the model.
        thesis = THESIS.search(original)
        old_thesis = LEGACY_THESIS.search(original) if not thesis else None
        skipped = thesis.group() if thesis else old_thesis.group() if old_thesis else "[No Advisor thesis supplied]"
        verified = verified.replace("[Investment Advisor thesis skipped by Verifier]", skipped, 1)
        first, _, rest = verified.partition("\n")
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ")
        table = ["| Number | Mark | Correction | Filing line / XBRL tag |", "|---|---|---|---|"]
        for c in self.checks:
            table.append("| " + " | ".join(cell(x) for x in (c["original"], c["mark"],
                         c["correction"] if c["mark"] != "correct" else "", c["source"] or "No supporting source")) + " |")
        if not self.checks:
            table.append("| No sourced numbers found | unsupported | N/A | No supported numeric report claims |")
        path = output / "verified_report.md"
        path.write_text(first + "\n" + "\n".join(table) + "\n\n" + rest, encoding="utf-8")
        write_json(output / "verification_marks.json", self.checks)
        write_json(output / "verifier_output.json", {"verified_report": path.name, "marks": self.checks})
        return path

    def legacy_checks(self, report, sources, replacements, covered, semantic_blocks):
        """Read the original Lab 03 Markdown citation format, without role artifacts."""
        for match in re.finditer(r"(?m)^.+$", report):
            if any(lo<=match.start()<hi for lo,hi in covered):
                continue
            x = XBRL.match(match.group())
            if x:
                form_accn = x[4].split()
                label = x[1].split(" ", 1)
                row = {"kind": "xbrl_fact", "ticker": label[0], "label": label[-1], "val": x[2], "unit": x[3],
                       "form": form_accn[0], "accn": form_accn[-1], "tag": x[5].strip(), "end": x[7], "filing_url": x[8]}
                if x[6] != "instant": row["start"] = x[6]
                rendered, _ = self.check_record({"record": row}, match.group(), sources)
                replacements.append((*match.span(), rendered))
                covered.append(match.span())
        pattern = r"(?m)^> (.+)\n\s*\n\[([^,\n]+), line (\d+); [^;]+; filing report date ([^\]]+)\]\([^\n]+\)"
        for match in re.finditer(pattern, report):
            if any(lo<match.end() and match.start()<hi for lo,hi in covered):
                continue
            payload = {"record": {"kind": "text", "text": match[1]}, "source": {"file": match[2], "period": match[4]}}
            visible, checks = self.check_record(payload, match.group(), sources)
            covered.append(match.span())
            semantic_blocks.append((*match.span(), visible, checks))
        # Legacy calculator statements still need full source operands, not metrics.json.
        saved = fact_records(sources["saved_facts"])
        inputs = {
            "revenue_growth": ["revenue.current", "revenue.prior"],
            "operating_margin": ["operating_income.current", "revenue.current"],
            "return_on_average_equity": ["net_income.current", "equity.current", "equity.prior"],
            "debt_to_equity": ["long_term_debt.current", "current_debt.current", "equity.current"],
            "free_cash_flow": ["cash_from_operations.current", "capex.current"],
            "pe": ["price", "diluted_eps.current"],
        }
        pattern = r"(?m)^((?:subject|peer\.[\w-]+)\.([a-z_]+)):\s+(.+?) = ([-\d.,eE+]+) (\S+)\. Definition: (.*)$"
        for match in re.finditer(pattern, report):
            if any(lo<=match.start()<hi for lo,hi in covered):
                continue
            prefix = match[1].rsplit(".", 1)[0]
            refs = [r if r == "price" else prefix + "." + r for r in inputs.get(match[2], [])]
            row = {"kind": "calculation", "value": match[4], "arithmetic": match[3], "definition": match[6], "inputs": refs}
            quoted = {r: {**saved[r], "kind": "user_input" if r == "price" else "xbrl_fact"} for r in refs if r in saved}
            value, checks = self.check_record({"id": match[1], "record": row, "inputs": quoted}, match.group(), sources)
            if not refs:
                for check in checks:
                    check.update(mark="unsupported", reason="Unknown ratio formula; source operands not established.")
                value, _ = self.replacement(checks[0], match.group())
            replacements.append((*match.span(), value))
            covered.append(match.span())
