"""Quote submission and viewing through isolated HTTP sessions."""

from decimal import Decimal
import re

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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


def decision(client, request_id, quote_id, action):
    path = f"/requests/{request_id}/quotes/{quote_id}/{action}"
    csrf = token(client, f"/requests/{request_id}/quotes")
    return client.post(path, data={"csrf_token": csrf})


def test_customer_accepts_one_quote_and_rejects_only_other_pending_quotes(app, client):
    ids = seed(app)
    selected = add_quote(app, ids["request"], ids["provider"])
    other_pending = add_quote(app, ids["request"], ids["other_provider"])
    with Session(app.extensions["db_engine"]) as db:
        password_hash = generate_password_hash("password-123")
        extra = [
            User(name="Third provider", email="third@example.com", password_hash=password_hash, role=UserRole.PROVIDER),
            User(name="Fourth provider", email="fourth@example.com", password_hash=password_hash, role=UserRole.PROVIDER),
        ]
        db.add_all(extra)
        db.commit()
        rejected_provider, withdrawn_provider = extra[0].id, extra[1].id
    already_rejected = add_quote(app, ids["request"], rejected_provider, QuoteStatus.REJECTED)
    already_withdrawn = add_quote(app, ids["request"], withdrawn_provider, QuoteStatus.WITHDRAWN)

    login(client, "one@example.com")
    before = client.get(f"/requests/{ids['request']}/quotes")
    assert before.data.count(b"Accept quote") == 2
    assert before.data.count(b"Reject quote") == 2
    response = decision(client, ids["request"], selected, "accept")
    assert response.status_code == 302
    assert b"Quote accepted" in client.get(response.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, selected).status == QuoteStatus.ACCEPTED
        assert db.get(Quote, other_pending).status == QuoteStatus.REJECTED
        assert db.get(Quote, already_rejected).status == QuoteStatus.REJECTED
        assert db.get(Quote, already_withdrawn).status == QuoteStatus.WITHDRAWN
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.ACCEPTED
    after = client.get(f"/requests/{ids['request']}/quotes")
    assert b"Accept quote" not in after.data
    assert b"Reject quote" not in after.data


def test_customer_rejects_one_pending_quote_and_request_stays_open(app, client):
    ids = seed(app)
    rejected = add_quote(app, ids["request"], ids["provider"])
    pending = add_quote(app, ids["request"], ids["other_provider"])
    login(client, "one@example.com")
    response = decision(client, ids["request"], rejected, "reject")
    assert response.status_code == 302
    assert b"Quote rejected" in client.get(response.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, rejected).status == QuoteStatus.REJECTED
        assert db.get(Quote, pending).status == QuoteStatus.PENDING
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.OPEN


@pytest.mark.parametrize("action", ["accept", "reject"])
def test_other_customer_and_provider_cannot_decide(app, client, action):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    path = f"/requests/{ids['request']}/quotes/{quote_id}/{action}"
    assert client.post(path, data={"csrf_token": token(client, "/login")}).status_code == 302
    login(client, "two@example.com")
    assert client.post(path, data={"csrf_token": token(client, "/dashboard")}).status_code == 404
    switch_user(client, "provider1@example.com")
    assert client.post(path, data={"csrf_token": token(client, "/dashboard")}).status_code == 403
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, quote_id).status == QuoteStatus.PENDING
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.OPEN


@pytest.mark.parametrize("request_status", [
    ServiceRequestStatus.QUOTED, ServiceRequestStatus.ACCEPTED,
    ServiceRequestStatus.COMPLETED, ServiceRequestStatus.CANCELLED,
])
def test_closed_request_cannot_accept_quote(app, client, request_status):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    with Session(app.extensions["db_engine"]) as db:
        db.get(ServiceRequest, ids["request"]).status = request_status
        db.commit()
    login(client, "one@example.com")
    response = decision(client, ids["request"], quote_id, "accept")
    assert response.status_code == 302
    assert b"no longer open" in client.get(response.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, quote_id).status == QuoteStatus.PENDING
        assert db.get(ServiceRequest, ids["request"]).status == request_status


@pytest.mark.parametrize("status", [
    QuoteStatus.ACCEPTED, QuoteStatus.REJECTED, QuoteStatus.WITHDRAWN,
])
@pytest.mark.parametrize("action", ["accept", "reject"])
def test_non_pending_quote_cannot_be_decided_again(app, client, status, action):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"], status=status)
    login(client, "one@example.com")
    response = decision(client, ids["request"], quote_id, action)
    assert response.status_code == 302
    assert b"no longer pending" in client.get(response.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, quote_id).status == status
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.OPEN


def test_second_acceptance_attempt_is_stale_and_preserves_first_decision(app, client):
    ids = seed(app)
    first = add_quote(app, ids["request"], ids["provider"])
    second = add_quote(app, ids["request"], ids["other_provider"])
    login(client, "one@example.com")
    assert decision(client, ids["request"], first, "accept").status_code == 302
    stale = decision(client, ids["request"], second, "accept")
    assert stale.status_code == 302
    assert b"no longer open" in client.get(stale.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, first).status == QuoteStatus.ACCEPTED
        assert db.get(Quote, second).status == QuoteStatus.REJECTED


