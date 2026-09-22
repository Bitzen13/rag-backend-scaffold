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
from db.repositories.query_logs import QueryLogRepository
from db.repositories.rate_limits import RateLimitRepository
from db.session import create_engine, create_session_factory, database_url_from_settings
from embedding.base import EmbeddingProvider
from embedding.factory import build_embedding_provider
from generation.base import LLMProvider
from generation.factory import build_llm_provider
from services.api_keys import ApiKeyService, ApiKeyStore
from services.factory import build_query_service
from services.query_service import QueryService
from services.rate_limit import RateLimitDecision, RateLimiter
from services.usage import QueryLogger, UsageRecorder
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
    engine = create_engine(database_url_from_settings(settings), settings.database)
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
    """One session per request.

    Retrieval only reads. The per-request bookkeeping (`last_used_at`, the rate-limit count, the
    query log) does write, and commits explicitly at the point it is done — see services/usage.py
    for why those commits are where they are.
    """
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


def get_usage_recorder(session: SessionDep, settings: SettingsDep) -> UsageRecorder:
    return UsageRecorder(
        ApiKeyRepository(session),
        RateLimiter(RateLimitRepository(session), settings.api.rate_limit),
        session,
    )


def get_query_logger(session: SessionDep, settings: SettingsDep) -> QueryLogger:
    return QueryLogger(QueryLogRepository(session), session, enabled=settings.api.log_queries)


QueryLoggerDep = Annotated[QueryLogger, Depends(get_query_logger)]


@dataclass(frozen=True)
class AuthorizedCaller:
    """An authenticated key that is inside its allowance, and the numbers proving it."""

    api_key: ApiKey
    rate_limit: RateLimitDecision


async def authorize(
    request: Request,
    api_key: ApiKeyDep,
    usage: Annotated[UsageRecorder, Depends(get_usage_recorder)],
) -> AuthorizedCaller:
    """Authenticate, record the use, and enforce the key's rate limit.

    A dependency rather than middleware, so it still resolves before the request body is
    validated: an over-limit caller is turned away before anything is parsed, embedded or sent to
    a provider, which is the whole point of limiting.
    """
    # The request log line names the key by id, never by value (see api/middleware.py).
    request.state.api_key_id = api_key.id
    decision = await usage.record_request(api_key.id)
    # Applied by the middleware, so they survive a response built by an exception handler.
    request.state.rate_limit_headers = decision.headers()
    if not decision.allowed:
        raise ApiError(
            429,
            ErrorCode.RATE_LIMIT_EXCEEDED,
            f"rate limit of {decision.limit} requests per window exceeded; retry in "
            f"{decision.retry_after_seconds}s",
            decision.headers(),
        )
    return AuthorizedCaller(api_key, decision)


AuthorizedDep = Annotated[AuthorizedCaller, Depends(authorize)]
