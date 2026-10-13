"""Service request routes with isolated database and HTTP sessions."""

from decimal import Decimal
import re

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from quotehub import create_app
from quotehub.models import Base, Category, ServiceRequest, ServiceRequestStatus, User, UserRole


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "SECRET_KEY": "phase5-test-only-secret",
        "SESSION_COOKIE_SECURE": False,
    })
    engine = app.extensions["db_engine"]
    Base.metadata.create_all(engine)
    yield app
    engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def seed(app):
    with Session(app.extensions["db_engine"]) as db:
        users = [
            User(name="Customer One", email="one@example.com", password_hash=generate_password_hash("password-123"), role=UserRole.CUSTOMER),
            User(name="Customer Two", email="two@example.com", password_hash=generate_password_hash("password-123"), role=UserRole.CUSTOMER),
            User(name="Provider", email="provider@example.com", password_hash=generate_password_hash("password-123"), role=UserRole.PROVIDER),
        ]
        category = Category(name="Plumbing", slug="plumbing")
        db.add_all([*users, category])
        db.commit()
        return users[0].id, users[1].id, users[2].id, category.id


def token(client, path):
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def login(client, email):
    response = client.post("/login", data={
        "email": email, "password": "password-123", "csrf_token": token(client, "/login"),
    })
    assert response.status_code == 302


def form(category_id_value, **changes):
    fields = {
        "title": "  Leaky   sink  ", "description": "  Please fix the sink.  ",
        "category_id": str(category_id_value), "location": "  North   Street  ",
        "budget": "123.45",
    }
    fields.update(changes)
    return fields


def post(client, path, fields, token_path=None):
    return client.post(path, data={**fields, "csrf_token": token(client, token_path or path)})


def add_request(app, customer_id, category_id, title="Owned request", status=ServiceRequestStatus.OPEN):
    with Session(app.extensions["db_engine"]) as db:
        item = ServiceRequest(
            customer_id=customer_id, category_id=category_id, title=title,
            description="Request details", location="West", status=status,
        )
        db.add(item)
        db.commit()
        return item.id


def test_customer_creates_open_request_with_server_owned_fields(app, client):
    customer_id, other_id, _, category_id = seed(app)
    login(client, "one@example.com")
    response = post(client, "/requests/new", {
        **form(category_id), "customer_id": str(other_id), "status": "COMPLETED",
    })
    assert response.status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        item = db.scalar(select(ServiceRequest))
        assert item.customer_id == customer_id
        assert item.status == ServiceRequestStatus.OPEN
        assert item.title == "Leaky sink"
        assert item.description == "Please fix the sink."
        assert item.location == "North Street"
        assert item.budget == Decimal("123.45")
        assert item.category_id == category_id
        assert item.created_at is not None and item.updated_at is not None


@pytest.mark.parametrize("changes, expected", [
    ({"title": "  "}, b"required"),
    ({"description": " \n "}, b"required"),
    ({"location": "  "}, b"required"),
    ({"title": "x" * 201}, b"200 characters"),
    ({"description": "x" * 5001}, b"5,000 characters"),
    ({"category_id": "99999"}, b"valid category"),
    ({"category_id": "bad"}, b"valid category"),
    ({"category_id": "9" * 5000}, b"valid category"),
    ({"budget": "-1.00"}, b"non-negative"),
    ({"budget": "1.234"}, b"two decimal"),
    ({"budget": "NaN"}, b"non-negative"),
    ({"budget": "Infinity"}, b"non-negative"),
    ({"budget": "1e2"}, b"non-negative"),
    ({"budget": "10000000000"}, b"at most"),
])
def test_invalid_create_inputs_are_rejected(app, client, changes, expected):
    _, _, _, category_id = seed(app)
    login(client, "one@example.com")
    response = post(client, "/requests/new", form(category_id, **changes))
    assert response.status_code == 400
    assert expected in response.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(ServiceRequest)) is None


def test_optional_budget_and_empty_category_list(app, client):
    customer_id, _, _, category_id = seed(app)
    login(client, "one@example.com")
    assert post(client, "/requests/new", form(category_id, budget="")).status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(ServiceRequest).where(ServiceRequest.customer_id == customer_id)).budget is None


def test_empty_category_list_is_handled(app, client):
    seed(app)
    with Session(app.extensions["db_engine"]) as db:
        db.delete(db.scalar(select(Category)))
        db.commit()
    login(client, "one@example.com")
    response = client.get("/requests/new")
    assert response.status_code == 200
    assert b"No categories are available yet" in response.data
    assert post(client, "/requests/new", form(1)).status_code == 400


def test_customer_listing_detail_and_private_ownership(app, client):
    first, second, _, category_id = seed(app)
    own_id = add_request(app, first, category_id, title="My sink")
    other_id = add_request(app, second, category_id, title="Private job")
    login(client, "one@example.com")
    assert b"View my requests" in client.get("/dashboard").data
    listing = client.get("/requests")
    assert b"My sink" in listing.data
    assert b"Private job" not in listing.data
    assert b"My sink" in client.get(f"/requests/{own_id}").data
    assert client.get(f"/requests/{other_id}").status_code == 404
    assert client.get(f"/requests/{other_id}/edit").status_code == 404
    assert post(client, f"/requests/{other_id}/edit", form(category_id), "/dashboard").status_code == 404
    assert post(client, f"/requests/{other_id}/cancel", {}, "/dashboard").status_code == 404
    assert client.get("/requests/99999").status_code == 404
    assert client.get("/requests/99999/edit").status_code == 404
    assert post(client, "/requests/99999/cancel", {}, "/dashboard").status_code == 404


