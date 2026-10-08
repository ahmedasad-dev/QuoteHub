"""Phase 2 database models."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from flask_login import UserMixin
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates


class Base(DeclarativeBase):
    pass


class UserRole(StrEnum):
    CUSTOMER = "CUSTOMER"
    PROVIDER = "PROVIDER"
    ADMIN = "ADMIN"


class ServiceRequestStatus(StrEnum):
    OPEN = "OPEN"
    QUOTED = "QUOTED"
    ACCEPTED = "ACCEPTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class QuoteStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


def normalize_email_address(value: str) -> str:
    return value.strip().lower()


class User(UserMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=False, create_constraint=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    provider_profile: Mapped["ProviderProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    service_requests: Mapped[list["ServiceRequest"]] = relationship(
        back_populates="customer", passive_deletes=True
    )
    quotes: Mapped[list["Quote"]] = relationship(
        back_populates="provider", passive_deletes=True
    )

    @validates("email")
    def normalize_email(self, _key: str, value: str) -> str:
        return normalize_email_address(value)


Index("uq_users_email_lower", func.lower(User.email), unique=True)


class ProviderProfile(Base):
    __tablename__ = "provider_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(40))
    location: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="provider_profile")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("name", name="uq_categories_name"),
        UniqueConstraint("slug", name="uq_categories_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    service_requests: Mapped[list["ServiceRequest"]] = relationship(
        back_populates="category", passive_deletes=True
    )


class ServiceRequest(Base):
    __tablename__ = "service_requests"
    __table_args__ = (
        CheckConstraint("budget >= 0", name="ck_service_requests_budget_nonnegative"),
        Index("ix_service_requests_customer_id", "customer_id"),
        Index("ix_service_requests_category_id", "category_id"),
        Index("ix_service_requests_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(String(200), nullable=False)
    budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[ServiceRequestStatus] = mapped_column(
        Enum(
            ServiceRequestStatus,
            name="service_request_status",
            native_enum=False,
            create_constraint=True,
        ),
        default=ServiceRequestStatus.OPEN,
        server_default=ServiceRequestStatus.OPEN.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    customer: Mapped[User] = relationship(back_populates="service_requests")
    category: Mapped[Category] = relationship(back_populates="service_requests")
    quotes: Mapped[list["Quote"]] = relationship(
        back_populates="service_request", passive_deletes=True
    )


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint(
            "service_request_id", "provider_id", name="uq_quotes_request_provider"
        ),
        CheckConstraint("amount > 0", name="ck_quotes_amount_positive"),
        Index("ix_quotes_provider_status", "provider_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_request_id: Mapped[int] = mapped_column(
        ForeignKey("service_requests.id", ondelete="RESTRICT"), nullable=False
    )
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[QuoteStatus] = mapped_column(
        Enum(QuoteStatus, name="quote_status", native_enum=False, create_constraint=True),
        default=QuoteStatus.PENDING,
        server_default=QuoteStatus.PENDING.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    service_request: Mapped[ServiceRequest] = relationship(back_populates="quotes")
    provider: Mapped[User] = relationship(back_populates="quotes")
