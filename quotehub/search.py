"""Small helpers shared by read-only discovery queries."""

from sqlalchemy import func


def contains_text(column, value):
    """Case-insensitive literal substring match with LIKE wildcards escaped."""
    escaped = value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return func.lower(column).like(f"%{escaped}%", escape="\\")
