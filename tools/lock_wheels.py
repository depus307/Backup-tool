"""Pin downloaded Windows/Python wheels by metadata and SHA-256."""
from email.parser import BytesParser
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    rows = []
    for wheel in sorted((ROOT / '.wheelhouse').glob('*.whl')):
        with zipfile.ZipFile(wheel) as archive:
            metadata_file = next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))
            metadata = BytesParser().parsebytes(archive.read(metadata_file))
        checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
        rows.append(f'{metadata["Name"]}=={metadata["Version"]} --hash=sha256:{checksum}')
    if not rows:
        raise SystemExit('No wheels; lock not written')
    (ROOT / 'requirements.lock').write_text('# Python 3.12.14 x64 / Windows 11; generated from downloaded wheels\n' + '\n'.join(sorted(rows, key=str.lower)) + '\n', encoding='utf-8')
    print(f'Locked {len(rows)} wheels')
