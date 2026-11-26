import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--iteration', default='0.2')
    arguments = parser.parse_args()
    if arguments.iteration not in ('0.2','1.1'):
        raise SystemExit('Unsupported report destination')
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = (result.wasSuccessful() and result.testsRun > 0 and not result.skipped
              and not result.expectedFailures and not result.unexpectedSuccesses)
    payload = {'status':'PASS' if passed else 'FAIL', 'tests_run':result.testsRun,
               'failures':[(str(test), message) for test,message in result.failures],
               'errors':[(str(test), message) for test,message in result.errors],
               'skipped':[(str(test), message) for test,message in result.skipped],
               'expected_failures':len(result.expectedFailures), 'unexpected_successes':len(result.unexpectedSuccesses)}
    output = ROOT / 'reports/iterations' / arguments.iteration / 'unit-tests.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    sys.exit(0 if passed else 1)
