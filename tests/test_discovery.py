"""Search and discovery routes against an isolated database."""

from decimal import Decimal
import re

import pytest
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from quotehub import create_app
from quotehub.models import (
    Base, Category, ProviderProfile, ServiceRequest, ServiceRequestStatus, User, UserRole,
)


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "SECRET_KEY": "discovery-test-only-secret",
        "SESSION_COOKIE_SECURE": False,
    })
    engine = app.extensions["db_engine"]
    Base.metadata.create_all(engine)
    yield app
    engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def user(db, email, role):
    account = User(
        name="Private Account Name", email=email,
        password_hash=generate_password_hash("password-123"), role=role,
    )
    db.add(account)
    db.flush()
    return account


def login(client, email):
    page = client.get("/login")
    token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
    response = client.post("/login", data={
        "email": email, "password": "password-123", "csrf_token": token,
    })
    assert response.status_code == 302


def request_item(db, owner, category, title, *, description="Details", location="North", budget=None,
                 status=ServiceRequestStatus.OPEN):
    item = ServiceRequest(
        customer_id=owner.id, category_id=category.id, title=title,
        description=description, location=location, budget=budget, status=status,
    )
    db.add(item)
    return item


def test_request_search_filters_and_exclusions(app, client):
    with Session(app.extensions["db_engine"]) as db:
        provider = user(db, "provider@example.com", UserRole.PROVIDER)
        customer = user(db, "customer@example.com", UserRole.CUSTOMER)
        plumbing = Category(name="Plumbing", slug="plumbing")
        roofing = Category(name="Roofing", slug="roofing")
        db.add_all([plumbing, roofing])
        db.flush()
        request_item(db, customer, plumbing, "Leaky sink", description="Kitchen tap", location="Northside", budget=Decimal("100.25"))
        request_item(db, customer, plumbing, "Pipe repair", description="Leaky pipe", location="South", budget=Decimal("250.00"))
        request_item(db, customer, roofing, "Roof repair", location="Northside", budget=None)
        request_item(db, customer, plumbing, "Closed leak", status=ServiceRequestStatus.ACCEPTED)
        request_item(db, provider, plumbing, "Own leak")
        db.commit()
        plumbing_id = plumbing.id
        roofing_id = roofing.id
    login(client, "provider@example.com")
    base = client.get("/provider/requests")
    assert base.status_code == 200
    assert b"Leaky sink" in base.data and b"Pipe repair" in base.data and b"Roof repair" in base.data
    assert b"Closed leak" not in base.data and b"Own leak" not in base.data
    assert b"Leaky sink" in client.get("/provider/requests?q=sink").data
    assert b"Pipe repair" in client.get("/provider/requests?q=LeAkY+PiPe").data
    assert b"Roof repair" not in client.get(f"/provider/requests?category_id={plumbing_id}").data
    assert b"Roof repair" in client.get(f"/provider/requests?category_id={roofing_id}").data
    assert b"Pipe repair" not in client.get("/provider/requests?location=north").data
    assert b"Leaky sink" in client.get("/provider/requests?min_budget=100.25&max_budget=100.25").data
    assert b"Pipe repair" not in client.get("/provider/requests?max_budget=100.25").data
    assert b"Pipe repair" in client.get("/provider/requests?min_budget=250").data
    assert b"Roof repair" not in client.get("/provider/requests?min_budget=0").data


@pytest.mark.parametrize("query,message", [
    ("min_budget=-1", b"non-negative"),
    ("max_budget=2.123", b"two decimal"),
    ("min_budget=NaN", b"non-negative"),
    ("min_budget=99999999999", b"9,999,999,999.99"),
    ("min_budget=20&max_budget=10", b"Minimum budget cannot exceed"),
    ("category_id=abc", b"Choose a valid category"),
    ("category_id=999999", b"Choose a valid category"),
])
def test_invalid_request_filters(app, client, query, message):
    with Session(app.extensions["db_engine"]) as db:
        user(db, "provider@example.com", UserRole.PROVIDER)
        db.commit()
    login(client, "provider@example.com")
    response = client.get(f"/provider/requests?{query}")
    assert response.status_code == 400
    assert message in response.data


def test_request_pagination_preserves_filters(app, client):
    with Session(app.extensions["db_engine"]) as db:
        user(db, "provider@example.com", UserRole.PROVIDER)
        customer = user(db, "customer@example.com", UserRole.CUSTOMER)
        category = Category(name="Plumbing", slug="plumbing")
        db.add(category)
        db.flush()
        for index in range(21):
            request_item(db, customer, category, f"Match {index:02d}", location="North", budget=Decimal("10.00"))
        db.commit()
        category_id = category.id
    login(client, "provider@example.com")
    query = f"q=Match&category_id={category_id}&location=North&min_budget=10&max_budget=10"
    first = client.get(f"/provider/requests?{query}")
    assert first.status_code == 200
    assert first.data.count(b"Submit quote") == 20
    assert f"page=2&amp;q=Match&amp;category_id={category_id}".encode() in first.data or b"page=2" in first.data
    second = client.get(f"/provider/requests?{query}&page=2")
    assert second.status_code == 200
    assert second.data.count(b"Submit quote") == 1
    assert b"min_budget=10" in second.data and b"max_budget=10" in second.data


def test_request_browse_requires_provider(app, client):
    assert client.get("/provider/requests").status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        user(db, "customer@example.com", UserRole.CUSTOMER)
        db.commit()
    login(client, "customer@example.com")
    assert client.get("/provider/requests").status_code == 403


def test_public_provider_search_privacy_and_empty_results(app, client):
    with Session(app.extensions["db_engine"]) as db:
        first = user(db, "secret-one@example.com", UserRole.PROVIDER)
        second = user(db, "secret-two@example.com", UserRole.PROVIDER)
        user(db, "unlisted@example.com", UserRole.PROVIDER)
        db.add_all([
            ProviderProfile(user_id=first.id, business_name="Handy Works", description="Kitchen repairs", location="North District", phone="555-0101"),
            ProviderProfile(user_id=second.id, business_name="Roof Experts", description="Roof repairs", location="South District"),
        ])
        db.commit()
    page = client.get("/providers")
    assert page.status_code == 200
    assert b"Handy Works" in page.data and b"Roof Experts" in page.data
    for private in (b"secret-one@example.com", b"Private Account Name", b"password_hash", b"unlisted@example.com"):
        assert private not in page.data
    assert b"Handy Works" in client.get("/providers?q=handy").data
    assert b"Handy Works" in client.get("/providers?q=kitchen").data
    location = client.get("/providers?location=north")
    assert b"Handy Works" in location.data and b"Roof Experts" not in location.data
    empty = client.get("/providers?q=unmatched")
    assert empty.status_code == 200 and b"No provider profiles match" in empty.data
    assert client.get("/providers?q=%25").status_code == 200
    assert b"Handy Works" not in client.get("/providers?q=%25").data


def test_provider_pagination_preserves_filters(app, client):
    with Session(app.extensions["db_engine"]) as db:
        for index in range(21):
            account = user(db, f"provider{index}@example.com", UserRole.PROVIDER)
            db.add(ProviderProfile(user_id=account.id, business_name=f"Handy {index:02d}", location="North"))
        db.commit()
    first = client.get("/providers?q=Handy&location=North")
    assert first.status_code == 200
    assert first.data.count(b"/providers/") == 20
    assert b"page=2" in first.data and b"q=Handy" in first.data and b"location=North" in first.data
    second = client.get("/providers?q=Handy&location=North&page=2")
    assert second.status_code == 200
    assert second.data.count(b"/providers/") == 1
