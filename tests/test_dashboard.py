"""Role-scoped dashboard aggregates and bounded recent activity."""

from decimal import Decimal
import re

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from quotehub import create_app
from quotehub.models import (
    Base, Category, Quote, QuoteStatus, ServiceRequest, ServiceRequestStatus,
    User, UserRole,
)


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "SECRET_KEY": "dashboard-test-only-secret",
        "SESSION_COOKIE_SECURE": False,
    })
    engine = app.extensions["db_engine"]
    Base.metadata.create_all(engine)
    yield app
    engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def add_accounts(app):
    with Session(app.extensions["db_engine"]) as db:
        password_hash = generate_password_hash("password-123")
        users = {
            name: User(
                name=name, email=f"{name}@example.com", password_hash=password_hash, role=role
            )
            for name, role in (
                ("customer1", UserRole.CUSTOMER), ("customer2", UserRole.CUSTOMER),
                ("provider1", UserRole.PROVIDER), ("provider2", UserRole.PROVIDER),
                ("provider3", UserRole.PROVIDER), ("admin", UserRole.ADMIN),
            )
        }
        category = Category(name="Plumbing", slug="plumbing")
        db.add_all([*users.values(), category])
        db.commit()
        return {name: user.id for name, user in users.items()}, category.id


def add_request(db, customer_id, category_id, title, status):
    item = ServiceRequest(
        customer_id=customer_id, category_id=category_id, title=title,
        description="Work details", location="North", status=status,
    )
    db.add(item)
    db.flush()
    return item


def add_quote(db, item, provider_id, status):
    db.add(Quote(
        service_request_id=item.id, provider_id=provider_id,
        amount=Decimal("42.50"), status=status,
    ))


def login(client, email):
    page = client.get("/login")
    match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert match is not None
    response = client.post("/login", data={
        "email": email, "password": "password-123",
        "csrf_token": match.group(1).decode(),
    })
    assert response.status_code == 302


def seed_dashboard_data(app):
    users, category_id = add_accounts(app)
    with Session(app.extensions["db_engine"]) as db:
        items = {
            "open_a": add_request(db, users["customer1"], category_id, "Own open A", ServiceRequestStatus.OPEN),
            "open_b": add_request(db, users["customer1"], category_id, "Own open B", ServiceRequestStatus.OPEN),
            "accepted": add_request(db, users["customer1"], category_id, "Own accepted", ServiceRequestStatus.ACCEPTED),
            "completed": add_request(db, users["customer1"], category_id, "Own completed", ServiceRequestStatus.COMPLETED),
            "cancelled": add_request(db, users["customer1"], category_id, "Own cancelled", ServiceRequestStatus.CANCELLED),
            "other_open": add_request(db, users["customer2"], category_id, "Private other open", ServiceRequestStatus.OPEN),
            "other_accepted": add_request(db, users["customer2"], category_id, "Private other accepted", ServiceRequestStatus.ACCEPTED),
            "provider_own": add_request(db, users["provider1"], category_id, "Provider own request", ServiceRequestStatus.OPEN),
            "provider_other": add_request(db, users["provider2"], category_id, "Other provider request", ServiceRequestStatus.OPEN),
        }
        for key, provider, status in (
            ("open_a", "provider1", QuoteStatus.PENDING),
            ("open_a", "provider2", QuoteStatus.PENDING),
            ("open_a", "provider3", QuoteStatus.PENDING),
            ("open_b", "provider1", QuoteStatus.PENDING),
            ("accepted", "provider1", QuoteStatus.ACCEPTED),
            ("completed", "provider1", QuoteStatus.ACCEPTED),
            ("completed", "provider2", QuoteStatus.REJECTED),
            ("cancelled", "provider1", QuoteStatus.WITHDRAWN),
            ("other_accepted", "provider2", QuoteStatus.ACCEPTED),
        ):
            add_quote(db, items[key], users[provider], status)
        db.commit()
        return users, {key: item.id for key, item in items.items()}


def metric(number, label):
    return f'<span class="metric-number">{number}</span><span>{label}</span>'.encode()


def test_customer_dashboard_counts_recent_items_and_quote_counts(app, client):
    _, items = seed_dashboard_data(app)
    login(client, "customer1@example.com")
    response = client.get("/dashboard")
    assert response.status_code == 200
    for number, label in ((5, "Total requests"), (2, "Open requests"),
                          (1, "Accepted requests"), (1, "Completed requests")):
        assert metric(number, label) in response.data
    assert response.data.count(b'class="request-card"') == 5
    assert b"Own open A" in response.data
    assert b"Own open B" in response.data
    assert b"Private other open" not in response.data
    assert b"Private other accepted" not in response.data
    assert f'/requests/{items["open_a"]}'.encode() in response.data
    assert b"3 quotes" in response.data
    assert b"1 quote" in response.data
    assert b"Created " in response.data
    assert b"Plumbing" in response.data


