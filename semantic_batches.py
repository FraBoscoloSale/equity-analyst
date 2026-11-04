"""Deterministic, lossless selection and packing of semantic verification evidence."""

import copy
import json
import re
from decimal import Decimal, InvalidOperation

from model_budget import MODEL_INPUT_CHAR_LIMIT, request_characters


MAX_CHECKS_PER_BATCH = 64  # Reserve response space as well as limiting request size.
PACKING_TARGET_CHARACTERS = 150_000  # Leave headroom below the transport guard.
TASK = (
    "After the completed code checks below, check each semantic=true entry. Does its cited filing line "
    "actually state this number for the asserted period and definition? A matching line alone is insufficient. "
    "Check surrounding table columns, scales, GAAP versus adjusted, total revenue versus net sales. "
    "For a source-bound calculated statement, review each quoted operand's filing column, period, "
    "units and definition, and whether the stated formula represents the claimed measure. "
    "The derived result is recomputed in code; it need not appear verbatim in a filing. "
    "When source_binding is supplied, review that exact numeric span and only the operands used by "
    "its expression. display.scale converts the displayed value to base units: a scale of 1000000000 "
    "means billions, not base dollars. A conversion must preserve the source amount. For each operand, "
    "source.filing.number_index is the zero-based numeric-token index on the complete quoted row; "
    "check the selected value against the quoted period/unit headers and asserted operand definition. "
    "Verify that mapping independently; do not assume a binding is true merely because it is supplied. "
    "Other columns remain evidence context, not the target of this ID. Keep each result and reason "
    "attached to its own ID and original value, never a neighboring check's number. "
    "If ambiguous, supported=false. If a ratio has alternatives and no definition, unsupported. "
    "Return checks as an object keyed by every supplied semantic id, never an array. "
    "Every key in expected_output.checks.required is mandatory, with no extra keys. "
    "Each value must contain supported, reason and correction; do not repeat the id inside it. "
    "Preserve the claimed financial measure; do not substitute a different metric. "
    "Corrections must quote an actual saved filing line with its period, definition and numeric literal, "
    "or null if not recoverable. Skip the Investment Advisor thesis. "
    "Inputs contain only relevant excerpts of the compiled report, saved filing texts and saved facts. "
    "L prefixes are original line labels, not text belonging to the filing. Each selected evidence "
    "block is complete, including its table headers and notes; gaps between blocks contain no evidence "
    "selected for this batch. This batch is fixed by Python. Check every supplied id. Code checks: "
)
FILING_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s|item\s+\d+[A-Za-z]?\b|note\s+\d+\b|"
    r"(?:consolidated\s+)?(?:statements?\s+of\b|balance\s+sheets?\b|income\s+statements?\b))", re.I)
REPORT_HEADING = re.compile(r"^(#{1,6})\s")
NUM = re.compile(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\w|\.\d)")


def blocks(lines, filing=False):
    """Complete sections bounded by actual headings, never by character clipping."""
    starts = [0] + [i for i, line in enumerate(lines) if i and
                    (FILING_HEADING.match(line) if filing else REPORT_HEADING.match(line))]
    return [(a, b) for a, b in zip(starts, starts[1:] + [len(lines)]) if a < b]


def containing_sections(lines, sections, anchors, normalize, inline_anchors=()):
    wanted = {normalize(a) for a in anchors if a}
    inline = {normalize(a) for a in inline_anchors if a}
    return {index for index, (a, b) in enumerate(sections)
            if any(anchor in {normalize(line) for line in lines[a:b]} or
                   (len(anchor) > 40 and anchor in normalize("\n".join(lines[a:b]))) for anchor in wanted)
            or any(anchor in normalize("\n".join(lines[a:b])) for anchor in inline)}


def expand_references(lines, sections, selected):
    """Keep explicitly referenced filing notes/items, including their complete text."""
    by_heading = {}
    for index, (a, _) in enumerate(sections):
        match = re.match(r"\s*(note\s+\d+|item\s+\d+[A-Za-z]?)\b", lines[a], re.I)
        if match:
            by_heading.setdefault(re.sub(r"\s+", " ", match[1]).lower(), set()).add(index)
    pending = list(sorted(selected))
    selected = set(selected)
    while pending:
        index = pending.pop()
        a, b = sections[index]
        for match in re.finditer(r"\b(note\s+\d+|item\s+\d+[A-Za-z]?)\b", "\n".join(lines[a:b]), re.I):
            for referred in sorted(by_heading.get(re.sub(r"\s+", " ", match[1]).lower(), ())):
                if referred not in selected:
                    selected.add(referred)
                    pending.append(referred)
    return selected


def report_parents(lines, sections, selected):
    # A parent preamble may define units, periods, or terminology for child claims.
    stack, parents = [], {}
    for index, (a, _) in enumerate(sections):
        match = REPORT_HEADING.match(lines[a])
        if match:
            level = len(match[1])
            while stack and stack[-1][0] >= level:
                stack.pop()
            parents[index] = [entry[1] for entry in stack]
            stack.append((level, index))
    return selected | {p for index in selected for p in parents.get(index, ())} | {0}


def excerpts(lines, sections, selected):
    indexes = {i for section in selected for i in range(*sections[section])}
    return "\n".join(f"L{i + 1}: {lines[i]}" for i in sorted(indexes))


def numeric_values(text):
    result = set()
    for match in NUM.finditer(text):
        try:
            value = Decimal(match.group().replace(",", ""))
            if value.is_finite():
                # Quoted statement values may use dollars, thousands, millions or billions.
                result.update(value * scale for scale in (1, 1000, 1000000, 1000000000, 1000000000000))
        except InvalidOperation:
            pass
    return result


