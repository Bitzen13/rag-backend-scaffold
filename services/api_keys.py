"""Issuing and verifying API keys. Plaintext keys exist only in transit — never in the database.

A key is 32 random bytes, so a single SHA-256 is the right hash for it: there is no low-entropy
secret to brute-force, and a slow KDF would be paid on every request instead of once at sign-up.
"""

import hashlib
import hmac
import secrets
from typing import Protocol

from db.models import ApiKey

KEY_PREFIX = "rag_"  # makes a leaked key recognisable in a log or a paste
KEY_BYTES = 32


def generate_api_key() -> str:
    """A new key, in plaintext. It is shown to the operator once and never stored."""
    return KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)


def hash_api_key(key: str) -> str:
    """The value stored in `api_keys.key_hash`."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ApiKeyStore(Protocol):
    """The data-layer contract this service needs (see db/repositories/api_keys.py)."""

    async def create(self, *, key_hash: str, label: str) -> ApiKey: ...

    async def get_by_hash(self, key_hash: str) -> ApiKey | None: ...


class ApiKeyService:
    def __init__(self, keys: ApiKeyStore) -> None:
        self._keys = keys

    async def issue(self, label: str) -> tuple[str, ApiKey]:
        """Create a key, returning the plaintext (the only time it exists) and the stored row."""
        key = generate_api_key()
        return key, await self._keys.create(key_hash=hash_api_key(key), label=label)

    async def authenticate(self, presented: str) -> ApiKey | None:
        """The active key this plaintext belongs to, or None if it is unknown or revoked.

        The lookup is by hash, so a plaintext key is never compared against anything, and the
        final check is `hmac.compare_digest`: a revoked key and an unknown one take the same path
        and are reported the same way, so neither the response nor its timing says which it was.
        """
        digest = hash_api_key(presented)
        api_key = await self._keys.get_by_hash(digest)
        if api_key is None:
            return None
        if not hmac.compare_digest(api_key.key_hash, digest):
            return None
        return api_key if api_key.is_active else None
