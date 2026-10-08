"""Authentication, authorization, and CSRF tests."""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from quotehub import create_app
from quotehub.models import Base, ProviderProfile, User, UserRole


@pytest.fixture
def app():
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": "test-only-session-key",
            "SESSION_COOKIE_SECURE": False,
        }
    )
    engine = app.extensions["db_engine"]
    Base.metadata.create_all(engine)
    yield app
    engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def csrf_token(client, path):
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def post_form(client, path, data):
    return client.post(path, data={**data, "csrf_token": csrf_token(client, path)})


def register(client, email="person@example.com", role="CUSTOMER"):
    return post_form(
        client,
        "/register",
        {
            "name": "Test Person",
            "email": email,
            "password": "strong-password-123",
            "role": role,
        },
    )


def add_user(app, email="person@example.com", role=UserRole.CUSTOMER):
    with Session(app.extensions["db_engine"]) as db:
        user = User(
            name="Test Person",
            email=email,
            password_hash=generate_password_hash("strong-password-123"),
            role=role,
        )
        db.add(user)
        db.commit()
        return user.id


def login(client, email="person@example.com", password="strong-password-123"):
    return post_form(client, "/login", {"email": email, "password": password})


def test_registration_stores_hash_and_signs_in_customer(app, client):
    response = register(client, "  Person@Example.COM  ")
    assert response.status_code == 302
    assert response.location.endswith("/dashboard")
    assert client.get("/dashboard").status_code == 200

    with Session(app.extensions["db_engine"]) as db:
        user = db.scalar(select(User).where(User.email == "person@example.com"))
        assert user is not None
        assert user.role == UserRole.CUSTOMER
        assert user.password_hash != "strong-password-123"
        assert check_password_hash(user.password_hash, "strong-password-123")


def test_provider_registration_does_not_create_incomplete_profile(app, client):
    assert register(client, role="PROVIDER").status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        user = db.scalar(select(User).where(User.email == "person@example.com"))
        assert user.role == UserRole.PROVIDER
        assert db.scalar(select(ProviderProfile)) is None
    assert client.get("/provider").status_code == 200


def test_duplicate_email_is_rejected(app, client):
    add_user(app)
    response = register(client, "  PERSON@example.com  ")
    assert response.status_code == 400
    assert b"already exists" in response.data
    with Session(app.extensions["db_engine"]) as db:
        assert len(db.scalars(select(User)).all()) == 1


def test_admin_registration_is_rejected(app, client):
    response = register(client, role="ADMIN")
    assert response.status_code == 400
    assert b"Choose Customer or Provider" in response.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(User)) is None


def test_registration_requires_fields(client):
    response = post_form(
        client,
        "/register",
        {"name": "", "email": "person@example.com", "password": "abc", "role": "CUSTOMER"},
    )
    assert response.status_code == 400
    assert b"Complete all required fields" in response.data


def test_login_accepts_correct_credentials_and_normalized_email(app, client):
    add_user(app)
    response = login(client, " PERSON@EXAMPLE.COM ")
    assert response.status_code == 302
    assert response.location.endswith("/dashboard")
    assert b"Welcome, Test Person" in client.get("/dashboard").data


def test_login_rejects_wrong_password_and_unknown_email_equally(app, client):
    add_user(app)
    wrong_password = login(client, password="wrong-password")
    unknown_email = login(client, email="missing@example.com")
    assert wrong_password.status_code == unknown_email.status_code == 400
    assert b"Invalid email or password" in wrong_password.data
    assert b"Invalid email or password" in unknown_email.data
    assert client.get("/dashboard").status_code == 302


def test_login_ignores_external_next_url(app, client):
    add_user(app)
    response = post_form(
        client,
        "/login?next=https://example.invalid/elsewhere",
        {"email": "person@example.com", "password": "strong-password-123"},
    )
    assert response.status_code == 302
    assert response.location.endswith("/dashboard")


def test_logout_is_post_only_and_ends_session(app, client):
    add_user(app)
    assert login(client).status_code == 302
    assert client.get("/logout").status_code == 405
    token = csrf_token(client, "/dashboard")
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 302
    assert client.get("/dashboard").status_code == 302


def test_dashboard_requires_login(client):
    response = client.get("/dashboard")
    assert response.status_code == 302
    assert "/login" in response.location


@pytest.mark.parametrize(
    ("role", "allowed", "denied"),
    [
        (UserRole.CUSTOMER, "/customer", ("/provider", "/admin")),
        (UserRole.PROVIDER, "/provider", ("/customer", "/admin")),
        (UserRole.ADMIN, "/admin", ("/customer", "/provider")),
    ],
)
def test_role_protected_routes(app, client, role, allowed, denied):
    add_user(app, role=role)
    assert login(client).status_code == 302
    assert client.get(allowed).status_code == 200
    for path in denied:
        assert client.get(path).status_code == 403


def test_csrf_blocks_registration_login_and_logout_without_token(app, client):
    assert client.post(
        "/register",
        data={"name": "Test", "email": "person@example.com", "password": "password123", "role": "CUSTOMER"},
    ).status_code == 400
    add_user(app)
    assert client.post(
        "/login", data={"email": "person@example.com", "password": "strong-password-123"}
    ).status_code == 400
    assert login(client).status_code == 302
    assert client.post("/logout").status_code == 400
    assert client.get("/dashboard").status_code == 200


def test_session_cookie_flags_follow_environment(client):
    local_cookie = client.get("/register").headers["Set-Cookie"]
    assert "HttpOnly" in local_cookie
    assert "SameSite=Lax" in local_cookie
    assert "Secure" not in local_cookie

    secure_app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": "test-only-session-key",
            "SESSION_COOKIE_SECURE": True,
        }
    )
    secure_cookie = secure_app.test_client().get("/register").headers["Set-Cookie"]
    assert "Secure" in secure_cookie
    secure_app.extensions["db_engine"].dispose()