def test_provider_cannot_withdraw_after_request_closes(app, client):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    with Session(app.extensions["db_engine"]) as db:
        db.get(ServiceRequest, ids["request"]).status = ServiceRequestStatus.ACCEPTED
        db.commit()
    login(client, "provider1@example.com")
    detail = client.get(f"/provider/quotes/{quote_id}")
    assert b"Withdraw quote" not in detail.data
    response = client.post(
        f"/provider/quotes/{quote_id}/withdraw",
        data={"csrf_token": token(client, "/dashboard")},
    )
    assert response.status_code == 409
    assert b"only while the request is open" in response.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, quote_id).status == QuoteStatus.PENDING


def test_unknown_or_mismatched_quote_decision_returns_404(app, client):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    with Session(app.extensions["db_engine"]) as db:
        other = ServiceRequest(
            customer_id=ids["customer"], category_id=ids["category"],
            title="Other job", description="Other work", location="South",
        )
        db.add(other)
        db.commit()
        other_id = other.id
    login(client, "one@example.com")
    csrf = token(client, "/dashboard")
    assert client.post(f"/requests/99999/quotes/{quote_id}/accept", data={"csrf_token": csrf}).status_code == 404
    assert client.post(f"/requests/{ids['request']}/quotes/99999/reject", data={"csrf_token": csrf}).status_code == 404
    assert client.post(f"/requests/{other_id}/quotes/{quote_id}/accept", data={"csrf_token": csrf}).status_code == 404


def test_quote_decisions_are_post_only_and_require_csrf(app, client):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    login(client, "one@example.com")
    for action in ("accept", "reject"):
        path = f"/requests/{ids['request']}/quotes/{quote_id}/{action}"
        assert client.get(path).status_code == 405
        assert client.post(path).status_code == 400
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, quote_id).status == QuoteStatus.PENDING


