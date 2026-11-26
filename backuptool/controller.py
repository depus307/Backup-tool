"""Owns one worker and its bounded Pipe messages."""
import multiprocessing
import time
import uuid

from .worker import worker_main


class WorkerSession:
    def __init__(self):
        self.state = 'idle'
        self.session_id = None
        self.process = None
        self.connection = None
        self.final = None
        self.worker_pid = None

    def start(self, duration=0.2, payload=None):
        if self.state in ('starting', 'running', 'stopping'):
            raise RuntimeError('A worker session is already active')
        context = multiprocessing.get_context('spawn')
        self.session_id = uuid.uuid4().hex
        self.final = None
        parent, child = context.Pipe(duplex=True)
        self.connection = parent
        self.process = context.Process(target=worker_main, args=(child, self.session_id), daemon=False)
        try:
            self.process.start()
        except BaseException:
            parent.close()
            child.close()
            self.connection = None
            self.state = 'finished'
            raise
        child.close()
        self.worker_pid = self.process.pid
        self.state = 'starting'
        self.connection.send({'type': 'job', 'session_id': self.session_id, 'payload': payload} if payload is not None
                             else {'type': 'probe', 'session_id': self.session_id, 'duration': duration})

    def send(self, kind, **values):
        if self.connection is not None:
            self.connection.send({'type': kind, 'session_id': self.session_id, **values})

    def cancel(self):
        if self.state not in ('starting', 'running'):
            return
        self.state = 'stopping'
        try:
            self.connection.send({'type': 'cancel', 'session_id': self.session_id})
        except (BrokenPipeError, EOFError, OSError):
            pass

    def _receive(self, limit=16):
        messages = []
        while limit is None or len(messages) < limit:
            try:
                if not self.connection.poll():
                    break
                message = self.connection.recv()
            except (EOFError, OSError):
                break
            if message.get('session_id') != self.session_id:
                continue
            messages.append(message)
            if message.get('type') == 'ready' and self.state == 'starting':
                self.state = 'running'
            elif message.get('type') == 'final':
                self.final = message
        return messages

    def poll(self):
        if self.connection is None:
            return []
        messages = self._receive()
        if self.process is not None and not self.process.is_alive():
            self.process.join()
            # The final message may arrive between the first poll and process exit.
            messages.extend(self._receive(limit=None))
            if self.final is None:
                self.final = {'type': 'final', 'session_id': self.session_id, 'outcome': 'error',
                              'reason': f'Worker exited without a result (exit code {self.process.exitcode})',
                              'pid': self.worker_pid}
                messages.append(self.final)
            self.process.close()
            self.process = None
            self.connection.close()
            self.connection = None
            self.state = 'finished'
        return messages

    def close(self, timeout=2.0):
        self.cancel()
        deadline = time.monotonic() + timeout
        while self.process is not None and self.process.is_alive() and time.monotonic() < deadline:
            self.poll()
            time.sleep(0.01)
        if self.process is not None and self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
            if self.process.is_alive():
                raise RuntimeError('Owned worker could not be terminated')
        self.poll()
