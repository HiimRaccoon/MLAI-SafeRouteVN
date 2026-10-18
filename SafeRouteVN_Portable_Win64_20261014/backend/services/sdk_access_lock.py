"""Bound M3 SDK subprocess access to one resolved authority at a time."""
from pathlib import Path
from time import monotonic, sleep

from .worker_lock import WorkerLock


class SdkAccessBusy(Exception):
    pass


class SdkAccessLock:
    def __init__(self, authority_path):
        authority = Path(authority_path).resolve()
        self.path = authority.with_name(authority.name + ".m3-sdk-access.lock")
        self.lock = WorkerLock(self.path)
        self.held = False

    def acquire(self, deadline):
        while monotonic() < deadline:
            try:
                self.lock.acquire()
                self.held = True
                return
            except RuntimeError as error:
                if str(error) != "WORKER_ALREADY_RUNNING":
                    raise
            except BaseException:
                self.release()
                raise
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(0.02, remaining))
        raise SdkAccessBusy("SDK access deadline expired")

    def release(self):
        if self.held:
            try:
                self.lock.release()
            finally:
                self.held = False
        elif self.lock.handle is not None:
            # WorkerLock initialization can fail before the OS lock is held.
            # Close that handle without trying to unlock unowned bytes.
            try:
                self.lock.handle.close()
            finally:
                self.lock.handle = None
