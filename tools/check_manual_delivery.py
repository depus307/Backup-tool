"""Actual visible Chrome PDF from the same page, plus explicit PDF attachment."""
import json
from pathlib import Path
import sqlite3
import time

from pypdf import PdfReader

from backuptool.controller import WorkerSession
from tools.check_delivery import job
from tools.fixture_server import ASSETS, serving


def main():
    folder = Path(__file__).resolve().parents[1] / 'reports' / 'delivery' / 'manual'
    folder.mkdir(parents=True, exist_ok=True)
    with serving() as (server, base):
        source = folder / 'source.csv'
        source.write_text(f'ИНН;Компания;URL\n0123456789;Ручной PDF;{base}/spa\n'
                          f'0987654321;Вложение;{base}/status/404\n', encoding='utf-8-sig')
        archive = str(folder / 'archive')
        loaded, _ = job({'operation': 'import', 'archive': archive, 'source': str(source)})
        list_id = loaded['list_id']
        db = sqlite3.connect(Path(archive) / 'archive.sqlite3')
        ids = [r[0] for r in db.execute('SELECT id FROM input_rows WHERE list_id=? ORDER BY id', (list_id,))]
        session = WorkerSession()
        session.start(payload={'operation': 'manual', 'archive': archive, 'list_id': list_id,
                               'row_id': ids[0], 'browser': 'chrome'})
        ready_at = None
        sent = False
        deadline = time.monotonic() + 90
        try:
            while session.state != 'finished':
                for event in session.poll():
                    if event['type'] == 'manual_ready':
                        ready_at = time.monotonic()
                if ready_at and not sent and time.monotonic() - ready_at > 1:
                    session.send('save_manual', tab=0)
                    sent = True
                assert time.monotonic() < deadline, 'Manual session timeout'
                time.sleep(.05)
            assert session.final['outcome'] == 'success', session.final
        finally:
            session.close()
        path, quality = db.execute('SELECT path,quality FROM resources WHERE id=(SELECT resource_id '
                                  'FROM input_rows WHERE id=?)', (ids[0],)).fetchone()
        assert quality == 'manual'
        text = ''.join(p.extract_text() for p in PdfReader(Path(archive) / path).pages)
        assert 'SPA-READY' in text and base + '/spa' in text
        assert server.requests['/spa'] == 1, server.requests
        job({'operation': 'attach', 'archive': archive, 'list_id': list_id, 'row_id': ids[1],
             'pdf': str(ASSETS / 'useful.pdf')})
        path, quality = db.execute('SELECT path,quality FROM resources WHERE id=(SELECT resource_id '
                                  'FROM input_rows WHERE id=?)', (ids[1],)).fetchone()
        assert quality == 'manual_attachment'
        assert (Path(archive) / path).read_bytes() == (ASSETS / 'useful.pdf').read_bytes()
        db.close()
    result = {'status': 'PASS', 'same_visible_page_without_reload': True, 'attachment_exact': True}
    (folder / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
