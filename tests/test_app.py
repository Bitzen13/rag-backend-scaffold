"""How the app is assembled: what is mounted, what is not, and what stops start-up.

Each of these is a deployment-shaped decision rather than a request-shaped one, which is why
they are asserted against `create_app` and not through a route.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import SecretStr

import customisations
import main
from customisations import Customisation, CustomisationError
from settings import Settings, load_settings

ORIGIN = "https://app.example.com"


def settings_with(monkeypatch: pytest.MonkeyPatch, **updates: Any) -> Settings:
    settings = load_settings(env_file=None).model_copy(update=updates)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return settings


def with_cors(monkeypatch: pytest.MonkeyPatch, **cors: Any) -> FastAPI:
    base = load_settings(env_file=None)
    api = base.api.model_copy(update={"cors": base.api.cors.model_copy(update=cors)})
    settings_with(monkeypatch, api=api)
    return main.create_app()


def middleware_names(app: FastAPI) -> list[str]:
    return [getattr(m.cls, "__name__", str(m.cls)) for m in app.user_middleware]


# --- CORS ----------------------------------------------------------------------------------------


def test_cors_is_not_installed_when_no_origin_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default. A consumer frontend calls from its own server, where CORS does not apply."""
    app = with_cors(monkeypatch)

    assert CORSMiddleware.__name__ not in middleware_names(app)


def test_cors_is_installed_for_a_named_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    app = with_cors(monkeypatch, allowed_origins=(ORIGIN,))

    assert CORSMiddleware.__name__ in middleware_names(app)


def test_a_named_origin_gets_the_header_and_others_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(with_cors(monkeypatch, allowed_origins=(ORIGIN,)))

    allowed = client.get("/health", headers={"Origin": ORIGIN})
    refused = client.get("/health", headers={"Origin": "https://evil.example.com"})

    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-origin" not in refused.headers


def test_the_preflight_allows_the_api_key_header_and_nothing_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(env_file=None)
    client = TestClient(with_cors(monkeypatch, allowed_origins=(ORIGIN,)))

    response = client.options(
        "/api/query",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": settings.auth.api_key_header,
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert settings.auth.api_key_header.lower() in allowed
    methods = response.headers["access-control-allow-methods"]
    assert set(methods.replace(" ", "").split(",")) == {"GET", "POST"}


def test_credentials_are_not_allowed_unless_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here uses cookies; an API key travels in a header."""
    client = TestClient(with_cors(monkeypatch, allowed_origins=(ORIGIN,)))

    response = client.get("/health", headers={"Origin": ORIGIN})

    assert "access-control-allow-credentials" not in response.headers


# --- the customisation is loaded at start-up ------------------------------------------------------


def test_a_customisation_that_does_not_resolve_stops_start_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rather than starting without a client's reader and quietly using the wrong one."""
    settings_with(monkeypatch, customisation="no_such_client")

    with pytest.raises(CustomisationError):
        main.create_app()


def test_a_loaded_customisation_is_reported_at_start_up(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A deployment should say out loud what it is running with."""
    settings_with(monkeypatch, customisation="acme")
    monkeypatch.setattr(customisations, "load", lambda name: Customisation())

    with caplog.at_level("INFO", logger="main"):
        main.create_app()

    assert "customisation 'acme' loaded" in caplog.text


def test_no_customisation_is_the_default_and_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings_with(monkeypatch)

    with caplog.at_level("INFO", logger="main"):
        main.create_app()

    assert "customisation" not in caplog.text


# --- the admin surface is off unless configured ---------------------------------------------------


def test_the_admin_router_is_absent_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    base = load_settings(env_file=None)
    settings_with(monkeypatch, secrets=base.secrets.model_copy(update={"admin_api_token": None}))

    client = TestClient(main.create_app())

    assert client.get("/internal/api-keys").status_code == 404


def test_the_admin_router_is_present_with_one(monkeypatch: pytest.MonkeyPatch) -> None:
    base = load_settings(env_file=None)
    secrets = base.secrets.model_copy(update={"admin_api_token": SecretStr("t")})
    settings_with(monkeypatch, secrets=secrets)

    client = TestClient(main.create_app())

    assert client.get("/internal/api-keys").status_code == 401  # there, and locked
