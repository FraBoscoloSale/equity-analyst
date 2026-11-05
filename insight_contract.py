"""Single-claim, filing-line insights; no issuer-specific rules or model calls."""

import re
from decimal import Decimal

from claim_validation import claim_errors, normalize, source_sentences
from quoted_calculations import DATE, NUM, SCALES


INSIGHT = re.compile(r'^Insight: (?P<claim>[^\n]+?) \[(?P<file>[^\[\],\n]+\.txt), line (?P<line>[1-9]\d*)\]$')
INSIGHT_LABEL = re.compile(r'\bInsight:')


def insight_locations(parts, owners):
    """The last owned part is each role's one location, in report order."""
    return {owners[part]: part for part in parts}


INSIGHT_OUTPUT_CONTRACT = (
    'Structured Insight output contract: when expected_output requires insight, return the complete '
    'standalone final line in insight.text, its designated part in insight.part, and exactly one supplied '
    'filing-text ID in insight.evidence_ids. The whole line must begin exactly Insight: and include '
    '[filing.txt, line N]. This field is the role-written final block of that part; do not also put an '
    'Insight in sections, thesis, or reason. sections contains ordinary analysis blocks. The Insight '
    'field is the explicit exception to the ordinary prose rule against numbers and inline citations. '
    'Copy supported source text and its real citation; do not invent a number, source, or evidence ID. '
    'If no supplied text evidence exists, return null for insight; that is a missing-Insight failure, '
    'never a passing substitute. When the schema has no insight field on a part-only revision, return '
    'only the requested parts without a new Insight.'
)


def insight_block(insight):
    """A view of the model's field, never a generated or repaired Insight."""
    return {'text': insight['text'], 'evidence_ids': insight['evidence_ids']}


def role_sections(result):
    """Logical section view for review; the stored model response is unchanged."""
    sections = {part: list(blocks) for part, blocks in result['sections'].items()}
    insight = result.get('insight')
    if isinstance(insight, dict):
        sections.setdefault(insight['part'], []).extend([insight_block(insight)])
    return sections


def parse_insight(text):
    match = INSIGHT.fullmatch(text)
    return match.groupdict() if match else None


def insight_errors(block, supplied):
    """Allow numbers only in one extractive claim, bound to one supplied line."""
    parsed = parse_insight(block['text'])
    if not parsed:
        return ['Insight must be one line: Insight: one claim [filing.txt, line N].']
    refs = block.get('evidence_ids', [])
    if len(refs) != 1 or refs[0] not in supplied or supplied[refs[0]].get('kind') != 'text':
        return ['Insight must select exactly one supplied filing-text evidence ID.']
    row = supplied[refs[0]]
    if row['source_id'].rsplit(':', 1)[-1] != parsed['file'] or row['line'] != int(parsed['line']):
        return ['Insight filing and line differ from its supplied evidence record.']
    claim = parsed['claim']
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', claim)
    if len(sentences) != 1 or ';' in claim:
        return ['Insight must state one claim, not multiple sentences or semicolon-separated claims.']
    if normalize(claim) not in source_sentences({refs[0]: row}):
        return ['Insight must reproduce one supported sentence or single table row from its cited filing line.']
    return claim_errors(claim, refs, supplied)


