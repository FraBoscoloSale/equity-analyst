"""Deterministic Research briefs from persona-permitted desk outputs only.

The catalog remains complete on disk. Selection changes the role's allowlist,
not source text, financial values, or verification policy.
"""

import json
import re
from itertools import zip_longest

from semantic_evidence import UNIT, FOOTNOTE, NOTE, _period_header


TOPICS = re.compile(r'growth|competit|segment|customers?|demand|subscriptions?|'
                    r'market|revenue|net sales|operating|products?|services?', re.I)
CORE_ROW = re.compile(r'^\s*(?:total\s+)?revenues?\b|^\s*net sales\b|diluted', re.I)
# Leave room for both schema copies, personas, peer records and bounded revisions.
TEXT_BRIEF_LIMIT = 65_000


def _heading(text):
    if re.fullmatch(r'\s*(?:item\s+\d+[A-Za-z]?|part\s+[IVX]+)\s*', text, re.I):
        return False  # Page running labels are not subsection titles.
    words = re.findall(r'[A-Za-z]+', text)
    return bool(words and len(text) < 150 and '|' not in text and
                not re.search(r'[.!?;]$', text) and
                (text.isupper() or sum(w[0].isupper() for w in words) / len(words) > .65) and
                not UNIT.search(text) and not _period_header(text))


def _bundles(rows, error_type):
    """Whole HTML paragraphs or tables, with actual heading/column context.

    No character windows: a table is indivisible, as are wrapped paragraphs and
    referenced definition sections. Line numbers continue to refer to disk.
    """
    lines = [r['text'] for r in rows]
    headings = [i for i, text in enumerate(lines) if _heading(text)]
    result = []
    i = 0
    while i < len(rows):
        start = i
        table = '|' in lines[i] or UNIT.search(lines[i]) or _period_header(lines[i])
        i += 1
        if table:
            while i < len(rows) and (not lines[i].strip() or '|' in lines[i] or UNIT.search(lines[i])
                                      or _period_header(lines[i]) or FOOTNOTE.search(lines[i])):
                i += 1
        elif not _heading(lines[start]):
            while (i < len(rows) and lines[i].strip() and '|' not in lines[i] and
                   not _heading(lines[i]) and not re.search(r'[.!?;]$', lines[i-1].rstrip())):
                i += 1
        indexes = set(range(start, i))
        # Preserve the enclosing Item and nearest subsection heading.
        previous = [n for n in headings if n <= start]
        indexes.update(previous[-1:])
        indexes.add(0)
        missing = []
        if table:
            units = [n for n in range(i) if UNIT.search(lines[n])]
            periods = [n for n in range(i) if _period_header(lines[n]) or
                       (UNIT.search(lines[n]) and len(re.findall(r'\b(?:19|20)\d{2}\b', lines[n])) >= 2)]
            indexes.update(units[-1:]); indexes.update(periods[-1:])
            numeric_core = any('|' in lines[n] and re.search(r'\d', lines[n]) and CORE_ROW.search(lines[n])
                               for n in range(start, i))
            if numeric_core:
                monetary = any('|' in lines[n] and re.search(r'\d', lines[n]) and
                               re.search(r'^\s*(?:total\s+)?revenues?\b|^\s*net sales\b', lines[n], re.I)
                               for n in range(start, i))
                if not units and (monetary or not any(re.search(r'per.share|\bEPS\b', lines[n], re.I) for n in range(start, i))):
                    missing.append('units/scale for table')
                if not periods:
                    missing.append('period/column headers for table')
        # References are followed by title, never silently discarded. Only
        # permitted sections are searchable; an absent required note fails.
        pending = list(indexes)
        seen = set()
        while pending:
            n = pending.pop()
            if n in seen:
                continue
            seen.add(n)
            references = NOTE.findall(lines[n])
            references += re.findall(r'(?:refer to|see) (?:the )?([A-Za-z -]+?) section', lines[n], re.I)
            for reference in references:
                matches = [h for h in headings if re.match(r'^\s*' + re.escape(reference) + r'\b', lines[h], re.I)]
                if not matches:
                    missing.append(reference)
                    continue
                for h in matches:
                    end = next((v for v in headings if v > h), len(rows))
                    more = set(range(h, end)) - indexes
                    indexes.update(more); pending.extend(sorted(more))
        result.append((start, i, indexes, missing))
    return result


