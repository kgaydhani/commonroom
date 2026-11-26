"""Kill a whole process tree on Windows without taskkill: a Job Object that kills its members when terminated.

`taskkill` can be refused in a sandbox (Codex), and ending only the `cmd.exe` parent leaves the grandchildren running
with the output handle they inherited. Every function is a no-op (False/None) off Windows or when the API is refused."""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

CREATE_SUSPENDED = 0x00000004
_KILL_ON_CLOSE = 0x2000
_EXTENDED_LIMIT_INFO = 9
_PROCESS_ALL_ACCESS = 0x1F0FFF


class _Basic(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]


class _Extended(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def start(command: str, cwd: str, capture: bool = False) -> tuple[subprocess.Popen, object]:
    """Start `command` in a shell, inside a kill-on-close job when Windows allows it. Returns (process, job or None).
    The process starts suspended and runs only once it is in the job, so no child can slip out first.
    `capture` gives it separate stdout and stderr pipes to read; otherwise it shares ours."""
    pipes = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if capture else {}
    if os.name != "nt":
        return subprocess.Popen(command, shell=True, cwd=cwd, start_new_session=True, **pipes), None
    job = None
    try:
        k = _kernel()
        job = k.CreateJobObjectW(None, None)
        if not job:
            raise OSError("no job object")
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_CLOSE
        if not k.SetInformationJobObject(job, _EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("job limits refused")
        process = subprocess.Popen(command, shell=True, cwd=cwd, creationflags=CREATE_SUSPENDED, **pipes)
    except OSError:
        if job:
            _close(job)
        return subprocess.Popen(command, shell=True, cwd=cwd, **pipes), None
    handle = k.OpenProcess(_PROCESS_ALL_ACCESS, False, process.pid)
    ok = bool(handle) and bool(k.AssignProcessToJobObject(job, handle))
    resumed = False
    if handle:
        try:
            resumed = ctypes.WinDLL("ntdll").NtResumeProcess(handle) == 0
        except (OSError, AttributeError):
            pass
        k.CloseHandle(handle)
    if not resumed:   # never leave it frozen
        process.kill()
        raise OSError("could not resume the suite process")
    if not ok:
        _close(job)
        job = None
    return process, job


def _close(job) -> None:
    try:
        _kernel().CloseHandle(job)
    except OSError:
        pass


def kill(job) -> bool:
    """End every process in the job (and close it). False if there is no job or Windows refused."""
    if not job:
        return False
    try:
        return bool(_kernel().TerminateJobObject(job, 1))
    except OSError:
        return False
    finally:
        _close(job)
