"""Opaque credential handoff contract. Durable broker implementation is external.

No token parsing, cloud clients, logging, refresh requests, or model requests.
One fenced lease owns one provider account across all invocations and machines.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re

MAX_AUTH_BYTES = 256 * 1024
PROFILES = frozenset(('ryan', 'blueeyes'))


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    profile: str
    account_ref: str
    fence: int
    version: str
    auth_bytes: bytes = field(repr=False)


class RefreshSession:
    """Requires a trusted protected, otherwise empty CODEX_HOME parent.

    Broker methods: assert_current(lease), commit(lease, opaque_bytes)->new_version,
    release(lease, new_version), quarantine(lease, reason). All operations atomic,
    fenced and durable. commit and release must be idempotent on the same input.
    Broker cannot admit a successor while the old native process may still run.
    """
    def __init__(self, broker, lease, home):
        if (lease.profile not in PROFILES or not re.fullmatch('[a-f0-9]{64}', lease.account_ref)
                or type(lease.fence) is not int or lease.fence < 1 or not lease.version
                or not isinstance(lease.auth_bytes, bytes) or not 0 < len(lease.auth_bytes) <= MAX_AUTH_BYTES):
            raise CredentialError('invalid_credential_lease')
        self.broker, self.lease, self.home = broker, lease, Path(home)
        self.state = 'new'

    def restore(self):
        if self.state != 'new' or self.home.exists() or self.home.is_symlink():
            raise CredentialError('fresh_private_home_required')
        self.broker.assert_current(self.lease)
        self.home.mkdir(mode=0o700)
        path = self.home / 'auth.json'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(self.lease.auth_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        self.state = 'active'

    def finish(self, *, native_stopped):
        if self.state != 'active' or native_stopped is not True:
            raise CredentialError('native_process_must_be_stopped')
        try:
            self.broker.assert_current(self.lease)
            path = self.home / 'auth.json'
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_AUTH_BYTES:
                raise CredentialError('invalid_refreshed_credential_file')
            with path.open('rb') as stream:
                body = stream.read(MAX_AUTH_BYTES + 1)
            if not 0 < len(body) <= MAX_AUTH_BYTES:
                raise CredentialError('invalid_refreshed_credential_file')
            version = self.broker.commit(self.lease, body)
            if not isinstance(version, str) or not version:
                raise CredentialError('credential_commit_uncertain')
            self.broker.release(self.lease, version)
        except Exception:
            self.state = 'quarantined'
            # Retain local refreshed auth for reconciliation. No stale restore,
            # auto retry, lease release or deletion after an uncertain writeback.
            try:
                self.broker.quarantine(self.lease, 'writeback_uncertain')
            finally:
                raise CredentialError('credential_writeback_quarantined') from None
        self.state = 'committed'
        return version
