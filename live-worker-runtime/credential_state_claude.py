"""Opaque refresh-safe credential handoff for the Claude live worker.

The lease bytes are the dedicated subscription token; the native CLI receives
them only through its own environment variable and never rewrites this file,
so finish() normally commits identical bytes (the broker deduplicates versions).
Packaged as /opt/runcrew/app/credential_state.py in the Claude live image only.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path

AGENT = "claude"
AUTH_FILE = "oauth-token"
ACCOUNT_REF = "fb55abaefbe43b9b1cbd82a01f04c398968b29182c598cda9b77477c9110b29f"
MAX_BYTES = 16 * 1024


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    account_ref: str
    fence: int
    version: str
    auth_bytes: bytes = field(repr=False)


class RefreshSession:
    """Broker: assert_current, commit, release, quarantine; durable/fenced/idempotent."""
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
