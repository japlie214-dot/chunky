"""Chunked Windows Credential Manager keyring backend.

Why this exists
---------------
Windows Credential Manager caps one credential blob at 2560 bytes and stores it
as UTF-16. Snowflake's OAuth access/refresh tokens exceed that, so the stock
`keyring` Windows backend dies with:

    (1783, 'CredWrite', 'The stub received bad data')

...which makes snowflake-connector-python unable to cache OAuth tokens on
Windows at all, forcing an interactive browser login on every single run.

This backend transparently splits an oversized secret across several Credential
Manager entries and reassembles it on read, so the connector's existing OAuth
refresh flow has a working cache to persist through. Verified round-tripping
9 KB secrets.

No-op on non-Windows platforms (macOS Keychain and the Linux file cache have no
such size limit).
"""

from __future__ import annotations

import base64
import sys

IS_WINDOWS = sys.platform == "win32"

# Conservative raw-byte chunk size: after base64 (x4/3) and UTF-16 (x2) plus the
# BOM this stays well under CRED_MAX_CREDENTIAL_BLOB_SIZE (2560 bytes).
_CHUNK_SIZE = 700

if IS_WINDOWS:  # pragma: no cover - platform specific
    import keyring
    from keyring.backends.Windows import WinVaultKeyring
    from keyring.errors import PasswordDeleteError

    class ChunkedWinVaultKeyring(WinVaultKeyring):
        """WinVaultKeyring that survives secrets larger than the blob limit."""

        def _meta_target(self, service, username):
            return f"{service}::chunked-meta::{username}"

        def _chunk_target(self, service, username, index):
            return f"{service}::chunked-part{index}::{username}"

        def set_password(self, service, username, password):
            self._delete_chunks(service, username)
            data = (password or "").encode("utf-8")
            chunks = [data[i:i + _CHUNK_SIZE] for i in range(0, len(data), _CHUNK_SIZE)] or [b""]
            for idx, chunk in enumerate(chunks):
                self._set_password(
                    self._chunk_target(service, username, idx),
                    username,
                    base64.b64encode(chunk).decode("ascii"),
                )
            # Meta written last: it is the commit marker for a complete write.
            self._set_password(self._meta_target(service, username), username, str(len(chunks)))

        def get_password(self, service, username):
            meta = self._read_credential(self._meta_target(service, username))
            if not meta:
                # Fall back to the stock layout so pre-existing unchunked
                # entries (e.g. an externalbrowser ID token) still resolve.
                return super().get_password(service, username)
            try:
                count = int(meta.value)
            except (TypeError, ValueError):
                return None
            parts = []
            for idx in range(count):
                cred = self._read_credential(self._chunk_target(service, username, idx))
                if cred is None:
                    return None
                parts.append(base64.b64decode(cred.value))
            try:
                return b"".join(parts).decode("utf-8")
            except UnicodeDecodeError:
                return None

        def delete_password(self, service, username):
            chunked = self._delete_chunks(service, username)
            legacy = False
            try:
                super().delete_password(service, username)
                legacy = True
            except PasswordDeleteError:
                pass
            if not (chunked or legacy):
                raise PasswordDeleteError(service)

        def _delete_chunks(self, service, username) -> bool:
            meta_target = self._meta_target(service, username)
            meta = self._read_credential(meta_target)
            if not meta:
                return False
            try:
                count = int(meta.value)
            except (TypeError, ValueError):
                count = 0
            for idx in range(count):
                self._safe_delete(self._chunk_target(service, username, idx))
            self._safe_delete(meta_target)
            return True

        def _safe_delete(self, target):
            try:
                self._delete_password(target)
            except Exception:
                pass


def install() -> bool:
    """Activate the chunked backend. Returns True if installed."""
    if not IS_WINDOWS:
        return False
    keyring.set_keyring(ChunkedWinVaultKeyring())
    return True
