"""Iteration 0.2 shell. Business UI is introduced in iteration 6.1."""
import json
import os
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk

from .controller import WorkerSession

HEAVY = ('playwright', 'openpyxl', 'pypdf', 'httpx', 'pandas', 'numpy', 'PySide6', 'PyQt6')


def run_shell(report=None, scenario='normal'):
    root = tk.Tk()
    root.title('BackupTool next — 0.2')
    root.geometry('560x220')
    root.minsize(480, 200)
    session = WorkerSession()
    events = []
    start_imports = [module for module in HEAVY if module in sys.modules]
    status = tk.StringVar(value='Каркас версии 0.2. Проверка рабочего процесса.')
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='BackupTool next', font=('Segoe UI', 16)).pack(anchor='w')
    ttk.Label(frame, textvariable=status, wraplength=500).pack(anchor='w', pady=14)
    bar = ttk.Frame(frame)
    bar.pack(anchor='w')

    def start():
        if session.state in ('starting', 'running', 'stopping'):
            return
        session.start(duration=5 if scenario == 'cancel' else 0.2)
        status.set('Рабочий процесс запускается…')
        button_start.configure(state='disabled')
        button_stop.configure(state='normal')
        if report and scenario == 'cancel':
            root.after(10, stop)

    def stop():
        session.cancel()
        status.set('Остановка…')
        button_stop.configure(state='disabled')

    def close():
        session.close()
        root.destroy()

    button_start = ttk.Button(bar, text='Проверить процесс', command=start)
    button_start.pack(side='left')
    button_stop = ttk.Button(bar, text='Отменить', command=stop, state='disabled')
    button_stop.pack(side='left', padx=10)
    root.protocol('WM_DELETE_WINDOW', close)

    def poll():
        events.extend(session.poll())
        if session.state == 'finished':
            status.set(f'Завершено: {session.final["outcome"]}')
            button_start.configure(state='normal')
            button_stop.configure(state='disabled')
            if report:
                payload = {'gui_pid': os.getpid(), 'worker_pid': session.worker_pid,
                           'state': session.state, 'final': session.final, 'events': events,
                           'startup_heavy_imports': start_imports,
                           'finish_heavy_imports': [m for m in HEAVY if m in sys.modules],
                           'tk_patchlevel': root.tk.call('info', 'patchlevel'),
                           'tk_version': root.tk.call('package', 'require', 'Tk')}
                Path(report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                root.after(500, close)
                return
        root.after(25, poll)

    root.after(25, poll)
    if report:
        root.after(500, start)
    root.mainloop()
