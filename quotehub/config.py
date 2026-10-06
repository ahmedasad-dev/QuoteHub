"""Environment-based application configuration."""

import os


class Config:
    DATABASE_URL = os.environ.get("DATABASE_URL")
    DEBUG = os.environ.get("FLASK_DEBUG", "0").lower() in {"1", "true", "yes"}
