"""Asking the computer not to go to sleep while a run is working.

`clock.py` makes a suspend harmless after the fact. This is the part that tries
to stop it happening: while a real-AI run is live on this machine, the process
holds a wake lock, and the machine's idle timer does not fire.

It is a request, not a guarantee, and the difference matters enough to say in
the interface rather than only here. A closed lid, a battery running out, a
forced sleep or an administrator's policy all win over any of these:

    macOS    `caffeinate -i -w <pid>`, which exits by itself if this process
             dies, so a crash cannot leave a machine permanently awake
    Linux    `systemd-inhibit --what=idle:sleep`, where logind is running
    Windows  SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

Where none of them is available — a container, a trimmed-down image, a BSD —
`hold()` returns a handle that says `held` is false and why, and nothing else
changes. A server that cannot keep a laptop awake is the ordinary case for a
hosted deployment, where the machine running the agents is not the machine the
person closes.

Nothing here ever raises: a failure to hold a wake lock must not be able to
stop a run that was about to start.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

# The reason the operating system shows beside the lock, where it shows one.
REASON = 'BioSense is running an AI discovery run'


class Hold:
    """A wake lock, released by `release()`. Safe to release more than once."""

    def __init__(self, *, held, how, why=None, proc=None, windows=False):
        self.held, self.how, self.why = held, how, why
        self._proc, self._windows = proc, windows
        self._lock = threading.Lock()

    def release(self):
        with self._lock:
            if not self.held:
                return False
            self.held = False
            try:
                if self._proc is not None and self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=5)
                    except Exception:  # noqa: BLE001 - it is going away either way
                        self._proc.kill()
                elif self._windows:
                    import ctypes
                    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:  # noqa: BLE001 - releasing is best effort
                return False
            return True

    def snapshot(self):
        return {'held': self.held, 'how': self.how, 'why': self.why}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _spawn(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def hold(reason=REASON, *, platform=None, spawn=_spawn, pid=None):
    """Ask this machine to stay awake. Never raises; check `.held`."""
    plat = platform or sys.platform
    pid = pid or os.getpid()
    try:
        if plat == 'darwin':
            # -w <pid>: caffeinate exits with this process, so a kill -9 here
            # cannot leave the machine awake for ever.
            return Hold(held=True, how='caffeinate',
                        proc=spawn(['caffeinate', '-i', '-w', str(pid)]))
        if plat.startswith('linux'):
            return Hold(held=True, how='systemd-inhibit',
                        proc=spawn(['systemd-inhibit', '--what=idle:sleep', '--mode=block',
                                    f'--why={reason}', 'sleep', 'infinity']))
        if plat.startswith('win'):
            import ctypes
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: keep the system up until
            # cleared. Not ES_DISPLAY_REQUIRED — the screen may sleep.
            ok = ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
            if not ok:
                return Hold(held=False, how='SetThreadExecutionState',
                            why='the operating system refused the request')
            return Hold(held=True, how='SetThreadExecutionState', windows=True)
    except FileNotFoundError:
        return Hold(held=False, how=plat,
                    why='the tool that holds a wake lock is not installed here')
    except Exception as e:  # noqa: BLE001 - a wake lock may never break a run
        return Hold(held=False, how=plat, why=f'{type(e).__name__}: {e}')
    return Hold(held=False, how=plat, why=f'no wake lock is known for {plat}')


class Keeper:
    """Holds the lock while at least one run is live. Counted, not a flag.

    Two runs overlapping must not have the first one to finish release the
    lock out from under the second, so this counts rather than toggles.
    """

    def __init__(self, *, holder=hold, reason=REASON):
        self._holder, self._reason = holder, reason
        self._lock = threading.Lock()
        self._n = 0
        self._hold = None

    def acquire(self):
        with self._lock:
            self._n += 1
            if self._hold is None:
                self._hold = self._holder(self._reason)
            return self._hold

    def release(self):
        with self._lock:
            self._n = max(0, self._n - 1)
            if self._n == 0 and self._hold is not None:
                self._hold.release()
                self._hold = None

    def snapshot(self):
        with self._lock:
            d = self._hold.snapshot() if self._hold else {'held': False, 'how': None, 'why': None}
            return dict(d, runs=self._n)
