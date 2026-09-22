"""Immutable SHA-256 artifacts with atomic writes and verified reads.

Artifact identifiers are hashes, never filesystem paths. The store uses relative
directory-descriptor operations and refuses symlinks when opening objects.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import uuid


class ArtifactError(ValueError):
    """An artifact identifier or stored artifact is invalid."""


class IntegrityError(ArtifactError):
    """Stored bytes do not match their content address."""


class ArtifactStore:
    """A process-safe content-addressed directory of immutable byte strings."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        self.root = path.resolve(strict=True)
        if not self.root.is_dir():
            raise ArtifactError("Artifact root must be a directory")

    @staticmethod
    def _validate_digest(digest: str) -> str:
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ArtifactError("Artifact identifier must be a lowercase SHA-256 digest")
        return digest

    def _open_root(self) -> int:
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def put(self, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError("Artifacts must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        directory = self._open_root()
        temporary = ".pending-" + uuid.uuid4().hex
        try:
            # Do not silently hide existing corruption by overwriting it.
            try:
                existing = self._read_at(directory, digest)
            except FileNotFoundError:
                pass
            else:
                self._check(digest, existing)
                return digest
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, digest, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory)
        return digest

    @staticmethod
    def _read_at(directory: int, digest: str) -> bytes:
        descriptor = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()

    @staticmethod
    def _check(digest: str, data: bytes) -> None:
        if hashlib.sha256(data).hexdigest() != digest:
            raise IntegrityError(f"Artifact integrity verification failed: {digest}")

    def get(self, digest: str) -> bytes:
        self._validate_digest(digest)
        directory = self._open_root()
        try:
            data = self._read_at(directory, digest)
        finally:
            os.close(directory)
        self._check(digest, data)
        return data

    def verify(self, digest: str) -> bool:
        """Verify existence and integrity; malformed identifiers still raise."""
        self._validate_digest(digest)
        try:
            self.get(digest)
        except (IntegrityError, OSError):
            return False
        return True
