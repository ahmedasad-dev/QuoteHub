"""Bounded, role-scoped dashboard queries."""

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from .models import Quote, QuoteStatus, ServiceRequest, ServiceRequestStatus


RECENT_LIMIT = 6


def customer_dashboard_data(db, customer_id):
    status_counts = {status: count for status, count in db.execute(
        select(ServiceRequest.status, func.count(ServiceRequest.id))
        .where(ServiceRequest.customer_id == customer_id)
        .group_by(ServiceRequest.status)
    )}
    recent = list(db.scalars(
        select(ServiceRequest)
        .options(joinedload(ServiceRequest.category))
        .where(ServiceRequest.customer_id == customer_id)
        .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())
        .limit(RECENT_LIMIT)
    ))
    quote_counts = {}
    if recent:
        quote_counts = {request_id: count for request_id, count in db.execute(
            select(Quote.service_request_id, func.count(Quote.id))
            .where(Quote.service_request_id.in_([item.id for item in recent]))
            .group_by(Quote.service_request_id)
        )}

    return {
        "customer_metrics": {
            "total": sum(status_counts.values()),
            "open": status_counts.get(ServiceRequestStatus.OPEN, 0),
            "accepted": status_counts.get(ServiceRequestStatus.ACCEPTED, 0),
            "completed": status_counts.get(ServiceRequestStatus.COMPLETED, 0),
        },
        "recent_requests": recent,
        "request_quote_counts": quote_counts,
    }


def provider_dashboard_data(db, provider_id):
    already_quoted = select(Quote.id).where(
        Quote.service_request_id == ServiceRequest.id,
        Quote.provider_id == provider_id,
    ).exists()
    open_opportunities = db.scalar(
        select(func.count(ServiceRequest.id)).where(
            ServiceRequest.status == ServiceRequestStatus.OPEN,
            ServiceRequest.customer_id != provider_id,
            ~already_quoted,
        )
    )
    status_rows = db.execute(
        select(Quote.status, ServiceRequest.status, func.count(Quote.id))
        .join(Quote.service_request)
        .where(Quote.provider_id == provider_id)
        .group_by(Quote.status, ServiceRequest.status)
    ).all()
    pending = sum(count for quote_status, _, count in status_rows if quote_status == QuoteStatus.PENDING)
    accepted = sum(count for quote_status, _, count in status_rows if quote_status == QuoteStatus.ACCEPTED)
    completed = sum(
        count for quote_status, request_status, count in status_rows
        if quote_status == QuoteStatus.ACCEPTED and request_status == ServiceRequestStatus.COMPLETED
    )
    recent = list(db.scalars(
        select(Quote)
        .options(joinedload(Quote.service_request))
        .where(Quote.provider_id == provider_id)
        .order_by(Quote.created_at.desc(), Quote.id.desc())
        .limit(RECENT_LIMIT)
    ))
    return {
        "provider_metrics": {
            "opportunities": open_opportunities,
            "pending": pending,
            "accepted": accepted,
            "completed": completed,
        },
        "recent_quotes": recent,
    }
