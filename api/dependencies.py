"""Injectable pieces of a request: settings, start-up resources, a session, auth, the service.

Everything a route needs arrives through `Depends`, so a test overrides the database, the
providers or the clock without touching the route (code-standards.md).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.errors import ApiError, ErrorCode
from db.models import ApiKey
from db.repositories.api_keys import ApiKeyRepository
from db.session import create_engine, create_session_factory, database_url_from_settings
from embedding.base import EmbeddingProvider
from embedding.factory import build_embedding_provider
from generation.base import LLMProvider
from generation.factory import build_llm_provider
from services.api_keys import ApiKeyService, ApiKeyStore
from services.factory import build_query_service
from services.query_service import QueryService
from settings import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass(frozen=True)
class Resources:
    """Built once at start-up and shared by every request: the engine and the provider clients.

    Provider clients hold connection pools, so they are made here rather than per request.
    """

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    embedder: EmbeddingProvider
    llm: LLMProvider

    async def close(self) -> None:
        await self.engine.dispose()


def build_resources(settings: Settings) -> Resources:
    """Open the engine and the provider clients. Raises if a secret or provider is missing."""
    engine = create_engine(database_url_from_settings(settings))
    return Resources(
        engine=engine,
        session_factory=create_session_factory(engine),
        embedder=build_embedding_provider(settings),
        llm=build_llm_provider(settings),
    )


def get_resources(request: Request) -> Resources:
    resources = getattr(request.app.state, "resources", None)
    if not isinstance(resources, Resources):  # the app was run without its lifespan
        raise RuntimeError("application resources are not initialised")
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


async def get_session(resources: ResourcesDep) -> AsyncIterator[AsyncSession]:
    """One session per request. /api/query only reads, so nothing here commits."""
    async with resources.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_api_key_store(session: SessionDep) -> ApiKeyStore:
    return ApiKeyRepository(session)


# The header name is configurable, so the scheme is built from config. Declaring it as a security
# scheme (rather than reading the header by hand) is what documents it in OpenAPI and puts the
# authorise button in /docs.
_api_key_header = APIKeyHeader(
    name=get_settings().auth.api_key_header,
    auto_error=False,  # a missing key is reported in this API's own error shape
    description="A key issued with `python scripts/create_api_key.py --label ...`.",
)


async def require_api_key(
    presented: Annotated[str | None, Depends(_api_key_header)],
    keys: Annotated[ApiKeyStore, Depends(get_api_key_store)],
    settings: SettingsDep,
) -> ApiKey:
    """Authenticate the calling application. Runs before the request body is looked at."""
    if presented is None or not presented.strip():
        raise ApiError(
            401,
            ErrorCode.MISSING_API_KEY,
            f"the {settings.auth.api_key_header} header is required",
        )
    api_key = await ApiKeyService(keys).authenticate(presented)
    if api_key is None:
        # Unknown and revoked are one answer on purpose: the caller learns nothing either way.
        raise ApiError(401, ErrorCode.INVALID_API_KEY, "API key is not valid or has been revoked")
    return api_key


ApiKeyDep = Annotated[ApiKey, Depends(require_api_key)]


def get_query_service(
    session: SessionDep, settings: SettingsDep, resources: ResourcesDep
) -> QueryService:
    return build_query_service(settings, session, embedder=resources.embedder, llm=resources.llm)


QueryServiceDep = Annotated[QueryService, Depends(get_query_service)]
