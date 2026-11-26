"""Real-browser acceptance for dated snapshots, also runnable against the shipped EXE."""
import argparse
import csv
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import time

from openpyxl import load_workbook
from pypdf import PdfReader

from backuptool.archive import Archive, result_label
from backuptool.evidence import original_path
from backuptool.controller import WorkerSession
from tools.check_delivery import job
from tools.fixture_server import ASSETS, serving

ROOT = Path(__file__).resolve().parents[1]


def pdf_text(path):
    return '\n'.join(page.extract_text() for page in PdfReader(path).pages)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--manual', action='store_true')
    args = parser.parse_args()
    folder = ROOT / 'reports' / 'snapshot-2.2' / ('frozen' if args.exe else 'source')
    folder.mkdir(parents=True, exist_ok=True)
    # Every acceptance run has its own archive; never edit a user's archive.
    folder = folder / time.strftime('%Y%m%d-%H%M%S')
    folder.mkdir()
    checks = []
    (folder / 'result.json').write_text('{"status":"RUNNING"}', encoding='utf-8')
    sequence = 0

    def run(payload):
        nonlocal sequence
        sequence += 1
        payload['_diagnostics'] = True
        if not args.exe:
            return job(payload)
        request = folder / f'job-{sequence}.json'
        report = folder / f'job-{sequence}-result.json'
        request.write_text(json.dumps(payload), encoding='utf-8')
        subprocess.run([str(args.exe.resolve()), '--internal-job-file', str(request),
                        '--internal-job-report', str(report)], check=True, timeout=150,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        data = json.loads(report.read_text('utf-8'))
        assert data['final']['outcome'] == 'success', data
        return data['final'], data['events']

    with serving() as (server, base):
        print(f'Fixture server: {base}', flush=True)
        routes = ['/simple', '/spa', '/expired', '/status/400', '/status/402', '/status/403',
                  '/status/404', '/status/429', '/status/500', '/status/502', '/status/503',
                  '/status/504', '/challenge', '/login', '/blank', '/redirect-error',
                  '/expired.pdf', '/inline.pdf', '/original.docx', '/fake.pdf']
        # Closed loopback port exercises a connection failure without external DNS/network.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            dead_url = f'http://127.0.0.1:{sock.getsockname()[1]}/unreachable'
        source = folder / 'source.csv'
        source.write_text('ИНН;Компания;URL\n' + '\n'.join(
            f'0123456789;Snapshot;{base}{route}' for route in routes) + '\n'
            + f'0987654321;Offline;{dead_url}\n', encoding='utf-8-sig')
        archive_path = folder / 'archive'
        print('Running browser worker', flush=True)
        final, events = run({'archive': str(archive_path), 'source': str(source), 'browser': 'chrome'})
        assert final['counts'] == {'ready': 19, 'document': 1, 'error': 1}, final
        with sqlite3.connect(archive_path / 'archive.sqlite3') as db:
            db.row_factory = sqlite3.Row
            rows = {row['url']: dict(row) for row in db.execute('SELECT * FROM resources')}
            history = [dict(row) for row in db.execute('SELECT * FROM attempts')]
        for route in routes[:-3]:
            row = rows[base + route]
            assert row['state'] == 'ready' and row['http_status'], row
            assert datetime.fromisoformat(row['checked_at']).utcoffset() is not None, row
            text = pdf_text(archive_path / row['path'])
            assert row['checked_at'] in text and f'HTTP {row["http_status"]}' in text, text
            assert row['final_url'] in text, text
            assert any(a['resource_id'] == row['id'] and a['checked_at'] == row['checked_at']
                       and a['http_status'] == row['http_status'] and a['reason'] == row['reason']
                       for a in history)
        for route in routes:
            if route.startswith('/status/'):
                status = int(route.rsplit('/', 1)[1])
                row = rows[base + route]
                assert row['http_status'] == status and f'HTTP {status}' in row['reason'], row
                assert f'HTTP {status}' in result_label(row)
                assert server.requests[route] == 1, server.requests
        for route in ('/expired', '/expired.pdf', '/redirect-error'):
            row = rows[base + route]
            assert row['http_status'] == 402
            text = pdf_text(archive_path / row['path'])
            assert 'TILDA-EXPIRED' in text and 'Please renew your subscription' in text
        redirected = rows[base + '/redirect-error']
        assert redirected['final_url'] == base + '/expired'
        assert base + '/redirect-error' not in pdf_text(archive_path / redirected['path'])
        assert rows[base + '/challenge']['reason'] and rows[base + '/login']['reason']
        assert 'CHALLENGE-PENDING' in pdf_text(archive_path / rows[base + '/challenge']['path'])
        assert 'SPA-READY' in pdf_text(archive_path / rows[base + '/spa']['path'])
        assert rows[base + '/blank']['reason']
        access = rows[dead_url]
        assert access['state'] == 'ready' and access['quality'].startswith('access_')
        text = pdf_text(archive_path / access['path'])
        assert dead_url in text and 'ERR_CONNECTION_REFUSED' in text and access['checked_at'] in text
        assert access['http_status'] is None
        assert rows[base + '/fake.pdf']['state'] == 'error' and not rows[base + '/fake.pdf']['path']
        catalog = rows[base + '/inline.pdf']
        catalog_path = archive_path / catalog['path']
        assert catalog['quality'] == 'dated_document'
        assert original_path(catalog_path).read_bytes() == (ASSETS / 'useful.pdf').read_bytes()
        assert 'ORIGINAL-PDF-001' in pdf_text(catalog_path)
        assert catalog['checked_at'] in pdf_text(catalog_path)
        assert (archive_path / rows[base + '/original.docx']['path']).read_bytes() == (ASSETS / 'original.docx').read_bytes()
        checks.append('Real Chrome: error HTML 400/402/403/404/429/500/502/503/504, Tilda-like placeholder, '
                      'login/challenge/blank/SPA, redirects, HTML at document URL; no repeated HTML requests')
        checks.append('Every rendered PDF has matching URL, HTTP status and timezone-aware timestamp in archive/history')
        checks.append('Connection failure produces dated evidence; invalid PDF is rejected; originals stay byte-identical')
        report = Path(next(e['path'] for e in events if e['type'] == 'report'))
        book = load_workbook(report, read_only=True)
        try:
            assert book['Все результаты'].max_row is None or book['Все результаты'].max_row == 22
            data = list(book['Все результаты'].values)
            assert len(data) == 22 and len(list(book['Нужны действия'].values)) == 2
            assert len(list(book['Снимки с предупреждениями'].values)) == 17
            assert data[3][3] == 'Снимок сохранён · HTTP 402'
            assert data[3][11] == '402' and data[3][12] == rows[base + '/expired']['checked_at']
        finally:
            book.close()
        with report.with_suffix('.csv').open(encoding='utf-8-sig', newline='') as stream:
            data = list(csv.reader(stream, delimiter=';'))
        assert data[3][11:13] == ['402', rows[base + '/expired']['checked_at']]
        checks.append('XLSX/CSV preserve HTTP status, exact capture timestamp and warnings; failures have no fake snapshot')

        before = server.requests.copy()
        first = rows[base + '/expired']
        run({'archive': str(archive_path), 'list_id': final['list_id'], 'browser': 'chrome'})
        with _archive(archive_path) as archive:
            current = next(r for r in archive.resources(final['list_id']) if r['id'] == first['id'])
            assert current['checked_at'] == first['checked_at'] and current['path'] == first['path']
        assert server.requests['/expired'] == before['/expired']
        run({'archive': str(archive_path), 'list_id': final['list_id'], 'browser': 'chrome', 'refresh': True})
        with _archive(archive_path) as archive:
            current = next(r for r in archive.resources(final['list_id']) if r['id'] == first['id'])
            assert current['checked_at'] != first['checked_at'] and current['path'] != first['path']
            assert (archive_path / first['path']).is_file()
            # Interrupted publication must recover the warning and capture metadata too.
            attempt = archive.begin(first['id'], 'recovery')
            staged = archive.work_dir('recovery') / (attempt + '.part')
            staged.write_bytes((archive_path / first['path']).read_bytes())
            with archive.db:
                archive.db.execute("UPDATE attempts SET state='publishing',staged=?,target=?,sha=?,reason=?,"
                                   "http_status=?,checked_at=? WHERE id=?",
                                   (str(staged.relative_to(archive_path)), 'captures/recovery/snapshot.pdf',
                                    hashlib.sha256(staged.read_bytes()).hexdigest(), first['reason'],
                                    first['http_status'], first['checked_at'], attempt))
        with _archive(archive_path) as archive:
            recovered = next(r for r in archive.resources(final['list_id']) if r['id'] == first['id'])
            assert recovered['reason'] == first['reason'] and recovered['http_status'] == 402
            assert recovered['checked_at'] == first['checked_at']
        checks.append('Resume preserves capture date; refresh creates a new snapshot and keeps the old file; '
                      'crash recovery preserves provenance and warning')

        # Simulate a pre-2.1 archive, then migrate without inventing dates/status for old captures.
        legacy = folder / 'legacy'
        with _archive(legacy) as archive:
            old_list = archive.import_source(source)
            resource = archive.resources(old_list)[0]
            attempt = archive.begin(resource['id'], 'legacy')
            staged = archive.work_dir('legacy') / (attempt + '.part')
            staged.write_bytes((ASSETS / 'useful.pdf').read_bytes())
            old_path = Path(archive.publish(attempt, staged, 'old-snapshot'))
            old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
        with sqlite3.connect(legacy / 'archive.sqlite3') as db:
            for table in ('resources', 'attempts'):
                for column in ('http_status', 'checked_at', 'original_sha'):
                    db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        with _archive(legacy) as archive:
            assert len(archive.rows(old_list)) == 21
            assert all(row['http_status'] is None and not row['checked_at'] for row in archive.rows(old_list))
            assert hashlib.sha256(old_path.read_bytes()).hexdigest() == old_hash
            assert archive.reusable(next(r for r in archive.resources(old_list) if r['id'] == resource['id']))
        checks.append('Pre-2.1 archive migrates additively, without invented historical capture timestamps')

        if args.manual:
            assert not args.exe, 'Manual check uses source worker'
            session = WorkerSession()
            with sqlite3.connect(archive_path / 'archive.sqlite3') as db:
                row_id = db.execute('SELECT id FROM input_rows WHERE url=?', (base + '/expired',)).fetchone()[0]
            count = server.requests['/expired']
            session.start(payload={'operation': 'manual', 'archive': str(archive_path),
                                   'list_id': final['list_id'], 'row_id': row_id, 'browser': 'chrome'})
            deadline = time.monotonic() + 75
            sent = False
            try:
                while session.state != 'finished':
                    for event in session.poll():
                        if event['type'] == 'manual_ready' and not sent:
                            session.send('save_manual', tab=0)
                            sent = True
                    assert time.monotonic() < deadline, 'Manual session timeout'
                    time.sleep(.05)
                assert session.final['outcome'] == 'success', session.final
            finally:
                session.close()
            with _archive(archive_path) as archive:
                manual = next(r for r in archive.resources(final['list_id']) if r['id'] == first['id'])
                assert manual['quality'] == 'manual' and manual['http_status'] == 402
                text = pdf_text(archive_path / manual['path'])
                assert 'TILDA-EXPIRED' in text and manual['checked_at'] in text
            assert server.requests['/expired'] == count + 1
            checks.append('Visible Chrome manual capture saves HTTP 402 without reloading and records exact metadata')

    result = {'status': 'PASS', 'checks': checks, 'archive': str(archive_path),
              'exe_sha256': hashlib.sha256(args.exe.read_bytes()).hexdigest() if args.exe else None,
              'at': datetime.now().astimezone().isoformat(timespec='seconds')}
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


@contextmanager
def _archive(path):
    archive = Archive(path)
    try:
        yield archive
    finally:
        archive.close()


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
