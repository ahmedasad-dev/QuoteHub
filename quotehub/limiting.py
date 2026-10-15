"""Rate limits for public authentication forms."""

from flask import current_app
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from limits import parse


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    swallow_errors=False,
    in_memory_fallback_enabled=False,
    headers_enabled=True,
)


def configured_limit(value, name):
    """Reject a malformed route limit at startup instead of dropping it silently."""
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{name} must be a valid rate limit.")
    try:
        limit = parse(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a valid rate limit.") from error
    if limit.amount < 1:
        raise RuntimeError(f"{name} must permit at least one request.")
    return value


def login_limit():
    return current_app.config["AUTH_LOGIN_RATE_LIMIT"]


def register_limit():
    return current_app.config["AUTH_REGISTER_RATE_LIMIT"]
