"""The internal admin surface: that it is closed by default, and locked when it is open.

The first two sections matter most. An admin path that is mounted when nobody asked for one, or
that a consumer's own API key can reach, is a worse outcome than not having one at all — so they
are tested before anything the surface can actually do.

No database and no real ingestion: the stores are in-memory (tests/fakes.py, and the key store
from tests/test_api_keys.py), and the job repository is covered against a real Postgres in
tests/test_jobs.py.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.api.dependencies as dependencies
from app import main
from app.api.admin import (
    ADMIN_TOKEN_HEADER,
    admin_routes_enabled,
    get_job_admin,
    get_job_service,
    get_key_admin,
)
from app.db.models import IngestionJob, JobState
from app.db.repositories.jobs import JobAlreadyRunningError
from app.services.admin import JobAdminService, KeyAdminService
from app.settings import Settings, get_settings
from tests.fakes import FakeSession
from tests.test_api_keys import FakeApiKeyStore, add_key

ADMIN_TOKEN = "admin-token-for-tests-only"
CONSUMER_HEADER = get_settings().auth.api_key_header


def settings_with_admin_token(token: str | None) -> Settings:
    base = get_settings()
    secrets = base.secrets.model_copy(
        update={"admin_api_token": SecretStr(token) if token is not None else None}
    )
    return base.model_copy(update={"secrets": secrets})


# --- fakes for the stores the admin routes reach ---------------------------------------------


def a_job(state: JobState = JobState.RUNNING) -> IngestionJob:
    return IngestionJob(
        id=uuid.uuid4(), state=state.value, started_at=datetime.now(UTC), detail="", report={}
    )


class FakeJobStore:
    def __init__(self) -> None:
        self.jobs: list[IngestionJob] = []

    async def get(self, job_id: uuid.UUID) -> IngestionJob | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    async def recent(self, limit: int = 20) -> list[IngestionJob]:
        return self.jobs[:limit]

    async def abandon(self, job_id: uuid.UUID) -> bool:
        job = await self.get(job_id)
        if job is None or job.state != JobState.RUNNING.value:
            return False
        job.state = JobState.ABANDONED.value
        return True


class FakeJobService:
    """Stands in for IngestionJobService: claims the slot, or reports it taken."""

    def __init__(self, store: FakeJobStore, error: Exception | None = None) -> None:
        self.store = store
        self.error = error
        self.starts = 0

    async def start(self) -> IngestionJob:
        self.starts += 1
        if self.error is not None:
            raise self.error
        job = a_job()
        self.store.jobs.append(job)
        return job


@dataclass
class Admin:
    client: TestClient
    jobs: FakeJobService
    job_store: FakeJobStore
    keys: FakeApiKeyStore
    consumer_key: str

    def request(
        self, method: str, path: str, *, token: str | None = ADMIN_TOKEN, **kwargs: Any
    ) -> httpx.Response:
        headers = {} if token is None else {ADMIN_TOKEN_HEADER: token}
        return cast(httpx.Response, self.client.request(method, path, headers=headers, **kwargs))


def create_app_with(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """`create_app` decides whether to mount the router, so the settings must be in place first."""
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return main.create_app()


@contextmanager
def admin(
    *, token: str | None = ADMIN_TOKEN, start_error: Exception | None = None
) -> Iterator[Admin]:
    """An app built as this deployment's settings say it should be, with its stores faked."""
    settings = settings_with_admin_token(token)
    keys = FakeApiKeyStore()
    consumer = add_key(keys, label="a-frontend")
    job_store = FakeJobStore()
    jobs = FakeJobService(job_store, start_error)

    patch = pytest.MonkeyPatch()
    app = create_app_with(settings, patch)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[dependencies.get_api_key_store] = lambda: keys
    app.dependency_overrides[get_job_service] = lambda: jobs
    app.dependency_overrides[get_job_admin] = lambda: JobAdminService(job_store, FakeSession())
    app.dependency_overrides[get_key_admin] = lambda: KeyAdminService(keys, FakeSession())
    try:
        yield Admin(TestClient(app), jobs, job_store, keys, consumer)
    finally:
        app.dependency_overrides.clear()
        patch.undo()


# --- closed unless it was asked for ----------------------------------------------------------


def test_the_admin_surface_does_not_exist_without_a_token_configured() -> None:
    """Not 401: not there at all. A deployment that configured nothing has nothing to attack."""
    with admin(token=None) as ready:
        for method, path in [
            ("POST", "/internal/ingest"),
            ("GET", "/internal/api-keys"),
            ("POST", "/internal/api-keys"),
        ]:
            response = ready.request(method, path, token=None, json={"label": "x"})

            assert response.status_code == 404
            assert response.json()["code"] == "not_found"


def test_an_empty_token_counts_as_no_token() -> None:
    assert admin_routes_enabled(settings_with_admin_token("")) is False
    assert admin_routes_enabled(settings_with_admin_token(None)) is False
    assert admin_routes_enabled(settings_with_admin_token(ADMIN_TOKEN)) is True


# --- locked when it is open ------------------------------------------------------------------


def test_a_missing_admin_token_is_401_in_the_standard_shape() -> None:
    with admin() as ready:
        response = ready.request("GET", "/internal/api-keys", token=None)

    assert response.status_code == 401
    assert response.json() == {
        "error": f"the {ADMIN_TOKEN_HEADER} header is required",
        "code": "missing_admin_token",
    }


@pytest.mark.parametrize("token", ["", "   ", "wrong-token", ADMIN_TOKEN + "x"])
def test_a_wrong_admin_token_is_401(token: str) -> None:
    with admin() as ready:
        response = ready.request("GET", "/internal/api-keys", token=token)

    assert response.status_code == 401
    assert response.json()["code"] in {"missing_admin_token", "invalid_admin_token"}


