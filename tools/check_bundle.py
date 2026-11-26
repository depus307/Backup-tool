import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import pefile

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'reports/iterations/0.2'


def main():
    folder = ROOT / 'dist/BackupToolNext'
    exe = folder / 'BackupTool.exe'
    archive = subprocess.run([str(Path(sys.executable).parent / 'pyi-archive_viewer.exe'), '-l', '-r', str(exe)],
                             capture_output=True, text=True, check=True)
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / 'archive-content.txt').write_text(archive.stdout, encoding='utf-8')
    forbidden = re.compile(r'(?i)(?:^|[.\\/\s\x27\x22])(pandas|numpy|PySide\d?|PyQt\d?|playwright|openpyxl|pypdf|httpx|chrome|chromium|msedge)(?:[.\\/\s\x27\x22]|$)')
    files = [p for p in folder.rglob('*') if p.is_file()]
    assert not forbidden.search(archive.stdout), 'Unexpected heavy module in frozen archive'
    assert not any(forbidden.search(str(p.relative_to(folder))) for p in files), 'Unexpected heavy package/browser'
    with pefile.PE(exe) as pe:
        assert pe.OPTIONAL_HEADER.Subsystem == 2, 'EXE has a console subsystem'
    manifest = {str(p.relative_to(folder)).replace('\\', '/'): {'bytes':p.stat().st_size,
                'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(files)}
    payload = {'status':'PASS', 'file_count':len(files), 'total_bytes':sum(p.stat().st_size for p in files),
               'console_subsystem':False, 'heavy_packages_or_browsers':[], 'files':manifest}
    (REPORT / 'bundle.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print(json.dumps({key:value for key,value in payload.items() if key != 'files'}))


if __name__ == '__main__':
    main()
