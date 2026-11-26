"""Prove the hashed lock installs in a newly created, isolated environment."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


if __name__ == '__main__':
    folder = Path(tempfile.mkdtemp(prefix='lock-repro-', dir=ROOT / '.build'))
    venv.EnvBuilder(with_pip=False).create(folder)
    python = folder / 'Scripts/python.exe'
    subprocess.run([sys.executable, '-m', 'pip', '--python', str(python), 'install', '--no-index',
                    '--find-links', str(ROOT / '.wheelhouse'), '--require-hashes', '-r', str(ROOT / 'requirements.lock'),
                    '--disable-pip-version-check'], check=True)
    code = 'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"]:d.version for d in m.distributions()}))'
    installed = json.loads(subprocess.check_output([str(python), '-c', code], text=True))
    expected = {}
    for line in (ROOT / 'requirements.lock').read_text().splitlines():
        if line and not line.startswith('#'):
            name, version = line.split()[0].split('==')
            expected[normalize(name)] = version
    actual = {normalize(name):version for name,version in installed.items()}
    assert actual == expected, {'expected':expected,'installed':actual}
    payload = {'status':'PASS', 'fresh_environment':str(folder.relative_to(ROOT)),
               'packages':actual, 'exact_lock_match':True, 'network_used':False}
    (ROOT / 'reports/iterations/0.2/fresh-lock-deployment.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
