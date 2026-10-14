"""Customer request management and provider browsing."""

from decimal import Decimal, InvalidOperation
import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_login import login_required
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from .auth import role_required
from .models import Category, Quote, QuoteStatus, ServiceRequest, ServiceRequestStatus, UserRole
from .search import contains_text


requests = Blueprint("requests", __name__)
PAGE_SIZE = 20
MAX_PAGE = 1000
MAX_BUDGET = Decimal("9999999999.99")  # Numeric(12, 2)


def form_fields():
    return {
        "title": " ".join(request.form.get("title", "").split()),
        "description": request.form.get("description", "").strip(),
        "category_id": request.form.get("category_id", "").strip(),
        "location": " ".join(request.form.get("location", "").split()),
        "budget": request.form.get("budget", "").strip(),
    }


def validate_fields(fields):
    if not fields["title"] or not fields["description"] or not fields["location"]:
        return "Title, description, and location are required.", None, None
    if len(fields["title"]) > 200 or len(fields["location"]) > 200:
        return "Title and location must be 200 characters or fewer.", None, None
    if len(fields["description"]) > 5000:
        return "Description must be 5,000 characters or fewer.", None, None
    if (not fields["category_id"].isascii() or not fields["category_id"].isdecimal()
            or len(fields["category_id"]) > 10):
        return "Choose a valid category.", None, None
    category_id = int(fields["category_id"])
    if category_id < 1:
        return "Choose a valid category.", None, None

    budget = None
    if fields["budget"]:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", fields["budget"]):
            return "Budget must be a non-negative amount with at most two decimal places.", None, None
        try:
            budget = Decimal(fields["budget"])
        except InvalidOperation:
            return "Enter a valid budget.", None, None
        if budget > MAX_BUDGET:
            return "Budget must be at most 9,999,999,999.99.", None, None
    return None, category_id, budget


def categories_for_form(db):
    return list(db.scalars(select(Category).order_by(Category.name)))


def form_response(db, fields, *, editing=False, request_id=None, error=None, status=200):
    return render_template(
        "request_form.html",
        categories=categories_for_form(db),
        form_data=fields,
        editing=editing,
        request_id=request_id,
        error=error,
    ), status


def owned_request(db, request_id):
    item = db.scalar(
        select(ServiceRequest)
        .options(joinedload(ServiceRequest.category))
        .where(ServiceRequest.id == request_id, ServiceRequest.customer_id == current_user.id)
    )
    if item is None:
        abort(404)
    return item


def page_number():
    raw = request.args.get("page", "1")
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 4:
        abort(404)
    page = int(raw)
    if not 1 <= page <= MAX_PAGE:
        abort(404)
    return page


@requests.route("/requests/new", methods=["GET", "POST"])
@role_required(UserRole.CUSTOMER)
def create_request():
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            if request.method == "GET":
                return form_response(db, {})

            fields = form_fields()
            error, category_id, budget = validate_fields(fields)
            if not error and db.get(Category, category_id) is None:
                error = "Choose a valid category."
            if error:
                return form_response(db, fields, error=error, status=400)

            item = ServiceRequest(
                customer_id=current_user.id,
                category_id=category_id,
                title=fields["title"],
                description=fields["description"],
                location=fields["location"],
                budget=budget,
                status=ServiceRequestStatus.OPEN,
            )
            db.add(item)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return form_response(
                    db, fields,
                    error="The request could not be saved because related data changed. Reload and try again.",
                    status=409,
                )
            request_id = item.id
    except SQLAlchemyError:
        current_app.logger.exception("Service request creation database error")
        abort(503)

    flash("Request created.")
    return redirect(url_for("requests.request_detail", request_id=request_id))


@requests.get("/requests")
@role_required(UserRole.CUSTOMER)
def my_requests():
    page = page_number()
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            items = list(db.scalars(
                select(ServiceRequest)
                .options(joinedload(ServiceRequest.category))
                .where(ServiceRequest.customer_id == current_user.id)
                .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())
                .limit(PAGE_SIZE + 1)
                .offset((page - 1) * PAGE_SIZE)
            ))
            return render_template(
                "request_list.html", items=items[:PAGE_SIZE], page=page,
                has_next=len(items) > PAGE_SIZE, provider_view=False,
            )
    except SQLAlchemyError:
        current_app.logger.exception("Customer request listing database error")
        abort(503)