def test_customer_dashboard_isolated_from_other_customer(app, client):
    seed_dashboard_data(app)
    login(client, "customer2@example.com")
    response = client.get("/dashboard")
    assert metric(2, "Total requests") in response.data
    assert metric(1, "Open requests") in response.data
    assert metric(1, "Accepted requests") in response.data
    assert b"Private other open" in response.data
    assert b"Own open A" not in response.data
    assert b"Own completed" not in response.data


def test_provider_dashboard_metrics_and_isolation(app, client):
    seed_dashboard_data(app)
    login(client, "provider1@example.com")
    response = client.get("/dashboard")
    assert response.status_code == 200
    for number, label in ((2, "Open opportunities"), (2, "Pending quotes"),
                          (2, "Accepted quotes"), (1, "Completed jobs")):
        assert metric(number, label) in response.data
    assert response.data.count(b'class="request-card"') == 5
    assert b"Own open A" in response.data
    assert b"42.50" in response.data
    assert b"Private other accepted" not in response.data
    assert b"customer1@example.com" not in response.data
    assert b"View my quotes" in response.data
    assert b"Set up or edit your business profile" in response.data


def test_provider_opportunities_exclude_own_request_and_other_quotes(app, client):
    seed_dashboard_data(app)
    login(client, "provider2@example.com")
    response = client.get("/dashboard")
    assert metric(3, "Open opportunities") in response.data
    assert metric(1, "Pending quotes") in response.data
    assert metric(1, "Accepted quotes") in response.data
    assert metric(0, "Completed jobs") in response.data
    assert b"Own open B" not in response.data


def test_withdrawn_quote_still_excludes_request_from_opportunities(app, client):
    seed_dashboard_data(app)
    with Session(app.extensions["db_engine"]) as db:
        provider_id = db.scalar(select(User.id).where(User.email == "provider1@example.com"))
        quote = db.scalar(
            select(Quote).join(Quote.service_request).where(
                Quote.provider_id == provider_id,
                ServiceRequest.title == "Own open A",
            )
        )
        assert quote is not None
        quote.status = QuoteStatus.WITHDRAWN
        db.commit()
    login(client, "provider1@example.com")
    response = client.get("/dashboard")
    assert metric(2, "Open opportunities") in response.data
    assert metric(1, "Pending quotes") in response.data


@pytest.mark.parametrize("role", [UserRole.CUSTOMER, UserRole.PROVIDER])
def test_empty_dashboard(app, client, role):
    add_accounts(app)
    email = "customer1@example.com" if role == UserRole.CUSTOMER else "provider1@example.com"
    login(client, email)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.data.count(b'class="metric-number">0</span>') == 4
    if role == UserRole.CUSTOMER:
        assert b"You have no requests yet" in response.data
        assert b"Create your first request" in response.data
    else:
        assert b"You have no quotes yet" in response.data
        assert b"Browse open requests" in response.data


def test_recent_lists_are_limited_and_queries_do_not_grow_per_row(app, client):
    users, category_id = add_accounts(app)
    with Session(app.extensions["db_engine"]) as db:
        for index in range(9):
            item = add_request(
                db, users["customer1"], category_id,
                f"Job {index}", ServiceRequestStatus.OPEN,
            )
            add_quote(db, item, users["provider1"], QuoteStatus.PENDING)
        db.commit()
    engine = app.extensions["db_engine"]
    selects = []

    def record_select(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    for email in ("customer1@example.com", "provider1@example.com"):
        current = app.test_client()
        login(current, email)
        selects.clear()
        event.listen(engine, "before_cursor_execute", record_select)
        try:
            response = current.get("/dashboard")
        finally:
            event.remove(engine, "before_cursor_execute", record_select)
        assert response.status_code == 200
        assert response.data.count(b'class="request-card"') == 6
        assert len(selects) <= 5
        if email.startswith("customer"):
            assert metric(9, "Total requests") in response.data
        else:
            assert metric(9, "Pending quotes") in response.data


def test_admin_dashboard_and_anonymous_behavior(app, client):
    add_accounts(app)
    anonymous = client.get("/dashboard")
    assert anonymous.status_code == 302
    assert "/login" in anonymous.location
    login(client, "admin@example.com")
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert b"Open admin area" in response.data
    assert b"Manage categories" in response.data
    assert b"Request summary" not in response.data
    assert b"Provider summary" not in response.data
    assert client.get("/admin").status_code == 200
