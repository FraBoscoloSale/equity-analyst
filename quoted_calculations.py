"""Parse explicit two-input growth/ratio claims without asking a model to do math."""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from fractions import Fraction


NUM = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
AMOUNT = re.compile(
    rf'''["“‘']\s*(?P<currency>USD|EUR|GBP|CAD|AUD|JPY|[$€£¥])?\s*
    (?P<number>{NUM})\s*(?P<scale>thousand|million|billion|trillion|bn|[kmb])?
    \s*(?P<suffix>USD|EUR|GBP|CAD|AUD|JPY)?\s*["”’']''', re.I | re.X)
RATE = re.compile(rf"(?P<number>{NUM}(?:\s*/\s*{NUM})?)\s*(?P<unit>%|percent\b|times\b|multiple\b|x\b|×)", re.I)
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b", re.I)
SCALES = {"": Decimal(1), "thousand": Decimal(1000), "k": Decimal(1000),
          "million": Decimal(1000000), "m": Decimal(1000000), "billion": Decimal(1000000000),
          "bn": Decimal(1000000000), "b": Decimal(1000000000), "trillion": Decimal(1000000000000)}


def number(text):
    return Decimal(text.replace(",", ""))


def end_date(text):
    matches = list(DATE.finditer(text))
    if not matches:
        return None
    selected = matches[0]
    if len(matches) > 1 and re.search(r"\b(?:to|through)\b", text[selected.end():matches[1].start()]):
        selected = matches[1]
    value = selected.group().replace(",", "")
    for fmt in ("%Y-%m-%d", "%B %d %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


@dataclass
class QuotedInput:
    text: str
    number: str
    value: Decimal
    scale: Decimal
    currency: str
    period: str | None


@dataclass
class StatedResult:
    text: str
    start: int
    end: int
    value: Decimal
    percent: bool
    tolerance: Decimal


@dataclass
class QuotedCalculation:
    kind: str
    definition: str
    explicit_definition: bool
    inputs: list[QuotedInput]
    results: list[StatedResult]
    expression: str
    recomputed: Decimal | None
    exact: Fraction | None
    errors: list[str]


def parse_quoted_calculation(text):
    """Return only identifiable two-input claims; ambiguous prose stays unsupported.

    Percent results are computed in percentage units. Mixed scales are normalized
    before division. Growth inputs are ordered by their stated period, or by an
    explicit 'from ... to ...' construction, never by their relative magnitude.
    """
    quotes = list(AMOUNT.finditer(text))
    if len(quotes) != 2:
        return None
    head = text[:quotes[0].start()]
    growth = bool(re.search(r"\bgrowth\b|\bgrew\b|\brose\b|\bfell\b|\b(?:increase|decrease)d?\b", head, re.I))
    rates = [(rate, rate["unit"]) for rate in RATE.finditer(head)]
    if not rates and not growth and re.search(r"\bratio\b|\bmargin\b|\bmultiple\b|\bP/E\b", head, re.I):
        bare = re.search(rf"(?:\bwas\b|\bis\b|\bequals\b|:|=)\s*(?:(?:approximately|about|roughly)\s+)?(?P<number>{NUM})", head, re.I)
        if bare:
            rates = [(bare, "multiple")]
    if not rates:
        return None
    if not growth and not re.search(r"\bratio\b|\bmargin\b|\bmultiple\b|\byield\b|\breturn\b|\bP/E\b", head, re.I):
        if all(unit.lower() in ("%", "percent") for _, unit in rates):
            return None
    results = []
    for rate, unit in rates:
        literal = rate["number"]
        with localcontext() as context:
            context.prec = 50
            if "/" in literal:
                a, b = literal.split("/")
                if number(b.strip()) == 0:
                    return None
                value = number(a.strip()) / number(b.strip())
                tolerance = Decimal("1e-40")
            else:
                value = number(literal)
                tolerance = Decimal(1).scaleb(value.as_tuple().exponent) / 2
        results.append(StatedResult(literal, rate.start("number"), rate.end("number"), value,
                                    unit.lower() in ("%", "percent"), tolerance))
    if len({r.percent for r in results}) != 1:
        return None  # Conflicting result units are not a single interpretable claim.
    inputs = []
    for i, quote in enumerate(quotes):
        context_end = quotes[i + 1].start() if i + 1 < len(quotes) else len(text)
        suffix = text[quote.end():context_end]
        scale = SCALES[(quote["scale"] or "").lower()]
        currency = (quote["currency"] or quote["suffix"] or "").upper()
        inputs.append(QuotedInput(quote.group(), quote["number"], number(quote["number"]) * scale,
                                  scale, currency, end_date(suffix)))
    errors = []
    currencies = {x.currency for x in inputs if x.currency}
    if len(currencies) > 1 and not currencies <= {"$", "USD"}:
        errors.append("Quoted inputs have incompatible currencies.")
    explicit = bool(re.search(r"\bdefinition\s*:|\bdefined as\b|\bratio of\b|\bdivided by\b|\bnumerator\b", text, re.I))
    if growth:
        explicit = explicit or bool(re.search(r"\b(?:total revenue|net sales|net revenue|sales|net income|operating income|earnings)\b", head, re.I))
        if inputs[0].period and inputs[1].period and inputs[0].period != inputs[1].period:
            inputs.sort(key=lambda x: x.period, reverse=True)
        elif re.search(r"\bfrom\s*$", head, re.I) and re.match(r"\s*to\b", text[quotes[0].end():quotes[1].start()], re.I):
            inputs.reverse()
        else:
            errors.append("Growth input periods/order are not established.")
    # Preserve quoted amounts in the arithmetic when their scales match. Otherwise
    # show the normalized amounts so, for example, billions/thousands aren't divided raw.
    operands = [x.number.replace(",", "") for x in inputs] if inputs[0].scale == inputs[1].scale else [str(x.value) for x in inputs]
    expression = f"({operands[0]} / {operands[1]} - 1)" if growth else f"({operands[0]} / {operands[1]})"
    if results[0].percent:
        expression += " * 100"
    definition = ("Relative change: current / prior - 1" if growth else "Ratio: numerator / denominator")
    if not explicit:
        errors.append("The financial measure/ratio definition is not explicit.")
    with localcontext() as context:
        context.prec = 50
        if inputs[1].value == 0:
            computed = None
            exact = None
            errors.append("Quoted denominator is zero; calculation is undefined.")
        else:
            exact = Fraction(inputs[0].value) / Fraction(inputs[1].value) - (1 if growth else 0)
            if results[0].percent:
                exact *= 100
            computed = Decimal(exact.numerator) / Decimal(exact.denominator)
    return QuotedCalculation("growth" if growth else "ratio", definition, explicit, inputs, results,
                             expression, computed, exact, errors)


def correction_text(calculation, exact=False):
    if calculation.recomputed is None:
        return "N/A (undefined: zero denominator)"
    value = str(calculation.exact) if exact else format(calculation.recomputed, ".12f").rstrip("0").rstrip(".")
    # Do not round a nonzero very small result to zero.
    if value in ("0", "-0") and calculation.recomputed != 0:
        value = str(calculation.recomputed.normalize())
    return value + ("%" if calculation.results[0].percent else " multiple")
