"""Explicit numeric statements bound to saved sources in the desk-record contract.

The report's own assertions are untrusted. Each displayed numeric span must have
a source-bound expression; all other financial literals remain unsupported.
"""

import ast
import copy
import json
import re
from datetime import datetime
from decimal import Decimal, localcontext

from quoted_calculations import DATE


NUMBER = re.compile(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?!\w|\.\d)")


def decimal(value):
    result = Decimal(str(value).replace(',', ''))
    if isinstance(value, bool) or not result.is_finite():
        raise ValueError('Invalid financial number')
    return result


def expression_value(expression, operands):
    tree = ast.parse(expression, mode='eval')
    if len(list(ast.walk(tree))) > 64:
        raise ValueError('Oversized claim expression')
    used = set()
    constants = set()
    def walk(node):
        if isinstance(node, ast.Name) and node.id in operands:
            used.add(node.id)
            return operands[node.id]
        if isinstance(node, ast.Constant) and type(node.value) in (int,float) and node.value in (1,2,100):
            constants.add(str(node.value))
            return decimal(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            return walk(node.operand) * (-1 if isinstance(node.op,ast.USub) else 1)
        if isinstance(node, ast.BinOp):
            a,b=walk(node.left),walk(node.right)
            if isinstance(node.op,ast.Add): return a+b
            if isinstance(node.op,ast.Sub): return a-b
            if isinstance(node.op,ast.Mult): return a*b
            if isinstance(node.op,ast.Div): return a/b
        raise ValueError('Only source input names and +, -, *, / with formula constants 1, 2, 100 are allowed')
    with localcontext() as context:
        context.prec=50
        return walk(tree.body), used, constants


def expression_dimension(expression, units):
    def walk(node):
        if isinstance(node,ast.Name): return {units[node.id]:1}
        if isinstance(node,ast.Constant): return {}
        if isinstance(node,ast.UnaryOp): return walk(node.operand)
        a,b=walk(node.left),walk(node.right)
        if isinstance(node.op,(ast.Add,ast.Sub)):
            if a!=b: raise ValueError('Addition/subtraction requires matching source units')
            return a
        result=dict(a)
        for unit,power in b.items(): result[unit]=result.get(unit,0)+power*(-1 if isinstance(node.op,ast.Div) else 1)
        return {unit:power for unit,power in result.items() if power}
    return walk(ast.parse(expression,mode='eval').body)


def render_claim(payload):
    record=payload['record']
    lines=[record['statement'],'','Quoted inputs and source citations:','']
    seen=set()
    for operand in record['inputs']:
        lines.extend([f"{operand['name']}: \"{operand['quoted']}\" {operand['unit']} (scale {operand['scale']}).",''])
        source=operand.get('source') or {}
        fact=source.get('fact')
        if fact:
            row=fact['record']
            lines.extend([f"[{fact['file']} {fact['pointer']}; {row['tag']}; {row.get('start','instant')} to {row['end']}; "
                          f"{row['accn']}; {row['unit']}]",''])
        filing=source.get('filing')
        if filing:
            for ref in filing.get('context',[])+[{'line':filing['line'],'quote':filing['quote']}]:
                identity=(filing['file'],ref['line'],ref['quote'])
                if identity in seen: continue
                seen.add(identity)
                lines.extend(['> '+ref['quote'],'',f"[{filing['file']}, line {ref['line']}]",''])
    lines.extend(['Arithmetic and reported units:',''])
    for mention in record['mentions']:
        numeric=record['statement'][mention['start']:mention['end']]
        lines.append(f"{numeric} {mention['unit']}: ({mention['expression']}) / {mention['scale']}; "
                     f"reported to {mention['decimals']} decimal places.")
    return '\n'.join(lines).strip()+'\n'


def source_pointer(saved, source):
    data=saved[source['file']]
    pointer=source['pointer']
    if not re.fullmatch(r'/concepts/[^/]+/(?:current|prior)',pointer):
        raise ValueError('A fact citation must identify a saved current/prior concept record')
    for part in pointer.split('/')[1:]:
        data=data[part]
    if not isinstance(data,dict) or not all(key in data for key in ('val','tag','end','unit','accn')):
        raise ValueError('Fact citation does not resolve to an XBRL observation')
    if data != source['record']:
        raise ValueError('Fact citation record differs from the saved record')
    return data


def resolve_operand(operand, sources, normalize):
    errors=[]
    quote=decimal(operand['quoted'])
    scale=decimal(operand['scale'])
    if scale not in {Decimal(1),Decimal(1000),Decimal(1000000),Decimal(1000000000),Decimal(1000000000000)}:
        raise ValueError('Unsupported quoted input scale')
    fact=None
    filing=None
    source=operand.get('source') or {}
    if not source or not set(source)<= {'fact','filing'}:
        errors.append('Numeric input has no valid explicit source citation.')
    if source.get('fact'):
        try:
            fact=source_pointer(sources['saved_facts'],source['fact'])
            if decimal(fact['val']) != quote*scale or fact['unit'] != operand['unit']:
                errors.append('Quoted input value/unit does not exactly match its saved XBRL record.')
            for field in ('start','end'):
                if operand['period'].get(field) != fact.get(field):
                    errors.append('Claimed input period does not match its saved XBRL record.')
        except (KeyError,TypeError,ValueError) as exc:
            errors.append('Unresolved saved fact citation: '+str(exc))
    if source.get('filing'):
        citation=source['filing']
        try:
            lines=sources['saved_filing_texts'][citation['file']].splitlines()
            if type(citation['line']) is not int or not 0<citation['line']<=len(lines):
                raise ValueError('Invalid source line')
            actual=lines[citation['line']-1]
            if not citation['quote'] or normalize(actual)!=normalize(citation['quote']):
                raise ValueError('Cited line does not equal the saved filing line')
            for ref in citation.get('context',[]):
                if type(ref['line']) is not int or not 0<ref['line']<=len(lines) or normalize(lines[ref['line']-1])!=normalize(ref['quote']):
                    raise ValueError('Cited period/unit/definition context differs from the saved filing')
            numbers=list(NUMBER.finditer(actual))
            index=citation['number_index']
            if type(index) is not int or not 0<=index<len(numbers):
                raise ValueError('Invalid source number index')
            observed=decimal(numbers[index].group())
            if citation.get('magnitude'):
                observed=abs(observed)
            if observed != quote:
                errors.append('Quoted input does not match the selected number on its cited filing line.')
            context='\n'.join(ref['quote'] for ref in citation.get('context',[]))
            unit_scale=None
            if '/shares' in operand['unit'] and re.search(r'except per.share',context,re.I): unit_scale=Decimal(1)
            elif re.search(r'in millions',context,re.I): unit_scale=Decimal(1000000)
            elif re.search(r'in thousands',context,re.I): unit_scale=Decimal(1000)
            if unit_scale is None or unit_scale!=scale:
                errors.append('Quoted input scale is not established by its cited saved unit context.')
            filing={**citation,'quote':actual}
        except (KeyError,IndexError,TypeError,ValueError) as exc:
            errors.append('Unresolved saved filing citation: '+str(exc))
    if not operand['period'].get('end'):
        errors.append('Numeric input has no specified period.')
    if not operand.get('definition','').strip():
        errors.append('Numeric input has no measure definition.')
    return fact,filing,errors


def metadata_spans(statement, record, constants):
    """Exclude only explicitly source-backed dates/durations and formula constants."""
    periods=[operand['period'] for operand in record['inputs']]
    allowed_dates={value for period in periods for key,value in period.items() if key in ('start','end')}
    spans=[]
    for match in DATE.finditer(statement):
        text=match.group().replace(',','')
        for fmt in ('%Y-%m-%d','%B %d %Y'):
            try:
                if datetime.strptime(text,fmt).date().isoformat() in allowed_dates:
                    spans.append(match.span())
                break
            except ValueError: pass
    years={str(datetime.fromisoformat(p['end']).year) for p in periods if p.get('end')}
    spans += [m.span() for m in re.finditer(r'\bFY\s*(\d{4})\b',statement) if m[1] in years]
    contexts='\n'.join(ref['quote'] for op in record['inputs'] for ref in (op.get('source') or {}).get('filing',{}).get('context',[]))
    for match in re.finditer(r'\b(\d+)[ -]weeks?\b',statement,re.I):
        if re.search(r'\b'+match[1]+r'[ -]weeks?\b',contexts,re.I): spans.append(match.span())
    for match in re.finditer(r'(?:multiplied by\s+|[-/]\s*)(1|2|100)\b',statement,re.I):
        if match[1] in constants: spans.append(match.span(1))
    return spans


def check_claim(verifier, payload, visible, sources, normalize):
    record=payload['record']
    if set(record)!={'kind','statement','inputs','mentions'} or not record['inputs'] or not record['mentions']:
        raise ValueError('Source-bound claim must provide a statement, inputs and numeric mentions')
    if visible.strip()!=render_claim(payload).strip():
        raise ValueError('Displayed claim/source text differs from its bound source record')
    inputs={}
    input_checks=[]
    source_checks=[]
    descriptions=[]
    bindings={}
    for operand in record['inputs']:
        if not re.fullmatch(r'[a-zA-Z_]+',operand['name']) or operand['name'] in inputs:
            raise ValueError('Invalid or duplicate source input name')
        fact,filing,errors=resolve_operand(operand,sources,normalize)
        inputs[operand['name']]=decimal(operand['quoted'])*decimal(operand['scale'])
        cited=[]
        if fact:
            ref=operand['source']['fact']
            cited.append(f"{ref['file']} {ref['pointer']}; {fact['tag']}; {fact.get('start','instant')} to {fact['end']}; {fact['accn']}")
        if filing:
            cited.append(f"{filing['file']}, line {filing['line']}: {filing['quote']}")
            # Every number in the actual cited evidence is checked too, not just
            # the selected statement input. Context includes headers and units.
            for ref in filing.get('context',[])+[{'line':filing['line'],'quote':filing['quote']}]:
                evidence={'record':{'kind':'text','text':ref['quote']},
                          'source':{'file':filing['file'],'period':operand['period'].get('end')}}
                _, extra=verifier.check_record(evidence,'> '+ref['quote']+'\n',sources)
                source_checks.extend(extra)
        citation='; '.join(cited) or 'No resolvable source citation'
        # Preserve the exact selected number, column and asserted period for the
        # semantic reviewer. A whole table row plus several unrelated periods is
        # insufficient to identify the operand being reviewed.
        binding={key:copy.deepcopy(operand[key]) for key in
                 ('name','quoted','scale','unit','period','definition','source') if key in operand}
        binding['base_value']=str(inputs[operand['name']])
        binding['source_errors']=list(errors)
        bindings[operand['name']]=binding
        period=json.dumps(operand['period'],sort_keys=True)
        check=verifier.add(operand['quoted'],'unsupported' if errors else 'correct',
                           '; '.join(errors) or 'Explicit source citation resolved; semantic binding review pending.',
                           source=citation,semantic=bool(filing or fact),file=filing['file'] if filing else None,
                           quote=filing['quote'] if filing else json.dumps(fact or {},sort_keys=True),
                           period=period,definition=operand['definition']+'; statement: '+record['statement'],
                           quoted_input=f"{operand['quoted']} {operand['unit']} at scale {operand['scale']}",
                           source_binding={'kind':'input','operands':[binding],
                                           'statement':record['statement']},
                           claim_id=payload['id'],claim_input=True)
        input_checks.append(check)
        descriptions.append(f"{operand['name']}=\"{operand['quoted']}\" {operand['unit']} x {operand['scale']}: {citation}")
    mentions=[]
    covered=[]
    constants=set()
    for mention in record['mentions']:
        start,end=mention['start'],mention['end']
        if type(start) is not int or type(end) is not int or not 0<=start<end<=len(record['statement']) or any(a<end and start<b for a,b in covered):
            raise ValueError('Invalid or overlapping numeric claim span')
        literal=record['statement'][start:end]
        if not re.fullmatch(NUMBER,literal): raise ValueError('Numeric claim span must identify exactly one number')
        scale=decimal(mention['scale'])
        if scale<=0 or type(mention['decimals']) is not int or not 0<=mention['decimals']<=12:
            raise ValueError('Invalid statement scale or rounding precision')
        computed,used,allowed=expression_value(mention['expression'],inputs)
        constants.update(allowed)
        if not used: raise ValueError('A numeric claim expression must use cited inputs')
        units={operand['unit'] for operand in record['inputs'] if operand['name'] in used}
        if len(units)>1: raise ValueError('Claim expression uses incompatible input units')
        dimension=expression_dimension(mention['expression'],{op['name']:op['unit'] for op in record['inputs']})
        expected={} if mention['unit'] in ('%','times','fraction','multiple') else {mention['unit']:1}
        if dimension!=expected: raise ValueError('Claim output unit does not match its source-bound expression')
        displayed=computed/scale
        quantum=Decimal(1).scaleb(-mention['decimals'])
        rounded=displayed.quantize(quantum)
        # Direct XBRL/filing amounts must match exactly. Only a calculated result
        # can use the explicitly declared display precision.
        direct=isinstance(ast.parse(mention['expression'],mode='eval').body,ast.Name)
        valid=decimal(literal)==(displayed if direct else rounded)
        expanded=re.sub(r'\b[a-zA-Z_]+\b',lambda m: str(inputs[m.group()]),mention['expression'])
        scale_name={Decimal(1000):'thousand',Decimal(1000000):'million',
                    Decimal(1000000000):'billion',Decimal(1000000000000):'trillion'}.get(scale)
        display_unit=(scale_name+' '+mention['unit']) if scale_name else (
            mention['unit'] if scale==1 else f"{mention['unit']} at display scale {scale}")
        arithmetic=f"({expanded}) / {scale} = {displayed} {display_unit} (expression: {mention['expression']})"
        selected=[op for op in record['inputs'] if op['name'] in used]
        selected_checks=[c for op,c in zip(record['inputs'],input_checks) if op['name'] in used]
        focus={'kind':'direct' if direct else 'calculation','expression':mention['expression'],
               'statement':record['statement'],'span':{'start':start,'end':end},
               'display':{'value':literal,'unit':mention['unit'],'unit_label':display_unit,'scale':str(scale),
                          'base_value':str(decimal(literal)*scale),'decimal_places':mention['decimals']},
               'computed_base_value':str(computed),'arithmetic':arithmetic,
               'operands':[bindings[op['name']] for op in selected]}
        check=verifier.add(literal,'correct' if valid else 'wrong','Recomputed source-bound statement value at its declared precision.',
                           format(displayed if direct else rounded,'f'),arithmetic+'; '+'; '.join(descriptions),
                           claim_id=payload['id'],claim_statement=True,arithmetic=arithmetic,
                           semantic=True,sourced_claim_result=True,
                           file=next((c['file'] for c in selected_checks if c.get('file')),None),
                           quote=next((c['quote'] for c in selected_checks if c.get('quote')),''),
                           period=json.dumps([op['period'] for op in selected],sort_keys=True),
                           definition=(selected[0]['definition'] if direct else record['statement'])+
                                      '; source-bound calculation: '+arithmetic,
                           quoted_input='; '.join(description for op,description in zip(record['inputs'],descriptions)
                                                  if op['name'] in used),
                           source_binding=focus)
        mentions.append((mention,check))
        covered.append((start,end))
    covered.extend(metadata_spans(record['statement'],record,constants))
    uncovered=[]
    for match in NUMBER.finditer(record['statement']):
        if not any(a<=match.start() and match.end()<=b for a,b in covered):
            uncovered.append(verifier.add(match.group(),'unsupported','Statement number has no source binding.',
                                          claim_id=payload['id'],claim_statement=True))
    return {'payload':payload,'inputs':input_checks,'mentions':mentions,'uncovered':uncovered,
            'checks':source_checks+input_checks+[c for _,c in mentions]+uncovered}


def finish_claim(group):
    record=group['payload']['record']
    failed_inputs=any(c['mark']!='correct' for c in group['inputs'])
    statement=record['statement']
    edits=[]
    for mention,check in group['mentions']:
        if failed_inputs or group['uncovered']:
            check['mark']='unsupported'
            check['reason']+=' One or more statement citations or semantic bindings failed.'
        if check['mark']!='correct':
            edits.append((mention['start'],mention['end'],check['correction']))
    for start,end,replacement in sorted(edits,reverse=True):
        statement=statement[:start]+replacement+statement[end:]
    if failed_inputs or group['uncovered']:
        statement='**Unsupported source-bound claim; arithmetic is conditional on the cited inputs.**\n\n'+statement
    # Source references remain visibly preserved; rendering doesn't use mention
    # spans after edits, so update the display directly rather than trust offsets.
    original=render_claim(group['payload'])
    return statement+original[len(record['statement']):]
