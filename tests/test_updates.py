import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from backuptool import updates


class UpdateTests(unittest.TestCase):
    def test_checks_numeric_versions_and_only_accepts_the_repo_asset(self):
        name = 'BackupTool-2.10-Windows-x64.zip'
        url = f'https://github.com/{updates.REPOSITORY}/releases/download/v2.10/{name}'
        asset = {'name': name, 'state': 'uploaded', 'browser_download_url': url,
                 'size': 24, 'digest': f'sha256:{"a" * 64}'}
        release = {'tag_name': 'v2.10', 'assets': [asset], 'body': 'Release notes'}

        def check(current):
            with patch.object(updates, 'github_open', return_value=BytesIO(json.dumps(release).encode())):
                return updates.latest_update(current)

        self.assertEqual(check('2.9').version, '2.10')
        self.assertIsNone(check('2.10.0'))
        self.assertIsNone(check('3.0'))
        asset['browser_download_url'] = 'https://example.com/' + name
        with self.assertRaisesRegex(ValueError, 'Windows x64'):
            check('2.9')
        release['prerelease'] = True
        self.assertIsNone(check('2.9'))

    def test_verifies_digest_before_unpacking_and_preserves_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / 'BackupTool.exe'
            executable.write_bytes(b'Installed app')
            (root / '_internal').mkdir()
            sidecar = root / 'delivery.local.json'
            sidecar.write_bytes(b'Existing config')
            source = BytesIO()
            with ZipFile(source, 'w') as package:
                package.writestr('BackupTool/BackupTool.exe', b'New app')
                package.writestr('BackupTool/_internal/delivery.json', b'{}')
            data = source.getvalue()
            with patch.object(updates, 'github_open', return_value=BytesIO(data)):
                bad = updates.Update('2.4', 'unused', len(data), '0' * 64, '')
                with self.assertRaisesRegex(ValueError, 'SHA-256'):
                    updates.prepare(bad, executable)
            self.assertEqual(list(root.glob('.backuptool-update-*')), [])
            with patch.object(updates, 'github_open', return_value=BytesIO(data)):
                valid = updates.Update('2.4', 'unused', len(data), hashlib.sha256(data).hexdigest(), '')
                stage = updates.prepare(valid, executable)
            self.assertEqual((stage / 'payload/BackupTool.exe').read_bytes(), b'New app')
            self.assertEqual(executable.read_bytes(), b'Installed app')
            self.assertEqual(sidecar.read_bytes(), b'Existing config')

    def test_rejects_archive_paths_outside_updated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('BackupTool/_internal/../settings.json', 'BackupTool/delivery.local.json',
                         'BackupTool/_internal/a:b'):
                with self.subTest(name=name):
                    archive = root / 'archive.zip'
                    with ZipFile(archive, 'w') as package:
                        package.writestr(name, b'test')
                    with self.assertRaisesRegex(ValueError, 'недопустимый путь'):
                        updates.unpack(archive, root / 'destination')
