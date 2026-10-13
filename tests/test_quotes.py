"""Quote submission and viewing through isolated HTTP sessions."""

from decimal import Decimal
import re

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from quotehub import create_app
from quotehub.models import (
    Base, Category, ProviderProfile, Quote, QuoteStatus,
    ServiceRequest, ServiceRequestStatus, User, UserRole,
)


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "SECRET_KEY": "phase6-test-only-secret",
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
        password_hash = generate_password_hash("password-123")
        accounts = [
            User(name="Private Customer One", email="one@example.com", password_hash=password_hash, role=UserRole.CUSTOMER),
            User(name="Private Customer Two", email="two@example.com", password_hash=password_hash, role=UserRole.CUSTOMER),
            User(name="Private Provider One", email="provider1@example.com", password_hash=password_hash, role=UserRole.PROVIDER),
            User(name="Private Provider Two", email="provider2@example.com", password_hash=password_hash, role=UserRole.PROVIDER),
            User(name="Admin", email="admin@example.com", password_hash=password_hash, role=UserRole.ADMIN),
        ]
        category = Category(name="Plumbing", slug="plumbing")
        db.add_all([*accounts, category])
        db.flush()
        db.add(ProviderProfile(user_id=accounts[2].id, business_name="Public Plumbing", location="West"))
        item = ServiceRequest(
            customer_id=accounts[0].id, category_id=category.id,
            title="Fix sink", description="Sink leaks at night", location="North",
            status=ServiceRequestStatus.OPEN,
        )
        db.add(item)
        db.commit()
        return {"customer": accounts[0].id, "other_customer": accounts[1].id,
                "provider": accounts[2].id, "other_provider": accounts[3].id,
                "admin": accounts[4].id, "category": category.id, "request": item.id}


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


def switch_user(client, email):
    client.post("/logout", data={"csrf_token": token(client, "/dashboard")})
    login(client, email)


def submit(client, request_id, **changes):
    path = f"/provider/requests/{request_id}/quote"
    data = {"amount": "123.45", "message": "  I can   help.  "}
    data.update(changes)
    return client.post(path, data={**data, "csrf_token": token(client, path)})


def add_quote(app, request_id, provider_id, status=QuoteStatus.PENDING, message="Can help"):
    with Session(app.extensions["db_engine"]) as db:
        quote = Quote(
            service_request_id=request_id, provider_id=provider_id,
            amount=Decimal("85.20"), message=message, status=status,
        )
        db.add(quote)
        db.commit()
        return quote.id


def test_provider_submits_pending_quote_with_server_owned_fields(app, client):
    ids = seed(app)
    login(client, "provider1@example.com")
    assert b"Submit quote" in client.get("/provider/requests").data
    response = submit(
        client, ids["request"], provider_id=str(ids["other_provider"]),
        status="ACCEPTED", customer_id=str(ids["other_customer"]),
    )
    assert response.status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        quote = db.scalar(select(Quote))
        assert quote.provider_id == ids["provider"]
        assert quote.service_request_id == ids["request"]
        assert quote.status == QuoteStatus.PENDING
        assert quote.amount == Decimal("123.45")
        assert quote.message == "I can   help."
        assert quote.created_at is not None
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.OPEN


@pytest.mark.parametrize("amount, message", [
    ("", b"positive amount"), ("0", b"greater than zero"),
    ("-1", b"positive amount"), ("NaN", b"positive amount"),
    ("Infinity", b"positive amount"), ("1e2", b"positive amount"),
    ("abc", b"positive amount"), ("1.234", b"two decimal"),
    ("10000000000", b"at most"),
])
def test_invalid_amount_rejected(app, client, amount, message):
    ids = seed(app)
    login(client, "provider1@example.com")
    response = submit(client, ids["request"], amount=amount)
    assert response.status_code == 400
    assert message in response.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(Quote)) is None


def test_message_limit_and_optional_message(app, client):
    ids = seed(app)
    login(client, "provider1@example.com")
    too_long = submit(client, ids["request"], message="x" * 2001)
    assert too_long.status_code == 400
    assert b"2,000 characters" in too_long.data
    assert submit(client, ids["request"], message="  ").status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(Quote)).message is None