def test_customer_empty_state(app, client):
    seed(app)
    login(client, "one@example.com")
    assert b"You have no requests yet" in client.get("/requests").data


def test_customer_edits_and_cancels_own_open_request(app, client):
    first, _, _, category_id = seed(app)
    request_id = add_request(app, first, category_id)
    login(client, "one@example.com")
    edit_path = f"/requests/{request_id}/edit"
    response = post(client, edit_path, form(category_id, title="  Updated   job ", budget="0"))
    assert response.status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        item = db.get(ServiceRequest, request_id)
        assert item.title == "Updated job"
        assert item.budget == Decimal("0.00")
        assert item.status == ServiceRequestStatus.OPEN
    assert post(client, f"/requests/{request_id}/cancel", {}, f"/requests/{request_id}").status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, request_id).status == ServiceRequestStatus.CANCELLED
    assert b"Cancelled" in client.get(f"/requests/{request_id}").data


def test_edit_reuses_creation_validation(app, client):
    first, _, _, category_id = seed(app)
    request_id = add_request(app, first, category_id)
    login(client, "one@example.com")
    path = f"/requests/{request_id}/edit"
    assert post(client, path, form(category_id, budget="1.234")).status_code == 400
    assert post(client, path, form(category_id, category_id="99999")).status_code == 400
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, request_id).title == "Owned request"


@pytest.mark.parametrize("status", [
    ServiceRequestStatus.QUOTED, ServiceRequestStatus.ACCEPTED,
    ServiceRequestStatus.COMPLETED, ServiceRequestStatus.CANCELLED,
])
def test_non_open_request_cannot_be_edited_or_cancelled(app, client, status):
    first, _, _, category_id = seed(app)
    request_id = add_request(app, first, category_id, status=status)
    login(client, "one@example.com")
    assert client.get(f"/requests/{request_id}/edit").status_code == 409
    assert post(client, f"/requests/{request_id}/edit", form(category_id), "/dashboard").status_code == 409
    assert post(client, f"/requests/{request_id}/cancel", {}, "/dashboard").status_code == 409
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, request_id).status == status


def test_provider_sees_only_open_requests_and_no_customer_details(app, client):
    first, _, _, category_id = seed(app)
    add_request(app, first, category_id, title="Visible job")
    add_request(app, first, category_id, title="Cancelled job", status=ServiceRequestStatus.CANCELLED)
    add_request(app, first, category_id, title="Quoted job", status=ServiceRequestStatus.QUOTED)
    login(client, "provider@example.com")
    assert b"Browse open requests" in client.get("/dashboard").data
    response = client.get("/provider/requests")
    assert response.status_code == 200
    assert b"Visible job" in response.data
    assert b"Cancelled job" not in response.data
    assert b"Quoted job" not in response.data
    assert b"one@example.com" not in response.data
    assert b"Customer One" not in response.data


def test_role_and_login_boundaries(app, client):
    first, _, _, category_id = seed(app)
    request_id = add_request(app, first, category_id)
    assert client.get("/requests").status_code == 302
    assert client.get("/requests/new").status_code == 302
    assert client.get("/provider/requests").status_code == 302
    login(client, "one@example.com")
    assert client.get("/provider/requests").status_code == 403
    assert client.get("/logout").status_code == 405
    assert post(client, "/logout", {}, "/dashboard").status_code == 302
    login(client, "provider@example.com")
    assert client.get("/requests").status_code == 403
    assert client.get("/requests/new").status_code == 403
    assert client.get(f"/requests/{request_id}").status_code == 403
    assert client.get(f"/requests/{request_id}/edit").status_code == 403
    assert post(client, "/requests/new", form(category_id), "/dashboard").status_code == 403
    assert post(client, f"/requests/{request_id}/edit", form(category_id), "/dashboard").status_code == 403
    assert post(client, f"/requests/{request_id}/cancel", {}, "/dashboard").status_code == 403


def test_csrf_and_post_only_changes(app, client):
    first, _, _, category_id = seed(app)
    request_id = add_request(app, first, category_id)
    login(client, "one@example.com")
    assert client.post("/requests/new", data=form(category_id)).status_code == 400
    assert client.post(f"/requests/{request_id}/edit", data=form(category_id)).status_code == 400
    assert client.post(f"/requests/{request_id}/cancel").status_code == 400
    assert client.get(f"/requests/{request_id}/cancel").status_code == 405


def test_bounded_pagination(app, client):
    first, _, _, category_id = seed(app)
    for index in range(22):
        add_request(app, first, category_id, title=f"Job {index:02d}")
    login(client, "provider@example.com")
    first_page = client.get("/provider/requests")
    second_page = client.get("/provider/requests?page=2")
    assert first_page.data.count(b"class=\"request-card\"") == 20
    assert second_page.data.count(b"class=\"request-card\"") == 2
    assert client.get("/provider/requests?page=not-a-number").status_code == 404
