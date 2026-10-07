"""Database model tests using an isolated in-memory database."""

import unittest
from decimal import Decimal

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quotehub.models import (
    Base,
    Category,
    ProviderProfile,
    Quote,
    QuoteStatus,
    ServiceRequest,
    ServiceRequestStatus,
    User,
    UserRole,
)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def user(self, email, role=UserRole.CUSTOMER):
        user = User(
            name="Test User",
            email=email,
            password_hash="stored-hash-value",
            role=role,
        )
        self.session.add(user)
        self.session.flush()
        return user

    def category(self):
        category = Category(name="Plumbing", slug="plumbing")
        self.session.add(category)
        self.session.flush()
        return category

    def service_request(self, customer, category):
        request = ServiceRequest(
            customer=customer,
            category=category,
            title="Repair a tap",
            description="Kitchen tap is leaking",
            location="Central",
            budget=Decimal("125.25"),
        )
        self.session.add(request)
        self.session.flush()
        return request

    def test_user_email_is_normalized_and_unique_case_insensitively(self):
        first = self.user("  Person@Example.COM  ")
        self.assertEqual(first.email, "person@example.com")
        self.assertIsNotNone(first.created_at)

        self.session.add(
            User(
                name="Other User",
                email="PERSON@example.com",
                password_hash="another-stored-hash",
                role=UserRole.PROVIDER,
            )
        )
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_provider_profile_is_one_to_one(self):
        provider = self.user("provider@example.com", UserRole.PROVIDER)
        profile = ProviderProfile(
            user=provider, business_name="Fix It", location="Central"
        )
        self.session.add(profile)
        self.session.flush()
        self.assertIs(provider.provider_profile, profile)
        self.assertIs(profile.user, provider)

        self.session.add(
            ProviderProfile(
                user_id=provider.id, business_name="Second Business", location="Central"
            )
        )
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_category_name_and_slug_are_unique(self):
        self.category()
        self.session.add(Category(name="Plumbing", slug="different"))
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()

        self.category()
        self.session.add(Category(name="Different", slug="plumbing"))
        with self.assertRaises(IntegrityError):
            self.session.flush()

    def test_service_request_relationships_and_decimal_budget(self):
        customer = self.user("customer@example.com")
        category = self.category()
        request = self.service_request(customer, category)
        self.session.commit()

        found = self.session.scalar(select(ServiceRequest).where(ServiceRequest.id == request.id))
        self.assertIs(found.customer, customer)
        self.assertIs(found.category, category)
        self.assertIn(found, customer.service_requests)
        self.assertIn(found, category.service_requests)
        self.assertEqual(found.budget, Decimal("125.25"))
        self.assertIsInstance(found.budget, Decimal)
        self.assertEqual(found.status, ServiceRequestStatus.OPEN)
        self.assertIsNotNone(found.created_at)
        self.assertIsNotNone(found.updated_at)

    def test_quote_relationships_decimal_amount_and_duplicate_prevention(self):
        customer = self.user("customer@example.com")
        provider = self.user("provider@example.com", UserRole.PROVIDER)
        request = self.service_request(customer, self.category())
        quote = Quote(
            service_request=request,
            provider=provider,
            amount=Decimal("99.95"),
            message="Can do this tomorrow",
        )
        self.session.add(quote)
        self.session.flush()
        self.assertIs(quote.service_request, request)
        self.assertIs(quote.provider, provider)
        self.assertIn(quote, request.quotes)
        self.assertIn(quote, provider.quotes)
        self.assertEqual(quote.amount, Decimal("99.95"))
        self.assertIsInstance(quote.amount, Decimal)
        self.assertEqual(quote.status, QuoteStatus.PENDING)

        self.session.add(
            Quote(
                service_request_id=request.id,
                provider_id=provider.id,
                amount=Decimal("100.00"),
            )
        )
        with self.assertRaises(IntegrityError):
            self.session.flush()


if __name__ == "__main__":
    unittest.main()
