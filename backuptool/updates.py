"""Check GitHub Releases and prepare a verified Windows update."""
from base64 import b64encode
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

from .delivery import file_digest, is_network_path

REPOSITORY = 'depus307/Backup-tool'
RELEASE_API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_UNPACKED = 768 * 1024 * 1024
RESULT_FILE = '.backuptool-update-result.json'
RELEASE_FILES = ('_internal', 'README.md', 'CHANGELOG.md', 'BackupTool.exe')
ALLOWED_HOSTS = ('api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                 'objects.githubusercontent.com')


@dataclass(frozen=True)
class Update:
    version: str
    url: str
    size: int
    digest: str
    notes: str


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        address = urlsplit(newurl)
        if (address.scheme != 'https' or address.hostname not in ALLOWED_HOSTS
                or address.port not in (None, 443) or address.username is not None):
            raise ValueError('GitHub перенаправил запрос за пределы адресов загрузки.')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def github_open(url):
    return build_opener(GitHubRedirects()).open(Request(url, headers={'User-Agent': 'BackupTool'}), timeout=20)


def version_key(version):
    if not re.fullmatch(r'[0-9]{1,6}\.[0-9]{1,6}(?:\.[0-9]{1,6})?', version):
        raise ValueError('Неподдерживаемый номер версии BackupTool.')
    parts = tuple(map(int, version.split('.')))
    return parts + (0,) * (3 - len(parts))


def latest_update(current_version):
    try:
        with github_open(RELEASE_API) as response:
            release = json.loads(response.read(1024 * 1024))
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    tag = release['tag_name']
    version = tag.removeprefix('v')
    if release.get('draft') or release.get('prerelease') or version_key(version) <= version_key(current_version):
        return None
    name = f'BackupTool-{version}-Windows-x64.zip'
    url = f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}'
    for asset in release['assets']:
        if asset.get('name') != name or asset.get('state') != 'uploaded':
            continue
        size, digest = asset.get('size'), asset.get('digest') or ''
        if (asset.get('browser_download_url') == url and type(size) is int and 0 < size <= MAX_DOWNLOAD
                and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest)):
            return Update(version, url, size, digest[7:].lower(), (release.get('body') or '')[:1600])
    raise ValueError(f'В релизе {version} нет проверяемого архива Windows x64.')


def download(update, archive):
    digest = hashlib.sha256()
    received = 0
    with github_open(update.url) as response, archive.open('xb') as output:
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > update.size:
                raise ValueError('Размер ZIP отличается от указанного на GitHub.')
            digest.update(chunk)
            output.write(chunk)
    if received != update.size or digest.hexdigest() != update.digest:
        raise ValueError('Архив обновления не прошёл проверку SHA-256.')


def unpack(archive, destination):
    with ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 12000 or sum(item.file_size for item in entries) > MAX_UNPACKED:
            raise ValueError('Архив обновления слишком большой.')
        seen = set()
        for item in entries:
            parts = item.filename.rstrip('/').split('/')
            if parts == ['BackupTool'] and item.is_dir():
                continue
            if (len(parts) < 2 or parts[0] != 'BackupTool' or parts[1] not in RELEASE_FILES
                    or (parts[1] != '_internal' and (len(parts) != 2 or item.is_dir()))
                    or item.external_attr >> 16 & 0o170000 == 0o120000
                    or any(not p or p in ('.', '..') or p.endswith(('.', ' ')) or PureWindowsPath(p).is_reserved()
                           or re.search(r'[<>:"\\|?*\x00-\x1f]', p) for p in parts)):
                raise ValueError('Архив обновления содержит недопустимый путь.')
            relative = '/'.join(parts[1:]).casefold()
            if relative in seen:
                raise ValueError('Архив обновления содержит совпадающие пути.')
            seen.add(relative)
            target = destination.joinpath(*parts[1:])
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(item) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
    if not (destination / 'BackupTool.exe').is_file() or not (destination / '_internal').is_dir():
        raise ValueError('В ZIP отсутствуют обязательные файлы BackupTool.')


def prepare(update, executable):
    root = executable.parent
    if is_network_path(root) or is_network_path(root.resolve()):
        raise ValueError('Для обновления запустите BackupTool из папки на локальном диске.')
    if executable.name.casefold() != 'backuptool.exe' or not (root / '_internal').is_dir():
        raise ValueError('Для обновления нужна полная распакованная Windows-сборка BackupTool.')
    stage = Path(tempfile.mkdtemp(prefix='.backuptool-update-', dir=root))
    try:
        archive = stage / 'release.zip'
        download(update, archive)
        unpack(archive, stage / 'payload')
        archive.unlink()
        request = {'root': str(root), 'stage': str(stage), 'pid': os.getpid(), 'original': file_digest(executable),
                   'version': update.version}
        (stage / 'request.json').write_text(json.dumps(request), encoding='ascii')
    except Exception:
        shutil.rmtree(stage)
        raise
    return stage


def install(stage):
    helper = Path(__file__).with_name('apply_update.ps1').read_text(encoding='ascii')
    request = b64encode(str(stage / 'request.json').encode('utf-8')).decode('ascii')
    command = helper.replace('__REQUEST_BASE64__', request)
    encoded = b64encode(command.encode('utf-16-le')).decode('ascii')
    powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    frozen = getattr(sys, 'frozen', False)
    if frozen:
        # PyInstaller sets a DLL search directory that child processes inherit.
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=stage.parent, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        if frozen:
            ctypes.windll.kernel32.SetDllDirectoryW(sys._MEIPASS)


def previous_result(root):
    path = root / RESULT_FILE
    try:
        result = json.loads(path.read_text(encoding='utf-8-sig'))
        path.unlink()
    except (OSError, ValueError):
        return None
    if not isinstance(result, dict) or not {'success', 'stage', 'version', 'error'} <= result.keys():
        return None
    if result['success']:
        stage = root / result['stage']
        # The updater only owns its direct staging directory.
        if (re.fullmatch(r'\.backuptool-update-[a-z0-9_]+', stage.name) and stage.resolve().parent == root.resolve()
                and stage == root / stage.name and not stage.is_symlink()):
            shutil.rmtree(stage, ignore_errors=True)
    return result