def research_evidence_ids(catalog, research, sources, error_type):
    """Keep complete relevant lines, their definitions, and period/unit context.

    Small inputs retain all permitted text. Large inputs prioritize revenue/EPS
    table rows and Step 6, then operating/growth/competitive narrative in source
    order. Every candidate closes over required context before size accounting.
    No required line is sliced, and essential evidence cannot be budget-skipped.
    """
    allowed = set()
    groups = []
    for alias, data in research['sources'].items():
        if alias not in {'annual', 'release'}:
            continue
        for field in (('business', 'mda') if alias == 'annual' else ('lines',)):
            rows = data.get(field, [])
            if rows:
                groups.append((alias, field, rows))
                allowed.update(catalog.line_ids[alias, row['line']] for row in rows)
    limits = {key for key in catalog.research_ids
              if catalog.records[key]['kind'] in {'limitation', 'missing_input'} and
              catalog.records[key]['source_id'] == 'evidence.inventory'}
    peer_keys = {key for key, row in catalog.records.items()
                 if key.startswith('peer.') and
                 (row.get('kind') == 'xbrl_fact' or
                  (row.get('kind') == 'calculation' and key.endswith('.operating_margin')))}
    # A peer margin may close only over peer facts, never subject Step 4/5 data.
    peers = catalog.select(peer_keys)
    if any(not key.startswith('peer.') for key in peers):
        raise error_type('Research peer evidence depends on a forbidden desk-step output.')
    step6 = {key for key in catalog.select(catalog.quarter_ids)
             if catalog.records[key]['kind'] == 'text'}
    if not step6 <= allowed:
        raise error_type('Research Step 6 evidence lies outside the permitted release text.')

    def size(keys):
        return len(json.dumps({key: catalog.records[key]['text'] for key in sorted(keys)}, ensure_ascii=False))

    if size(allowed) <= TEXT_BRIEF_LIMIT:
        return allowed | limits | set(peers)
    chosen = set()
    mandatory, optional_groups = [], []
    for alias, field, rows in groups:
        optional = []
        for start, end, indexes, missing in _bundles(rows, error_type):
            targets = rows[start:end]
            candidate = (alias, rows, indexes, missing)
            if (start == 0 or any(catalog.line_ids[alias, row['line']] in step6 or
                (re.search(r'\d', row['text']) and CORE_ROW.search(row['text']))
                for row in targets)):
                mandatory.append(candidate)
            elif any(TOPICS.search(row['text']) for row in targets):
                optional.append(candidate)
        optional_groups.append(optional)
    # Do not let a long Business section crowd out all MD&A/release narrative.
    optional = [candidate for turn in zip_longest(*optional_groups)
                for candidate in turn if candidate is not None]
    for required, candidates in ((True, mandatory), (False, optional)):
        for alias, rows, indexes, missing in candidates:
            if missing:
                if required:
                    raise error_type('Required Research evidence is missing referenced ' + ', '.join(missing))
                continue
            additions = {catalog.line_ids[alias, rows[index]['line']] for index in indexes}
            if not additions <= allowed:
                if required:
                    raise error_type('Required Research context is outside the permitted filing sections.')
                continue
            if size(chosen | additions) > TEXT_BRIEF_LIMIT:
                if required:
                    raise error_type('Required Research filing evidence exceeds the brief budget intact; no evidence was truncated.')
                continue
            chosen.update(additions)
    if not step6 <= chosen:
        raise error_type('Required Research Step 6 source lines were not selected.')
    return chosen | limits | set(peers)
