import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from backuptool.archive import Archive, backup_name
from backuptool.delivery import default_destination, is_unc
from backuptool.evidence import dated_pdf, format_capture_time, original_path


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'assets' / 'useful.pdf'


@unittest.skipUnless(os.name == 'nt', 'Archive uses a Windows writer lock')
class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'source.csv'
        self.source.write_text('url\nhttps://example.com\n', encoding='utf-8')
        self.destination = self.root / 'shared'

    def archive(self, machine):
        archive = Archive(self.root / machine, self.destination)
        self.addCleanup(archive.close)
        return archive

    def capture(self, archive, stamp, original=False):
        list_id = archive.import_source(self.source)
        row = archive.rows(list_id)[0]
        attempt = archive.begin(row['resource_id'], 'capture')
        staged = archive.work_dir('capture') / f'{attempt}.pdf'
        dated_pdf(FIXTURE, staged, row['url'], stamp, 200)
        source = None
        if original:
            source = original_path(staged)
            source.write_bytes(FIXTURE.read_bytes())
        name = backup_name('Company', '0123', row['url'])
        return Path(archive.publish(attempt, staged, name, checked_at=stamp, original=source))

    def test_filename_uses_pdf_calendar_date_and_keeps_history(self):
        archive = self.archive('machine')
        stamps = ['2026-11-27T23:59:59-08:00', '2026-11-28T00:00:00-08:00', '2026-11-28T10:00:00+10:00']
        paths = [self.capture(archive, stamp) for stamp in stamps]
        self.assertEqual([path.name for path in paths],
                         ['0123_Company_2026-11-27.pdf', '0123_Company_2026-11-28.pdf', '0123_Company_2026-11-28_1.pdf'])
        self.assertEqual(archive.send_pending(lambda: False, lambda _: None), 0)
        for path, stamp in zip(paths, stamps):
            saved = self.destination / path.name
            self.assertIn(format_capture_time(stamp), PdfReader(saved).pages[0].extract_text())
        targets = archive.db.execute("SELECT target FROM attempts WHERE state='ready'").fetchall()
        self.assertEqual({archive.root / item['target'] for item in targets}, set(paths))

    def test_failed_copy_retries_after_restart_and_avoids_other_backups(self):
        machine = self.root / 'machine'
        stamp = '2026-11-27T12:00:00+03:00'
        archive = Archive(machine, self.destination)
        try:
            self.capture(archive, stamp, original=True)
            notifications = []

            def broken_copy(source, target):
                target.write(source.read(64))
                raise OSError('Network disconnected')

            with patch('backuptool.delivery.shutil.copyfileobj', side_effect=broken_copy):
                self.assertEqual(archive.send_pending(lambda: False, notifications.append), 2)
            self.assertIn('Network disconnected', notifications[0])
            self.assertEqual(list(self.destination.iterdir()), [])
        finally:
            archive.close()

        colleague = self.archive('colleague')
        self.capture(colleague, '2026-11-27T12:01:00+03:00', original=True)
        self.assertEqual(colleague.send_pending(lambda: False, lambda _: None), 0)
        first = self.destination / '0123_Company_2026-11-27.pdf'
        previous = first.read_bytes()

        recovered = self.archive('machine')
        self.assertEqual(recovered.send_pending(lambda: False, lambda _: None), 0)
        self.assertEqual(recovered.send_pending(lambda: False, lambda _: None), 0)
        self.assertEqual(first.read_bytes(), previous)
        self.assertEqual({path.name for path in self.destination.iterdir()}, {
            first.name, original_path(first).name,
            '0123_Company_2026-11-27_1.pdf', '0123_Company_2026-11-27_1__original.pdf',
        })
        self.assertEqual((self.destination / '0123_Company_2026-11-27_1__original.pdf').read_bytes(),
                         FIXTURE.read_bytes())

    def test_lost_rename_response_does_not_create_a_second_backup(self):
        archive = self.archive('machine')
        self.capture(archive, '2026-11-27T12:00:00+03:00')
        rename = Path.rename

        def interrupted_after_rename(path, target):
            rename(path, target)
            raise OSError('Connection lost after rename')

        with patch.object(Path, 'rename', interrupted_after_rename):
            self.assertEqual(archive.send_pending(lambda: False, lambda _: None), 1)
        self.assertEqual(archive.send_pending(lambda: False, lambda _: None), 0)
        self.assertEqual([path.name for path in self.destination.iterdir()], ['0123_Company_2026-11-27.pdf'])


class DestinationTests(unittest.TestCase):
    def test_backup_name_without_inn_uses_company_or_hostname(self):
        self.assertEqual(backup_name('Company Name', '', 'https://example.com'), 'Company-Name')
        self.assertEqual(backup_name('', '', 'https://example.com'), 'example.com')

    def test_distribution_default_is_unc(self):
        config = FIXTURE.parents[2] / 'delivery.json'
        bundled = default_destination(config)
        self.assertTrue(is_unc(bundled))
        with tempfile.TemporaryDirectory() as folder:
            override = Path(folder) / 'delivery.local.json'
            self.assertEqual(default_destination(override, config), bundled)
            network_path = r'\\server\share\archive'
            override.write_text(json.dumps({'default_destination': network_path}), encoding='utf-8')
            self.assertEqual(default_destination(override, config), network_path)
            override.write_text(json.dumps({'default_destination': 'G:\\archive'}), encoding='utf-8')
            self.assertEqual(default_destination(override, config), bundled)
        self.assertTrue(is_unc(r'\\?\UNC\server\share'))
        self.assertFalse(is_unc(r'\\?\C:\archive'))
        self.assertFalse(is_unc('G:\\archive'))
