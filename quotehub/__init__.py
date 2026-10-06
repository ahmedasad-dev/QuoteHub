"""QuoteHub Flask application."""

from flask import Flask, jsonify, render_template
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .config import Config


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    if not app.config["DATABASE_URL"]:
        raise RuntimeError("DATABASE_URL must be set")

    engine = create_engine(app.config["DATABASE_URL"], pool_pre_ping=True)
    app.extensions["db_engine"] = engine

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
