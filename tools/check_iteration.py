"""Local evidence gate. Only a complete, current PASS unlocks the immediate successor."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


class GateError(ValueError):
    pass


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contained(base, relative):
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base.resolve()) or not candidate.is_file():
        raise GateError(f'Missing or unsafe evidence path: {relative}')
    return candidate


def registry(root):
    document = (root.parent / 'IMPLEMENTATION_ROADMAP.md').read_text(encoding='utf-8-sig')
    sections = re.split(r'^### Итерация (\d+\.\d+)\.', document, flags=re.MULTILINE)
    groups = {}
    for index in range(1, len(sections), 2):
        iteration, body = sections[index:index + 2]
        groups[iteration] = list(dict.fromkeys(re.findall(r'\*\*(T\d+\.\d+\.\d+)\s*·', body)))
    if not groups or any(not required for required in groups.values()):
        raise GateError('Empty mandatory registry')
    return groups


def validate(root, iteration, groups=None):
    groups = registry(root) if groups is None else groups
    order = list(groups)
    if iteration not in order:
        raise GateError(f'Unknown iteration: {iteration}')
    for current in order[:order.index(iteration) + 1]:
        report_dir = root / 'reports/iterations' / current
        report_file = report_dir / 'result.json'
        if not report_file.is_file():
            raise GateError(f'Missing report: {current}')
        try:
            result = json.loads(report_file.read_text(encoding='utf-8'))
        except (ValueError, OSError) as error:
            raise GateError(f'Unreadable report: {current}') from error
        if not isinstance(result, dict):
            raise GateError(f'Invalid report object: {current}')
        if result.get('iteration') != current or result.get('status') != 'PASS':
            raise GateError(f'Iteration is not PASS: {current}')
        required = groups[current]
        actual = result.get('mandatory_results')
        if not required or not isinstance(actual, dict) or set(actual) != set(required):
            raise GateError(f'Incomplete mandatory set: {current}')
        bootstrap = current == '0.1' and result.get('schema_version') is None
        if not bootstrap and result.get('schema_version') != 1:
            raise GateError(f'Unsupported report schema: {current}')
        for test_id, value in actual.items():
            if bootstrap:
                if value != 'PASS':
                    raise GateError(f'Mandatory check not PASS: {test_id}')
            elif not isinstance(value, dict) or value.get('status') != 'PASS' or not value.get('evidence'):
                raise GateError(f'Mandatory check lacks PASS/evidence: {test_id}')
        fingerprints = result.get('code_and_inputs_sha256')
        if not isinstance(fingerprints, dict) or not fingerprints:
            raise GateError(f'Missing code fingerprint: {current}')
        # Historical acceptances refer to their tested version. The current
        # iteration must bind the current code and execute accumulated regression.
        if current == iteration:
            for relative, expected in fingerprints.items():
                if sha(contained(root, relative)) != expected:
                    raise GateError(f'Stale code/input: {relative}')
        evidence = result.get('evidence') if bootstrap else result.get('evidence_sha256')
        if not evidence or not isinstance(evidence, list if bootstrap else dict):
            raise GateError(f'Missing evidence: {current}')
        for relative in evidence:
            path = contained(report_dir, relative)
            if not bootstrap and sha(path) != evidence[relative]:
                raise GateError(f'Stale evidence: {current}/{relative}')
        if not bootstrap:
            for value in actual.values():
                if not isinstance(value['evidence'], list) or not set(value['evidence']) <= set(evidence):
                    raise GateError(f'Unregistered evidence: {current}')
            predecessor = order.index(current) - 1
            if predecessor >= 0:
                previous = root / 'reports/iterations' / order[predecessor] / 'result.json'
                if result.get('predecessor_report_sha256') != sha(previous):
                    raise GateError(f'Changed predecessor report: {current}')
    position = order.index(iteration)
    return order[position + 1] if position + 1 < len(order) else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--iteration', required=True)
    parser.add_argument('--request-next')
    args = parser.parse_args()
    try:
        successor = validate(ROOT, args.iteration)
        if args.request_next and args.request_next != successor:
            raise GateError('Only the immediate successor can be unlocked')
        print(json.dumps({'iteration': args.iteration, 'status': 'PASS', 'allowed_next': successor}))
        return 0
    except (GateError, OSError, TypeError, KeyError) as error:
        print(json.dumps({'iteration': args.iteration, 'status': 'DENIED', 'reason': str(error)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
