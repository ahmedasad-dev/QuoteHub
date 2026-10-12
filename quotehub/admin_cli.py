"""CLI-only creation of administrator accounts."""

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from .models import User, UserRole, normalize_email_address


@click.command("create-admin")
@with_appcontext
def create_admin():
    """Interactively create an administrator account."""
    name = click.prompt("Name").strip()
    email = normalize_email_address(click.prompt("Email"))
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)

    if not name or not email or not password:
        raise click.ClickException("Name, email, and password are required.")
    if len(name) > 120 or len(email) > 255 or any(char.isspace() for char in email):
        raise click.ClickException("Enter a valid name and email address.")
    if email.count("@") != 1 or not all(email.split("@")):
        raise click.ClickException("Enter a valid email address.")
    if not 8 <= len(password) <= 128:
        raise click.ClickException("Use a password between 8 and 128 characters.")

    try:
        with Session(current_app.extensions["db_engine"]) as db:
            if db.scalar(select(User.id).where(func.lower(User.email) == email)) is not None:
                raise click.ClickException("An account with that email already exists.")
            db.add(
                User(
                    name=name,
                    email=email,
                    password_hash=generate_password_hash(password),
                    role=UserRole.ADMIN,
                )
            )
            db.commit()
    except IntegrityError as error:
        raise click.ClickException("An account with that email already exists.") from error
    except SQLAlchemyError as error:
        current_app.logger.exception("Admin creation database error")
        raise click.ClickException("Could not create admin account.") from error

    click.echo("Admin account created.")
