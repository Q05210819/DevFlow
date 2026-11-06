"""One cross-platform OS owner lock for the local model runtime.

The lock file is intentionally never unlinked. Removing it would let a second
process create a new inode while the first still holds the old one, so both
would appear to own the models. The file lives on a volume shared by every
model-capable container on the host.
"""

import os
from pathlib import Path


class ModelOwner:
    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._fd: int | None = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    os.close(fd)
                    return False
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    return False
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._acquired = True
        return True

    def close(self) -> None:
        fd, self._fd = self._fd, None
        self._acquired = False
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)
