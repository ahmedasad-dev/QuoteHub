"""Provider quote submission and private quote views."""

from decimal import Decimal
import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from .auth import role_required
from .models import Quote, QuoteStatus, ServiceRequest, ServiceRequestStatus, User, UserRole
from .service_requests import PAGE_SIZE, owned_request, page_number


quotes = Blueprint("quotes", __name__)
MAX_AMOUNT = Decimal("9999999999.99")  # Numeric(12, 2)


def quote_fields():
    return {
        "amount": request.form.get("amount", "").strip(),
        "message": request.form.get("message", "").strip(),
    }


def validate_quote(fields):
    raw_amount = fields["amount"]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", raw_amount):
        return "Enter a positive amount with at most two decimal places.", None
    if len(raw_amount) > 13:
        return "Amount must be at most 9,999,999,999.99.", None
    amount = Decimal(raw_amount)
    if amount <= 0:
        return "Amount must be greater than zero.", None
    if amount > MAX_AMOUNT:
        return "Amount must be at most 9,999,999,999.99.", None
    if len(fields["message"]) > 2000:
        return "Message must be 2,000 characters or fewer.", None
    return None, amount


def quote_form_response(item, fields, error=None, status=200):
    return render_template("quote_form.html", item=item, form_data=fields, error=error), status


def owned_quote(db, quote_id):
    quote = db.scalar(
        select(Quote)
        .options(joinedload(Quote.service_request).joinedload(ServiceRequest.category))
        .where(Quote.id == quote_id, Quote.provider_id == current_user.id)
    )
    if quote is None:
        abort(404)
    return quote


@quotes.route("/provider/requests/<int:request_id>/quote", methods=["GET", "POST"])
@role_required(UserRole.PROVIDER)
def submit_quote(request_id):
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            statement = select(ServiceRequest).where(ServiceRequest.id == request_id)
            if request.method == "POST":
                statement = statement.with_for_update()
            item = db.scalar(statement)
            if item is None:
                abort(404)
            if item.status != ServiceRequestStatus.OPEN:
                abort(409, description="Only open requests can receive quotes.")
            if item.customer_id == current_user.id:
                abort(403)

            existing = db.scalar(
                select(Quote.id).where(
                    Quote.service_request_id == request_id,
                    Quote.provider_id == current_user.id,
                )
            )
            if existing is not None:
                return quote_form_response(
                    item, {}, "You have already submitted a quote for this request.", 409
                )
            if request.method == "GET":
                return quote_form_response(item, {})

            fields = quote_fields()
            error, amount = validate_quote(fields)
            if error:
                return quote_form_response(item, fields, error, 400)

            quote = Quote(
                service_request_id=request_id,
                provider_id=current_user.id,
                amount=amount,
                message=fields["message"] or None,
                status=QuoteStatus.PENDING,
            )
            db.add(quote)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                duplicate = db.scalar(
                    select(Quote.id).where(
                        Quote.service_request_id == request_id,
                        Quote.provider_id == current_user.id,
                    )
                )
                message = (
                    "You have already submitted a quote for this request."
                    if duplicate is not None else
                    "The request changed while saving. Reload and try again."
                )
                return quote_form_response(item, fields, message, 409)
            quote_id = quote.id
    except SQLAlchemyError:
        current_app.logger.exception("Quote submission database error")
        abort(503)

    flash("Quote submitted.")
    return redirect(url_for("quotes.provider_quote_detail", quote_id=quote_id))


@quotes.get("/provider/quotes")
@role_required(UserRole.PROVIDER)
def provider_quotes():
    page = page_number()
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            items = list(db.scalars(
                select(Quote)
                .options(joinedload(Quote.service_request))
                .where(Quote.provider_id == current_user.id)
                .order_by(Quote.created_at.desc(), Quote.id.desc())
                .limit(PAGE_SIZE + 1)
                .offset((page - 1) * PAGE_SIZE)
            ))
            return render_template(
                "provider_quotes.html", items=items[:PAGE_SIZE],
                page=page, has_next=len(items) > PAGE_SIZE,
            )
    except SQLAlchemyError:
        current_app.logger.exception("Provider quote listing database error")
        abort(503)


@quotes.get("/provider/quotes/<int:quote_id>")
@role_required(UserRole.PROVIDER)
def provider_quote_detail(quote_id):
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            quote = owned_quote(db, quote_id)
            return render_template("provider_quote_detail.html", quote=quote)
    except SQLAlchemyError:
        current_app.logger.exception("Provider quote detail database error")
        abort(503)


