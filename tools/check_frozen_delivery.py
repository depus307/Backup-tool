"""Exercise the shipped executable, its spawned worker and installed Edge."""
import json
from pathlib import Path
import sqlite3
import subprocess

from backuptool.archive import valid_pdf
from tools.fixture_server import serving


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / 'reports' / 'delivery' / 'frozen'
    folder.mkdir(parents=True, exist_ok=True)
    exe = root / 'dist' / 'BackupToolNext' / 'BackupTool.exe'
    with serving() as (_, base):
        source = folder / 'source.csv'
        source.write_text(f'ИНН;Компания;URL\n0123456789;Frozen Edge;{base}/simple\n', encoding='utf-8-sig')
        payload = folder / 'job.json'
        payload.write_text(json.dumps({'archive': str(folder / 'archive'), 'source': str(source),
                                       'browser': 'msedge'}), encoding='utf-8')
        report = folder / 'result.json'
        subprocess.run([str(exe), '--internal-job-file', str(payload), '--internal-job-report', str(report)],
                       timeout=120, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        data = json.loads(report.read_text(encoding='utf-8'))
        assert data['final']['outcome'] == 'success', data
        assert data['final']['counts'] == {'ready': 1}, data
        with sqlite3.connect(folder / 'archive' / 'archive.sqlite3') as db:
            path = db.execute('SELECT path FROM resources WHERE state=?', ('ready',)).fetchone()[0]
            assert valid_pdf(folder / 'archive' / path)
        assert any(e['type'] == 'report' for e in data['events'])
    print('PASS: frozen executable + spawn worker + Edge PDF + XLSX/CSV')


if __name__ == '__main__':
    main()
