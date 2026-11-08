"""Issue, list and revoke the API keys consumers authenticate with.

Usage: python scripts/create_api_key.py --label "my-frontend"   # issue a key (shown once)
       python scripts/create_api_key.py --list                   # every key, active or revoked
       python scripts/create_api_key.py --revoke <id>            # deactivate one (repeatable)
       python scripts/create_api_key.py --revoke <id> --yes      # skip the confirmation prompt

Only the hash of a key is stored, so an issued key is printed exactly once and cannot be recovered
afterwards — if it is lost, revoke it and issue another. Revoking sets `is_active` to false rather
than deleting the row, so a key that has been used can still be accounted for.
"""

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ApiKey
from app.db.repositories.api_keys import ApiKeyRepository
from app.db.session import create_engine, create_session_factory, database_url_from_settings
from app.observability import configure_logging, install_handler
from app.services.api_keys import ApiKeyService
from app.settings import get_settings

SessionFactory = async_sessionmaker[AsyncSession]


async def issue_key(session_factory: SessionFactory, label: str, header: str) -> int:
    async with session_factory() as session, session.begin():
        key, row = await ApiKeyService(ApiKeyRepository(session)).issue(label)
        key_id = row.id
    print(f"\nIssued an API key for {label!r}:\n")
    print(f"    {key}\n")
    print("Copy it now: only its hash is stored, so this is the only time it can be shown.")
    print(f"Put it in the consumer's .env and send it in the {header} header.")
    print(f"\nid: {key_id}   (use this with --revoke)")
    return 0


async def list_keys(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        keys = await ApiKeyRepository(session).list_all()
    if not keys:
        print("No API keys have been issued.")
        return 0
    width = max(len(key.label) for key in keys)
    print(f"{'id':<38}{'label':<{width + 2}}{'state':<9}last used")
    for key in keys:
        state = "active" if key.is_active else "revoked"
        used = key.last_used_at.isoformat(timespec="seconds") if key.last_used_at else "never"
        print(f"{key.id!s:<38}{key.label:<{width + 2}}{state:<9}{used}")
    return 0


def parse_ids(raw_ids: list[str]) -> list[uuid.UUID] | None:
    try:
        return [uuid.UUID(raw) for raw in raw_ids]
    except ValueError as exc:
        print(f"error: {exc}; run --list to see the key ids")
        return None


def confirm(keys: list[ApiKey]) -> bool:
    """Revocation breaks a live consumer, so it is confirmed like any other destructive action."""
    print("Will revoke:")
    for key in keys:
        print(f"  {key.id}  {key.label}")
    if not sys.stdin.isatty():
        print("error: not a terminal; re-run with --yes to confirm non-interactively")
        return False
    try:
        answer = input("\nRevoke these keys? Type 'yes' to confirm: ")
    except EOFError:  # stdin closed under us: take it as "no", never as "yes"
        print("error: no answer read; re-run with --yes to confirm non-interactively")
        return False
    return answer.strip().lower() == "yes"


async def revoke_keys(
    session_factory: SessionFactory, raw_ids: list[str], *, skip_prompt: bool
) -> int:
    ids = parse_ids(raw_ids)
    if ids is None:
        return 1
    async with session_factory() as session:
        stored = {key.id: key for key in await ApiKeyRepository(session).list_all()}
    missing = [key_id for key_id in ids if key_id not in stored]
    if missing:
        print(f"error: no key with id {missing[0]}; run --list to see them")
        return 1
    targets = [stored[key_id] for key_id in ids]
    if not skip_prompt and not confirm(targets):
        print("Cancelled; nothing revoked.")
        return 1
    async with session_factory() as session, session.begin():
        repository = ApiKeyRepository(session)
        for key_id in ids:
            await repository.revoke(key_id)
    print(f"\nRevoked {len(ids)} key(s); calls using them now get 401.")
    return 0


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine(database_url_from_settings(settings))
    session_factory = create_session_factory(engine)
    try:
        if args.list:
            return await list_keys(session_factory)
        if args.revoke:
            return await revoke_keys(session_factory, args.revoke, skip_prompt=args.yes)
        return await issue_key(session_factory, args.label, settings.auth.api_key_header)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", help="what this key is for, e.g. 'my-frontend' or 'slack bot'")
    parser.add_argument("--list", action="store_true", help="list every key, active or revoked")
    parser.add_argument(
        "--revoke",
        action="append",
        default=[],
        metavar="ID",
        help="deactivate the key with this id (repeatable)",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()
    if not args.label and not args.list and not args.revoke:
        parser.error("one of --label, --list or --revoke is required")
    if args.label and (args.list or args.revoke):
        parser.error("--label issues a key; use it on its own")
    install_handler(level=logging.WARNING)
    try:
        sys.exit(asyncio.run(main(args)))
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")
