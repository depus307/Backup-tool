"""Internal packaging acceptance, deliberately absent from the public interface."""
import json
from pathlib import Path
import time

from .controller import WorkerSession


def execute(payload_path, report_path):
    payload = json.loads(Path(payload_path).read_text(encoding='utf-8'))
    session = WorkerSession()
    events = []
    try:
        session.start(payload=payload)
        deadline = time.monotonic() + 150
        while session.state != 'finished':
            for event in session.poll():
                events.append(event)
                if event['type'] == 'manual_ready' and payload.get('operation') == 'manual':
                    session.send('save_manual', tab=0)
            if time.monotonic() > deadline:
                raise TimeoutError('Frozen job timed out')
            time.sleep(.05)
        result = {'final': session.final, 'events': events}
    except Exception as error:
        result = {'error': str(error), 'events': events}
    finally:
        session.close()
    Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
