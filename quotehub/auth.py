"""Session authentication and basic role checks."""

from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from .models import User, UserRole, normalize_email_address


auth = Blueprint("auth", __name__)


def role_required(role: UserRole):
    """Require a signed-in user with one specific role."""

    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role != role:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def form_error(template: str, message: str, status: int = 400):
    return render_template(template, error=message, form_data=request.form), status


@auth.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("auth.dashboard"))
    if request.method == "GET":
        return render_template("register.html", form_data={})

    name = request.form.get("name", "").strip()
    email = normalize_email_address(request.form.get("email", ""))
    password = request.form.get("password", "")
    role_value = request.form.get("role", "")

    if not name or not email or not password or not role_value:
        return form_error("register.html", "Complete all required fields.")
    if len(name) > 120 or len(email) > 255 or " " in email or email.count("@") != 1:
        return form_error("register.html", "Enter a valid name and email address.")
    if not all(email.split("@")):
        return form_error("register.html", "Enter a valid email address.")
    if not 8 <= len(password) <= 128:
        return form_error("register.html", "Use a password between 8 and 128 characters.")
    if role_value not in {UserRole.CUSTOMER.value, UserRole.PROVIDER.value}:
        return form_error("register.html", "Choose Customer or Provider.")

    engine = current_app.extensions["db_engine"]
    try:
        with Session(engine, expire_on_commit=False) as db:
            if db.scalar(select(User.id).where(func.lower(User.email) == email)):
                return form_error("register.html", "An account with that email already exists.")
            user = User(
                name=name,
                email=email,
                password_hash=generate_password_hash(password),
                role=UserRole(role_value),
            )
            db.add(user)
            db.commit()
    except IntegrityError:
        return form_error("register.html", "An account with that email already exists.")
    except SQLAlchemyError:
        current_app.logger.exception("Registration database error")
        abort(503)

    login_user(user)
    return redirect(url_for("auth.dashboard"))


@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("auth.dashboard"))
    if request.method == "GET":
        return render_template("login.html", form_data={})

    email = normalize_email_address(request.form.get("email", ""))
    password = request.form.get("password", "")
    invalid_message = "Invalid email or password."
    if not email or not password:
        return form_error("login.html", invalid_message)

    try:
        with Session(current_app.extensions["db_engine"]) as db:
            user = db.scalar(select(User).where(func.lower(User.email) == email))
            valid = user is not None and check_password_hash(user.password_hash, password)
    except SQLAlchemyError:
        current_app.logger.exception("Login database error")
        abort(503)

    if not valid:
        return form_error("login.html", invalid_message)

    login_user(user)
    next_url = request.args.get("next", "")
    parsed = urlsplit(next_url)
    if next_url.startswith("/") and not next_url.startswith("//") and not parsed.netloc and "\\" not in next_url:
        return redirect(next_url)
    return redirect(url_for("auth.dashboard"))


@auth.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


@auth.get("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@auth.get("/customer")
@role_required(UserRole.CUSTOMER)
def customer_area():
    return render_template("role_area.html", area="Customer")


@auth.get("/provider")
@role_required(UserRole.PROVIDER)
def provider_area():
    return render_template("role_area.html", area="Provider")


@auth.get("/admin")
@role_required(UserRole.ADMIN)
def admin_area():
    return render_template("role_area.html", area="Admin")