@pytest.mark.parametrize("status", [
    ServiceRequestStatus.QUOTED, ServiceRequestStatus.ACCEPTED,
    ServiceRequestStatus.COMPLETED, ServiceRequestStatus.CANCELLED,
])
def test_non_open_request_cannot_receive_quote(app, client, status):
    ids = seed(app)
    with Session(app.extensions["db_engine"]) as db:
        db.get(ServiceRequest, ids["request"]).status = status
        db.commit()
    login(client, "provider1@example.com")
    path = f"/provider/requests/{ids['request']}/quote"
    assert client.get(path).status_code == 409
    assert client.post(path, data={"amount": "5", "csrf_token": token(client, "/dashboard")}).status_code == 409
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(Quote)) is None


def test_missing_request_and_own_customer_request_are_rejected(app, client):
    ids = seed(app)
    with Session(app.extensions["db_engine"]) as db:
        own = ServiceRequest(
            customer_id=ids["provider"], category_id=ids["category"],
            title="Own request", description="Internal record", location="North",
        )
        db.add(own)
        db.commit()
        own_id = own.id
    login(client, "provider1@example.com")
    assert client.get("/provider/requests/99999/quote").status_code == 404
    assert client.post("/provider/requests/99999/quote", data={"amount": "5", "csrf_token": token(client, "/dashboard")}).status_code == 404
    assert client.get(f"/provider/requests/{own_id}/quote").status_code == 403
    assert client.post(f"/provider/requests/{own_id}/quote", data={"amount": "5", "csrf_token": token(client, "/dashboard")}).status_code == 403


def test_duplicate_quote_is_rejected_even_after_withdrawal(app, client):
    ids = seed(app)
    login(client, "provider1@example.com")
    assert submit(client, ids["request"]).status_code == 302
    duplicate = client.post(
        f"/provider/requests/{ids['request']}/quote",
        data={"amount": "200", "csrf_token": token(client, "/dashboard")},
    )
    assert duplicate.status_code == 409
    assert b"already submitted" in duplicate.data
    with Session(app.extensions["db_engine"]) as db:
        quote_id = db.scalar(select(Quote.id))
    assert client.post(f"/provider/quotes/{quote_id}/withdraw", data={"csrf_token": token(client, f"/provider/quotes/{quote_id}")}).status_code == 302
    assert client.get(f"/provider/requests/{ids['request']}/quote").status_code == 409
    with Session(app.extensions["db_engine"]) as db:
        assert len(db.scalars(select(Quote)).all()) == 1


def test_concurrent_duplicate_insert_is_reported_cleanly(app, client, monkeypatch):
    ids = seed(app)
    login(client, "provider1@example.com")
    path = f"/provider/requests/{ids['request']}/quote"
    csrf = token(client, path)
    original_commit = Session.commit
    simulated = False

    def commit_after_competitor(db):
        nonlocal simulated
        if not simulated and any(isinstance(obj, Quote) for obj in db.new):
            simulated = True
            db.rollback()
            with Session(app.extensions["db_engine"]) as competing:
                competing.add(Quote(
                    service_request_id=ids["request"], provider_id=ids["provider"],
                    amount=Decimal("60.00"), status=QuoteStatus.PENDING,
                ))
                original_commit(competing)
            raise IntegrityError("simulated unique conflict", {}, Exception("duplicate"))
        return original_commit(db)

    monkeypatch.setattr(Session, "commit", commit_after_competitor)
    response = client.post(path, data={"amount": "80.00", "csrf_token": csrf})
    assert response.status_code == 409
    assert b"already submitted" in response.data
    with Session(app.extensions["db_engine"]) as db:
        quotes = db.scalars(select(Quote)).all()
        assert len(quotes) == 1
        assert quotes[0].amount == Decimal("60.00")


