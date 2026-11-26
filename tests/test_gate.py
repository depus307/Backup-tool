import copy
import json
from pathlib import Path
import tempfile
import unittest

from tools.check_iteration import GateError, sha, validate


class GateTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / '.build'
        temporary_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'source.py').write_text('CODE')
        self.groups = {'0.1': ['T0.1.1'], '0.2': ['T0.2.1', 'T0.2.2'], '1.1': ['T1.1.1']}
        for iteration, required in list(self.groups.items())[:2]:
            folder = self.root / 'reports/iterations' / iteration
            folder.mkdir(parents=True)
            evidence = folder / 'checks.json'
            evidence.write_text('{"executed":true}')
            result = {'schema_version': 1, 'iteration': iteration, 'status': 'PASS',
                      'mandatory_results': {test: {'status': 'PASS', 'evidence': ['checks.json']} for test in required},
                      'code_and_inputs_sha256': {'source.py': sha(self.root / 'source.py')},
                      'evidence_sha256': {'checks.json': sha(evidence)}}
            if iteration == '0.2':
                result['predecessor_report_sha256'] = sha(self.root / 'reports/iterations/0.1/result.json')
            (folder / 'result.json').write_text(json.dumps(result))
        self.file = self.root / 'reports/iterations/0.2/result.json'
        self.valid = json.loads(self.file.read_text())

    def write(self, value):
        self.file.write_text(json.dumps(value))

    def test_complete_unlocks_only_successor(self):
        self.assertEqual(validate(self.root, '0.2', self.groups), '1.1')
        with self.assertRaises(GateError):
            validate(self.root, '1.1', self.groups)

    def test_every_nonpass_denied(self):
        for status in ['FAIL', 'BLOCKED', 'NOT RUN', 'SKIP', 'XFAIL', '', None]:
            with self.subTest(status=status):
                value = copy.deepcopy(self.valid)
                value['mandatory_results']['T0.2.1']['status'] = status
                self.write(value)
                with self.assertRaises(GateError):
                    validate(self.root, '0.2', self.groups)

    def test_empty_partial_extra_sets_denied(self):
        for checks in [{}, {'T0.2.1': self.valid['mandatory_results']['T0.2.1']},
                       {**self.valid['mandatory_results'], 'invented': {'status': 'PASS'}}]:
            self.write({**self.valid, 'mandatory_results': checks})
            with self.assertRaises(GateError):
                validate(self.root, '0.2', self.groups)

    def test_empty_registry_denied(self):
        with self.assertRaises(GateError):
            validate(self.root, '0.2', {'0.2': []})

    def test_stale_code_denied(self):
        (self.root / 'source.py').write_text('NEW CODE')
        with self.assertRaises(GateError):
            validate(self.root, '0.2', self.groups)

    def test_stale_evidence_denied(self):
        (self.file.parent / 'checks.json').write_text('changed')
        with self.assertRaises(GateError):
            validate(self.root, '0.2', self.groups)

    def test_missing_evidence_denied(self):
        value = copy.deepcopy(self.valid)
        value['mandatory_results']['T0.2.1']['evidence'] = []
        self.write(value)
        with self.assertRaises(GateError):
            validate(self.root, '0.2', self.groups)

    def test_unclosed_predecessor_denied(self):
        previous = self.root / 'reports/iterations/0.1/result.json'
        value = json.loads(previous.read_text())
        value['status'] = 'BLOCKED'
        previous.write_text(json.dumps(value))
        with self.assertRaises(GateError):
            validate(self.root, '0.2', self.groups)

    def test_changed_predecessor_denied(self):
        previous = self.root / 'reports/iterations/0.1/result.json'
        previous.write_text(previous.read_text() + '\n')
        with self.assertRaises(GateError):
            validate(self.root, '0.2', self.groups)

    def test_invalid_report_denied(self):
        for invalid in ['{', '[]', 'null', '"PASS"']:
            self.file.write_text(invalid)
            with self.assertRaises(GateError):
                validate(self.root, '0.2', self.groups)
