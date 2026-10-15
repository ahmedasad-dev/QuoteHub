"""Deterministic abuse protection tests for public authentication forms."""

import re

import pytest
from limits.errors import StorageError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from quotehub import create_app
from quotehub.limiting import limiter
from quotehub.models import Base, User, UserRole


@pytest.fixture
def app():
    application = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "SECRET_KEY": "rate-limit-test-secret",
        "QUOTEHUB_ENV": "development",
        "AUTH_LOGIN_RATE_LIMIT": "2 per minute",
        "AUTH_REGISTER_RATE_LIMIT": "2 per minute",
        "RATELIMIT_STORAGE_URI": "memory://",
    })
    Base.metadata.create_all(application.extensions["db_engine"])
    yield application
    application.extensions["db_engine"].dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client, path):
    response = client.get(path)
    assert response.status_code == 200
    return re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()


def submit(client, path, fields, *, headers=None):
    return client.post(path, data={**fields, "csrf_token": csrf(client, path)}, headers=headers)


def login_fields(email="unknown@example.com", password="incorrect-password"):
    return {"email": email, "password": password}


def registration_fields(email):
    return {
        "name": "Test Person", "email": email, "password": "password-123", "role": "CUSTOMER",
    }


def test_login_limit_is_post_only_and_email_independent(app, client):
    with Session(app.extensions["db_engine"]) as db:
        db.add(User(name="Known", email="known@example.com",
                    password_hash=generate_password_hash("password-123"), role=UserRole.CUSTOMER))
        db.commit()
    assert submit(client, "/login", login_fields("known@example.com")).status_code == 400
    assert submit(client, "/login", login_fields()).status_code == 400
    for _ in range(3):
        assert client.get("/login").status_code == 200
    limited = submit(client, "/login", login_fields("known@example.com", "password-123"))
    assert limited.status_code == 429
    assert b"Too many attempts. Please try again later." in limited.data
    assert b"known@example.com" in limited.data
    assert limited.headers.get("Retry-After") is not None
    assert client.get("/dashboard").status_code == 302


def test_registration_limit_is_shared_by_sessions_at_one_ip(client):
    assert submit(client, "/register", registration_fields("first@example.com")).status_code == 302
    # A second client session from the same IP shares the same limiter bucket.
    other_session = client.application.test_client()
    assert submit(other_session, "/register", registration_fields("second@example.com")).status_code == 302
    third_session = client.application.test_client()
    limited = submit(third_session, "/register", registration_fields("third@example.com"))
    assert limited.status_code == 429
    assert b"Too many attempts. Please try again later." in limited.data
    assert third_session.get("/register").status_code == 200


def test_limit_recovers_after_window_without_sleep(client, monkeypatch):
    clock = [1_700_000_000.0]
    monkeypatch.setattr("limits.storage.memory.time.time", lambda: clock[0])
    for _ in range(2):
        assert submit(client, "/login", login_fields()).status_code == 400
    assert submit(client, "/login", login_fields()).status_code == 429
    clock[0] += 61
    assert submit(client, "/login", login_fields()).status_code == 400


def test_spoofed_forwarded_headers_do_not_change_default_ip_bucket(client):
    for address in ("198.51.100.1", "198.51.100.2"):
        assert submit(client, "/login", login_fields(), headers={"X-Forwarded-For": address}).status_code == 400
    assert submit(client, "/login", login_fields(), headers={
        "X-Forwarded-For": "198.51.100.3"
    }).status_code == 429


def test_explicit_proxy_hop_count_uses_forwarded_client_ip():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "SECRET_KEY": "proxy-rate-limit-test-key",
        "QUOTEHUB_ENV": "development",
        "TRUSTED_PROXY_HOPS": "1",
        "AUTH_LOGIN_RATE_LIMIT": "2 per minute",
        "RATELIMIT_STORAGE_URI": "memory://",
    })
    Base.metadata.create_all(app.extensions["db_engine"])
    client = app.test_client()
    first = {"X-Forwarded-For": "198.51.100.1"}
    second = {"X-Forwarded-For": "198.51.100.2"}
    assert submit(client, "/login", login_fields(), headers=first).status_code == 400
    assert submit(client, "/login", login_fields(), headers=first).status_code == 400
    assert submit(client, "/login", login_fields(), headers=first).status_code == 429
    assert submit(client, "/login", login_fields(), headers=second).status_code == 400
    app.extensions["db_engine"].dispose()


def test_storage_failure_fails_closed_and_is_logged(app, client, monkeypatch, caplog):
    def unavailable(*_args, **_kwargs):
        raise StorageError("simulated outage")

    with app.app_context():
        monkeypatch.setattr(limiter.storage, "incr", unavailable)
    response = submit(client, "/login", login_fields())
    assert response.status_code == 503
    assert b"temporarily unavailable" in response.data
    assert "Rate-limit storage unavailable" in caplog.text


@pytest.mark.parametrize("setting", ["", "nonsense", "0 per minute"])
def test_invalid_limit_config_fails_at_startup(setting):
    with pytest.raises(RuntimeError, match="AUTH_LOGIN_RATE_LIMIT"):
        create_app({
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": "test-key",
            "QUOTEHUB_ENV": "development",
            "AUTH_LOGIN_RATE_LIMIT": setting,
        })


@pytest.mark.parametrize("storage_uri", ["memory://", "redis://", "redis://host:bad"])
def test_production_requires_shared_storage(storage_uri):
    with pytest.raises(RuntimeError, match="RATELIMIT_STORAGE_URI"):
        create_app({
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": "a" * 32,
            "QUOTEHUB_ENV": "production",
            "RATELIMIT_STORAGE_URI": storage_uri,
        })
