"""Resolve semantic evidence by citation, paragraph and table metadata, not file size."""

import copy
import json
import re
from decimal import Decimal, InvalidOperation

from quoted_calculations import DATE


UNIT = re.compile(r'\bin (?:thousands|millions|billions)\b|\b(?:amounts|dollars|figures) (?:are |shown |expressed )?in\b|'
                  r'except.{0,45}per.share|\b(?:USD|EUR|GBP|CAD|AUD|JPY)\b', re.I)
PERIOD = re.compile(r'\b(?:weeks?|months?|years?|quarters?) (?:ended|ending)\b|fiscal years? (?:ends?|ended)|'
                    r'references to.{0,70}(?:relate|refer)|\b(?:FY\s*)?(?:19|20)\d{2}\s+(?:versus|vs\.?|compared to)\s+(?:19|20)\d{2}', re.I)
DEFINITION = re.compile(r'\bdefin(?:ed|ition)\b|\bmeans\b|\b(?:includes?|excludes?|excluding|calculated)\b', re.I)
NOTE = re.compile(r'\b(?:see|refer to|described in|defined in|definition in)\s+(?:the\s+)?(note\s+\d+|item\s+\d+[A-Za-z]?)\b', re.I)
FOOTNOTE = re.compile(r'^\s*(?:footnote\b|required (?:definition|evidence|explanation)\b|\*|\(\d+\)\s+[A-Za-z])', re.I)
MONEY = re.compile(r'[$\u20ac\u00a3]\s*\(?\d|\b(?:thousand|million|billion|trillion)\b', re.I)
STOP = set('the a an as at be by for from in is it of on or our that this to was were with and '
           'line filing saved source statement claim insight measure value number original reported '
           'period annual current prior year years million millions dollars usd '
           'we us company issuer new net total compared versus increase increased decrease decreased'.split())