@quotes.post("/provider/quotes/<int:quote_id>/withdraw")
@role_required(UserRole.PROVIDER)
def withdraw_quote(quote_id):
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            request_id = db.scalar(
                select(Quote.service_request_id).where(
                    Quote.id == quote_id, Quote.provider_id == current_user.id
                )
            )
            if request_id is None:
                abort(404)
            item = db.scalar(
                select(ServiceRequest).where(ServiceRequest.id == request_id).with_for_update()
            )
            if item is None:
                abort(404)
            quote = owned_quote(db, quote_id)
            if item.status != ServiceRequestStatus.OPEN:
                flash("Quotes can be withdrawn only while the request is open.")
                return render_template("provider_quote_detail.html", quote=quote), 409
            if quote.status != QuoteStatus.PENDING:
                flash("Only pending quotes can be withdrawn.")
                return render_template("provider_quote_detail.html", quote=quote), 409
            result = db.execute(
                update(Quote)
                .where(
                    Quote.id == quote_id,
                    Quote.provider_id == current_user.id,
                    Quote.status == QuoteStatus.PENDING,
                )
                .values(status=QuoteStatus.WITHDRAWN)
            )
            if result.rowcount != 1:
                db.rollback()
                flash("This quote changed. Reload it before withdrawing.")
                return redirect(url_for("quotes.provider_quote_detail", quote_id=quote_id))
            db.commit()
    except SQLAlchemyError:
        current_app.logger.exception("Quote withdrawal database error")
        abort(503)

    flash("Quote withdrawn.")
    return redirect(url_for("quotes.provider_quote_detail", quote_id=quote_id))


@quotes.get("/requests/<int:request_id>/quotes")
@role_required(UserRole.CUSTOMER)
def customer_quotes(request_id):
    page = page_number()
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            item = owned_request(db, request_id)
            entries = list(db.scalars(
                select(Quote)
                .options(joinedload(Quote.provider).joinedload(User.provider_profile))
                .where(Quote.service_request_id == request_id)
                .order_by(Quote.created_at.desc(), Quote.id.desc())
                .limit(PAGE_SIZE + 1)
                .offset((page - 1) * PAGE_SIZE)
            ))
            return render_template(
                "customer_quotes.html", item=item, quotes=entries[:PAGE_SIZE],
                page=page, has_next=len(entries) > PAGE_SIZE,
            )
    except SQLAlchemyError:
        current_app.logger.exception("Customer quote listing database error")
        abort(503)


def decide_quote(request_id, quote_id, *, accept):
    destination = url_for("quotes.customer_quotes", request_id=request_id)
    action = "accept" if accept else "reject"
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            item = db.scalar(
                select(ServiceRequest)
                .where(
                    ServiceRequest.id == request_id,
                    ServiceRequest.customer_id == current_user.id,
                )
                .with_for_update()
            )
            if item is None:
                abort(404)
            quote = db.scalar(
                select(Quote).where(
                    Quote.id == quote_id, Quote.service_request_id == request_id
                )
            )
            if quote is None:
                abort(404)
            if item.status != ServiceRequestStatus.OPEN:
                flash("This request is no longer open. No quote was changed.")
                return redirect(destination)
            if quote.status != QuoteStatus.PENDING:
                flash("This quote is no longer pending. No quote was changed.")
                return redirect(destination)

            try:
                result = db.execute(
                    update(Quote)
                    .where(
                        Quote.id == quote_id,
                        Quote.service_request_id == request_id,
                        Quote.status == QuoteStatus.PENDING,
                    )
                    .values(status=QuoteStatus.ACCEPTED if accept else QuoteStatus.REJECTED)
                )
                if result.rowcount != 1:
                    db.rollback()
                    flash("This quote changed. Reload the list before trying again.")
                    return redirect(destination)

                if accept:
                    db.execute(
                        update(Quote)
                        .where(
                            Quote.service_request_id == request_id,
                            Quote.id != quote_id,
                            Quote.status == QuoteStatus.PENDING,
                        )
                        .values(status=QuoteStatus.REJECTED)
                    )
                    request_result = db.execute(
                        update(ServiceRequest)
                        .where(
                            ServiceRequest.id == request_id,
                            ServiceRequest.customer_id == current_user.id,
                            ServiceRequest.status == ServiceRequestStatus.OPEN,
                        )
                        .values(status=ServiceRequestStatus.ACCEPTED, updated_at=func.now())
                    )
                    if request_result.rowcount != 1:
                        db.rollback()
                        flash("This request changed. Reload the list before trying again.")
                        return redirect(destination)
                db.commit()
            except SQLAlchemyError:
                db.rollback()
                raise
    except SQLAlchemyError:
        current_app.logger.exception("Customer quote decision database error")
        flash(f"Could not {action} the quote. No changes were saved.")
        return redirect(destination)

    flash("Quote accepted." if accept else "Quote rejected.")
    return redirect(destination)


@quotes.post("/requests/<int:request_id>/quotes/<int:quote_id>/accept")
@role_required(UserRole.CUSTOMER)
def accept_quote(request_id, quote_id):
    return decide_quote(request_id, quote_id, accept=True)


@quotes.post("/requests/<int:request_id>/quotes/<int:quote_id>/reject")
@role_required(UserRole.CUSTOMER)
def reject_quote(request_id, quote_id):
    return decide_quote(request_id, quote_id, accept=False)
