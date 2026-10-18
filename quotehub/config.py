"""Environment-based application configuration."""

import os
from datetime import timedelta


def boolean_setting(value, name: str, *, default: bool) -> bool:
    """Parse an environment-style boolean without silently accepting typos."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
    raise RuntimeError(f"{name} must be 1 or 0 (true or false).")


def proxy_hops(value) -> int:
    """Count only explicitly configured trusted reverse-proxy hops."""
    if isinstance(value, bool):
        raise RuntimeError("TRUSTED_PROXY_HOPS must be an integer from 0 to 10.")
    if isinstance(value, int):
        hops = value
    elif (isinstance(value, str) and len(value) <= 2
          and value.isascii() and value.isdecimal()):
        hops = int(value)
    else:
        raise RuntimeError("TRUSTED_PROXY_HOPS must be an integer from 0 to 10.")
    if not 0 <= hops <= 10:
        raise RuntimeError("TRUSTED_PROXY_HOPS must be an integer from 0 to 10.")
    return hops


def bounded_integer_setting(value, name: str, *, minimum: int, maximum: int) -> int:
    """Accept decimal environment values within an explicit safe range."""
    if isinstance(value, bool):
        raise RuntimeError(f"{name} must be an integer from {minimum} to {maximum}.")
    if isinstance(value, int):
        result = value
    elif (isinstance(value, str) and len(value) <= 9
          and value.isascii() and value.isdecimal()):
        result = int(value)
    else:
        raise RuntimeError(f"{name} must be an integer from {minimum} to {maximum}.")
    if not minimum <= result <= maximum:
        raise RuntimeError(f"{name} must be an integer from {minimum} to {maximum}.")
    return result


class Config:
    QUOTEHUB_ENV = os.environ.get("QUOTEHUB_ENV", "development")
    DATABASE_URL = os.environ.get("DATABASE_URL")
    SECRET_KEY = os.environ.get("SECRET_KEY")
    DEBUG = os.environ.get("FLASK_DEBUG", "0")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    TRUSTED_PROXY_HOPS = os.environ.get("TRUSTED_PROXY_HOPS", "0")
    AUTH_LOGIN_RATE_LIMIT = os.environ.get("AUTH_LOGIN_RATE_LIMIT", "20 per minute")
    AUTH_REGISTER_RATE_LIMIT = os.environ.get("AUTH_REGISTER_RATE_LIMIT", "10 per hour")
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    MAX_CONTENT_LENGTH = os.environ.get("MAX_CONTENT_LENGTH", "131072")
    HSTS_MAX_AGE = os.environ.get("HSTS_MAX_AGE", "0")
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
