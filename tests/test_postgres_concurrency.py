"""Real PostgreSQL concurrency tests; see README for the isolated test database."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import re
import threading
import time
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from quotehub import create_app
from quotehub.models import (
    Base, Category, Quote, QuoteStatus, ServiceRequest, ServiceRequestStatus,
    User, UserRole,
)


pytestmark = pytest.mark.postgres
TEST_DATABASE_NAME = "quotehub_concurrency_test"


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


@pytest.fixture(scope="session")
def postgres_url():
    if os.environ.get("QUOTEHUB_ENABLE_POSTGRES_TESTS") != "1":
        pytest.skip("Set QUOTEHUB_ENABLE_POSTGRES_TESTS=1 to opt in to PostgreSQL tests")
    url = os.environ.get("QUOTEHUB_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.fail("QUOTEHUB_TEST_DATABASE_URL must point to the isolated test database")
    try:
        parsed = urlsplit(url)
    except ValueError:
        pytest.fail("QUOTEHUB_TEST_DATABASE_URL is not a valid URL")
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        pytest.fail("PostgreSQL concurrency tests require a PostgreSQL URL")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1", "postgres-test"}:
        pytest.fail("The test URL must target the local disposable PostgreSQL service")
    if parsed.username != "quotehub_test":
        pytest.fail("The test URL must use the non-superuser quotehub_test login")
    if _database_name(url) != TEST_DATABASE_NAME:
        pytest.fail(f"Refusing test cleanup outside database {TEST_DATABASE_NAME!r}")
    runtime_url = os.environ.get("DATABASE_URL", "").strip()
    if runtime_url and _database_name(runtime_url) == TEST_DATABASE_NAME:
        pytest.fail("DATABASE_URL must not point to the dedicated test database")
    migration_url = os.environ.get("MIGRATION_DATABASE_URL", "").strip()
    if migration_url and migration_url != url:
        pytest.fail("MIGRATION_DATABASE_URL must exactly match QUOTEHUB_TEST_DATABASE_URL during test runs")
    if os.environ.get("QUOTEHUB_ALLOW_TEST_DATABASE_RESET") != "1":
        pytest.fail("Set QUOTEHUB_ALLOW_TEST_DATABASE_RESET=1 to allow cleanup of the dedicated test database")
    return url


@pytest.fixture(scope="session")
def app(postgres_url):
    application = create_app({
        "TESTING": True,
        "DATABASE_URL": postgres_url,
        "SECRET_KEY": "postgres-integration-test-secret-only",
        "SESSION_COOKIE_SECURE": False,
        "RATELIMIT_STORAGE_URI": "memory://",
    })
    engine = application.extensions["db_engine"]
    # This destructive schema setup is guarded above and targets only the exact
    # database name reserved for the disposable integration-test service.
    if engine.url.database != TEST_DATABASE_NAME:
        pytest.fail("Refusing to initialize an unexpected PostgreSQL database")
    with engine.begin() as connection:
        connection.execute(text("DROP FUNCTION IF EXISTS fail_quotehub_acceptance() CASCADE"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield application
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def empty_test_database(app):
    engine = app.extensions["db_engine"]
    with engine.begin() as connection:
        connection.execute(text(
            "TRUNCATE TABLE quotes, service_requests, provider_profiles, categories, users "
            "RESTART IDENTITY CASCADE"
        ))


def seed(app, *, accepted=False, with_quotes=True):
    engine = app.extensions["db_engine"]
    with Session(engine) as db:
        password_hash = generate_password_hash("integration-password-123")
        customer = User(name="Customer", email="customer@example.test", password_hash=password_hash, role=UserRole.CUSTOMER)
        provider_one = User(name="Provider One", email="one@example.test", password_hash=password_hash, role=UserRole.PROVIDER)
        provider_two = User(name="Provider Two", email="two@example.test", password_hash=password_hash, role=UserRole.PROVIDER)
        db.add_all([customer, provider_one, provider_two])
        category = Category(name="Plumbing", slug="plumbing")
        db.add(category)
        db.flush()
        item = ServiceRequest(
            customer_id=customer.id, category_id=category.id,
            title="Repair a sink", description="A leaking sink", location="North",
            status=ServiceRequestStatus.ACCEPTED if accepted else ServiceRequestStatus.OPEN,
        )
        db.add(item)
        db.flush()
        first_quote = second_quote = None
        if with_quotes:
            first_quote = Quote(
                service_request_id=item.id, provider_id=provider_one.id,
                amount=Decimal("100.00"), message="First", status=(
                    QuoteStatus.ACCEPTED if accepted else QuoteStatus.PENDING
                ),
            )
            db.add(first_quote)
            if not accepted:
                second_quote = Quote(
                    service_request_id=item.id, provider_id=provider_two.id,
                    amount=Decimal("110.00"), message="Second", status=QuoteStatus.PENDING,
                )
                db.add(second_quote)
        db.commit()
        return {
            "customer": customer.id, "provider_one": provider_one.id,
            "provider_two": provider_two.id, "request": item.id,
            "quote_one": first_quote.id if first_quote is not None else None,
            "quote_two": second_quote.id if second_quote is not None else None,
        }


def csrf_token(client, path):
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match is not None
    return match.group(1).decode()


def authenticated_client(app, email):
    client = app.test_client()
    token = csrf_token(client, "/login")
    response = client.post("/login", data={
        "email": email, "password": "integration-password-123", "csrf_token": token,
    })
    assert response.status_code == 302
    return client


def _assert_lock_wait(app, *, timeout=8):
    """Wait for PostgreSQL itself to report a competing transaction blocked on a lock."""
    deadline = time.monotonic() + timeout
    engine = app.extensions["db_engine"]
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.scalar(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND state = 'active' AND wait_event_type = 'Lock'"
            ))
        if waiting:
            return
        threading.Event().wait(0.02)
    pytest.fail("PostgreSQL did not report the competing operation waiting on a row lock")


def run_overlapping_requests(app, first_client, first_path, first_data, second_client, second_path, second_data):
    """Pause one route after it acquires FOR UPDATE, then prove the other blocks in PostgreSQL."""
    engine = app.extensions["db_engine"]
    lock_acquired = threading.Event()
    release_first = threading.Event()
    claim_guard = threading.Lock()
    claimed = False

    def pause_first_lock(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal claimed
        normalized = statement.upper()
        if "FOR UPDATE" not in normalized or "SERVICE_REQUESTS" not in normalized:
            return
        with claim_guard:
            if claimed:
                return
            claimed = True
        lock_acquired.set()
        if not release_first.wait(10):
            raise AssertionError("Timed out waiting for the test to release the locked transaction")

    event.listen(engine, "after_cursor_execute", pause_first_lock)
    def post(client, path, data):
        return client.post(path, data=data)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, first_client, first_path, first_data)
        second = None
        try:
            assert lock_acquired.wait(8), "No route acquired the expected PostgreSQL row lock"
            second = pool.submit(post, second_client, second_path, second_data)
            _assert_lock_wait(app)
        finally:
            release_first.set()
        try:
            first_response = first.result(timeout=10)
            if second is None:
                raise AssertionError("The competing request was not started")
            second_response = second.result(timeout=10)
            return first_response, second_response
        finally:
            event.remove(engine, "after_cursor_execute", pause_first_lock)


def test_duplicate_quote_submissions_overlap_and_unique_constraint_is_final_guard(app):
    ids = seed(app, with_quotes=False)
    one = authenticated_client(app, "one@example.test")
    another_session = authenticated_client(app, "one@example.test")
    quote_path = f"/provider/requests/{ids['request']}/quote"
    data_one = {"amount": "90.00", "message": "One", "csrf_token": csrf_token(one, "/dashboard")}
    data_two = {"amount": "91.00", "message": "Two", "csrf_token": csrf_token(another_session, "/dashboard")}
    responses = run_overlapping_requests(app, one, quote_path, data_one, another_session, quote_path, data_two)
    assert sorted(response.status_code for response in responses) == [302, 409]
    with Session(app.extensions["db_engine"]) as db:
        assert db.scalar(select(func.count(Quote.id)).where(
            Quote.service_request_id == ids["request"], Quote.provider_id == ids["provider_one"]
        )) == 1

    # Bypass the route lock deliberately to prove PostgreSQL's unique constraint
    # rejects an actual competing insert and leaves the losing session reusable.
    barrier = threading.Barrier(2)

    def insert_duplicate(amount):
        with Session(app.extensions["db_engine"]) as db:
            db.add(Quote(
                service_request_id=ids["request"], provider_id=ids["provider_two"],
                amount=Decimal(amount), status=QuoteStatus.PENDING,
            ))
            barrier.wait(timeout=8)
            try:
                db.commit()
                return "committed"
            except IntegrityError:
                db.rollback()
                # A failed SQLAlchemy transaction is usable again after rollback.
                assert db.scalar(select(Quote.id).where(Quote.provider_id == ids["provider_two"])) is not None
                return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(insert_duplicate, ("92.00", "93.00")))
    assert sorted(outcomes) == ["committed", "duplicate"]
    with Session(app.extensions["db_engine"]) as db:
        for provider_id in (ids["provider_one"], ids["provider_two"]):
            count = db.scalar(select(func.count(Quote.id)).where(
                Quote.service_request_id == ids["request"], Quote.provider_id == provider_id
            ))
            assert count == 1


def test_competing_quote_acceptance_serializes_and_rejects_other_pending_quotes(app):
    ids = seed(app)
    first = authenticated_client(app, "customer@example.test")
    second = authenticated_client(app, "customer@example.test")
    quote_list = f"/requests/{ids['request']}/quotes"
    first_path = f"{quote_list}/{ids['quote_one']}/accept"
    second_path = f"{quote_list}/{ids['quote_two']}/accept"
    responses = run_overlapping_requests(
        app, first, first_path, {"csrf_token": csrf_token(first, quote_list)},
        second, second_path, {"csrf_token": csrf_token(second, quote_list)},
    )
    assert all(response.status_code == 302 for response in responses)
    with Session(app.extensions["db_engine"]) as db:
        item = db.get(ServiceRequest, ids["request"])
        statuses = {quote.id: quote.status for quote in db.scalars(select(Quote))}
        assert item.status == ServiceRequestStatus.ACCEPTED
        assert list(statuses.values()).count(QuoteStatus.ACCEPTED) == 1
        assert list(statuses.values()).count(QuoteStatus.REJECTED) == 1


def test_acceptance_and_cancellation_compete_without_invalid_transition(app):
    ids = seed(app)
    accepting = authenticated_client(app, "customer@example.test")
    cancelling = authenticated_client(app, "customer@example.test")
    quote_list = f"/requests/{ids['request']}/quotes"
    accept_path = f"{quote_list}/{ids['quote_one']}/accept"
    detail = f"/requests/{ids['request']}"
    cancel_path = f"{detail}/cancel"
    responses = run_overlapping_requests(
        app, accepting, accept_path, {"csrf_token": csrf_token(accepting, quote_list)},
        cancelling, cancel_path, {"csrf_token": csrf_token(cancelling, detail)},
    )
    assert sorted(response.status_code for response in responses) == [302, 409]
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.ACCEPTED
        assert db.get(Quote, ids["quote_one"]).status == QuoteStatus.ACCEPTED
        assert db.get(Quote, ids["quote_two"]).status == QuoteStatus.REJECTED


def test_competing_customer_and_provider_completion_serializes(app):
    ids = seed(app, accepted=True)
    customer = authenticated_client(app, "customer@example.test")
    provider = authenticated_client(app, "one@example.test")
    detail = f"/requests/{ids['request']}"
    quote_detail = f"/provider/quotes/{ids['quote_one']}"
    responses = run_overlapping_requests(
        app, customer, f"{detail}/complete", {"csrf_token": csrf_token(customer, detail)},
        provider, f"{detail}/complete", {"csrf_token": csrf_token(provider, quote_detail)},
    )
    assert all(response.status_code == 302 for response in responses)
    with Session(app.extensions["db_engine"]) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.COMPLETED
        assert db.get(Quote, ids["quote_one"]).status == QuoteStatus.ACCEPTED


def test_acceptance_database_failure_rolls_back_all_updates_and_session_recovers(app):
    ids = seed(app)
    engine = app.extensions["db_engine"]
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE FUNCTION fail_quotehub_acceptance() RETURNS trigger AS $$
            BEGIN
              IF NEW.status = 'ACCEPTED' THEN
                RAISE EXCEPTION 'forced integration-test failure';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """))
        connection.execute(text("""
            CREATE TRIGGER fail_quotehub_acceptance_trigger
            BEFORE UPDATE ON service_requests
            FOR EACH ROW EXECUTE FUNCTION fail_quotehub_acceptance()
        """))

    client = authenticated_client(app, "customer@example.test")
    quote_list = f"/requests/{ids['request']}/quotes"
    response = client.post(
        f"{quote_list}/{ids['quote_one']}/accept",
        data={"csrf_token": csrf_token(client, quote_list)},
    )
    assert response.status_code == 302
    assert b"No changes were saved" in client.get(response.location).data
    with Session(engine) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.OPEN
        assert db.get(Quote, ids["quote_one"]).status == QuoteStatus.PENDING
        assert db.get(Quote, ids["quote_two"]).status == QuoteStatus.PENDING
        # New sessions continue to work after a failed route transaction.
        assert db.scalar(select(Quote.id)) is not None

    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER fail_quotehub_acceptance_trigger ON service_requests"))
        connection.execute(text("DROP FUNCTION fail_quotehub_acceptance()"))
    retry = client.post(
        f"{quote_list}/{ids['quote_one']}/accept",
        data={"csrf_token": csrf_token(client, quote_list)},
    )
    assert retry.status_code == 302
    with Session(engine) as db:
        assert db.get(ServiceRequest, ids["request"]).status == ServiceRequestStatus.ACCEPTED
        assert db.get(Quote, ids["quote_one"]).status == QuoteStatus.ACCEPTED
        assert db.get(Quote, ids["quote_two"]).status == QuoteStatus.REJECTED


