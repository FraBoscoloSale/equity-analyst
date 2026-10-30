"""Read-only, offline validation of saved role outputs against their actual briefs."""

import argparse
import json
from pathlib import Path

from claim_validation import claim_errors, numeric_record_errors, review_policy
from research_roles import StrategyManager, RESEARCH, FINANCIAL, ADVISOR, validate_content


def audit_saved_run(folder):
    folder = Path(folder)
    def read(name):
        return json.loads((folder / name).read_text(encoding='utf-8'))
    manager = StrategyManager(folder, None)  # constructs local evidence; no model/client call
    outputs = read('role_outputs.json')
    results = {}
    for role in (RESEARCH, FINANCIAL, ADVISOR):
        suffix = role.lower().replace(' ', '_')
        request = sorted(folder.glob(f'role_*_{suffix}_request.json'))[-1]
        context = json.loads(json.loads(request.read_text(encoding='utf-8'))['messages'][1]['content'])['context']
        supplied = ({key: row for output in context.values() for key, row in output['cited_evidence'].items()}
                    if role == ADVISOR else context['evidence'])
        result = outputs[role]
        cited = result['cited_evidence']
        unknown, changed, numerical_errors = [], [], {}
        for key, row in cited.items():
            if key not in supplied:
                unknown.append(key)
            elif row != supplied[key] or row != manager.catalog.records.get(key):
                changed.append(key)
            if row['kind'] in {'xbrl_fact', 'calculation', 'user_input'}:
                errors = numeric_record_errors(key, supplied)
                if errors:
                    numerical_errors[key] = errors
        block_errors = []
        for part, blocks in result['sections'].items():
            for i, block in enumerate(blocks, 1):
                errors = claim_errors(block['text'], block['evidence_ids'], supplied)
                if errors:
                    block_errors.append({'part': part, 'block': i, 'text': block['text'], 'errors': errors})
        results[role] = {'request': request.name, 'cited_record_count': len(cited),
                         'unknown_ids': unknown, 'changed_records': changed,
                         'numerical_provenance_errors': numerical_errors,
                         'part_errors': validate_content(role, result, supplied), 'claim_errors': block_errors}
    prior_review = read('review_final.json')
    exclusions = []
    for part, check in prior_review['checks'].items():
        for finding in check['issues']:
            text = finding['text'] if isinstance(finding, dict) else finding
            code = finding['code'] if isinstance(finding, dict) else 'unsupported_claim'
            reason = review_policy(code, text)
            if reason:
                exclusions.append({'part': part, 'finding': text, 'policy_reason': reason})
    return {'run_folder': str(folder.resolve()), 'mode': 'offline; original artifacts preserved; no API calls',
            'roles': results, 'prior_review_policy_exclusions': exclusions,
            'verified_peer_comparisons': {key: row for key, row in manager.catalog.records.items() if row['kind'] == 'peer_comparison'},
            'available_inventory_limitations': {key: row for key, row in manager.catalog.records.items() if row['kind'] == 'limitation'},
            'legacy_contract_note': 'Saved free-form limitations and advisor fields are checked without silently upgrading them to the new ID-only contract.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit_saved_run(args.folder)
    data = json.dumps(result, ensure_ascii=True, indent=2)
    if args.output:
        output = args.output.resolve()
        if output.is_relative_to(args.folder.resolve()):
            parser.error('--output must be outside the original run folder')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(data + '\n', encoding='utf-8')
        print(output)
    else:
        print(data)


if __name__ == '__main__':
    main()
