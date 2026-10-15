"""Select only the dedicated migration connection for Alembic."""

import os


def migration_database_url(environ=None) -> str:
    values = os.environ if environ is None else environ
    url = values.get("MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATION_DATABASE_URL must be set for migrations")
    return url