def relevant_facts(saved_facts, checks, filing_text):
    """Retain complete original fact records for selected numbers and source lines."""
    numbers = numeric_values(filing_text + "\n" + "\n".join(str(c.get("original", "")) + " " + c.get("candidate_quote", "") for c in checks))
    result = {}
    for name, data in sorted(saved_facts.items()):
        concepts = {}
        for key, concept in data.get("concepts", {}).items():
            periods = {period: copy.deepcopy(concept[period]) for period in ("current", "prior")
                       if concept.get(period) and Decimal(str(concept[period]["val"])) in numbers}
            if periods:
                concepts[key] = {**{k: copy.deepcopy(v) for k, v in concept.items() if k not in ("current", "prior")}, **periods}
        if concepts:
            result[name] = {**{k: copy.deepcopy(v) for k, v in data.items() if k != "concepts"}, "concepts": concepts}
        elif name == "input_facts.json" and data.get("price") is not None and Decimal(str(data["price"])) in numbers:
            result[name] = copy.deepcopy(data)
    return result


def plan_batches(checks, sources, report, instructions, schema_factory, normalize, error_type,
                 max_characters=MODEL_INPUT_CHAR_LIMIT, max_checks=MAX_CHECKS_PER_BATCH):
    if max_checks < 1 or max_characters < 1:
        raise ValueError("Semantic batch budgets must be positive.")
    ids = [c["id"] for c in checks]
    if len(set(ids)) != len(ids):
        raise error_type("Duplicate required semantic check IDs in the batch plan.")
    report_lines = report.splitlines()
    report_sections = blocks(report_lines)
    filings = {name: text.splitlines() for name, text in sorted(sources["saved_filing_texts"].items())}
    filing_sections = {name: blocks(lines, filing=True) for name, lines in filings.items()}
    units = []
    for check in checks:
        anchors = [check.get("quote")]
        report_selected = containing_sections(report_lines, report_sections, anchors, normalize,
                                              inline_anchors=[check.get("quoted_input")])
        if not report_selected:
            # Unknown claim location: keep the complete report rather than guessing away context.
            report_selected = set(range(len(report_sections)))
        report_selected = report_parents(report_lines, report_sections, report_selected)
        selected = {}
        name = check.get("file")
        if name in filings:
            source_anchors = [check.get("quote"), check.get("candidate_quote")]
            found = containing_sections(filings[name], filing_sections[name], source_anchors, normalize)
            if not found:
                # This is a failed source check; preserve the whole specified filing so
                # the semantic call can find a correction without invented evidence.
                found = set(range(len(filing_sections[name])))
            selected[name] = expand_references(filings[name], filing_sections[name], found)
        # Include source sections explicitly referenced by the claim/report preamble,
        # including distant unit/period headers and definition lines.
        report_text = "\n".join(report_lines[i] for section in report_selected for i in range(*report_sections[section]))
        for filename, lines in filings.items():
            if filename not in report_text:
                continue
            for match in re.finditer(re.escape(filename) + r"[^\n]*?\blines?\s+(\d+)(?:\s*[-–]\s*(\d+))?", report_text, re.I):
                lo, hi = int(match[1]), int(match[2] or match[1])
                if 0 < lo <= hi <= len(lines):
                    selected.setdefault(filename, set()).update(j for j, (a, b) in enumerate(filing_sections[filename]) if a < hi and lo <= b)
        units.append((check, report_selected, selected))

    def build(group):
        batch_checks = [unit[0] for unit in group]
        report_selected = set().union(*(unit[1] for unit in group)) if group else {0}
        chosen = {}
        for _, _, selected in group:
            for name, selected_sections in selected.items():
                chosen.setdefault(name, set()).update(selected_sections)
        source_texts = {name: excerpts(filings[name], filing_sections[name], expand_references(filings[name], filing_sections[name], selected))
                        for name, selected in sorted(chosen.items())}
        context = {"compiled_report": excerpts(report_lines, report_sections, report_selected) if group else (report_lines[0] if report_lines else ""),
                   "saved_filing_texts": source_texts,
                   "saved_facts": relevant_facts(sources["saved_facts"], batch_checks, "\n".join(source_texts.values()))}
        task = TASK + json.dumps(batch_checks, ensure_ascii=True)
        schema = schema_factory([c["id"] for c in batch_checks])
        return {"ids": [c["id"] for c in batch_checks], "task": task, "context": context, "schema": schema,
                "input_characters": request_characters(instructions, task, context, schema)}

    batches, current = [], []
    for unit in units:
        candidate = build(current + [unit])
        if current and (len(current) >= max_checks or candidate["input_characters"] > min(max_characters, PACKING_TARGET_CHARACTERS)):
            batches.append(build(current))
            current = []
            candidate = build([unit])
        if candidate["input_characters"] > max_characters:
            raise error_type(f"Semantic check {unit[0]['id']} requires {candidate['input_characters']} input characters; "
                             f"budget is {max_characters}. Required evidence cannot fit intact; no checks were skipped.")
        current.append(unit)
    if current or not batches:
        batch = build(current)
        if batch["input_characters"] > max_characters:
            raise error_type("Required Verifier context cannot fit intact, even with no semantic checks.")
        batches.append(batch)
    scheduled = [key for batch in batches for key in batch["ids"]]
    if scheduled != ids or len(set(scheduled)) != len(scheduled):
        raise error_type("Semantic batch plan did not cover every required check exactly once.")
    return batches
