"""QuoteHub Flask application."""

from flask import Flask, jsonify, render_template
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.middleware.proxy_fix import ProxyFix

from .admin_cli import create_admin
from .auth import auth
from .categories import categories, seed_categories
from .config import Config, boolean_setting, proxy_hops
from .models import User
from .provider_profiles import profiles
from .quotes import quotes
from .service_requests import requests


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    environment = app.config["QUOTEHUB_ENV"]
    if environment not in {"development", "production"}:
        raise RuntimeError("QUOTEHUB_ENV must be development or production.")
    production = environment == "production"
    app.config["DEBUG"] = boolean_setting(app.config["DEBUG"], "FLASK_DEBUG", default=False)
    app.config["SESSION_COOKIE_SECURE"] = boolean_setting(
        app.config["SESSION_COOKIE_SECURE"], "SESSION_COOKIE_SECURE", default=production
    )
    hops = proxy_hops(app.config["TRUSTED_PROXY_HOPS"])
    app.config["TRUSTED_PROXY_HOPS"] = hops

    if not app.config["DATABASE_URL"]:
        raise RuntimeError("DATABASE_URL must be set")
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SECRET_KEY must be set")
    if production:
        secret = app.config["SECRET_KEY"]
        if not isinstance(secret, str) or len(secret.strip().encode("utf-8")) < 32:
            raise RuntimeError("Production SECRET_KEY must be at least 32 bytes.")
        if app.config["DEBUG"]:
            raise RuntimeError("FLASK_DEBUG must be disabled in production.")
        if not app.config["SESSION_COOKIE_SECURE"]:
            raise RuntimeError("SESSION_COOKIE_SECURE must be enabled in production.")
        if app.config["SESSION_COOKIE_HTTPONLY"] is not True:
            raise RuntimeError("SESSION_COOKIE_HTTPONLY must be enabled in production.")
        if app.config["SESSION_COOKIE_SAMESITE"] not in {"Lax", "Strict"}:
            raise RuntimeError("SESSION_COOKIE_SAMESITE must be Lax or Strict in production.")

    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops)

    engine = create_engine(app.config["DATABASE_URL"], pool_pre_ping=True)
    app.extensions["db_engine"] = engine

    login_manager = LoginManager()
    login_manager.login_view = "auth.login"
    login_manager.init_app(app)
    CSRFProtect(app)
    app.register_blueprint(auth)
    app.register_blueprint(profiles)
    app.register_blueprint(categories)
    app.register_blueprint(requests)
    app.register_blueprint(quotes)
    app.cli.add_command(create_admin)
    app.cli.add_command(seed_categories)

    @login_manager.user_loader
    def load_user(user_id):
        try:
            identifier = int(user_id)
        except (TypeError, ValueError):
            return None
        with Session(engine) as db:
            return db.get(User, identifier)

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/health")
    def health():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            app.logger.exception("Database health check failed")
            return jsonify(status="unhealthy", database="unavailable"), 503

        return jsonify(status="ok", database="ok"), 200

    return app
