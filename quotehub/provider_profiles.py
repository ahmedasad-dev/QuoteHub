"""Provider-owned profile editing and public business profiles."""

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import role_required
from .models import ProviderProfile, UserRole


profiles = Blueprint("profiles", __name__)


def inline_text(value: str) -> str:
    return " ".join(value.split())


def profile_fields(source) -> dict[str, str]:
    return {
        "business_name": source.business_name or "",
        "description": source.description or "",
        "phone": source.phone or "",
        "location": source.location or "",
    }


def submitted_fields() -> dict[str, str]:
    return {
        "business_name": inline_text(request.form.get("business_name", "")),
        "description": request.form.get("description", "").strip(),
        "phone": inline_text(request.form.get("phone", "")),
        "location": inline_text(request.form.get("location", "")),
    }


def validate_profile(fields: dict[str, str]) -> str | None:
    if not fields["business_name"] or not fields["location"]:
        return "Business name and location are required."
    if len(fields["business_name"]) > 200 or len(fields["location"]) > 200:
        return "Business name and location must be 200 characters or fewer."
    if len(fields["phone"]) > 40:
        return "Phone must be 40 characters or fewer."
    if len(fields["description"]) > 2000:
        return "Description must be 2,000 characters or fewer."
    return None


@profiles.route("/provider/profile", methods=["GET", "POST"])
@role_required(UserRole.PROVIDER)
def manage_profile():
    engine = current_app.extensions["db_engine"]
    try:
        with Session(engine, expire_on_commit=False) as db:
            profile = db.scalar(
                select(ProviderProfile).where(ProviderProfile.user_id == current_user.id)
            )
            profile_id = profile.id if profile else None

            if request.method == "GET":
                fields = profile_fields(profile) if profile else {}
                return render_template(
                    "provider_profile_form.html", form_data=fields, profile_id=profile_id
                )

            fields = submitted_fields()
            error = validate_profile(fields)
            if error:
                return render_template(
                    "provider_profile_form.html",
                    form_data=fields,
                    profile_id=profile_id,
                    error=error,
                ), 400

            values = {
                **fields,
                "description": fields["description"] or None,
                "phone": fields["phone"] or None,
            }
            if profile is None:
                profile = ProviderProfile(user_id=current_user.id, **values)
                db.add(profile)
            else:
                for field, value in values.items():
                    setattr(profile, field, value)

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return render_template(
                    "provider_profile_form.html",
                    form_data=fields,
                    profile_id=profile_id,
                    error="A profile already exists for this account. Reload to edit it.",
                ), 409
    except SQLAlchemyError:
        current_app.logger.exception("Provider profile database error")
        abort(503)

    flash("Provider profile saved.")
    return redirect(url_for("profiles.manage_profile"))


@profiles.get("/providers/<int:profile_id>")
def public_profile(profile_id: int):
    try:
        with Session(current_app.extensions["db_engine"]) as db:
            profile = db.get(ProviderProfile, profile_id)
            if profile is None:
                abort(404)
            public_fields = profile_fields(profile)
    except SQLAlchemyError:
        current_app.logger.exception("Public provider profile database error")
        abort(503)

    return render_template("provider_profile_public.html", profile=public_fields)