def test_acceptance_rolls_back_if_second_update_fails(app, client, monkeypatch):
    ids = seed(app)
    first = add_quote(app, ids["request"], ids["provider"])
    second = add_quote(app, ids["request"], ids["other_provider"])
    login(client, "one@example.com")
    csrf = token(client, f"/requests/{ids['request']}/quotes")
    original_execute = Session.execute
    quote_updates = 0

    def fail_second_quote_update(db, statement, *args, **kwargs):
        nonlocal quote_updates
        if str(statement).startswith("UPDATE quotes"):
            quote_updates += 1
            if quote_updates == 2:
                raise SQLAlchemyError("simulated database failure")
        return original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", fail_second_quote_update)
    response = client.post(
        f"/requests/{ids['request']}/quotes/{first}/accept",
        data={"csrf_token": csrf},
    )
    assert response.status_code == 302
    assert b"No changes were saved" in client.get(response.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(Quote, first).status == QuoteStatus.PENDING
        assert db.get(Quote, second).status == QuoteStatus.PENDING
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.OPEN


def accepted_request(app, ids):
    quote_id = add_quote(app, ids["request"], ids["provider"], QuoteStatus.ACCEPTED)
    with Session(app.extensions["db_engine"]) as db:
        db.get(ServiceRequest, ids["request"]).status = ServiceRequestStatus.ACCEPTED
        db.commit()
    return quote_id


def complete(client, request_id, token_path, **extra):
    return client.post(
        f"/requests/{request_id}/complete",
        data={**extra, "csrf_token": token(client, token_path)},
    )


def test_owning_customer_completes_accepted_request(app, client):
    ids = seed(app)
    quote_id = accepted_request(app, ids)
    login(client, "one@example.com")
    detail_path = f"/requests/{ids['request']}"
    assert b"Mark request completed" in client.get(detail_path).data
    response = complete(client, ids["request"], detail_path, provider_id=str(ids["other_provider"]))
    assert response.status_code == 302
    assert response.location.endswith(detail_path)
    detail = client.get(response.location)
    assert b"Request completed" in detail.data
    assert b"Mark request completed" not in detail.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.COMPLETED
        assert db.get(Quote, quote_id).status == QuoteStatus.ACCEPTED


def test_accepted_provider_completes_request(app, client):
    ids = seed(app)
    quote_id = accepted_request(app, ids)
    login(client, "provider1@example.com")
    quote_path = f"/provider/quotes/{quote_id}"
    assert b"Mark request completed" in client.get(quote_path).data
    response = complete(client, ids["request"], quote_path, customer_id=str(ids["other_customer"]))
    assert response.status_code == 302
    assert response.location.endswith(quote_path)
    detail = client.get(response.location)
    assert b"Request completed" in detail.data
    assert b"Mark request completed" not in detail.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.COMPLETED


def test_other_quote_provider_cannot_complete_accepted_request(app, client):
    ids = seed(app)
    accepted_request(app, ids)
    other_quote = add_quote(app, ids["request"], ids["other_provider"], QuoteStatus.REJECTED)
    login(client, "provider2@example.com")
    quote_path = f"/provider/quotes/{other_quote}"
    assert b"Mark request completed" not in client.get(quote_path).data
    assert complete(client, ids["request"], quote_path).status_code == 404
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.ACCEPTED


def test_unrelated_accounts_cannot_complete_request(app, client):
    ids = seed(app)
    accepted_request(app, ids)
    login(client, "two@example.com")
    assert complete(client, ids["request"], "/dashboard").status_code == 404
    switch_user(client, "provider2@example.com")
    assert complete(client, ids["request"], "/dashboard").status_code == 404
    switch_user(client, "admin@example.com")
    assert complete(client, ids["request"], "/dashboard").status_code == 403
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.ACCEPTED


@pytest.mark.parametrize("status", [
    ServiceRequestStatus.OPEN, ServiceRequestStatus.CANCELLED,
    ServiceRequestStatus.COMPLETED, ServiceRequestStatus.QUOTED,
])
def test_request_in_other_status_cannot_be_completed(app, client, status):
    ids = seed(app)
    quote_id = accepted_request(app, ids)
    with Session(app.extensions["db_engine"]) as db:
        db.get(ServiceRequest, ids["request"]).status = status
        db.commit()
    login(client, "one@example.com")
    detail_path = f"/requests/{ids['request']}"
    assert b"Mark request completed" not in client.get(detail_path).data
    response = complete(client, ids["request"], detail_path)
    assert response.status_code == 302
    assert b"Only accepted requests can be completed" in client.get(response.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == status
        assert db.get(Quote, quote_id).status == QuoteStatus.ACCEPTED


def test_completed_request_cannot_be_completed_twice(app, client):
    ids = seed(app)
    accepted_request(app, ids)
    login(client, "one@example.com")
    detail_path = f"/requests/{ids['request']}"
    assert complete(client, ids["request"], detail_path).status_code == 302
    stale = complete(client, ids["request"], detail_path)
    assert stale.status_code == 302
    assert b"Only accepted requests can be completed" in client.get(stale.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.COMPLETED


def test_completion_requires_one_accepted_quote_for_request(app, client):
    ids = seed(app)
    accepted_request(app, ids)
    add_quote(app, ids["request"], ids["other_provider"], QuoteStatus.ACCEPTED)
    login(client, "one@example.com")
    response = complete(client, ids["request"], f"/requests/{ids['request']}")
    assert response.status_code == 302
    assert b"no single accepted quote" in client.get(response.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.ACCEPTED


def test_customer_cancels_open_request_and_preserves_quotes(app, client):
    ids = seed(app)
    quote_id = add_quote(app, ids["request"], ids["provider"])
    login(client, "one@example.com")
    detail_path = f"/requests/{ids['request']}"
    assert b"Cancel request" in client.get(detail_path).data
    response = client.post(
        f"{detail_path}/cancel", data={"csrf_token": token(client, detail_path)}
    )
    assert response.status_code == 302
    detail = client.get(response.location)
    assert b"Request cancelled" in detail.data
    assert b"Cancel request" not in detail.data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.CANCELLED
        assert db.get(Quote, quote_id).status == QuoteStatus.PENDING


@pytest.mark.parametrize("status", [ServiceRequestStatus.ACCEPTED, ServiceRequestStatus.COMPLETED])
def test_accepted_or_completed_request_cannot_be_cancelled(app, client, status):
    ids = seed(app)
    accepted_request(app, ids)
    with Session(app.extensions["db_engine"]) as db:
        db.get(ServiceRequest, ids["request"]).status = status
        db.commit()
    login(client, "one@example.com")
    assert client.post(
        f"/requests/{ids['request']}/cancel",
        data={"csrf_token": token(client, f"/requests/{ids['request']}")},
    ).status_code == 409
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == status


def test_completion_is_post_only_and_csrf_protected(app, client):
    ids = seed(app)
    accepted_request(app, ids)
    path = f"/requests/{ids['request']}/complete"
    assert client.get(path).status_code == 405
    login(client, "one@example.com")
    assert client.get(path).status_code == 405
    assert client.post(path).status_code == 400
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.ACCEPTED


def test_completion_rolls_back_when_commit_fails(app, client, monkeypatch):
    ids = seed(app)
    accepted_request(app, ids)
    login(client, "one@example.com")
    detail_path = f"/requests/{ids['request']}"
    csrf = token(client, detail_path)

    def failed_commit(_db):
        raise SQLAlchemyError("simulated commit failure")

    monkeypatch.setattr(Session, "commit", failed_commit)
    response = client.post(
        f"{detail_path}/complete", data={"csrf_token": csrf}
    )
    assert response.status_code == 302
    assert b"No change was saved" in client.get(response.location).data
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.ACCEPTED
