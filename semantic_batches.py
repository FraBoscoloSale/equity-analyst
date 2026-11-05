"""Deterministic, lossless selection and packing of semantic verification evidence."""

import json
import re

from model_budget import MODEL_INPUT_CHAR_LIMIT, request_characters
from semantic_evidence import EvidenceSelector, merge_facts


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
    "line/paragraph is complete, including its selected headers and required notes; gaps contain no evidence "
    "selected for this batch. This batch is fixed by Python. Check every supplied id. Code checks: "
)
FILING_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s|item\s+\d+[A-Za-z]?\b|note\s+\d+\b|"
    r"(?:consolidated\s+)?(?:statements?\s+of\b|balance\s+sheets?\b|income\s+statements?\b))", re.I)
REPORT_HEADING = re.compile(r"^(#{1,6})\s")


def blocks(lines, filing=False):
    """Complete sections bounded by actual headings, never by character clipping."""
    starts = [0] + [i for i, line in enumerate(lines) if i and
                    (FILING_HEADING.match(line) if filing else REPORT_HEADING.match(line))]
    return [(a, b) for a, b in zip(starts, starts[1:] + [len(lines)]) if a < b]


def plan_batches(checks, sources, report, instructions, schema_factory, normalize, error_type,
                 max_characters=MODEL_INPUT_CHAR_LIMIT, max_checks=MAX_CHECKS_PER_BATCH):
    if max_checks < 1 or max_characters < 1:
        raise ValueError("Semantic batch budgets must be positive.")
    ids = [c["id"] for c in checks]
    if len(set(ids)) != len(ids):
        raise error_type("Duplicate required semantic check IDs in the batch plan.")
    report_lines = report.splitlines()
    filings = {name: text.splitlines() for name, text in sorted(sources["saved_filing_texts"].items())}
    filing_sections = {name: blocks(lines, filing=True) for name, lines in filings.items()}
    selector = EvidenceSelector(sources, report, filing_sections, normalize, error_type)
    units = [selector.unit(check) for check in checks]

    def build(group):
        batch_checks = [unit[0] for unit in group]
        report_selected = set().union(*(unit[1] for unit in group)) if group else {0}
        chosen = {}
        for _, _, selected, _ in group:
            for name, selected_sections in selected.items():
                chosen.setdefault(name, set()).update(selected_sections)
        source_texts = {name: "\n".join(f"L{i+1}: {filings[name][i]}" for i in sorted(selected))
                        for name, selected in sorted(chosen.items())}
        context = {"compiled_report": "\n".join(f"L{i+1}: {report_lines[i]}" for i in sorted(report_selected)) if group else (report_lines[0] if report_lines else ""),
                   "saved_filing_texts": source_texts,
                   "saved_facts": merge_facts(unit[3] for unit in group)}
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
