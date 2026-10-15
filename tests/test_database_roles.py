"""Configuration boundaries for dedicated database roles."""

import pytest

from migrations.database import migration_database_url
from scripts.provision_db_roles import settings


def valid_settings():
    return {
        "DB_ADMIN_USER": "bootstrap",
        "DB_ADMIN_PASSWORD": "admin-private",
        "POSTGRES_DB": "quotehub",
        "RUNTIME_DB_USER": "runtime",
        "RUNTIME_DB_PASSWORD": "runtime-private",
        "MIGRATION_DB_USER": "migrator",
        "MIGRATION_DB_PASSWORD": "migration-private",
    }


def test_alembic_requires_dedicated_url_even_when_runtime_url_exists():
    with pytest.raises(RuntimeError, match="MIGRATION_DATABASE_URL"):
        migration_database_url({"DATABASE_URL": "postgresql://runtime"})
    assert migration_database_url({
        "DATABASE_URL": "postgresql://runtime",
        "MIGRATION_DATABASE_URL": "postgresql://migrator",
    }) == "postgresql://migrator"


def test_role_setup_requires_all_settings_without_echoing_secrets():
    values = valid_settings()
    del values["RUNTIME_DB_PASSWORD"]
    with pytest.raises(RuntimeError, match="RUNTIME_DB_PASSWORD"):
        settings(values)


@pytest.mark.parametrize("key,value", [
    ("RUNTIME_DB_USER", "bootstrap"),
    ("MIGRATION_DB_USER", "runtime"),
    ("MIGRATION_DB_PASSWORD", "runtime-private"),
])
def test_role_setup_rejects_shared_identity_or_password(key, value):
    values = valid_settings()
    values[key] = value
    with pytest.raises(RuntimeError, match="distinct"):
        settings(values)


def test_role_setup_accepts_separate_roles():
    assert settings(valid_settings())["RUNTIME_DB_USER"] == "runtime"