@requests.get("/requests/<int:request_id>")
@role_required(UserRole.CUSTOMER)
def request_detail(request_id):
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            item = owned_request(db, request_id)
            return render_template("request_detail.html", item=item)
    except SQLAlchemyError:
        current_app.logger.exception("Service request detail database error")
        abort(503)


@requests.route("/requests/<int:request_id>/edit", methods=["GET", "POST"])
@role_required(UserRole.CUSTOMER)
def edit_request(request_id):
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            item = owned_request(db, request_id)
            if item.status != ServiceRequestStatus.OPEN:
                abort(409, description="Only open requests can be edited.")
            if request.method == "GET":
                fields = {
                    "title": item.title, "description": item.description,
                    "category_id": str(item.category_id), "location": item.location,
                    "budget": str(item.budget) if item.budget is not None else "",
                }
                return form_response(db, fields, editing=True, request_id=request_id)

            fields = form_fields()
            error, category_id, budget = validate_fields(fields)
            if not error and db.get(Category, category_id) is None:
                error = "Choose a valid category."
            if error:
                return form_response(
                    db, fields, editing=True, request_id=request_id, error=error, status=400
                )

            try:
                result = db.execute(
                    update(ServiceRequest)
                    .where(
                        ServiceRequest.id == request_id,
                        ServiceRequest.customer_id == current_user.id,
                        ServiceRequest.status == ServiceRequestStatus.OPEN,
                    )
                    .values(
                        title=fields["title"], description=fields["description"],
                        category_id=category_id, location=fields["location"],
                        budget=budget, updated_at=func.now(),
                    )
                )
                if result.rowcount != 1:
                    db.rollback()
                    abort(409, description="This request changed. Reload it before editing.")
                db.commit()
            except IntegrityError:
                db.rollback()
                return form_response(
                    db, fields, editing=True, request_id=request_id,
                    error="The request could not be saved because related data changed. Reload and try again.", status=409,
                )
    except SQLAlchemyError:
        current_app.logger.exception("Service request edit database error")
        abort(503)

    flash("Request updated.")
    return redirect(url_for("requests.request_detail", request_id=request_id))


@requests.post("/requests/<int:request_id>/cancel")
@role_required(UserRole.CUSTOMER)
def cancel_request(request_id):
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            item = owned_request(db, request_id)
            if item.status != ServiceRequestStatus.OPEN:
                abort(409, description="Only open requests can be cancelled.")
            result = db.execute(
                update(ServiceRequest)
                .where(
                    ServiceRequest.id == request_id,
                    ServiceRequest.customer_id == current_user.id,
                    ServiceRequest.status == ServiceRequestStatus.OPEN,
                )
                .values(status=ServiceRequestStatus.CANCELLED, updated_at=func.now())
            )
            if result.rowcount != 1:
                db.rollback()
                abort(409, description="This request changed. Reload it before cancelling.")
            db.commit()
    except SQLAlchemyError:
        current_app.logger.exception("Service request cancellation database error")
        abort(503)

    flash("Request cancelled.")
    return redirect(url_for("requests.request_detail", request_id=request_id))


@requests.post("/requests/<int:request_id>/complete")
@login_required
def complete_request(request_id):
    if current_user.role not in {UserRole.CUSTOMER, UserRole.PROVIDER}:
        abort(403)

    destination = (
        url_for("requests.request_detail", request_id=request_id)
        if current_user.role == UserRole.CUSTOMER
        else url_for("quotes.provider_quotes")
    )
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            # PostgreSQL serializes completion with acceptance, cancellation, and quote changes.
            item = db.scalar(
                select(ServiceRequest)
                .where(ServiceRequest.id == request_id)
                .with_for_update()
            )
            if item is None:
                abort(404)
            if current_user.role == UserRole.CUSTOMER and item.customer_id != current_user.id:
                abort(404)

            accepted_quotes = list(db.scalars(
                select(Quote)
                .where(
                    Quote.service_request_id == request_id,
                    Quote.status == QuoteStatus.ACCEPTED,
                )
                .limit(2)
            ))
            if current_user.role == UserRole.PROVIDER:
                if len(accepted_quotes) != 1 or accepted_quotes[0].provider_id != current_user.id:
                    abort(404)
                destination = url_for(
                    "quotes.provider_quote_detail", quote_id=accepted_quotes[0].id
                )

            if item.status != ServiceRequestStatus.ACCEPTED:
                flash("Only accepted requests can be completed. No change was made.")
                return redirect(destination)
            if len(accepted_quotes) != 1:
                flash("This request has no single accepted quote. No change was made.")
                return redirect(destination)

            try:
                result = db.execute(
                    update(ServiceRequest)
                    .where(
                        ServiceRequest.id == request_id,
                        ServiceRequest.status == ServiceRequestStatus.ACCEPTED,
                    )
                    .values(status=ServiceRequestStatus.COMPLETED, updated_at=func.now())
                )
                if result.rowcount != 1:
                    db.rollback()
                    flash("This request changed. Reload it before completing.")
                    return redirect(destination)
                db.commit()
            except SQLAlchemyError:
                db.rollback()
                raise
    except SQLAlchemyError:
        current_app.logger.exception("Service completion database error")
        flash("Could not complete the request. No change was saved.")
        return redirect(destination)

    flash("Request completed.")
    return redirect(destination)


