"""Narrow native Windows Job launch and read-handle byte locking.

Git is associated with the Job atomically at CreateProcess, then resumed only
after all handles are owned. Unsupported Job policies fail closed.
"""

import ctypes as c
import os
import subprocess
import time
from ctypes import wintypes as w

k = c.WinDLL("kernel32", use_last_error=True)
SIZE = c.c_size_t
PTR = c.c_void_p


class IO_COUNTERS(c.Structure):
    _fields_ = [
        (name, c.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class BASIC_LIMIT(c.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", c.c_longlong),
        ("PerJobUserTimeLimit", c.c_longlong),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", SIZE),
        ("MaximumWorkingSetSize", SIZE),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", SIZE),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class EXTENDED_LIMIT(c.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC_LIMIT),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE),
        ("JobMemoryLimit", SIZE),
        ("PeakProcessMemoryUsed", SIZE),
        ("PeakJobMemoryUsed", SIZE),
    ]


class ACCOUNTING(c.Structure):
    _fields_ = [
        (name, c.c_longlong)
        for name in (
            "TotalUserTime",
            "TotalKernelTime",
            "ThisPeriodTotalUserTime",
            "ThisPeriodTotalKernelTime",
        )
    ]
    _fields_ += [
        (name, w.DWORD)
        for name in (
            "TotalPageFaultCount",
            "TotalProcesses",
            "ActiveProcesses",
            "TotalTerminatedProcesses",
        )
    ]


class STARTUPINFO(c.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
    ]
    _fields_ += [
        (name, w.DWORD)
        for name in (
            "dwX",
            "dwY",
            "dwXSize",
            "dwYSize",
            "dwXCountChars",
            "dwYCountChars",
            "dwFillAttribute",
            "dwFlags",
        )
    ]
    _fields_ += [
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", PTR),
        ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE),
        ("hStdError", w.HANDLE),
    ]


class STARTUPINFOEX(c.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", PTR)]


class PROCESS_INFORMATION(c.Structure):
    _fields_ = [
        ("hProcess", w.HANDLE),
        ("hThread", w.HANDLE),
        ("dwProcessId", w.DWORD),
        ("dwThreadId", w.DWORD),
    ]


class OVERLAPPED(c.Structure):
    _fields_ = [
        ("Internal", SIZE),
        ("InternalHigh", SIZE),
        ("Offset", w.DWORD),
        ("OffsetHigh", w.DWORD),
        ("hEvent", w.HANDLE),
    ]


def _bind(name, args, result=w.BOOL):
    fn = getattr(k, name)
    fn.argtypes, fn.restype = args, result
    return fn


_close = _bind("CloseHandle", [w.HANDLE])
_create_job = _bind("CreateJobObjectW", [PTR, w.LPCWSTR], w.HANDLE)
_set_job = _bind("SetInformationJobObject", [w.HANDLE, c.c_int, PTR, w.DWORD])
_query_job = _bind("QueryInformationJobObject", [w.HANDLE, c.c_int, PTR, w.DWORD, PTR])
_terminate_job = _bind("TerminateJobObject", [w.HANDLE, w.UINT])
_init_attrs = _bind(
    "InitializeProcThreadAttributeList", [PTR, w.DWORD, w.DWORD, c.POINTER(SIZE)]
)
_update_attrs = _bind(
    "UpdateProcThreadAttribute", [PTR, w.DWORD, SIZE, PTR, SIZE, PTR, PTR]
)
_delete_attrs = _bind("DeleteProcThreadAttributeList", [PTR], None)
_create_process = _bind(
    "CreateProcessW",
    [w.LPCWSTR, w.LPWSTR, PTR, PTR, w.BOOL, w.DWORD, PTR, w.LPCWSTR, PTR, PTR],
)
_resume = _bind("ResumeThread", [w.HANDLE], w.DWORD)
_wait = _bind("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)
_exit_code = _bind("GetExitCodeProcess", [w.HANDLE, c.POINTER(w.DWORD)])
_lock = _bind("LockFileEx", [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, PTR])
_unlock = _bind("UnlockFileEx", [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, PTR])
_times = _bind("GetProcessTimes", [w.HANDLE, PTR, PTR, PTR, PTR])
_current = _bind("GetCurrentProcess", [], w.HANDLE)


def _check(value):
    if not value:
        raise OSError(c.get_last_error(), "native_containment_failed")
    return value


def creation_identity():
    values = [w.FILETIME() for _ in range(4)]
    _check(_times(_current(), *(c.byref(v) for v in values)))
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


class FileLock:
    def __init__(self, file, shared):
        import msvcrt

        self.handle = msvcrt.get_osfhandle(file.fileno())
        self.shared = shared
        self.overlapped = OVERLAPPED()

    def acquire(self):
        if _lock(
            self.handle,
            1 | (0 if self.shared else 2),
            0,
            1,
            0,
            c.byref(self.overlapped),
        ):
            return True
        error = c.get_last_error()
        if error in (33, 997):
            return False
        raise OSError(error, "lock_unavailable")

    def release(self):
        _check(_unlock(self.handle, 0, 1, 0, c.byref(self.overlapped)))


class Job:
    def __init__(self, name):
        self.handle = _check(_create_job(None, name))
        if c.get_last_error() == 183:
            _close(self.handle)
            raise OSError("native_containment_failed")
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = (
            0x2000  # KILL_ON_JOB_CLOSE, no breakaway
        )
        try:
            _check(_set_job(self.handle, 9, c.byref(limits), c.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def active(self):
        info = ACCOUNTING()
        _check(_query_job(self.handle, 1, c.byref(info), c.sizeof(info), None))
        return info.ActiveProcesses

    def terminate(self):
        _check(_terminate_job(self.handle, 1))

    def close(self):
        if self.handle:
            _close(self.handle)
            self.handle = None

    def spawn(self, argv, cwd, env):
        import msvcrt

        fds = []
        pi = PROCESS_INFORMATION()
        attrs = None
        initialized = False
        try:
            out_r, out_w = os.pipe()
            fds.extend((out_r, out_w))
            err_r, err_w = os.pipe()
            fds.extend((err_r, err_w))
            stdin = os.open(os.devnull, os.O_RDONLY)
            fds.append(stdin)
            handles = (w.HANDLE * 3)(
                *(msvcrt.get_osfhandle(fd) for fd in (stdin, out_w, err_w))
            )
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            size = SIZE()
            _init_attrs(None, 2, 0, c.byref(size))
            attrs = c.create_string_buffer(size.value)
            _check(_init_attrs(attrs, 2, 0, c.byref(size)))
            initialized = True
            jobs = (w.HANDLE * 1)(self.handle)
            _check(
                _update_attrs(attrs, 0, 0x20002, handles, c.sizeof(handles), None, None)
            )
            _check(_update_attrs(attrs, 0, 0x2000D, jobs, c.sizeof(jobs), None, None))
            si = STARTUPINFOEX()
            si.StartupInfo.cb = c.sizeof(si)
            si.StartupInfo.dwFlags = 0x100
            (
                si.StartupInfo.hStdInput,
                si.StartupInfo.hStdOutput,
                si.StartupInfo.hStdError,
            ) = handles
            si.lpAttributeList = c.cast(attrs, PTR)
            environment = c.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted(env.items()))
                + "\0\0"
            )
            command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
            _check(
                _create_process(
                    argv[0],
                    command,
                    None,
                    None,
                    True,
                    0x80000 | 0x400 | 0x4 | 0x08000000,
                    environment,
                    str(cwd),
                    c.byref(si),
                    c.byref(pi),
                )
            )
            if _resume(pi.hThread) == 0xFFFFFFFF:
                raise OSError("native_containment_failed")
            process = NativeProcess(
                pi.hProcess,
                pi.dwProcessId,
                os.fdopen(out_r, "rb", buffering=0),
                os.fdopen(err_r, "rb", buffering=0),
            )
            fds.remove(out_r)
            fds.remove(err_r)
            pi.hProcess = None
            return process
        except BaseException:
            if pi.hProcess:
                self.terminate()
                _wait(pi.hProcess, 2000)
            raise
        finally:
            if initialized:
                _delete_attrs(attrs)
            if pi.hThread:
                _close(pi.hThread)
            if pi.hProcess:
                _close(pi.hProcess)
            for fd in fds:
                os.close(fd)


class NativeProcess:
    def __init__(self, handle, pid, stdout, stderr):
        self.handle, self.pid = handle, pid
        self.stdout, self.stderr = stdout, stderr

    def poll(self):
        result = _wait(self.handle, 0)
        if result == 258:
            return None
        if result != 0:
            raise OSError("native_containment_failed")
        code = w.DWORD()
        _check(_exit_code(self.handle, c.byref(code)))
        return code.value

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("owned_git", timeout)
            time.sleep(0.01)
        return self.poll()

    def close(self):
        if self.handle:
            _close(self.handle)
            self.handle = None
