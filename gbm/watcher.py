"""Detached polling watcher with a token-scoped heartbeat."""
import os
import time
from . import __version__

def active(project):
    state = project.get('watcher', {})
    if not (state.get('running') and time.time() - state.get('heartbeat', 0) < 5):
        return False
    pid = state.get('pid')
    if not pid:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def run(project, token):
    pending = None
    since = time.monotonic()
    while True:
        with project.lock():
            status = project.get('watcher', {})
            if status.get('token') != token or not status.get('running'):
                return
            status.update(pid=os.getpid(), heartbeat=time.time(), version=__version__)
            project.set('watcher', status)
            config = project.config()
            if not project.pending():
                state = project.scan()
                if state == project.get('baseline'):
                    pending = None
                elif state != pending:
                    pending, since = state, time.monotonic()
                elif time.monotonic() - since >= config['settle_seconds']:
                    project.capture(state)
                    pending = None
        time.sleep(config['poll_seconds'])
