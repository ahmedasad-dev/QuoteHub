"""Tests for interactive administrator creation."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from quotehub import create_app
from quotehub.models import Base, User, UserRole


@pytest.fixture
def app():
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": "test-only-session-key",
        }
    )
    engine = app.extensions["db_engine"]
    Base.metadata.create_all(engine)
    yield app
    engine.dispose()


def run_create_admin(app, name="Test Admin", email="admin@example.com", password="strong-password-123"):
    return app.test_cli_runner().invoke(
        args=["create-admin"],
        input=f"{name}\n{email}\n{password}\n{password}\n",
    )


def test_create_admin_normalizes_email_and_hashes_password(app):
    result = run_create_admin(app, name="  Test Admin  ", email="  ADMIN@EXAMPLE.COM  ")

    assert result.exit_code == 0, result.output
    assert "Admin account created." in result.output
    assert "strong-password-123" not in result.output
    with Session(app.extensions["db_engine"]) as db:
        user = db.scalar(select(User))
        assert user is not None
        assert user.name == "Test Admin"
        assert user.email == "admin@example.com"
        assert user.role == UserRole.ADMIN
        assert user.password_hash != "strong-password-123"
        assert check_password_hash(user.password_hash, "strong-password-123")


def test_duplicate_email_is_rejected_without_changing_existing_role(app):
    with Session(app.extensions["db_engine"]) as db:
        db.add(
            User(
                name="Existing Customer",
                email="admin@example.com",
                password_hash=generate_password_hash("existing-password"),
                role=UserRole.CUSTOMER,
            )
        )
        db.commit()

    result = run_create_admin(app, email="  ADMIN@example.com  ")

    assert result.exit_code != 0
    assert "already exists" in result.output
    with Session(app.extensions["db_engine"]) as db:
        users = db.scalars(select(User)).all()
        assert len(users) == 1
        assert users[0].role == UserRole.CUSTOMER


@pytest.mark.parametrize(
    ("name", "email", "password", "message"),
    [
        ("  ", "admin@example.com", "strong-password-123", "required"),
        ("Test Admin", "invalid-email", "strong-password-123", "valid email"),
        ("Test Admin", "admin@example.com", "short", "between 8 and 128"),
    ],
)
def test_invalid_input_creates_no_account(app, name, email, password, message):
    result = run_create_admin(app, name=name, email=email, password=password)

    assert result.exit_code != 0
    assert message in result.output
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(User)) is None


def test_admin_role_cannot_be_supplied_as_command_option(app):
    result = app.test_cli_runner().invoke(args=["create-admin", "--role", "CUSTOMER"])

    assert result.exit_code != 0
    assert "No such option '--role'" in result.output


def test_admin_creation_has_no_public_route(app):
    assert app.test_client().get("/create-admin").status_code == 404
