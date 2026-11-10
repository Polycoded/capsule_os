from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import signal
import subprocess
from dataclasses import dataclass


CREATE_NEW_PROCESS_GROUP = 0x00000200 if os.name == "nt" else 0
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040
NORMAL_PRIORITY_CLASS = 0x00000020
HIGH_PRIORITY_CLASS = 0x00000080
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
# Toolhelp works without WMI permissions and is fast enough for live dashboard polling.
_WMI_AVAILABLE: bool | None = False


@dataclass
class WinProcessInfo:
    pid: int
    parent_pid: int | None
    name: str
    command_line: str
    memory_bytes: int
    kernel_time: int
    user_time: int
    read_ops: int
    write_ops: int


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    def as_int(self) -> int:
        return (self.high << 32) | self.low


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("read_ops", ctypes.c_ulonglong),
        ("write_ops", ctypes.c_ulonglong),
        ("other_ops", ctypes.c_ulonglong),
        ("read_bytes", ctypes.c_ulonglong),
        ("write_bytes", ctypes.c_ulonglong),
        ("other_bytes", ctypes.c_ulonglong),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("page_fault_count", wintypes.DWORD),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


def launch_process(command: list[str], cwd: str | None, env: dict[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        cwd=cwd or None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=CREATE_NEW_PROCESS_GROUP,
    )


def list_processes() -> dict[int, WinProcessInfo]:
    global _WMI_AVAILABLE
    if os.name != "nt":
        return {}
    if _WMI_AVAILABLE is False:
        return _list_processes_toolhelp()
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine,WorkingSetSize,KernelModeTime,UserModeTime,ReadOperationCount,WriteOperationCount | "
        "ConvertTo-Json -Depth 2"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        _WMI_AVAILABLE = False
        return _list_processes_toolhelp()
    _WMI_AVAILABLE = True
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _list_processes_toolhelp()
    if isinstance(raw, dict):
        raw = [raw]
    info: dict[int, WinProcessInfo] = {}
    for item in raw:
        try:
            pid = int(item.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        info[pid] = WinProcessInfo(
            pid=pid,
            parent_pid=_to_int(item.get("ParentProcessId")),
            name=item.get("Name") or "",
            command_line=item.get("CommandLine") or "",
            memory_bytes=_to_int(item.get("WorkingSetSize")) or 0,
            kernel_time=_to_int(item.get("KernelModeTime")) or 0,
            user_time=_to_int(item.get("UserModeTime")) or 0,
            read_ops=_to_int(item.get("ReadOperationCount")) or 0,
            write_ops=_to_int(item.get("WriteOperationCount")) or 0,
        )
    return info


def _list_processes_toolhelp() -> dict[int, WinProcessInfo]:
    """Collect process-tree and PCB telemetry without WMI administrator access."""
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return {}

    info: dict[int, WinProcessInfo] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
    try:
        while has_entry:
            pid = int(entry.th32ProcessID)
            parent_pid = int(entry.th32ParentProcessID) or None
            name = entry.szExeFile
            memory_bytes = 0
            kernel_time = 0
            user_time = 0
            read_ops = 0
            write_ops = 0
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                False,
                pid,
            )
            if handle:
                try:
                    memory = PROCESS_MEMORY_COUNTERS()
                    memory.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                    if psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
                        memory_bytes = int(memory.working_set_size)

                    created = FILETIME()
                    exited = FILETIME()
                    kernel = FILETIME()
                    user = FILETIME()
                    if kernel32.GetProcessTimes(
                        handle,
                        ctypes.byref(created),
                        ctypes.byref(exited),
                        ctypes.byref(kernel),
                        ctypes.byref(user),
                    ):
                        kernel_time = kernel.as_int()
                        user_time = user.as_int()

                    io = IO_COUNTERS()
                    if kernel32.GetProcessIoCounters(handle, ctypes.byref(io)):
                        read_ops = int(io.read_ops)
                        write_ops = int(io.write_ops)
                finally:
                    kernel32.CloseHandle(handle)

            info[pid] = WinProcessInfo(
                pid=pid,
                parent_pid=parent_pid,
                name=name,
                command_line="",
                memory_bytes=memory_bytes,
                kernel_time=kernel_time,
                user_time=user_time,
                read_ops=read_ops,
                write_ops=write_ops,
            )
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return info


def descendants(root_pid: int, process_map: dict[int, WinProcessInfo]) -> set[int]:
    result = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, info in process_map.items():
            if info.parent_pid in result and pid not in result:
                result.add(pid)
                changed = True
    return result


def set_priority(pid: int, priority: str) -> bool:
    if os.name != "nt":
        return False
    priority_map = {
        "idle": IDLE_PRIORITY_CLASS,
        "low": BELOW_NORMAL_PRIORITY_CLASS,
        "normal": NORMAL_PRIORITY_CLASS,
        "high": HIGH_PRIORITY_CLASS,
    }
    value = priority_map.get(priority, NORMAL_PRIORITY_CLASS)
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0200 | 0x0400, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.SetPriorityClass(handle, value))
    finally:
        kernel32.CloseHandle(handle)


def suspend(pid: int) -> bool:
    return _nt_process_call(pid, "NtSuspendProcess")


def resume(pid: int) -> bool:
    return _nt_process_call(pid, "NtResumeProcess")


def terminate(pid: int) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _nt_process_call(pid: int, fn_name: str) -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    handle = kernel32.OpenProcess(0x0800 | 0x0400, False, pid)
    if not handle:
        return False
    try:
        fn = getattr(ntdll, fn_name)
        return fn(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


def system_metrics() -> dict[str, object]:
    if os.name != "nt":
        return {"platform": os.name}
    ps = (
        "$os=Get-CimInstance Win32_OperatingSystem; "
        "$cpu=(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; "
        "[PSCustomObject]@{"
        "cpuPercent=$cpu;"
        "totalMemory=$os.TotalVisibleMemorySize*1024;"
        "freeMemory=$os.FreePhysicalMemory*1024;"
        "pageFileFree=$os.FreeSpaceInPagingFiles*1024"
        "} | ConvertTo-Json"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {"platform": "windows", "error": result.stderr.strip()}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"platform": "windows"}


def _to_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
