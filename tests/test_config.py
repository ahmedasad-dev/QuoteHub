"""Configuration boundaries for local development and HTTPS proxy deployments."""

import pytest
from flask import request
from werkzeug.middleware.proxy_fix import ProxyFix

from quotehub import create_app


def settings(**overrides):
    values = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "SECRET_KEY": "development-test-key",
        "QUOTEHUB_ENV": "development",
        "DEBUG": False,
        "SESSION_COOKIE_SECURE": "",
        "TRUSTED_PROXY_HOPS": "0",
    }
    values.update(overrides)
    return values


def make_app(**overrides):
    return create_app(settings(**overrides))


def test_local_http_development_uses_untrusted_proxy_and_nonsecure_cookie():
    app = make_app()
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert not isinstance(app.wsgi_app, ProxyFix)
    response = app.test_client().get("/register")
    assert response.status_code == 200
    cookie = response.headers["Set-Cookie"]
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    app.extensions["db_engine"].dispose()


def test_production_defaults_to_secure_cookie_with_explicit_secret():
    app = make_app(QUOTEHUB_ENV="production", SECRET_KEY="a" * 32)
    assert app.config["DEBUG"] is False
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert "Secure" in app.test_client().get("/register").headers["Set-Cookie"]
    app.extensions["db_engine"].dispose()


@pytest.mark.parametrize("secret", [None, "", "short", " " * 32])
def test_production_rejects_missing_or_invalid_secret(secret):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        make_app(QUOTEHUB_ENV="production", SECRET_KEY=secret)


@pytest.mark.parametrize("overrides, expected", [
    ({"QUOTEHUB_ENV": "staging"}, "QUOTEHUB_ENV"),
    ({"QUOTEHUB_ENV": "production", "SECRET_KEY": "a" * 32, "DEBUG": True}, "FLASK_DEBUG"),
    ({"QUOTEHUB_ENV": "production", "SECRET_KEY": "a" * 32, "SESSION_COOKIE_SECURE": "0"}, "SESSION_COOKIE_SECURE"),
    ({"QUOTEHUB_ENV": "production", "SECRET_KEY": "a" * 32, "SESSION_COOKIE_HTTPONLY": False}, "SESSION_COOKIE_HTTPONLY"),
    ({"QUOTEHUB_ENV": "production", "SECRET_KEY": "a" * 32, "SESSION_COOKIE_SAMESITE": None}, "SESSION_COOKIE_SAMESITE"),
    ({"DEBUG": "maybe"}, "FLASK_DEBUG"),
    ({"SESSION_COOKIE_SECURE": "maybe"}, "SESSION_COOKIE_SECURE"),
])
def test_invalid_security_settings_fail_clearly(overrides, expected):
    with pytest.raises(RuntimeError, match=expected):
        make_app(**overrides)


@pytest.mark.parametrize("hops", ["-1", "one", "11", "", True, 999])
def test_invalid_proxy_hop_count_is_rejected(hops):
    with pytest.raises(RuntimeError, match="TRUSTED_PROXY_HOPS"):
        make_app(TRUSTED_PROXY_HOPS=hops)


def test_proxy_headers_ignored_by_default_and_limited_when_enabled():
    forwarded = {
        "X-Forwarded-For": "203.0.113.7",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "untrusted.example",
    }
    for hops, expected_scheme, expected_ip in (
        ("0", "http", "127.0.0.1"),
        ("1", "https", "203.0.113.7"),
    ):
        app = make_app(TRUSTED_PROXY_HOPS=hops)
        observed = {}

        @app.before_request
        def capture_request():
            observed.update(scheme=request.scheme, ip=request.remote_addr, host=request.host)

        response = app.test_client().get("/", headers=forwarded)
        assert response.status_code == 200
        assert observed == {
            "scheme": expected_scheme,
            "ip": expected_ip,
            "host": "localhost",
        }
        assert isinstance(app.wsgi_app, ProxyFix) is (hops == "1")
        app.extensions["db_engine"].dispose()
