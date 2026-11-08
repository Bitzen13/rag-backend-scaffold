"""API key issuance and verification, with the data layer faked (no database needed).

`tests/test_repositories.py` covers the repository itself against a real Postgres; what is tested
here is the hashing and the active/revoked decision that authentication turns on.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.db.models import ApiKey
from app.services.api_keys import (
    KEY_PREFIX,
    ApiKeyService,
    generate_api_key,
    hash_api_key,
)


def new_row(key_hash: str, label: str, active: bool = True) -> ApiKey:
    """A row as the database would return it: `created_at` is a server default in Postgres."""
    return ApiKey(
        id=uuid.uuid4(),
        key_hash=key_hash,
        label=label,
        is_active=active,
        created_at=datetime.now(UTC),
    )


class FakeApiKeyStore:
    """In-memory stand-in for ApiKeyRepository, keyed by hash exactly as the real one is.

    It covers the whole repository surface, not just authentication, because the internal admin
    routes list and revoke through the same contract (services/admin.ApiKeyAdminStore).
    """

    def __init__(self) -> None:
        self.rows: dict[str, ApiKey] = {}

    async def create(self, *, key_hash: str, label: str) -> ApiKey:
        api_key = new_row(key_hash, label)
        self.rows[key_hash] = api_key
        return api_key

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        return self.rows.get(key_hash)

    async def list_all(self) -> list[ApiKey]:
        return sorted(self.rows.values(), key=lambda row: row.created_at)

    async def revoke(self, api_key_id: uuid.UUID) -> bool:
        for row in self.rows.values():
            if row.id == api_key_id:
                row.is_active = False
                return True
        return False

    async def touch_last_used(self, api_key_id: uuid.UUID) -> None:
        """Also a KeyUsageStore: the API records every key's last use (services/usage.py)."""
        for row in self.rows.values():
            if row.id == api_key_id:
                row.last_used_at = datetime.now(UTC)


def add_key(store: FakeApiKeyStore, *, label: str = "consumer", active: bool = True) -> str:
    """Store a key and return its plaintext."""
    key = generate_api_key()
    store.rows[hash_api_key(key)] = new_row(hash_api_key(key), label, active)
    return key


# --- key material ------------------------------------------------------------------------------


def test_generated_keys_are_prefixed_and_unique() -> None:
    keys = {generate_api_key() for _ in range(100)}
    assert len(keys) == 100
    assert all(key.startswith(KEY_PREFIX) for key in keys)
    assert all(len(key) > len(KEY_PREFIX) + 32 for key in keys)


def test_hash_is_stable_and_is_not_the_key() -> None:
    key = generate_api_key()
    assert hash_api_key(key) == hash_api_key(key)
    assert key not in hash_api_key(key)
    assert hash_api_key(key) != hash_api_key(generate_api_key())
    assert len(hash_api_key(key)) == 64  # sha256 hex


# --- issuing -----------------------------------------------------------------------------------


async def test_issue_stores_only_the_hash() -> None:
    store = FakeApiKeyStore()
    key, row = await ApiKeyService(store).issue("my-frontend")

    assert row.label == "my-frontend"
    assert row.is_active is True
    assert row.key_hash == hash_api_key(key)
    assert key not in {stored.key_hash for stored in store.rows.values()}


# --- authenticating ----------------------------------------------------------------------------


async def test_active_key_authenticates() -> None:
    store = FakeApiKeyStore()
    key = add_key(store, label="slack bot")

    api_key = await ApiKeyService(store).authenticate(key)

    assert api_key is not None
    assert api_key.label == "slack bot"


@pytest.mark.parametrize("presented", ["", "not-a-key", f"{KEY_PREFIX}wrong"])
async def test_unknown_key_does_not_authenticate(presented: str) -> None:
    store = FakeApiKeyStore()
    add_key(store)

    assert await ApiKeyService(store).authenticate(presented) is None


async def test_revoked_key_does_not_authenticate() -> None:
    store = FakeApiKeyStore()
    key = add_key(store, active=False)

    assert await ApiKeyService(store).authenticate(key) is None
