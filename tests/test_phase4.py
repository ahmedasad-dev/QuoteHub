"""Provider profile and category behavior with isolated test data."""

import re

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from quotehub import create_app
from quotehub.models import Base, Category, ProviderProfile, User, UserRole


@pytest.fixture
def app():
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": "phase4-test-only-secret",
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


def add_user(app, email, role):
    with Session(app.extensions["db_engine"]) as db:
        user = User(
            name="Private Account Name",
            email=email,
            password_hash=generate_password_hash("strong-password-123"),
            role=role,
        )
        db.add(user)
        db.commit()
        return user.id


def token(client, path):
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def login(client, email):
    csrf = token(client, "/login")
    response = client.post(
        "/login",
        data={"email": email, "password": "strong-password-123", "csrf_token": csrf},
    )
    assert response.status_code == 302


def profile_data(**changes):
    data = {
        "business_name": "  Handy   Works  ",
        "description": "  Repairs and maintenance.  ",
        "phone": "  +1  555  0123  ",
        "location": "  North   District  ",
    }
    data.update(changes)
    return data


def save_profile(client, **changes):
    csrf = token(client, "/provider/profile")
    return client.post(
        "/provider/profile", data={**profile_data(**changes), "csrf_token": csrf}
    )


def category_data(**changes):
    data = {"name": "  Home   Repair  ", "slug": "  Home_Repair  "}
    data.update(changes)
    return data


def add_category_form(client, **changes):
    csrf = token(client, "/admin/categories")
    return client.post(
        "/admin/categories", data={**category_data(**changes), "csrf_token": csrf}
    )


def test_provider_can_create_profile_and_fields_are_trimmed(app, client):
    provider_id = add_user(app, "provider@example.com", UserRole.PROVIDER)
    login(client, "provider@example.com")
    assert b"Set up or edit your business profile" in client.get("/dashboard").data

    response = save_profile(client, user_id="9999", role="ADMIN")
    assert response.status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        profile = db.scalar(select(ProviderProfile))
        assert profile.user_id == provider_id
        assert profile.business_name == "Handy Works"
        assert profile.description == "Repairs and maintenance."
        assert profile.phone == "+1 555 0123"
        assert profile.location == "North District"
        assert db.get(User, provider_id).role == UserRole.PROVIDER


def test_provider_can_update_own_profile_without_creating_second(app, client):
    provider_id = add_user(app, "provider@example.com", UserRole.PROVIDER)
    login(client, "provider@example.com")
    assert save_profile(client).status_code == 302
    assert save_profile(client, business_name="  New Name  ", phone=" ").status_code == 302

    with Session(app.extensions["db_engine"]) as db:
        profiles = list(db.scalars(select(ProviderProfile)))
        assert len(profiles) == 1
        assert profiles[0].user_id == provider_id
        assert profiles[0].business_name == "New Name"
        assert profiles[0].phone is None


def test_second_provider_cannot_edit_first_provider_profile(app, client):
    first_id = add_user(app, "first@example.com", UserRole.PROVIDER)
    second_id = add_user(app, "second@example.com", UserRole.PROVIDER)
    with Session(app.extensions["db_engine"]) as db:
        first_profile = ProviderProfile(
            user_id=first_id, business_name="First Business", location="West"
        )
        db.add(first_profile)
        db.commit()
        first_profile_id = first_profile.id

    login(client, "second@example.com")
    assert save_profile(
        client, business_name="Second Business", user_id=str(first_id), profile_id=str(first_profile_id)
    ).status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ProviderProfile, first_profile_id).business_name == "First Business"
        own = db.scalar(select(ProviderProfile).where(ProviderProfile.user_id == second_id))
        assert own.business_name == "Second Business"


def test_customer_cannot_create_or_edit_provider_profile(app, client):
    add_user(app, "customer@example.com", UserRole.CUSTOMER)
    login(client, "customer@example.com")
    assert client.get("/provider/profile").status_code == 403
    csrf = token(client, "/dashboard")
    assert client.post(
        "/provider/profile", data={**profile_data(), "csrf_token": csrf}
    ).status_code == 403
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(ProviderProfile)) is None


def test_unauthenticated_profile_access_follows_login_behavior(client):
    assert client.get("/provider/profile").status_code == 302
    csrf = token(client, "/register")
    response = client.post(
        "/provider/profile", data={**profile_data(), "csrf_token": csrf}
    )
    assert response.status_code == 302
    assert "/login" in response.location


def test_public_profile_shows_only_business_fields_and_escapes_html(app, client):
    provider_id = add_user(app, "private@example.com", UserRole.PROVIDER)
    with Session(app.extensions["db_engine"]) as db:
        profile = ProviderProfile(
            user_id=provider_id,
            business_name="Helpful Co",
            description="<script>alert('x')</script>",
            phone="555-0101",
            location="East",
        )
        db.add(profile)
        db.commit()
        profile_id = profile.id

    response = client.get(f"/providers/{profile_id}")
    assert response.status_code == 200
    assert b"Helpful Co" in response.data
    assert b"555-0101" in response.data
    assert b"&lt;script&gt;" in response.data
    assert b"<script>" not in response.data
    assert b"private@example.com" not in response.data
    assert b"Private Account Name" not in response.data
    assert b"scrypt:" not in response.data