def test_provider_lists_only_own_quotes_and_cannot_access_others(app, client):
    ids = seed(app)
    own_id = add_quote(app, ids["request"], ids["provider"], message="My quote")
    other_id = add_quote(app, ids["request"], ids["other_provider"], message="Private other quote")
    login(client, "provider1@example.com")
    assert b"View my quotes" in client.get("/dashboard").data
    listing = client.get("/provider/quotes")
    assert listing.status_code == 200
    assert listing.data.count(b'class="request-card"') == 1
    assert b"My quote" in client.get(f"/provider/quotes/{own_id}").data
    assert b"Private other quote" not in listing.data
    assert client.get(f"/provider/quotes/{other_id}").status_code == 404
    assert client.post(f"/provider/quotes/{other_id}/withdraw", data={"csrf_token": token(client, "/dashboard")}).status_code == 404
    assert client.get("/provider/quotes/99999").status_code == 404
    assert client.post("/provider/quotes/99999/withdraw", data={"csrf_token": token(client, "/dashboard")}).status_code == 404


def test_provider_can_withdraw_only_pending_quote(app, client):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    login(client, "provider1@example.com")
    path = f"/provider/quotes/{quote_id}/withdraw"
    assert client.get(path).status_code == 405
    assert client.post(path, data={"csrf_token": token(client, f"/provider/quotes/{quote_id}")}).status_code == 302
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, quote_id).status == QuoteStatus.WITHDRAWN
    assert b"Withdraw quote" not in client.get(f"/provider/quotes/{quote_id}").data
    assert client.post(path, data={"csrf_token": token(client, "/dashboard")}).status_code == 409
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, quote_id).status == QuoteStatus.WITHDRAWN


@pytest.mark.parametrize("status", [QuoteStatus.ACCEPTED, QuoteStatus.REJECTED, QuoteStatus.WITHDRAWN])
def test_other_statuses_cannot_be_withdrawn(app, client, status):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"], status=status)
    login(client, "provider1@example.com")
    assert client.post(f"/provider/quotes/{quote_id}/withdraw", data={"csrf_token": token(client, "/dashboard")}).status_code == 409


def test_customer_sees_own_request_quotes_with_public_provider_identity(app, client):
    ids = seed(app)
    add_quote(app, ids["request"], ids["provider"], message="<script>alert('x')</script>")
    add_quote(app, ids["request"], ids["other_provider"], status=QuoteStatus.WITHDRAWN)
    login(client, "one@example.com")
    assert b"View quotes for this request" in client.get(f"/requests/{ids['request']}").data
    response = client.get(f"/requests/{ids['request']}/quotes")
    assert response.status_code == 200
    assert b"Public Plumbing" in response.data
    assert b"Provider profile unavailable" in response.data
    assert b"85.20" in response.data
    assert b"Withdrawn" in response.data
    assert b"&lt;script&gt;" in response.data
    assert b"<script>" not in response.data
    assert b"provider1@example.com" not in response.data
    assert b"Private Provider One" not in response.data
    assert b"scrypt:" not in response.data


def test_customer_cannot_view_another_request_quotes(app, client):
    ids = seed(app)
    add_quote(app, ids["request"], ids["provider"])
    login(client, "two@example.com")
    assert client.get(f"/requests/{ids['request']}/quotes").status_code == 404
    assert client.get("/requests/99999/quotes").status_code == 404


def test_roles_and_anonymous_access(app, client):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    submit_path = f"/provider/requests/{ids['request']}/quote"
    assert client.get(submit_path).status_code == 302
    assert client.get("/provider/quotes").status_code == 302
    assert client.get(f"/requests/{ids['request']}/quotes").status_code == 302
    login(client, "one@example.com")
    assert client.get(submit_path).status_code == 403
    assert client.get("/provider/quotes").status_code == 403
    assert client.get(f"/provider/quotes/{quote_id}").status_code == 403
    assert client.post(submit_path, data={"amount": "5", "csrf_token": token(client, "/dashboard")}).status_code == 403
    switch_user(client, "provider2@example.com")
    assert client.get(f"/requests/{ids['request']}/quotes").status_code == 403
    switch_user(client, "admin@example.com")
    assert client.get(submit_path).status_code == 403
    assert client.get("/provider/quotes").status_code == 403
    assert client.get(f"/requests/{ids['request']}/quotes").status_code == 403


def test_quote_writes_require_csrf(app, client):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    login(client, "provider1@example.com")
    assert client.post(f"/provider/requests/{ids['request']}/quote", data={"amount": "12.50"}).status_code == 400
    assert client.post(f"/provider/quotes/{quote_id}/withdraw").status_code == 400
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, quote_id).status == QuoteStatus.PENDING
