"""Produce a dated copy of the original PDF pages."""
from datetime import datetime
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
from urllib.parse import quote


def format_capture_time(value):
    return datetime.fromisoformat(value).strftime('%d.%m.%Y %H:%M') if value else ''


def has_page_content(pdf):
    from pypdf import PdfReader

    # Chrome can write a page with only a white background and a clipping path.
    for page in PdfReader(pdf).pages:
        stream = page.get_contents()
        if stream is None:
            continue
        fill = stroke = True
        stack = []
        text_mode = 0
        for args, operator in stream.operations:
            if operator == b'q':
                stack.append((fill, stroke, text_mode))
            elif operator == b'Q' and stack:
                fill, stroke, text_mode = stack.pop()
            elif operator == b'Tr':
                text_mode = args[0]
            elif operator in (b'g', b'rg', b'k', b'G', b'RG', b'K'):
                paints = any(args) if operator.lower() == b'k' else not all(color == 1 for color in args)
                if operator.islower():
                    fill = paints
                else:
                    stroke = paints
            elif operator in (b'sc', b'scn'):
                fill = True
            elif operator in (b'SC', b'SCN'):
                stroke = True
            elif (operator in (b'Do', b'INLINE IMAGE', b'sh')
                  or fill and operator in (b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*')
                  or stroke and operator in (b'S', b's', b'B', b'B*', b'b', b'b*')):
                return True
            elif operator in (b'Tj', b'TJ', b"'", b'"'):
                if (fill and text_mode in (0, 2, 4, 6)) or (stroke and text_mode in (1, 2, 5, 6)):
                    content = args[-1] if operator == b'TJ' else [args[-1]]
                    if any(isinstance(part, (str, bytes)) and part.strip() for part in content):
                        return True
    return False


@lru_cache(maxsize=1)
def footer_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'arial.ttf'
    if path.is_file():
        pdfmetrics.registerFont(TTFont('EvidenceArial', str(path)))
        return 'EvidenceArial'
    return 'Helvetica'


def dated_pdf(original, output, url, checked_at, status, *, reserved_footer=False):
    from pypdf import PdfReader, PdfWriter, Transformation
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen.canvas import Canvas

    source = PdfReader(original)
    if source.is_encrypted or not source.pages:
        raise ValueError('PDF зашифрован или не содержит страниц')
    writer = PdfWriter()
    font = footer_font()
    address = quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")
    address_label = ('Адрес: ' if font != 'Helvetica' else 'Address: ') if reserved_footer else ''
    left = 28 if reserved_footer else 12
    caption = 'Дата и время снимка' if font != 'Helvetica' else 'Capture time'
    for page in source.pages:
        if page.rotation:
            page.transfer_rotation_to_content()
        box = page.cropbox
        unit = float(page.user_unit)
        width, height = float(box.width) * unit, float(box.height) * unit
        sheet_width = width if reserved_footer else max(240, width)
        lines = []
        line = address_label
        visible = url if not reserved_footer or len(url) <= 500 else url[:497] + '…'
        for char in visible:
            if line and pdfmetrics.stringWidth(line + char, font, 9) > sheet_width - 2 * left:
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
            footer.drawString(left, y, line)
            footer.linkURL(address, (left, y - 2, left + pdfmetrics.stringWidth(line, font, 9), y + 9))
        footer.setFillColorRGB(.12, .15, .19)
        footer.drawString(left, 10, f'{caption}: {format_capture_time(checked_at)}'
                          + (f' · HTTP {status}' if status else ''))
        footer.save()
        if reserved_footer:
            sheet = writer.add_page(page)
        else:
            sheet = writer.add_blank_page(width=sheet_width, height=height + band)
            shift = Transformation().translate(-float(box.left), -float(box.bottom)).scale(unit).translate(0, band)
            sheet.merge_transformed_page(page, shift)
        sheet.merge_page(PdfReader(footer_bytes).pages[0])
    writer.write(output)


def original_path(path):
    return path.with_name(path.stem + '__original.pdf')