def test_unknown_public_profile_returns_404(client):
    assert client.get("/providers/99999").status_code == 404


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"business_name": "  "}, b"Business name and location are required"),
        ({"phone": "1" * 41}, b"Phone must be 40 characters"),
        ({"description": "x" * 2001}, b"Description must be 2,000 characters"),
    ],
)
def test_profile_validation_rejects_bad_fields(app, client, changes, message):
    add_user(app, "provider@example.com", UserRole.PROVIDER)
    login(client, "provider@example.com")
    response = save_profile(client, **changes)
    assert response.status_code == 400
    assert message in response.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(ProviderProfile)) is None


def test_profile_post_requires_csrf(app, client):
    add_user(app, "provider@example.com", UserRole.PROVIDER)
    login(client, "provider@example.com")
    assert client.post("/provider/profile", data=profile_data()).status_code == 400


def test_categories_are_publicly_viewable(app, client):
    with Session(app.extensions["db_engine"]) as db:
        db.add(Category(name="Plumbing", slug="plumbing"))
        db.commit()
    response = client.get("/categories")
    assert response.status_code == 200
    assert b"Plumbing" in response.data
    assert b"Manage categories" not in response.data


@pytest.mark.parametrize("role", [UserRole.CUSTOMER, UserRole.PROVIDER])
def test_non_admin_cannot_manage_categories(app, client, role):
    add_user(app, "member@example.com", role)
    login(client, "member@example.com")
    assert client.get("/categories").status_code == 200
    assert client.get("/admin/categories").status_code == 403
    assert client.get("/admin/categories/1/edit").status_code == 403
    csrf = token(client, "/dashboard")
    assert client.post(
        "/admin/categories", data={**category_data(), "csrf_token": csrf}
    ).status_code == 403
    assert client.post(
        "/admin/categories/1/edit", data={**category_data(), "csrf_token": csrf}
    ).status_code == 403


def test_admin_can_create_and_rename_category(app, client):
    add_user(app, "admin@example.com", UserRole.ADMIN)
    login(client, "admin@example.com")
    assert b"Manage categories" in client.get("/categories").data
    assert add_category_form(client).status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        category = db.scalar(select(Category))
        assert category.name == "Home Repair"
        assert category.slug == "home-repair"
        category_id = category.id

    csrf = token(client, f"/admin/categories/{category_id}/edit")
    response = client.post(
        f"/admin/categories/{category_id}/edit",
        data={"name": "  Repairs  ", "slug": "  repairs  ", "csrf_token": csrf},
    )
    assert response.status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        updated = db.get(Category, category_id)
        assert updated.name == "Repairs"
        assert updated.slug == "repairs"
        assert db.scalar(select(func.count(Category.id))) == 1


def test_duplicate_category_name_and_slug_are_handled(app, client):
    add_user(app, "admin@example.com", UserRole.ADMIN)
    login(client, "admin@example.com")
    assert add_category_form(client).status_code == 302

    duplicate_name = add_category_form(client, name="home repair", slug="other")
    duplicate_slug = add_category_form(client, name="Other", slug="HOME_REPAIR")
    assert duplicate_name.status_code == duplicate_slug.status_code == 400
    assert b"already exists" in duplicate_name.data
    assert b"already exists" in duplicate_slug.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(func.count(Category.id))) == 1


def test_category_edit_rejects_duplicate_and_unknown_id(app, client):
    add_user(app, "admin@example.com", UserRole.ADMIN)
    with Session(app.extensions["db_engine"]) as db:
        db.add_all(
            [Category(name="Plumbing", slug="plumbing"), Category(name="Cleaning", slug="cleaning")]
        )
        db.commit()
        cleaning_id = db.scalar(select(Category.id).where(Category.slug == "cleaning"))
    login(client, "admin@example.com")
    csrf = token(client, f"/admin/categories/{cleaning_id}/edit")
    response = client.post(
        f"/admin/categories/{cleaning_id}/edit",
        data={"name": "Plumbing", "slug": "cleaning", "csrf_token": csrf},
    )
    assert response.status_code == 400
    assert b"already exists" in response.data
    assert client.get("/admin/categories/99999/edit").status_code == 404


def test_category_forms_require_csrf_and_valid_slug(app, client):
    add_user(app, "admin@example.com", UserRole.ADMIN)
    login(client, "admin@example.com")
    assert client.post("/admin/categories", data=category_data()).status_code == 400
    invalid = add_category_form(client, slug="bad/slug")
    assert invalid.status_code == 400
    assert b"Slug may contain" in invalid.data


def test_seed_command_is_idempotent_and_preserves_admin_edits(app):
    runner = app.test_cli_runner()
    first = runner.invoke(args=["seed-categories"])
    second = runner.invoke(args=["seed-categories"])
    assert first.exit_code == second.exit_code == 0
    assert "Added 5 categories; skipped 0" in first.output
    assert "Added 0 categories; skipped 5" in second.output

    with Session(app.extensions["db_engine"]) as db:
        plumbing = db.scalar(select(Category).where(Category.slug == "plumbing"))
        plumbing.name = "Pipes and drains"
        db.commit()

    third = runner.invoke(args=["seed-categories"])
    assert third.exit_code == 0
    assert "Added 0 categories; skipped 5" in third.output
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(func.count(Category.id))) == 5
        assert db.scalar(select(Category.name).where(Category.slug == "plumbing")) == "Pipes and drains"
