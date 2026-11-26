"""One spawn worker. Standard library only; never imports the GUI."""
import os
import sys
import time

HEAVY_MODULES = ('tkinter', 'playwright', 'openpyxl', 'pypdf', 'httpx', 'pandas', 'numpy')


def worker_main(connection, session_id):
    try:
        connection.send({'type': 'ready', 'session_id': session_id, 'pid': os.getpid()})
        command = connection.recv()
        if command.get('session_id') != session_id:
            raise ValueError('Unexpected worker command')
        if command.get('type') == 'job':
            from .engine import run_job
            result = run_job(connection, session_id, command['payload'])
            connection.send({'type': 'final', 'session_id': session_id, 'pid': os.getpid(), **result})
            return
        if command.get('type') != 'probe':
            raise ValueError('Unexpected worker command')
        duration = command.get('duration', 0.2)
        if not isinstance(duration, (int, float)) or not 0 <= duration <= 30:
            raise ValueError('Invalid probe duration')
        deadline = time.monotonic() + duration
        outcome = 'success'
        while time.monotonic() < deadline:
            if connection.poll(min(0.05, max(0, deadline - time.monotonic()))):
                request = connection.recv()
                if request.get('session_id') == session_id and request.get('type') == 'cancel':
                    outcome = 'cancelled'
                    break
        connection.send({'type': 'final', 'session_id': session_id, 'outcome': outcome,
                         'pid': os.getpid(), 'parent_pid': os.getppid(),
                         'imported_forbidden_modules': [m for m in HEAVY_MODULES if m in sys.modules]})
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        try:
            connection.send({'type': 'final', 'session_id': session_id, 'outcome': 'error',
                             'pid': os.getpid(), 'reason': f'{type(error).__name__}: {error}'})
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()
