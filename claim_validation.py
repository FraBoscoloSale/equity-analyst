"""Offline claim contracts: source identity, numeric provenance and risky language.

Free-form prose is not a theorem prover. High-risk statements therefore use
Python-generated comparisons or extractive filing sentences, not fuzzy entailment.
"""

import ast
import math
import operator
import re


def normalize(text):
    return re.sub(r'\s+', ' ', text).strip().rstrip('.')


def source_sentences(records):
    sentences = set()
    for row in records.values():
        if row['kind'] != 'text':
            continue
        text = row['text'].strip().lstrip('•■ ').strip()
        sentences.add(normalize(text))
        sentences.update(normalize(s) for s in re.split(r'(?<=[.!?])\s+(?=[A-Z])', text))
    return sentences


def financial_numeric_literals(text, supplied):
    """Exempt identifiers, never bare years, quantities, prices, percentages or ratios."""
    # Models commonly use a nonbreaking hyphen in form names (the saved run did).
    # Normalize metadata matching only; verbatim evidence matching stays exact.
    text = re.sub('[\u2010\u2011\u2012\u2013\u2212]', '-', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\b(?:10-[KQ](?:/A)?|8-K(?:/A)?|20-F|40-F|6-K|EX-\d+(?:\.\d+)?)\b', '', text, flags=re.I)
    text = re.sub(r'\b\d{10}-\d{2}-\d{6}\b', '', text)
    text = re.sub(r'\bus-gaap:[A-Za-z][A-Za-z0-9]*\b', '', text)
    # Opaque evidence IDs and tags must actually have been supplied. Removing a
    # made-up identifier would otherwise hide fabricated financial digits.
    for key in sorted(supplied, key=len, reverse=True):
        text = re.sub(r'(?<![\w.])' + re.escape(key) + r'(?![\w.])', '', text)
    for row in supplied.values():
        for field in ('start', 'end', 'filed', 'supplied_at'):
            value = row.get(field)
            if value:
                text = text.replace(str(value), '')
        if row.get('line'):
            text = re.sub(r'\bline\s+' + str(row['line']) + r'\b', '', text, flags=re.I)
    years = {str(row.get('fy')) for row in supplied.values() if row.get('fy')}
    years.update(row['end'][:4] for row in supplied.values() if row.get('end'))
    for year in years:
        text = re.sub(r'\bFY\s*' + year + r'\b', '', text, flags=re.I)
    return bool(re.search(r'\d', text))


def numeric_record_errors(key, supplied, seen=None):
    seen = set() if seen is None else seen
    if key in seen:
        return []
    seen.add(key)
    if key not in supplied:
        return [f'Missing numerical input record: {key}.']
    row = supplied[key]
    kind = row['kind']
    errors = []
    if kind == 'xbrl_fact':
        required = ('val', 'unit', 'accn', 'tag', 'form', 'end', 'filing_url')
        if any(row.get(field) is None or row.get(field) == '' for field in required):
            errors.append(f'Incomplete filing/accession/tag/period provenance: {key}.')
        if not isinstance(row.get('val'), (int, float)) or not math.isfinite(row['val']):
            errors.append(f'Invalid numeric fact: {key}.')
    elif kind == 'user_input':
        if not all(row.get(field) is not None for field in ('val', 'unit', 'input_field', 'supplied_at')):
            errors.append(f'Incomplete user input provenance: {key}.')
    elif kind == 'calculation':
        refs = row.get('inputs', [])
        if not refs or row.get('references') != refs or not row.get('definition') or not row.get('arithmetic'):
            errors.append(f'Incomplete calculator evidence: {key}.')
        for ref in refs:
            errors.extend(numeric_record_errors(ref, supplied, seen))
        if row.get('value') is not None:
            try:
                if any(supplied[ref]['kind'] == 'missing_input' for ref in refs):
                    raise ValueError('missing operand')
                expression = re.sub(r'(?<=\d),(?=\d)', '', row['arithmetic'])
                tree = ast.parse(expression, mode='eval')
                if len(list(ast.walk(tree))) > 64:
                    raise ValueError('oversized expression')
                operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
                operands = [supplied[ref]['val'] for ref in refs]
                position = 0
                def literal(value):
                    nonlocal position
                    # Printed Step 5 arithmetic rounds each displayed input to
                    # four decimals; validate its identity, then recompute using
                    # the exact source value, including very small EPS amounts.
                    if position < len(operands) and math.isclose(value, operands[position], rel_tol=1e-12, abs_tol=0.000050001):
                        result = operands[position]
                        position += 1
                        return result
                    if value in (1, 2):
                        return value  # growth and average-equity formula constants
                    raise ValueError('arithmetic operand does not match source input')
                def compute(node):
                    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                        return literal(node.value)
                    if isinstance(node, ast.BinOp) and type(node.op) in operations:
                        return operations[type(node.op)](compute(node.left), compute(node.right))
                    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
                        if isinstance(node.operand, ast.Constant):
                            return literal((-1 if isinstance(node.op, ast.USub) else 1) * node.operand.value)
                        return (-1 if isinstance(node.op, ast.USub) else 1) * compute(node.operand)
                    raise ValueError('non-arithmetic expression')
                calculated = compute(tree.body)
                if position != len(operands) or not math.isfinite(row['value']) or not math.isclose(calculated, row['value'], rel_tol=1e-9, abs_tol=1e-10):
                    raise ValueError('arithmetic mismatch')
            except (ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError, KeyError):
                errors.append(f'Calculator arithmetic does not reproduce the supplied value: {key}.')
        elif not row.get('reason'):
            errors.append(f'Missing explanation for unavailable calculation: {key}.')
    return errors


COMPARATIVE = re.compile(r'\b(?:higher|lower|highest|lowest|exceeds?|outperforms?|underperforms?|superior|inferior|'
                         r'leads?|lags?|ahead|behind|above|below|greater|less|better|worse|best|worst|ranking|ranked)\b', re.I)
CAUSAL_TREND = re.compile(r'\b(?:accelerat\w*|decelerat\w*|drives?|driven|driving|causes?|caused|causing|'
                          r'leads? to|led to|results? in|resulted in|because|due to|attributable to|'
                          r'without (?:reliance|relying|dependence)|independent of|not (?:reliant|dependent)|'
                          r'without (?:price|pricing)|faster|slower|momentum|cataly\w*|boosts?|fuels?|'
                          r'generates?|generating|enables?|enhances?|reflecting|contributes? to)\b', re.I)
MISSING_LANGUAGE = re.compile(r'\b(?:missing|unavailable|not (?:available|provided|supplied|disclosed|captured|present)|'
                              r'does not (?:disclose|provide)|lack(?:s|ing)?|cannot (?:calculate|compute|derive))\b', re.I)


def claim_errors(text, refs, supplied):
    selected = {key: supplied[key] for key in refs if key in supplied}
    errors = []
    canonical = {normalize(row['statement']) for row in selected.values() if 'statement' in row}
    extractive = source_sentences(selected)
    exact = normalize(text) in canonical or normalize(text) in extractive
    if not text.strip():
        errors.append('Analysis must contain substantive prose.')
    if financial_numeric_literals(text, supplied) and not exact:
        errors.append('Financial numeric prose must be rendered from deterministic evidence, not authored by the model.')
    if re.search(r'[{}]', text):
        errors.append('Inline interpolation placeholders are not supported.')
    for accession in re.findall(r'\b\d{10}-\d{2}-\d{6}\b', text):
        if accession not in {row.get('accn') for row in supplied.values()}:
            # Text evidence source_id contains its filing accession.
            if not any(accession in row.get('source_id', '') for row in supplied.values()):
                errors.append('Accession identifier in prose was not supplied as source metadata.')
    for tag in re.findall(r'\bus-gaap:[A-Za-z][A-Za-z0-9]*\b', text):
        if tag not in {row.get('tag') for row in supplied.values()}:
            errors.append('XBRL tag in prose was not supplied as source metadata.')
    if not refs:
        errors.append('Claim has no supplied evidence ID.')
    peer_context = any(key.startswith('peer.') or row['kind'] == 'peer_comparison' for key, row in selected.items())
    peer_context |= bool(re.search(r'\b(?:peers?|competitors?|companies|retailers)\b', text, re.I))
    metric_context = any(row['kind'] == 'calculation' for row in selected.values())
    numeric_metric_terms = bool(re.search(r'margin|ratio|multiple|\bEPS\b|earnings per share|revenue|net income|'
                                         r'growth|return on equity|cash.flow|leverage', text, re.I))
    peer_context |= numeric_metric_terms and bool(re.search(r'\b(?:highest|lowest|best|worst|ranked|outperforms?)\b', text, re.I))
    if COMPARATIVE.search(text) and (peer_context or metric_context):
        comparisons = {normalize(row['statement']) for row in selected.values() if row['kind'] == 'peer_comparison'}
        if normalize(text) not in comparisons:
            # Qualitative issuer claims can only be quoted exactly. Numeric peer
            # rankings always need the Python-computed comparison record.
            if metric_context or numeric_metric_terms or any(key.startswith('peer.') for key in selected) or not exact:
                errors.append('Unverified peer comparison: select the Python comparison record and its exact statement.')
    if re.search(r'\bsegments?\b', text, re.I) and normalize(text) not in extractive:
        errors.append('Segment terminology must reproduce an explicit supplied filing sentence identifying segments; categories are not segments.')
    if CAUSAL_TREND.search(text) and not exact:
        errors.append('Causality or acceleration is not established: use an exact supporting filing sentence or remove the inference.')
    if MISSING_LANGUAGE.search(text) and normalize(text) not in canonical:
        errors.append('Missing-data assertions must use a supplied inventory limitation record, not infer nondisclosure or unavailability.')
    # Explicitly named supporting metrics must be in this claim's evidence closure,
    # not merely elsewhere in the Advisor's supplied universe.
    metric_terms = {
        'operating_margin': r'operating.margin', 'revenue_growth': r'revenue.growth',
        'free_cash_flow': r'free.cash.flow',
        'debt_to_equity': r'debt.to.equity|\bleverage\b',
        'return_on_average_equity': r'return on (?:average )?equity',
        'pe': r'earnings multiple|price.to.earnings|\bP/E\b',
    }
    closure = set(selected)
    pending = list(selected)
    while pending:
        row = supplied[pending.pop()]
        for key in row.get('references', []):
            if key in supplied and key not in closure:
                closure.add(key)
                pending.append(key)
    if not exact:
        for metric, pattern in metric_terms.items():
            if re.search(pattern, text, re.I) and not any(
                key.endswith('.' + metric) and supplied[key]['kind'] == 'calculation' and supplied[key].get('value') is not None
                for key in closure):
                errors.append(f'Mentioned supporting metric is not backed by this claim\'s available numeric evidence: {metric}.')
    for key, row in selected.items():
        if row['kind'] in {'calculation', 'xbrl_fact', 'user_input'}:
            errors.extend(numeric_record_errors(key, supplied))
    return list(dict.fromkeys(errors))


REVIEW_CODES = ('unsupported_claim', 'contradiction', 'missing_analysis', 'definition_ambiguity',
                'invalid_provenance', 'missing_ratio_narrative', 'limitation_placement', 'additional_deciding_metrics')


def review_policy(code, text):
    """Reviewers cannot add requirements that contradict the evidence contract."""
    if code in {'missing_ratio_narrative', 'limitation_placement', 'additional_deciding_metrics', 'invalid_provenance'}:
        return 'Provenance is checked by Python; ratio narrative, limitation placement and extra deciding metrics are not requirements.'
    normalized = text.lower().replace('\u2011', '-').replace('\u2010', '-')
    if re.search(r'(?:without|lack\w*|no|missing).{0,50}(?:filing narrative|narrative evidence|narrative sentence)|'
                 r'without accompanying narrative', normalized):
        return 'A validated calculation needs its inputs and provenance, not a separate filing narrative.'
    if 'limitation' in normalized and re.search(r'separat|integrat|woven|embed|bullet', normalized):
        return 'Limitations are a separate schema field rendered by Python.'
    if re.search(r'(?:single|one) deciding metric', normalized) and re.search(r'other|additional|without', normalized):
        return 'One deciding metric is required; additional supported metrics are optional.'
    return None
