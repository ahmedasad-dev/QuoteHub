"""Browser headers and request-body limits."""

import re

import pytest

from quotehub import create_app
from quotehub.models import Base


def app_with(**overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "SECRET_KEY": "local-security-test-key",
        "QUOTEHUB_ENV": "development",
        "TRUSTED_PROXY_HOPS": "0",
        "MAX_CONTENT_LENGTH": "131072",
        "HSTS_MAX_AGE": "0",
    }
    config.update(overrides)
    if config["QUOTEHUB_ENV"] == "production":
        config.setdefault("RATELIMIT_STORAGE_URI", "redis://localhost:6379/0")
    app = create_app(config)
    Base.metadata.create_all(app.extensions["db_engine"])
    return app


def test_headers_cover_pages_static_assets_and_errors():
    app = app_with()
    client = app.test_client()
    for path, expected_status in (
        ("/", 200),
        ("/health", 200),
        ("/static/style.css", 200),
        ("/missing", 404),
    ):
        response = client.get(path)
        assert response.status_code == expected_status
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "camera=()" in response.headers["Permissions-Policy"]
        csp = response.headers["Content-Security-Policy"]
        assert "script-src 'none'" in csp
        assert "style-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert "Strict-Transport-Security" not in response.headers
    assert client.post("/logout").status_code == 400  # CSRF error
    assert client.post("/logout").headers["X-Content-Type-Options"] == "nosniff"
    app.extensions["db_engine"].dispose()


def test_oversized_form_is_413_and_small_form_reaches_authentication():
    app = app_with(MAX_CONTENT_LENGTH=1024)
    client = app.test_client()
    page = client.get("/login")
    token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
    too_large = client.post("/login", data={
        "csrf_token": token,
        "email": "person@example.com",
        "password": "x" * 2000,
    })
    assert too_large.status_code == 413
    assert b"Your submission is too large" in too_large.data
    assert b"Traceback" not in too_large.data
    assert too_large.headers["Content-Security-Policy"]
    normal = client.post("/login", data={
        "csrf_token": token,
        "email": "person@example.com",
        "password": "wrong",
    })
    assert normal.status_code == 400
    assert b"Invalid email or password" in normal.data
    app.extensions["db_engine"].dispose()


def test_hsts_is_opt_in_and_requires_trusted_https_proxy():
    production = {
        "QUOTEHUB_ENV": "production", "SECRET_KEY": "s" * 32,
        "TRUSTED_PROXY_HOPS": "1", "HSTS_MAX_AGE": "86400",
    }
    app = app_with(**production)
    client = app.test_client()
    assert "Strict-Transport-Security" not in client.get("/").headers
    response = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert response.headers["Strict-Transport-Security"] == "max-age=86400"
    assert "Strict-Transport-Security" not in client.get(
        "/", headers={"X-Forwarded-Proto": "http"}
    ).headers
    app.extensions["db_engine"].dispose()

    without_trust = app_with(QUOTEHUB_ENV="production", SECRET_KEY="s" * 32)
    assert "Strict-Transport-Security" not in without_trust.test_client().get(
        "/", headers={"X-Forwarded-Proto": "https"}
    ).headers
    without_trust.extensions["db_engine"].dispose()


@pytest.mark.parametrize("setting", [0, 1023, 10485761, "9" * 1000, "bad", True])
def test_invalid_body_limit_rejected(setting):
    with pytest.raises(RuntimeError, match="MAX_CONTENT_LENGTH"):
        app_with(MAX_CONTENT_LENGTH=setting)


@pytest.mark.parametrize("setting", [-1, 31536001, "bad", True])
def test_invalid_hsts_age_rejected(setting):
    with pytest.raises(RuntimeError, match="HSTS_MAX_AGE"):
        app_with(HSTS_MAX_AGE=setting)


def test_hsts_cannot_be_enabled_for_development_or_untrusted_proxy():
    with pytest.raises(RuntimeError, match="HSTS_MAX_AGE"):
        app_with(HSTS_MAX_AGE="86400")
    with pytest.raises(RuntimeError, match="HSTS_MAX_AGE"):
        app_with(QUOTEHUB_ENV="production", SECRET_KEY="s" * 32,
                 HSTS_MAX_AGE="86400", TRUSTED_PROXY_HOPS="0")
