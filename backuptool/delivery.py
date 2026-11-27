"""Copy completed files to their destination without replacing existing files."""
from contextlib import suppress
import ctypes
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil


def is_unc(path):
    drive = PureWindowsPath(path).drive
    if drive.casefold().startswith('\\\\?\\unc\\'):
        return True
    return drive.startswith('\\\\') and not drive.startswith(('\\\\?\\', '\\\\.\\'))


def is_network_path(path):
    if is_unc(path):
        return True
    root = PureWindowsPath(path).anchor
    return bool(os.name == 'nt' and root and ctypes.windll.kernel32.GetDriveTypeW(root) == 4)


def default_destination(*configs):
    for config in configs:
        try:
            path = json.loads(config.read_text(encoding='utf-8')).get('default_destination')
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(path, str) and is_unc(path):
            return path
    return ''


def file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def exists(path):
    try:
        path.stat()
    except FileNotFoundError:
        return False
    return True


def publish_file(source, folder, name, temp_name, candidate, remember):
    """Stage on the destination volume, then publish with no replacing rename."""
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / temp_name
    if candidate and not exists(temp):
        target = folder / candidate
        if exists(target) and file_digest(target) == file_digest(source):
            return target
        candidate = ''
        remember(candidate)
    if not candidate:
        try:
            with source.open('rb') as incoming, temp.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        except OSError:
            with suppress(OSError):
                temp.unlink()
            raise

    stem, suffix = Path(name).stem, Path(name).suffix
    copy_number = 0
    if candidate and candidate != name:
        copy_number = int(Path(candidate).stem.removeprefix(stem + '_'))
    while True:
        candidate = f'{stem}_{copy_number}{suffix}' if copy_number else name
        target = folder / candidate
        # Persist the intended name before rename so a lost response can be retried.
        remember(candidate)
        try:
            if os.name == 'nt':
                temp.rename(target)
            else:
                os.link(temp, target)
                with suppress(OSError):
                    temp.unlink()
        except FileExistsError:
            copy_number += 1
        else:
            return target