STOP.update('one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split())


def _terms(text):
    return {word.lower().rstrip('s') for word in re.findall(r'[A-Za-z]{3,}', text) if word.lower() not in STOP}


def _year_columns(line):
    """Year columns can share a row with a units caption and change labels.

    Inspect whole cells, not arbitrary year-looking tokens in a numeric body
    row. Keep the original line intact so column positions remain auditable.
    """
    if re.fullmatch(r'[\s|]*(?:(?:19|20)\d{2}[\s|]*){2,}', line):
        return True
    cells = [cell.strip() for cell in line.split('|') if cell.strip()]
    years = [cell for cell in cells if re.fullmatch(r'(?:FY\s*)?(?:19|20)\d{2}', cell, re.I)]
    if len(years) < 2:
        return False
    for cell in cells:
        if cell in years:
            continue
        units_caption = (UNIT.search(cell) and
                         re.match(r'^\(?\s*(?:(?:[$\u20ac\u00a3]\s+)?in\b|amounts\b|dollars\b|figures\b|except\b)', cell, re.I) and
                         not re.search(r'\d', cell))
        column_label = re.fullmatch(r'(?:percentage|percent|%)?\s*change(?:\s+(?:Y/Y|YoY|year.over.year))?|'
                                   r'(?:fiscal\s+)?years?(?:\s+ended)?|'
                                   r'as reported(?:\s*\(GAAP\))?|as adjusted(?:\s*\(non-GAAP\))?', cell, re.I)
        if not (units_caption or column_label):
            return False
    return True


def _comparison_period_header(line):
    return bool(re.fullmatch(r'\s*(?:fiscal\s+)?(?:year|quarter)\s+(?:19|20)\d{2}\s+'
                            r'(?:compared\s+(?:with|to)|versus|vs\.?)\s+'
                            r'(?:(?:fiscal\s+)?(?:year|quarter)\s+)?(?:19|20)\d{2}\s*', line, re.I))


def _period_header(line):
    # Full dates in ordinary narrative are not automatically column headers.
    remainder = DATE.sub('', line).strip(' |,:;()-')
    return bool(PERIOD.search(line) or (DATE.search(line) and not remainder) or
                _year_columns(line) or _comparison_period_header(line))


def _paragraphs(lines):
    spans = []
    start = None
    for i, line in enumerate(lines + ['']):
        if re.match(r'^#{1,6}\s|^<!--', line):
            if start is not None: spans.append((start,i)); start=None
            continue
        if line.strip() and start is None: start = i
        if not line.strip() and start is not None:
            spans.append((start, i)); start = None
    return spans


def _identity(row, wanted, ticker):
    return ((not wanted.get('ticker') or wanted['ticker']==ticker) and
            all(row.get(k) == wanted.get(k) for k in ('tag','start','end','unit','accn','val') if k in wanted))


def merge_facts(groups):
    result = {}
    for group in groups:
        for filename, data in group.items():
            if filename not in result:
                result[filename] = copy.deepcopy(data)
            else:
                for concept, records in data.get('concepts', {}).items():
                    result[filename]['concepts'].setdefault(concept, {}).update(copy.deepcopy(records))
    return result


class EvidenceSelector:
    def __init__(self, sources, report, sections, normalize, error_type):
        self.sources, self.report = sources, report.splitlines()
        self.filings = {name:text.splitlines() for name,text in sources['saved_filing_texts'].items()}
        self.sections, self.normalize, self.error_type = sections, normalize, error_type
        self.paragraphs = _paragraphs(self.report)

    def fail(self, check, message):
        raise self.error_type(f"Semantic check {check['id']}: required evidence {message}; no checks were skipped.")

    def matches(self, lines, anchor):
        if not anchor: return set()
        needle = self.normalize(anchor)
        # Numeric page markers must not match arbitrary digits inside other rows.
        inline = bool(re.search(r'[A-Za-z]{2}', anchor))
        selected = {i for i,line in enumerate(lines) if self.normalize(line) == needle or
                    (inline and needle in self.normalize(line))}
        if not selected and inline:
            # A PDF paragraph can wrap across saved lines. Keep every complete
            # original line touched by the exact normalized quotation.
            fragments=[]; offsets=[]; position=0
            for i,line in enumerate(lines):
                fragment=self.normalize(line)
                offsets.append((position,position+len(fragment),i))
                fragments.append(fragment); position+=len(fragment)
            joined=''.join(fragments); start=joined.find(needle)
            while start>=0:
                end=start+len(needle)
                selected.update(i for a,b,i in offsets if a<end and start<b)
                start=joined.find(needle,start+1)
        return selected

    def report_lines(self, check):
        binding = check.get('source_binding') or {}
        indexes = set()
        for anchor in (binding.get('statement'), check.get('quoted_input'), check.get('quote')):
            indexes.update(self.matches(self.report, anchor))
        if not indexes:
            self.fail(check, 'has no resolvable compiled-report claim')
        selected = {0} if self.report else set()
        for a,b in self.paragraphs:
            if indexes & set(range(a,b)):
                selected.update(range(a,b))
                # A quotation's separate following citation belongs to that quote.
                n=b
                while n<len(self.report) and not self.report[n].strip(): n+=1
                if n<len(self.report) and self.report[n].startswith('['): selected.add(n)
        # Keep heading ancestry and explicit unit/definition preambles, not all
        # unrelated prose/evidence elsewhere in the same report part.
        stack=[]
        for i,line in enumerate(self.report):
            heading=re.match(r'^(#{1,6})\s',line)
            if heading:
                level=len(heading[1])
                while stack and stack[-1][0]>=level: stack.pop()
                stack.append((level,i))
            if i in indexes:
                selected.update(n for _,n in stack)
                for _,n in stack:
                    j=n+1
                    while j<len(self.report) and not self.report[j].strip(): j+=1
                    end=j
                    while end<len(self.report) and self.report[end].strip() and not re.match(r'^#{1,6}\s|^<!--',self.report[end]): end+=1
                    if any(UNIT.search(line) or DEFINITION.search(line) for line in self.report[j:end]):
                        selected.update(range(j,end))
        return selected

    def references(self, text):
        found={}
        for filename in self.filings:
            for match in re.finditer(re.escape(filename)+r'[^\n]*?\blines?\s+(\d+)(?:\s*[-\u2013]\s*(\d+))?',text or '',re.I):
                lo,hi=int(match[1]),int(match[2] or match[1])
                if 0<lo<=hi and hi-lo<=1000:
                    found.setdefault(filename,set()).update(range(lo-1,hi))
        return found

    def source_context(self, check, filename, anchors):
        lines=self.filings[filename]; selected=set()
        for anchor in sorted(anchors):
            if not 0<=anchor<len(lines): continue  # Invalid citation stays failed in code checks.
            a,b=next((a,b) for a,b in self.sections[filename] if a<=anchor<b)
            selected.add(anchor); selected.add(a)
            target=lines[anchor]
            terms=_terms(target)
            # Nearest complete metadata lines in the actual enclosing source
            # section. This searches across intervening table rows, not a clipped
            # character window, so distant column headers remain available.
            units=[i for i in range(a,anchor+1) if UNIT.search(lines[i])]
            periods=[i for i in range(a,anchor+1) if _period_header(lines[i])]
            # Notes inherit document/Item preambles before the first note, not
            # arbitrary units or dates from an unrelated preceding note.
            if re.match(r'^\s*note\s+\d+\b',lines[a],re.I):
                item=max([0]+[i for i in range(a) if re.match(r'^\s*item\s+\d+',lines[i],re.I)])
                first_note=next(i for i in range(item,a+1) if re.match(r'^\s*note\s+\d+\b',lines[i],re.I))
                if not units: units=[i for i in range(item,first_note) if UNIT.search(lines[i])]
                if not periods: periods=[i for i in range(item,first_note) if _period_header(lines[i])]
            selected.update(units[-1:])
            date_headers=[i for i in periods if DATE.search(lines[i])]
            year_headers=[i for i in periods if _year_columns(lines[i])]
            duration_headers=[i for i in periods if re.search(r'\b\d+\s+(?:weeks?|months?) (?:ended|ending)\b',lines[i],re.I)]
            selected.update(date_headers[-1:]); selected.update(year_headers[-1:]); selected.update(duration_headers[-1:])
            if not (date_headers or year_headers or duration_headers): selected.update(periods[-1:])
            if anchor>a and len(lines[anchor-1])<160 and lines[anchor-1].rstrip().endswith(':'):
                selected.add(anchor-1)
            # Include the current table's label/column-header rows and any directly
            # attached footnotes, while excluding unrelated numeric body rows.
            if '|' in target:
                top=anchor
                while top>a and ('|' in lines[top-1] or not lines[top-1].strip()): top-=1
                selected.update(i for i in range(top,anchor) if not re.search(r'\d',lines[i]) and lines[i].strip())
                if top>a and len(lines[top-1])<160 and (lines[top-1].isupper() or lines[top-1].rstrip().endswith(':')):
                    selected.add(top-1)
                # A fiscal-year/quarter comparison caption may follow the table
                # and its explanatory footnotes. Require the same year columns;
                # never borrow a caption from another table or unrelated prose.
                bottom=anchor+1
                while bottom<b and ('|' in lines[bottom] or not lines[bottom].strip()): bottom+=1
                table_years=set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',lines[year_headers[-1]])) if year_headers else set()
                for i in range(bottom,b):
                    if _comparison_period_header(lines[i]):
                        if table_years and set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',lines[i]))==table_years:
                            selected.add(i)
                        break
                    if lines[i].strip() and not (FOOTNOTE.search(lines[i]) or DEFINITION.search(lines[i])):
                        break
            for i in range(a,b):
                line=lines[i]
                # Inspect each definition's sentence subject, not unrelated
                # words earlier in a long MD&A paragraph.
                boundaries=[m.end() for m in re.finditer(r'(?<=[.!?;])\s+(?=[A-Z])',line)]
                labels=[_terms(line[max([0]+[n for n in boundaries if n<=d.start()]):d.start()])
                        for d in DEFINITION.finditer(line)]
                marked=bool(re.search(r'\(\d+\)|\*',target.split('|',1)[0]))
                footnote=(FOOTNOTE.search(line) and
                          (i==anchor+1 or marked or terms & _terms(line)))
                if (footnote or re.match(r'^\s*required (?:definition|evidence|explanation)\b',line,re.I)
                        or any(terms & label for label in labels)
                        or (i==anchor+1 and NOTE.search(line))):
                    selected.add(i)
            # Keep complete wrapped continuation paragraphs around a selected
            # definition. Plain-text HTML paragraphs usually occupy one line.
            for i in list(selected):
                if (i+1<b and '|' not in lines[i] and
                        (re.search(r'[-:]$',lines[i].rstrip()) or
                         ((i==anchor or DEFINITION.search(lines[i])) and not re.search(r'[.!?;]$',lines[i].rstrip())))):
                    j=i+1
                    while j<b and lines[j].strip() and '|' not in lines[j] and not re.match(r'^\s*[\u2022*]',lines[j]):
                        selected.add(j)
                        if re.search(r'[.!?;]$',lines[j].rstrip()): break
                        j+=1
            # A table amount needs its filing's actual scale and period headers;
            # a claimed period or matching fact alone is not source context.
            financial_table='|' in target and bool(re.search(r'[A-Za-z]{2}',target)) and bool(re.search(r'\d',target)) and not _period_header(target)
            if financial_table:
                if not (units or re.search(r'per.share|\bEPS\b',target,re.I)):
                    self.fail(check, f'is missing units/scale context for {filename}, line {anchor+1}')
            explicit_period=bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(check.get('period',''))))
            if financial_table or (MONEY.search(target) and explicit_period):
                if not periods and not DATE.search(target):
                    self.fail(check, f'is missing period/column context for {filename}, line {anchor+1}')
        # Follow explicit required note/item references. Referenced definitions
        # are never shortened to fit; unresolved references fail closed.
        pending=list(sorted(selected)); seen=set()
        while pending:
            i=pending.pop()
            if i in seen: continue
            seen.add(i)
            for reference in NOTE.findall(lines[i]):
                matches=[(a,b) for a,b in self.sections[filename]
                         if re.match(r'^\s*'+re.escape(reference)+r'\b',lines[a],re.I)]
                if not matches: self.fail(check, 'is missing referenced '+reference+' in '+filename)
                for a,b in matches:
                    additions=set(range(a,b))-selected
                    selected.update(additions); pending.extend(sorted(additions))
        return selected

    def facts(self, check):
        chosen={}
        def keep(filename, concept, period, data):
            target=chosen.setdefault(filename,{k:copy.deepcopy(data[k]) for k in ('company','currency','source_url') if k in data})
            target.setdefault('concepts',{}).setdefault(concept,{k:copy.deepcopy(v) for k,v in data['concepts'][concept].items() if k not in ('current','prior')})
            target['concepts'][concept][period]=copy.deepcopy(data['concepts'][concept][period])
        bound=[]
        for operand in (check.get('source_binding') or {}).get('operands',[]):
            fact=(operand.get('source') or {}).get('fact')
            if fact: bound.append(fact)
        for operand in (check.get('calculation_context') or {}).get('operands',[]):
            if operand.get('fact'): bound.append({'record':operand['fact']})
        if not bound:
            try:
                row=json.loads(check.get('quote',''))
                if isinstance(row,dict) and row.get('tag'): bound=[{'record':row}]
            except (ValueError,TypeError): pass
        wanted_values=set()
        if not bound:
            try:
                value=Decimal(str(check.get('original','')).replace(',',''))
                wanted_values={value*s for s in (1,1000,1000000,1000000000,1000000000000)}
            except InvalidOperation: pass
        for filename,data in sorted(self.sources['saved_facts'].items()):
            for concept, observations in data.get('concepts',{}).items():
                for period in ('current','prior'):
                    row=observations.get(period)
                    if not row: continue
                    exact=any((ref.get('file',filename)==filename and
                               (ref.get('pointer')==f'/concepts/{concept}/{period}' or
                                _identity(row,ref['record'],data.get('company',{}).get('ticker')))) for ref in bound)
                    period_ok=not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(check.get('period',''))) or row.get('end')==check['period']
                    if exact or (not bound and period_ok and Decimal(str(row['val'])) in wanted_values):
                        keep(filename,concept,period,data)
        if bound and not chosen and check.get('mark')=='correct':
            self.fail(check,'has no exact saved XBRL fact for its citation')
        return chosen

    def unit(self, check):
        report=self.report_lines(check)
        selected={}
        # Only citations attached to this check/claim are source anchors. Never
        # collect all other citations in the surrounding report section.
        references=self.references(check.get('quoted_input',''))
        for operand in (check.get('calculation_context') or {}).get('operands',[]):
            for name, refs in self.references(operand.get('source','')).items():
                references.setdefault(name,set()).update(refs)
        for operand in (check.get('source_binding') or {}).get('operands',[]):
            filing=(operand.get('source') or {}).get('filing')
            if filing and filing.get('file') in self.filings:
                refs=references.setdefault(filing['file'],set())
                refs.add(filing['line']-1)
                refs.update(row['line']-1 for row in filing.get('context',[]))
        filename=check.get('file')
        if filename in self.filings:
            anchors=references.setdefault(filename,set())
            for quote in (check.get('quote'),check.get('candidate_quote')):
                anchors.update(self.matches(self.filings[filename],quote))
            if not any(0<=i<len(self.filings[filename]) for i in anchors):
                self.fail(check,'has no resolvable saved filing line in '+filename)
        for filename,anchors in references.items():
            selected[filename]=self.source_context(check,filename,anchors)
        return check,report,selected,self.facts(check)