def test_a_non_ascii_admin_token_is_401_not_500() -> None:
    """`compare_digest` raises on a non-ASCII str; comparing bytes keeps it a plain refusal."""
    with admin() as ready:
        response = ready.client.get(
            "/internal/api-keys", headers={ADMIN_TOKEN_HEADER: "tökén-ü".encode()}
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_admin_token"


def test_a_consumer_api_key_cannot_reach_the_admin_surface() -> None:
    """The key a frontend holds must never be able to issue itself another one."""
    with admin() as ready:
        response = ready.client.post(
            "/internal/api-keys",
            headers={CONSUMER_HEADER: ready.consumer_key},
            json={"label": "sneaky"},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "missing_admin_token"


def test_authorisation_runs_before_the_body_is_validated() -> None:
    with admin() as ready:
        response = ready.request("POST", "/internal/api-keys", token=None, json={"bad": "body"})

        assert response.status_code == 401
        assert len(ready.keys.rows) == 1  # only the consumer key; nothing was issued


def test_authorisation_runs_before_a_run_can_be_started() -> None:
    with admin() as ready:
        ready.request("POST", "/internal/ingest", token=None)
        ready.request("POST", "/internal/ingest", token="wrong")

        assert ready.jobs.starts == 0


def test_the_admin_surface_is_absent_from_the_public_openapi_document() -> None:
    """A consumer reading /docs must not learn that an operations path exists."""
    with admin() as ready:
        schema = ready.client.get("/openapi.json").json()

    assert not [path for path in schema["paths"] if path.startswith("/internal")]


# --- triggering ingestion --------------------------------------------------------------------


def test_starting_a_run_answers_202_rather_than_holding_the_request_open() -> None:
    with admin() as ready:
        response = ready.request("POST", "/internal/ingest")

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "running"
    assert uuid.UUID(body["id"])
    assert body["finished_at"] is None


def test_a_run_requested_while_one_is_in_flight_is_a_409() -> None:
    taken = JobAlreadyRunningError("an ingestion run is already in progress")
    with admin(start_error=taken) as ready:
        response = ready.request("POST", "/internal/ingest")

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert "already in progress" in response.json()["error"]


def test_a_started_run_can_be_polled_for_its_outcome() -> None:
    with admin() as ready:
        started = ready.request("POST", "/internal/ingest").json()

        polled = ready.request("GET", f"/internal/ingest/{started['id']}")

    assert polled.status_code == 200
    assert polled.json()["id"] == started["id"]


def test_recent_runs_are_listed() -> None:
    with admin() as ready:
        ready.request("POST", "/internal/ingest")

        response = ready.request("GET", "/internal/ingest")

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_an_unknown_job_is_404_in_the_standard_shape() -> None:
    with admin() as ready:
        response = ready.request("GET", f"/internal/ingest/{uuid.uuid4()}")

    assert response.status_code == 404
    assert set(response.json()) == {"error", "code"}


def test_a_malformed_job_id_is_400_not_500() -> None:
    with admin() as ready:
        response = ready.request("GET", "/internal/ingest/not-a-uuid")

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_releasing_a_dead_run_marks_it_abandoned() -> None:
    with admin() as ready:
        started = ready.request("POST", "/internal/ingest").json()

        response = ready.request("DELETE", f"/internal/ingest/{started['id']}")

    assert response.status_code == 200
    assert response.json()["state"] == "abandoned"


def test_releasing_a_run_that_is_not_running_is_404() -> None:
    with admin() as ready:
        finished = a_job(JobState.SUCCEEDED)
        ready.job_store.jobs.append(finished)

        response = ready.request("DELETE", f"/internal/ingest/{finished.id}")

    assert response.status_code == 404


# --- key management ----------------------------------------------------------------------------


def test_issuing_a_key_returns_its_plaintext_exactly_once() -> None:
    with admin() as ready:
        response = ready.request("POST", "/internal/api-keys", json={"label": "slack bot"})

        assert response.status_code == 201
        body = response.json()
        assert body["key"].startswith("rag_")
        assert body["label"] == "slack bot"
        assert body["is_active"] is True

        listed = ready.request("GET", "/internal/api-keys").json()

    issued = next(k for k in listed if k["id"] == body["id"])
    assert "key" not in issued  # the plaintext is never returned again


def test_listing_keys_never_exposes_a_hash_or_a_plaintext() -> None:
    with admin() as ready:
        keys = ready.request("GET", "/internal/api-keys").json()

    assert keys
    for key in keys:
        assert set(key) == {"id", "label", "is_active", "created_at", "last_used_at"}


@pytest.mark.parametrize("body", [{}, {"label": ""}, {"label": "x", "extra": 1}, {"label": 7}])
def test_a_malformed_issue_request_is_400(body: dict[str, Any]) -> None:
    with admin() as ready:
        response = ready.request("POST", "/internal/api-keys", json=body)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_revoking_an_unknown_key_is_404() -> None:
    with admin() as ready:
        response = ready.request("DELETE", f"/internal/api-keys/{uuid.uuid4()}")

    assert response.status_code == 404


def test_revoking_a_key_deactivates_its_row_rather_than_deleting_it() -> None:
    """Revocation takes effect on the next request, and the row stays for accounting."""
    with admin() as ready:
        issued = ready.request("POST", "/internal/api-keys", json={"label": "doomed"}).json()
        row = next(k for k in ready.keys.rows.values() if k.label == "doomed")

        revoked = ready.request("DELETE", f"/internal/api-keys/{issued['id']}")

        assert revoked.status_code == 200
        assert revoked.json()["is_active"] is False
        assert row.is_active is False
        assert row in ready.keys.rows.values()
