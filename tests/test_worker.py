import subprocess
import sys
import time
import unittest

from backuptool.controller import WorkerSession


def await_final(session, timeout=10):
    deadline = time.monotonic() + timeout
    while session.state != 'finished' and time.monotonic() < deadline:
        session.poll()
        time.sleep(0.01)
    if session.state != 'finished':
        raise AssertionError('Worker did not finish')
    return session.final


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.session = WorkerSession()
        self.addCleanup(self.session.close)

    def test_normal_and_repeat(self):
        pids = set()
        for _ in range(5):
            self.session.start(0.01)
            value = await_final(self.session)
            pids.add(value['pid'])
            self.assertEqual(value['outcome'], 'success')
            self.assertEqual(value['imported_forbidden_modules'], [])
            self.assertIsNone(self.session.process)
            self.assertIsNone(self.session.connection)
        self.assertEqual(len(pids), 5)

    def test_early_cancel_and_duplicate_start(self):
        self.session.start(5)
        with self.assertRaises(RuntimeError):
            self.session.start()
        self.session.cancel()
        value = await_final(self.session)
        self.assertEqual(value['outcome'], 'cancelled')

    def test_invalid_command_is_error(self):
        self.session.start(-1)
        self.assertEqual(await_final(self.session)['outcome'], 'error')

    def test_crash_is_not_success(self):
        self.session.start(5)
        self.session.process.terminate()
        self.assertEqual(await_final(self.session)['outcome'], 'error')

    def test_close_preserves_unrelated_process(self):
        sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        try:
            self.session.start(5)
            self.session.close()
            self.assertEqual(self.session.state, 'finished')
            self.assertIsNone(sentinel.poll())
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=5)