def test_postgres_partial_index_rejects_a_second_accepted_quote(app):
    ids = seed(app, accepted=True)
    engine = app.extensions["db_engine"]
    indexes = {index["name"]: index for index in inspect(engine).get_indexes("quotes")}
    assert "uq_quotes_one_accepted_per_request" in indexes

    with Session(engine) as db:
        db.add(Quote(
            service_request_id=ids["request"], provider_id=ids["provider_two"],
            amount=Decimal("120.00"), status=QuoteStatus.ACCEPTED,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.scalar(select(func.count(Quote.id)).where(
            Quote.service_request_id == ids["request"], Quote.status == QuoteStatus.ACCEPTED
        )) == 1


def test_z_migration_preserves_data_and_refuses_legacy_conflicts(app, postgres_url, monkeypatch):
    """Exercise the real Alembic upgrade from the current production revision."""
    engine = app.extensions["db_engine"]
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    monkeypatch.setenv("MIGRATION_DATABASE_URL", postgres_url)
    alembic_config = Config("alembic.ini")
    command.upgrade(alembic_config, "7c504dafc2a9")

    with Session(engine) as db:
        customer = User(
            name="Legacy Customer", email="legacy-customer@example.test",
            password_hash="test-hash", role=UserRole.CUSTOMER,
        )
        providers = [
            User(name=f"Legacy Provider {number}", email=f"legacy-{number}@example.test",
                 password_hash="test-hash", role=UserRole.PROVIDER)
            for number in (1, 2)
        ]
        category = Category(name="Legacy Category", slug="legacy-category")
        db.add_all([customer, *providers, category])
        db.flush()
        item = ServiceRequest(
            customer_id=customer.id, category_id=category.id,
            title="Existing request", description="Preserve this row", location="Central",
            status=ServiceRequestStatus.ACCEPTED,
        )
        db.add(item)
        db.flush()
        quotes = [
            Quote(
                service_request_id=item.id, provider_id=provider.id,
                amount=Decimal("80.00"), status=QuoteStatus.ACCEPTED,
            )
            for provider in providers
        ]
        db.add_all(quotes)
        db.commit()
        request_id = item.id
        quote_ids = [quote.id for quote in quotes]

    with pytest.raises(RuntimeError, match="Reconcile the existing quote statuses"):
        command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == "7c504dafc2a9"
        assert connection.scalar(text("SELECT count(*) FROM quotes")) == 2

    # Resolve the legacy conflict explicitly, retry the migration, and verify
    # that its rows remain present after adding the partial unique index.
    with Session(engine) as db:
        db.get(Quote, quote_ids[1]).status = QuoteStatus.REJECTED
        db.commit()
    command.upgrade(alembic_config, "head")
    with engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == "b61f4e0a2c9d3"
        assert connection.scalar(text("SELECT count(*) FROM service_requests WHERE id = :id"), {"id": request_id}) == 1
        assert connection.scalar(text("SELECT count(*) FROM quotes WHERE service_request_id = :id"), {"id": request_id}) == 2
    indexes = {index["name"] for index in inspect(engine).get_indexes("quotes")}
    assert "uq_quotes_one_accepted_per_request" in indexes
