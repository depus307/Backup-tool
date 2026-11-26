import csv
import io
import re
import tkinter as tk
import unittest

from backuptool.clipboard import WindowsClipboard, clipboard_payload


class ClipboardTests(unittest.TestCase):
    def test_utf8_offsets_and_escaping(self):
        payload = clipboard_payload([['0123456789','Кириллица <&"', '=1+1','первая\nвторая']], (0,))
        data = payload['html']
        offsets = {key.decode():int(value) for key,value in re.findall(rb'(StartHTML|EndHTML|StartFragment|EndFragment):(\d+)', data)}
        fragment = data[offsets['StartFragment']:offsets['EndFragment']].decode('utf-8')
        self.assertTrue(fragment.startswith('<table>'))
        self.assertTrue(fragment.endswith('</table>'))
        self.assertIn('Кириллица &lt;&amp;&quot;', fragment)
        self.assertIn('первая<br style="mso-data-placement:same-cell">вторая', fragment)
        self.assertEqual(offsets['EndHTML'], len(data))
        self.assertTrue(data[offsets['StartHTML']:].startswith(b'<html'))

    def test_formula_and_identifier_protection_does_not_mutate_model(self):
        original = [['0123456789','=1+1','+1+1','-1+1','@SUM(1;1)','  =1+1']]
        payload = clipboard_payload(original, (0,))
        expected = ["'0123456789","'=1+1","'+1+1","'-1+1","'@SUM(1;1)","'  =1+1"]
        self.assertEqual(next(csv.reader(io.StringIO(payload['text']),delimiter='\t')), expected)
        self.assertEqual(original[0][0], '0123456789')
        self.assertEqual(original[0][1], '=1+1')
        original[0][0] = 'changed later'
        self.assertEqual(payload['snapshot'][0][0], '0123456789')

    def test_multiline_and_tabs_keep_tsv_structure(self):
        rows = [['первая\nвторая','a\tb','"quoted"'], ['next','x','y']]
        payload = clipboard_payload(rows)
        self.assertEqual(list(csv.reader(io.StringIO(payload['text']),delimiter='\t')), rows)

    def test_real_windows_clipboard_formats(self):
        root = tk.Tk()
        root.withdraw()
        try:
            adapter = WindowsClipboard(root.winfo_id())
            payload = clipboard_payload([['0123456789','Тест','=1+1']], (0,))
            adapter.write(payload)
            self.assertEqual(adapter.read(), payload['text'])
            self.assertEqual(adapter.read('html'), payload['html'])
        finally:
            root.destroy()
