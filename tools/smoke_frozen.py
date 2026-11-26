"""Real Windows EXE checks, including process/window ownership and teardown."""
import json
from pathlib import Path
import subprocess
import sys
import time

from win_metrics import TreeMonitor, processes, visible_windows

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'reports/iterations/0.2'


def main():
    REPORT.mkdir(parents=True, exist_ok=True)
    executable = ROOT / 'dist/BackupToolNext/BackupTool.exe'
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                creationflags=subprocess.CREATE_NO_WINDOW)
    rows = []
    try:
        for scenario in ('normal', 'cancel'):
            report_file = REPORT / f'frozen-{scenario}.json'
            if report_file.exists():
                report_file.unlink()  # Exact owned evidence file, never a user artifact.
            start = time.perf_counter()
            process = subprocess.Popen([str(executable), '--internal-smoke-report', str(report_file),
                                        '--internal-smoke-scenario', scenario], cwd=ROOT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            monitor = TreeMonitor(process.pid)
            root_windows, worker_windows = set(), set()
            while process.poll() is None and time.perf_counter() - start < 20:
                monitor.sample()
                root_windows.update(visible_windows(process.pid))
                for pid in monitor.known - {process.pid}:
                    worker_windows.update(visible_windows(pid))
                time.sleep(0.025)
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
                raise AssertionError('Frozen smoke timed out; no PASS')
            wall = time.perf_counter() - start
            assert process.returncode == 0, process.returncode
            assert report_file.is_file(), 'Missing worker result'
            result = json.loads(report_file.read_text(encoding='utf-8'))
            assert result['state'] == 'finished'
            assert result['final']['outcome'] == ('success' if scenario == 'normal' else 'cancelled'), result
            assert result['final']['imported_forbidden_modules'] == []
            assert result['startup_heavy_imports'] == result['finish_heavy_imports'] == []
            assert result['gui_pid'] == process.pid
            assert result['worker_pid'] != process.pid
            assert result['final']['parent_pid'] == process.pid
            assert monitor.known == {process.pid, result['worker_pid']}, monitor.known
            assert root_windows == {'BackupTool next — 0.2'}, root_windows
            assert not worker_windows, worker_windows
            assert [message['type'] for message in result['events']] == ['ready', 'final']
            remaining = set(processes()) & monitor.known
            assert not remaining, remaining
            assert sentinel.poll() is None, 'Unrelated sentinel was terminated'
            rows.append({'scenario':scenario, 'status':'PASS', 'wall_seconds':wall,
                         'gui_pid':process.pid, 'worker_pid':result['worker_pid'],
                         'process_count':len(monitor.known), 'main_window_titles':sorted(root_windows),
                         'worker_windows':sorted(worker_windows), 'remaining_own_pids':sorted(remaining),
                         'unrelated_process_alive':True, 'metrics':monitor.result(wall)})
            print(scenario, 'PASS', flush=True)
        (REPORT / 'frozen-smoke.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


if __name__ == '__main__':
    main()
