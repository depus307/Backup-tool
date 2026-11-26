"""Small Windows HTML/Unicode clipboard adapter; source values remain unchanged."""
import csv
import ctypes
from ctypes import wintypes
import html
import io
import re
import time


def clipboard_payload(rows, text_columns=()):
    snapshot = tuple(tuple(str(value) for value in row) for row in rows)
    table = '<table>' + ''.join('<tr>' + ''.join(
        '<td style="mso-number-format:\'\\@\';white-space:pre-wrap" x:str>'
        + html.escape(value).replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br style="mso-data-placement:same-cell">') + '</td>'
        for value in row) + '</tr>' for row in snapshot) + '</table>'
    prefix = '<html xmlns:x="urn:schemas-microsoft-com:office:excel"><head><meta charset="utf-8"></head><body><!--StartFragment-->'
    suffix = '<!--EndFragment--></body></html>'
    header_template = 'Version:1.0\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\nStartFragment:{:010d}\r\nEndFragment:{:010d}\r\n'
    header_length = len(header_template.format(0, 0, 0, 0).encode('ascii'))
    fragment_start = header_length + len(prefix.encode('utf-8'))
    fragment_end = fragment_start + len(table.encode('utf-8'))
    html_end = fragment_end + len(suffix.encode('utf-8'))
    data = header_template.format(header_length, html_end, fragment_start, fragment_end).encode('ascii')
    data += (prefix + table + suffix).encode('utf-8')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter='\t', lineterminator='\r\n')
    for row in snapshot:
        safe = []
        for column, value in enumerate(row):
            stripped = value.lstrip(' \t\r\n')
            risky = stripped.startswith(('=', '+', '-', '@'))
            if risky or (column in text_columns and re.fullmatch(r'\d+', value)):
                value = "'" + value
            safe.append(value)
        writer.writerow(safe)
    return {'html':data, 'text':stream.getvalue().removesuffix('\r\n'), 'snapshot':snapshot}


class WindowsClipboard:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user.OpenClipboard.argtypes = [wintypes.HWND]
        self.user.OpenClipboard.restype = wintypes.BOOL
        self.user.CloseClipboard.argtypes = []
        self.user.EmptyClipboard.argtypes = []
        self.user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self.user.SetClipboardData.restype = wintypes.HANDLE
        self.user.GetClipboardData.argtypes = [wintypes.UINT]
        self.user.GetClipboardData.restype = wintypes.HANDLE
        self.user.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        self.user.RegisterClipboardFormatW.restype = wintypes.UINT
        self.kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.kernel.GlobalAlloc.restype = wintypes.HGLOBAL
        self.kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalLock.restype = ctypes.c_void_p
        self.kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalSize.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalSize.restype = ctypes.c_size_t
        self.html_format = self.user.RegisterClipboardFormatW('HTML Format')
        if not self.html_format:
            raise ctypes.WinError(ctypes.get_last_error())

    def _open(self):
        for _ in range(10):
            if self.user.OpenClipboard(self.hwnd):
                return
            time.sleep(0.01)
        raise OSError('Windows clipboard is busy')

    def write(self, payload):
        blocks = []
        try:
            for format_id, content in ((self.html_format, payload['html'] + b'\0'),
                                       (13, payload['text'].encode('utf-16-le') + b'\0\0')):
                handle = self.kernel.GlobalAlloc(0x2, len(content))
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                blocks.append([format_id, handle])
                pointer = self.kernel.GlobalLock(handle)
                if not pointer:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    ctypes.memmove(pointer, content, len(content))
                finally:
                    self.kernel.GlobalUnlock(handle)
            self._open()
            try:
                if not self.user.EmptyClipboard():
                    raise ctypes.WinError(ctypes.get_last_error())
                for block in blocks:
                    if not self.user.SetClipboardData(*block):
                        raise ctypes.WinError(ctypes.get_last_error())
                    block[1] = None  # Windows owns the successful allocation.
            finally:
                self.user.CloseClipboard()
        finally:
            for _, handle in blocks:
                if handle:
                    self.kernel.GlobalFree(handle)

    def read(self, kind='text'):
        self._open()
        try:
            handle = self.user.GetClipboardData(13 if kind == 'text' else self.html_format)
            if not handle:
                raise OSError('Requested clipboard format is unavailable')
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                data = ctypes.string_at(pointer, self.kernel.GlobalSize(handle))
            finally:
                self.kernel.GlobalUnlock(handle)
            return data.decode('utf-16-le').rstrip('\0') if kind == 'text' else data.rstrip(b'\0')
        finally:
            self.user.CloseClipboard()
