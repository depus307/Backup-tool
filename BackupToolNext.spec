# Uses the installed Chrome. No Chromium download is included.
from pathlib import Path
import ssl
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH)
icon = root / 'BackupToolNext.ico'
# The bundled Python runtime's ssl module loads this companion CA file.
# Keep its verification behavior intact when freezing with that runtime.
ssl_companion = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
ssl_data = [(str(ssl_companion), '.')] if ssl_companion.is_file() else []
analysis = Analysis(
    [str(root / 'launcher.py')],
    pathex=[str(root)],
    binaries=[],
    datas=collect_data_files('playwright') + [
        (str(icon), '.'), (str(root / 'CHANGELOG.md'), '.'), (str(root / 'delivery.json'), '.'),
    ] + ssl_data,
    hiddenimports=['playwright.async_api', 'openpyxl', 'pypdf', 'httpx'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'numpy', 'PySide6', 'PyQt6', 'pytest', 'ruff'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name='BackupTool',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, icon=str(icon),
)
collection = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='BackupToolNext')
