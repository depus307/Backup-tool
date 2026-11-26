"""Check the released executable against the reported catalog and access failures."""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import time

from pypdf import PdfReader

from backuptool.evidence import original_path
from tools.fixture_server import serving

CATALOG = 'https://kremlina.ru/assets/files/catalog.pdf'
ROOT = Path(__file__).resolve().parents[1]


def tls_certificate(folder):
    # Test only. The bundled development runtime has cryptography outside the venv.
    sys.path.append(str(Path(sys.base_prefix) / 'Lib/site-packages'))
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    pem = folder / 'test-tls.pem'
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM))
    return pem


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('--batch-only', action='store_true')
    args = parser.parse_args()
    exe = args.exe.resolve()
    folder = ROOT / 'reports/snapshot-2.2/access' / time.strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True)
    archive = folder / 'archive'

    def run(name, **payload):
        request, report = folder / f'{name}.json', folder / f'{name}-result.json'
        request.write_text(json.dumps({'archive': str(archive), 'browser': 'chrome', **payload}), 'utf-8')
        subprocess.run([str(exe), '--internal-job-file', str(request), '--internal-job-report', str(report)],
                       creationflags=subprocess.CREATE_NO_WINDOW, check=True, timeout=165)
        result = json.loads(report.read_text('utf-8'))
        assert result['final']['outcome'] == 'success', result
        return result['final']['list_id']

    def rows():
        with sqlite3.connect(archive / 'archive.sqlite3') as db:
            db.row_factory = sqlite3.Row
            return {row['url']: dict(row) for row in db.execute(
                'SELECT r.*, i.id AS row_id FROM resources r JOIN input_rows i ON i.resource_id=r.id')}

    def evidence(row, expected):
        path = archive / row['path']
        assert row['state'] == 'ready' and row['quality'].startswith('access_'), row
        pdf = PdfReader(path)
        text = '\n'.join(page.extract_text() for page in pdf.pages)
        assert row['url'] in text and row['checked_at'] in text and expected in text, text
        assert ('Экран ошибки браузера' if row['quality'] == 'access_screenshot'
                else 'Отчёт BackupTool') in text, text
        return path

    def catalog(row):
        assert row['quality'] == 'dated_document', row
        path = archive / row['path']
        original = original_path(path)
        assert hashlib.sha256(original.read_bytes()).hexdigest() == row['original_sha']
        original_pages, copy_pages = PdfReader(original).pages, PdfReader(path).pages
        assert len(original_pages) == len(copy_pages) == 38
        for page in copy_pages:
            assert row['checked_at'] in page.extract_text()
            links = [ref.get_object() for ref in page.get('/Annots', [])]
            assert sum(link.get('/A', {}).get('/URI') == row['final_url'] for link in links) == 1
        return path

    with ExitStack() as stack:
        _, base = stack.enter_context(serving())
        tls, tls_base = stack.enter_context(serving())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_certificate(folder))
        tls.socket = context.wrap_socket(tls.socket, server_side=True)
        cases = {
            'http://backuptool-evidence.invalid/': 'ERR_NAME_NOT_RESOLVED',
            base + '/closed': 'ERR_EMPTY_RESPONSE',
            base + '/timeout': 'TIMEOUT',
            tls_base.replace('http:', 'https:') + '/simple': 'ERR_CERT_',
        }
        source = folder / 'cases.txt'
        tilda = 'http://ilina.ru/'
        reported_sites = ('https://baltchoco.ru/', 'https://detchinokkz.ru/')
        source.write_text('\n'.join([CATALOG, *cases, base + '/redirect-error', tilda, *reported_sites]), 'utf-8')
        list_id = run('batch', source=str(source), _diagnostics=True)
        results = rows()
        catalog_pdf = catalog(results[CATALOG])
        evidence_files = {url: str(evidence(results[url], code)) for url, code in cases.items()}
        redirect = results[base + '/redirect-error']
        text = PdfReader(archive / redirect['path']).pages[0].extract_text()
        assert base + '/expired' in text and base + '/redirect-error' not in text and 'HTTP 402' in text
        tilda_row = results[tilda]
        assert tilda_row['state'] == 'ready' and tilda_row['http_status'] == 402, tilda_row
        assert 'Please renew your subscription' in PdfReader(archive / tilda_row['path']).pages[0].extract_text()
        reported = {}
        for url in reported_sites:
            row = results[url]
            assert row['state'] == 'ready' and row['path'], row
            reported[url] = {'quality': row['quality'], 'reason': row['reason'], 'http_status': row['http_status']}
        manual_urls = () if args.batch_only else (CATALOG, *cases)
        for index, url in enumerate(manual_urls):
            run(f'manual-{index}', operation='manual', list_id=list_id, row_id=results[url]['row_id'])
            manual = rows()[url]
            assert manual['path'] != results[url]['path']
            if url == CATALOG:
                catalog(manual)
            else:
                evidence(manual, cases[url])
        result = {'status': 'PASS', 'catalog_pdf': str(catalog_pdf), 'evidence': evidence_files,
                  'reported_sites': reported, 'manual_checked': not args.batch_only,
                  'exe_sha256': hashlib.sha256(exe.read_bytes()).hexdigest()}
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
