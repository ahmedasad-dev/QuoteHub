"""QuoteHub Flask application."""

from flask import Flask, jsonify, render_template
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import auth
from .config import Config
from .models import User


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    if not app.config["DATABASE_URL"]:
        raise RuntimeError("DATABASE_URL must be set")
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SECRET_KEY must be set")

    engine = create_engine(app.config["DATABASE_URL"], pool_pre_ping=True)
    app.extensions["db_engine"] = engine

    login_manager = LoginManager()
    login_manager.login_view = "auth.login"
    login_manager.init_app(app)
    CSRFProtect(app)
    app.register_blueprint(auth)

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
