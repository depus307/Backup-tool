"""Produce a dated copy of the original PDF pages."""
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
from urllib.parse import quote


@lru_cache(maxsize=1)
def footer_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'arial.ttf'
    if path.is_file():
        pdfmetrics.registerFont(TTFont('EvidenceArial', str(path)))
        return 'EvidenceArial'
    return 'Helvetica'


def dated_pdf(original, output, url, checked_at, status):
    from pypdf import PdfReader, PdfWriter, Transformation
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen.canvas import Canvas

    source = PdfReader(original)
    if source.is_encrypted or not source.pages:
        raise ValueError('PDF зашифрован или не содержит страниц')
    writer = PdfWriter()
    font = footer_font()
    address = quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")
    caption = 'Дата и время снимка' if font != 'Helvetica' else 'Capture time'
    for page in source.pages:
        if page.rotation:
            page.transfer_rotation_to_content()
        box = page.cropbox
        unit = float(page.user_unit)
        width, height = float(box.width) * unit, float(box.height) * unit
        sheet_width = max(240, width)
        lines = []
        line = ''
        for char in address:
            if line and pdfmetrics.stringWidth(line + char, font, 9) > sheet_width - 24:
                lines.append(line)
                line = ''
            line += char
        lines.append(line)
        band = 30 + 12 * len(lines)
        footer_bytes = BytesIO()
        footer = Canvas(footer_bytes, pagesize=(sheet_width, band))
        footer.setFont(font, 9)
        footer.setFillColorRGB(.08, .27, .53)
        for index, line in enumerate(lines):
            y = band - 14 - 12 * index
            footer.drawString(12, y, line)
            footer.linkURL(address, (12, y - 2, 12 + pdfmetrics.stringWidth(line, font, 9), y + 9))
        footer.setFillColorRGB(.12, .15, .19)
        footer.drawString(12, 10, f'{caption}: {checked_at}' + (f' · HTTP {status}' if status else ''))
        footer.save()
        sheet = writer.add_blank_page(width=sheet_width, height=height + band)
        shift = Transformation().translate(-float(box.left), -float(box.bottom)).scale(unit).translate(0, band)
        sheet.merge_transformed_page(page, shift)
        sheet.merge_page(PdfReader(footer_bytes).pages[0])
    writer.write(output)


def original_path(path):
    return path.with_name(path.stem + '__original.pdf')