def opening_paragraph(text):
    """Saved HTML text has one paragraph per line; also retain wrapped paragraphs.

    Skip exhibit identifiers, titles, date-only headings and table rows. Choose
    the first narrative paragraph, never the first paragraph with a financial value.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        clean = line.strip()
        words = re.findall(r'[A-Za-z]+', clean)
        if (not clean or '|' in clean or clean.isupper() or len(words) < 3
                or clean.startswith(('http://', 'https://', '#'))):
            continue
        gathered = [clean]
        end = index
        while not re.search(r'[.!?][\"\u201d\)]?$', gathered[-1]) and end + 1 < len(lines):
            next_line = lines[end + 1].strip()
            if not next_line or '|' in next_line or next_line.isupper():
                break
            gathered.append(next_line)
            end += 1
        paragraph = ' '.join(gathered)
        if re.search(r'[.!?][\"\u201d\)]?$', paragraph):
            return {'file': 'earnings_release.txt', 'line': index + 1,
                    'end_line': end + 1, 'text': paragraph}
    return {'file': 'earnings_release.txt', 'line': None, 'end_line': None, 'text': ''}


def figures(text):
    """Normalize displayed scales for the reviewer's headline-overlap evidence."""
    clean = DATE.sub(' ', text)
    clean = re.sub(r'\b\d+[ -](?:weeks?|months?|years?)\b|\b(?:FY\s*)?(?:19|20)\d{2}\b', ' ', clean, flags=re.I)
    result = set()
    for match in re.finditer(rf'(?<![\w.])(?P<currency>[$\u20ac\u00a3]|USD\s*)?\s*(?P<number>{NUM})\s*'
                             r'(?P<scale>thousand|million|billion|trillion|bn)?\s*(?P<percent>%|percent\b)?', clean, re.I):
        scale = SCALES.get((match['scale'] or '').lower(), Decimal(1))
        value = Decimal(match['number'].replace(',', '')) * scale
        unit = 'percent' if match['percent'] else (match['currency'] or 'amount' if match['scale'] else match['currency'] or 'number').strip()
        if unit in ('$', 'USD'): unit = 'amount'
        result.add((unit, value))
    return result


def part_insight_audit(blocks, supplied, opening):
    insights = [block for block in blocks if block['text'].lstrip().startswith('Insight:')]
    issues = []
    if len(insights) != 1 or not blocks or insights[0] is not blocks[-1]:
        issues.append('Report part must end with exactly one Insight: line.')
    for block in insights:
        issues.extend(insight_errors(block, supplied))
    parsed = parse_insight(insights[0]['text']) if len(insights) == 1 else None
    claim = parsed['claim'] if parsed else ''
    overlap = figures(claim) & figures(opening['text'])
    # Exact headline restatements fail mechanically. Paraphrases and differences
    # in measure/period require the Manager's mandatory separate semantic check.
    if figures(claim) and normalize(claim).casefold() in normalize(opening['text']).casefold():
        issues.append('Insight only repeats a figure already stated in the earnings release first paragraph.')
    return issues, {'insight': insights[0]['text'] if len(insights) == 1 else None,
                    'opening_figure_overlap': [{'unit': unit, 'value': format(value.normalize(), 'f')}
                                               for unit, value in sorted(overlap)]}


def role_insight_audit(sections, supplied, opening, designated_part, extra_text=()):
    """Audit the whole role; attribute failures to parts without editing output."""
    errors = {part: [] for part in sections}
    errors.setdefault(designated_part, [])
    occurrences = [(part, index, block, len(INSIGHT_LABEL.findall(block['text'])))
                   for part, blocks in sections.items() for index, block in enumerate(blocks)
                   if INSIGHT_LABEL.search(block['text'])]
    extra_count = sum(len(INSIGHT_LABEL.findall(text)) for text in extra_text)
    count = sum(n for _, _, _, n in occurrences) + extra_count
    if count != 1:
        message = f'Role must have exactly one Insight: across its output; found {count}.'
        errors[designated_part].append(message)
        for part, _, _, _ in occurrences:
            if part != designated_part:
                errors[part].append(message)
    if extra_count:
        errors[designated_part].append('Insight must be in its designated report part, not thesis or recommendation reason.')
    issues, audit = part_insight_audit(sections.get(designated_part, []), supplied, opening)
    errors[designated_part].extend(issues)
    for part, index, block, n in occurrences:
        if part != designated_part:
            errors[part].append('Misplaced Insight: this role must put its sole Insight in ' + designated_part + '.')
            errors[part].extend(insight_errors(block, supplied))
        if n != 1 or parse_insight(block['text']) is None or index != len(sections[part]) - 1:
            errors[part].append('Insight must be a standalone final line in a separate final block; embedded or non-final Insights fail.')
    audit.update({'designated_part': designated_part, 'insight_count': count,
                  'occurrences': [{'part': part, 'block': index, 'text': block['text'], 'count': n}
                                  for part, index, block, n in occurrences],
                  'outside_sections_count': extra_count})
    return errors, audit
