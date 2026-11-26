"""Explicit fixture generation; normal checks never update expected hashes."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import zipfile

from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'fixtures' / 'assets'


def pdf(lines):
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842), invariant=1, pageCompression=0)
    page.setFont('Helvetica', 14)
    for i, line in enumerate(lines):
        page.drawString(48, 790 - i * 24, line)
    page.showPage()
    page.save()
    return buffer.getvalue()


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    files = {
        'useful.pdf': pdf(['BackupTool reference', 'Useful original PDF', 'Control: ORIGINAL-PDF-001']),
        'small.pdf': pdf(['Small useful PDF']),
        'empty.pdf': b'',
        'corrupt.pdf': b'%PDF-1.7\nnot a complete PDF\n%%EOF\n',
        'image.svg': b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120"><rect width="240" height="120" fill="#207568"/><text x="20" y="65" fill="white" font-size="20">IMAGE-READY</text></svg>',
    }
    doc = io.BytesIO()
    with zipfile.ZipFile(doc, 'w', zipfile.ZIP_STORED) as z:
        parts = {
            '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
            '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
            'word/document.xml': '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>ORIGINAL-DOCX-001</w:t></w:r></w:p></w:body></w:document>',
        }
        for name, content in parts.items():
            z.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), content.encode())
    files['original.docx'] = doc.getvalue()
    for name, content in files.items():
        (ASSETS / name).write_bytes(content)
    # Copied once into the isolated test data, never served from the OS directory.
    shutil.copyfile(Path('C:/Windows/Fonts/arial.ttf'), ASSETS / 'fixture.ttf')
    manifest = {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(ASSETS.iterdir()) if p.is_file()}
    (ROOT / 'fixtures' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
