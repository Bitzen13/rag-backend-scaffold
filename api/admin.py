"""Internal/admin routes: trigger ingestion, and manage the keys consumers authenticate with.

Not part of the public contract, and not part of the public OpenAPI document — every route here
is `include_in_schema=False`, so a consumer reading `/docs` never learns the surface exists.

Three things keep it out of reach:

1. It is **not mounted at all** unless `ADMIN_API_TOKEN` is set. An operations surface nobody
   configured should not be reachable, and a default-on admin path is how a deployment ends up
   with one it has forgotten about.
2. It requires its own token in its own header, compared in constant time. The consumer keys in
   `api_keys` cannot reach it: a key issued to a frontend must never be able to issue itself
   another one, or start an ingestion run.
3. It is expected to sit behind a network boundary as well (architecture-context.md §7). The
   token is the second lock, not the only one.

Ingestion is started here, not performed here: the route claims a job and answers 202, because a
run takes minutes and a request handler is the wrong place to spend them (code-standards.md).
"""

import hmac
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

from api.dependencies import ResourcesDep, SessionDep, SettingsDep
from api.errors import ApiError, ErrorCode
from db.models import ApiKey, IngestionJob
from db.repositories.api_keys import ApiKeyRepository
from db.repositories.jobs import IngestionJobRepository, JobAlreadyRunningError
from services.admin import JobAdminService, KeyAdminService
from services.jobs import IngestionJobService
from settings import Settings

ADMIN_TOKEN_HEADER = "X-Admin-Token"

_admin_token_header = APIKeyHeader(
    name=ADMIN_TOKEN_HEADER,
    auto_error=False,  # a missing token is reported in this API's own error shape
    description="The value of the ADMIN_API_TOKEN environment variable.",
)


def admin_routes_enabled(settings: Settings) -> bool:
    """Whether this deployment has an admin token, and so an admin surface at all."""
    token = settings.secrets.admin_api_token
    return token is not None and bool(token.get_secret_value())


async def require_admin_token(
    presented: Annotated[str | None, Depends(_admin_token_header)], settings: SettingsDep
) -> None:
    """Authorise an operator, in constant time and for the same reason the consumer check is."""
    if presented is None or not presented.strip():
        raise ApiError(
            401, ErrorCode.MISSING_ADMIN_TOKEN, f"the {ADMIN_TOKEN_HEADER} header is required"
        )
    token = settings.secrets.admin_api_token
    expected = "" if token is None else token.get_secret_value()
    if not expected or not hmac.compare_digest(presented, expected):
        raise ApiError(401, ErrorCode.INVALID_ADMIN_TOKEN, "admin token is not valid")


router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(require_admin_token)],
)


# --- ingestion ----------------------------------------------------------------------------------


class JobResponse(BaseModel):
    """An ingestion run. `report` stays empty until it finishes."""

    id: uuid.UUID
    state: str  # running | succeeded | failed | abandoned
    started_at: datetime
    finished_at: datetime | None
    detail: str
    report: dict[str, object]

    @classmethod
    def of(cls, job: IngestionJob) -> "JobResponse":
        return cls(
            id=job.id,
            state=job.state,
            started_at=job.started_at,
            finished_at=job.finished_at,
            detail=job.detail,
            report=job.report,
        )


def get_job_service(resources: ResourcesDep, settings: SettingsDep) -> IngestionJobService:
    """Starting a run needs the session *factory*: the work outlives this request's session."""
    return IngestionJobService(resources.session_factory, settings)


def get_job_admin(session: SessionDep) -> JobAdminService:
    return JobAdminService(IngestionJobRepository(session), session)


def get_key_admin(session: SessionDep) -> KeyAdminService:
    return KeyAdminService(ApiKeyRepository(session), session)


JobServiceDep = Annotated[IngestionJobService, Depends(get_job_service)]
JobAdminDep = Annotated[JobAdminService, Depends(get_job_admin)]
KeyAdminDep = Annotated[KeyAdminService, Depends(get_key_admin)]


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED, response_model=JobResponse)
async def start_ingestion(jobs: JobServiceDep) -> JobResponse:
    """Start a run. Re-syncing is the same operation: a run opens only what actually changed."""
    try:
        return JobResponse.of(await jobs.start())
    except JobAlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/ingest", response_model=list[JobResponse])
async def list_ingestion_jobs(jobs: JobAdminDep) -> list[JobResponse]:
    return [JobResponse.of(job) for job in await jobs.recent()]


@router.get("/ingest/{job_id}", response_model=JobResponse)
async def get_ingestion_job(job_id: uuid.UUID, jobs: JobAdminDep) -> JobResponse:
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no ingestion job {job_id}")
    return JobResponse.of(job)


@router.delete("/ingest/{job_id}", response_model=JobResponse)
async def abandon_ingestion_job(job_id: uuid.UUID, jobs: JobAdminDep) -> JobResponse:
    """Release a run whose worker died, so the next one can start.

    It cannot stop a run that is still going — nothing here can reach into another worker — so it
    is for a job known to be dead. Using it on a live run means two runs at once.
    """
    job = await jobs.abandon(job_id)
    if job is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no running ingestion job {job_id} to release"
        )
    return JobResponse.of(job)


# --- key management -----------------------------------------------------------------------------


class ApiKeyResponse(BaseModel):
    """A key's record. Never its plaintext: only a hash is stored, so there is none to return."""

    id: uuid.UUID
    label: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None

    @classmethod
    def of(cls, key: ApiKey) -> "ApiKeyResponse":
        return cls(
            id=key.id,
            label=key.label,
            is_active=key.is_active,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
        )


class IssueApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=200, examples=["my-frontend"])


class IssuedApiKeyResponse(ApiKeyResponse):
    """The one response carrying a plaintext key, because it is the only moment one exists."""

    key: str


@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(keys: KeyAdminDep) -> list[ApiKeyResponse]:
    return [ApiKeyResponse.of(key) for key in await keys.list()]


@router.post("/api-keys", status_code=status.HTTP_201_CREATED, response_model=IssuedApiKeyResponse)
async def issue_api_key(payload: IssueApiKeyRequest, keys: KeyAdminDep) -> IssuedApiKeyResponse:
    """Issue a key. The plaintext is in this response and nowhere else, ever again."""
    plaintext, row = await keys.issue(payload.label)
    return IssuedApiKeyResponse(**ApiKeyResponse.of(row).model_dump(), key=plaintext)


@router.delete("/api-keys/{api_key_id}", response_model=ApiKeyResponse)
async def revoke_api_key(api_key_id: uuid.UUID, keys: KeyAdminDep) -> ApiKeyResponse:
    """Revoke a key. The row stays, so a key that was used can still be accounted for."""
    key = await keys.revoke(api_key_id)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no API key {api_key_id}")
    return ApiKeyResponse.of(key)