@requests.get("/provider/requests")
@role_required(UserRole.PROVIDER)
def browse_requests():
    page = page_number()
    filters = {
        "q": " ".join(request.args.get("q", "").split()),
        "category_id": request.args.get("category_id", "").strip(),
        "location": " ".join(request.args.get("location", "").split()),
        "min_budget": request.args.get("min_budget", "").strip(),
        "max_budget": request.args.get("max_budget", "").strip(),
    }
    error = None
    if len(filters["q"]) > 200 or len(filters["location"]) > 200:
        error = "Search text and location must be 200 characters or fewer."
    category_id = None
    if filters["category_id"]:
        raw = filters["category_id"]
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 10 or int(raw) < 1:
            error = "Choose a valid category."
        else:
            category_id = int(raw)
    budgets = {}
    for key in ("min_budget", "max_budget"):
        raw = filters[key]
        if raw:
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", raw):
                error = "Budgets must be non-negative amounts with at most two decimal places."
            else:
                try:
                    budgets[key] = Decimal(raw)
                except InvalidOperation:
                    error = "Enter a valid budget."
                if key in budgets and budgets[key] > MAX_BUDGET:
                    error = "Budget must be at most 9,999,999,999.99."
    if ("min_budget" in budgets and "max_budget" in budgets
            and budgets["min_budget"] > budgets["max_budget"]):
        error = "Minimum budget cannot exceed maximum budget."

    try:
        with Session(current_app.extensions["db_engine"]) as db:
            categories = categories_for_form(db)
            if category_id is not None and db.get(Category, category_id) is None:
                error = "Choose a valid category."
            active_filters = {key: value for key, value in filters.items() if value}
            if error:
                return render_template(
                    "request_list.html", items=[], page=page, has_next=False,
                    provider_view=True, categories=categories, filters=filters,
                    active_filters=active_filters, error=error,
                ), 400
            query = select(ServiceRequest).options(joinedload(ServiceRequest.category)).where(
                ServiceRequest.status == ServiceRequestStatus.OPEN,
                ServiceRequest.customer_id != current_user.id,
            )
            if filters["q"]:
                query = query.where(or_(
                    contains_text(ServiceRequest.title, filters["q"]),
                    contains_text(ServiceRequest.description, filters["q"]),
                ))
            if category_id is not None:
                query = query.where(ServiceRequest.category_id == category_id)
            if filters["location"]:
                query = query.where(contains_text(ServiceRequest.location, filters["location"]))
            if budgets:
                query = query.where(ServiceRequest.budget.is_not(None))
            if "min_budget" in budgets:
                query = query.where(ServiceRequest.budget >= budgets["min_budget"])
            if "max_budget" in budgets:
                query = query.where(ServiceRequest.budget <= budgets["max_budget"])
            items = list(db.scalars(
                query
                .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id.desc())
                .limit(PAGE_SIZE + 1)
                .offset((page - 1) * PAGE_SIZE)
            ))
            return render_template(
                "request_list.html", items=items[:PAGE_SIZE], page=page,
                has_next=len(items) > PAGE_SIZE, provider_view=True,
                categories=categories, filters=filters, active_filters=active_filters,
            )
    except SQLAlchemyError:
        current_app.logger.exception("Provider request browsing database error")
        abort(503)
