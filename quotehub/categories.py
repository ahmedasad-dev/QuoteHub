"""Public categories, admin management, and repeatable seeding."""

import re

import click
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask.cli import with_appcontext
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import role_required
from .models import Category, UserRole


categories = Blueprint("categories", __name__)
INITIAL_CATEGORIES = (
    ("Plumbing", "plumbing"),
    ("Electrical", "electrical"),
    ("Cleaning", "cleaning"),
    ("Painting", "painting"),
    ("Carpentry", "carpentry"),
)


def list_categories(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.name)))


def normalized_fields() -> tuple[str, str]:
    name = " ".join(request.form.get("name", "").split())
    slug = re.sub(r"[\s_]+", "-", request.form.get("slug", "").strip().lower())
    return name, slug


def validate_category(name: str, slug: str) -> str | None:
    if not name or not slug:
        return "Name and slug are required."
    if len(name) > 100 or len(slug) > 120:
        return "Name must be at most 100 characters and slug at most 120."
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        return "Slug may contain lowercase letters, numbers, and single hyphens."
    return None


def duplicate_category(db: Session, name: str, slug: str, exclude_id: int | None = None):
    query = select(Category.id).where(
        or_(func.lower(Category.name) == name.lower(), Category.slug == slug)
    )
    if exclude_id is not None:
        query = query.where(Category.id != exclude_id)
    return db.scalar(query) is not None


@categories.get("/categories")
def category_list():
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            items = list_categories(db)
    except SQLAlchemyError:
        current_app.logger.exception("Category listing database error")
        abort(503)
    return render_template("categories.html", categories=items)


@categories.route("/admin/categories", methods=["GET", "POST"])
@role_required(UserRole.ADMIN)
def manage_categories():
    engine = current_app.extensions["db_engine"]
    try:
        with Session(engine) as db:
            if request.method == "GET":
                return render_template(
                    "category_manage.html", categories=list_categories(db), form_data={}
                )

            name, slug = normalized_fields()
            error = validate_category(name, slug)
            if not error and duplicate_category(db, name, slug):
                error = "A category with that name or slug already exists."
            if error:
                return render_template(
                    "category_manage.html",
                    categories=list_categories(db),
                    form_data={"name": name, "slug": slug},
                    error=error,
                ), 400

            db.add(Category(name=name, slug=slug))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return render_template(
                    "category_manage.html",
                    categories=list_categories(db),
                    form_data={"name": name, "slug": slug},
                    error="A category with that name or slug already exists.",
                ), 409
    except SQLAlchemyError:
        current_app.logger.exception("Category management database error")
        abort(503)

    flash("Category created.")
    return redirect(url_for("categories.manage_categories"))


@categories.route("/admin/categories/<int:category_id>/edit", methods=["GET", "POST"])
@role_required(UserRole.ADMIN)
def edit_category(category_id: int):
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            category = db.get(Category, category_id)
            if category is None:
                abort(404)
            if request.method == "GET":
                return render_template(
                    "category_edit.html",
                    category_id=category_id,
                    form_data={"name": category.name, "slug": category.slug},
                )

            name, slug = normalized_fields()
            error = validate_category(name, slug)
            if not error and duplicate_category(db, name, slug, exclude_id=category_id):
                error = "A category with that name or slug already exists."
            if error:
                return render_template(
                    "category_edit.html",
                    category_id=category_id,
                    form_data={"name": name, "slug": slug},
                    error=error,
                ), 400

            category.name = name
            category.slug = slug
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return render_template(
                    "category_edit.html",
                    category_id=category_id,
                    form_data={"name": name, "slug": slug},
                    error="A category with that name or slug already exists.",
                ), 409
    except SQLAlchemyError:
        current_app.logger.exception("Category edit database error")
        abort(503)

    flash("Category updated.")
    return redirect(url_for("categories.manage_categories"))


@click.command("seed-categories")
@with_appcontext
def seed_categories():
    """Add the starter categories without changing existing rows."""
    added = 0
    skipped = 0
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            for name, slug in INITIAL_CATEGORIES:
                if duplicate_category(db, name, slug):
                    skipped += 1
                    continue
                db.add(Category(name=name, slug=slug))
                db.flush()
                added += 1
            db.commit()
    except SQLAlchemyError as error:
        current_app.logger.exception("Category seed database error")
        raise click.ClickException("Could not seed categories; no changes were saved.") from error

    click.echo(f"Added {added} categories; skipped {skipped} already present.")
