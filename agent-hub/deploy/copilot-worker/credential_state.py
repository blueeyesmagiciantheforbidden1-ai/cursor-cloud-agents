"""Opaque refresh-safe credential handoff; no provider calls or cloud clients.

The coordinator must supply a real durable fenced broker. A local file lock,
taskCount=1, or one Cloud Run task is not serialization across job executions.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re

AGENT = "copilot"
AUTH_FILE = "config.json"
ACCOUNT_REF = "9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc"
MAX_BYTES = 256 * 1024

class CredentialError(RuntimeError):
    pass

@dataclass(frozen=True)
class Lease:
    account_ref: str
    fence: int
    version: str
    auth_bytes: bytes = field(repr=False)

class RefreshSession:
    """Broker: assert_current, commit, release, quarantine; durable/fenced/idempotent.

    The parent must be protected from task code. Native CLI alone refreshes
    credentials. No other writer may own this provider account until the native
    process is stopped and commit is acknowledged. An uncertain commit keeps
    both lease and refreshed local bytes quarantined for reconciliation.
    """
    def __init__(self, broker, lease, home):
        if (lease.account_ref != ACCOUNT_REF or type(lease.fence) is not int
                or lease.fence < 1 or not isinstance(lease.version, str) or not lease.version
                or not isinstance(lease.auth_bytes, bytes) or not 0 < len(lease.auth_bytes) <= MAX_BYTES):
            raise CredentialError("invalid_credential_lease")
        self.broker, self.lease, self.home = broker, lease, Path(home)
        self.state = "new"

    @property
    def auth_path(self):
        return self.home / ("." + AGENT) / AUTH_FILE

    def restore(self):
        if self.state != "new" or self.home.exists() or self.home.is_symlink():
            raise CredentialError("fresh_private_home_required")
        self.broker.assert_current(self.lease)
        self.home.mkdir(mode=0o700)
        self.auth_path.parent.mkdir(mode=0o700)
        descriptor = os.open(self.auth_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(self.lease.auth_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        self.state = "active"

    def finish(self, *, native_stopped):
        if self.state != "active" or native_stopped is not True:
            raise CredentialError("native_process_must_be_stopped")
        try:
            self.broker.assert_current(self.lease)
            if (self.auth_path.is_symlink() or not self.auth_path.is_file()
                    or self.auth_path.stat().st_size > MAX_BYTES):
                raise CredentialError("invalid_refreshed_credential_file")
            with self.auth_path.open("rb") as stream:
                body = stream.read(MAX_BYTES + 1)
            if not 0 < len(body) <= MAX_BYTES:
                raise CredentialError("invalid_refreshed_credential_file")
            version = self.broker.commit(self.lease, body)
            if not isinstance(version, str) or not version:
                raise CredentialError("credential_commit_uncertain")
            self.broker.release(self.lease, version)
        except Exception:
            self.state = "quarantined"
            try:
                self.broker.quarantine(self.lease, "writeback_uncertain")
            finally:
                raise CredentialError("credential_writeback_quarantined") from None
        self.state = "committed"
        return version

