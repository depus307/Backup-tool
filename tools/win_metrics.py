"""Read-only Windows process instrumentation. Never controls application UI."""
import ctypes as c
from ctypes import wintypes as w
import time

kernel = c.WinDLL('kernel32', use_last_error=True)
psapi = c.WinDLL('psapi', use_last_error=True)


class ProcessEntry(c.Structure):
    _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD), ('heap', c.c_size_t),
                ('module', w.DWORD), ('threads', w.DWORD), ('parent', w.DWORD),
                ('priority', w.LONG), ('flags', w.DWORD), ('exe', w.WCHAR * 260)]


class Memory(c.Structure):
    _fields_ = [('cb', w.DWORD), ('faults', w.DWORD)] + [(name, c.c_size_t) for name in
                ('peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private')]


kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
kernel.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
kernel.Process32NextW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel.OpenProcess.restype = w.HANDLE
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4
psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.POINTER(Memory), w.DWORD]


def processes():
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = c.sizeof(entry)
        success = kernel.Process32FirstW(handle, c.byref(entry))
        rows = {}
        while success:
            rows[entry.pid] = (entry.parent, entry.exe)
            success = kernel.Process32NextW(handle, c.byref(entry))
        return rows
    finally:
        kernel.CloseHandle(handle)


def process_sample(pid):
    handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
    if not handle:
        return None
    try:
        created, exited, system, user = (w.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, c.byref(created), c.byref(exited), c.byref(system), c.byref(user)):
            return None
        ticks = lambda value: value.dwLowDateTime + (value.dwHighDateTime << 32)
        memory = Memory()
        memory.cb = c.sizeof(memory)
        if not psapi.GetProcessMemoryInfo(handle, c.byref(memory), c.sizeof(memory)):
            return None
        return {'created': ticks(created), 'cpu_seconds': (ticks(system) + ticks(user)) / 1e7,
                'working_set': memory.ws, 'private_bytes': memory.private}
    finally:
        kernel.CloseHandle(handle)


class TreeMonitor:
    def __init__(self, pid):
        self.root = pid
        self.identities = {}
        self.known = {pid}
        self.peak_ws = self.peak_private = self.peak_count = 0
        self.samples = 0
        self.gaps = set()

    def sample(self):
        rows = processes()
        current = {self.root} if self.root in rows else set()
        while True:
            children = {pid for pid, (parent, _) in rows.items() if parent in current}
            if children <= current:
                break
            current |= children
        self.known |= current
        ws = private = count = 0
        for pid in current:
            value = process_sample(pid)
            if value is None:
                if pid in rows:
                    self.gaps.add(pid)  # Transient exits are explicitly reported.
                continue
            identity = (pid, value['created'])
            self.identities[identity] = max(self.identities.get(identity, 0), value['cpu_seconds'])
            ws += value['working_set']
            private += value['private_bytes']
            count += 1
        self.peak_ws = max(self.peak_ws, ws)
        self.peak_private = max(self.peak_private, private)
        self.peak_count = max(self.peak_count, count)
        self.samples += 1

    def result(self, wall_seconds):
        cpu = sum(self.identities.values())
        return {'cpu_seconds_sampled': cpu, 'cpu_one_core_percent': cpu / wall_seconds * 100,
                'peak_tree_working_set_bytes': self.peak_ws, 'peak_tree_private_bytes': self.peak_private,
                'peak_process_count': self.peak_count, 'samples': self.samples,
                'transient_unreadable_pids': sorted(self.gaps),
                'method': 'Toolhelp32 + GetProcessTimes + GetProcessMemoryInfo, 50ms sampling; sums last observed CPU per PID/creation time; short-lived between samples excluded'}


def visible_windows(pid):
    user32 = c.WinDLL('user32', use_last_error=True)
    callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user32.EnumWindows.argtypes = [callback, w.LPARAM]
    user32.IsWindowVisible.argtypes = [w.HWND]
    user32.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    titles = []

    @callback
    def inspect(hwnd, _):
        owner = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, c.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = c.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            if title.value:
                titles.append(title.value)
        return True
    user32.EnumWindows(inspect, 0)
    return titles
