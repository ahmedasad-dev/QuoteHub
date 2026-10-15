"""Non-persistent checks for the two QuoteHub database connections."""

import os
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from provision_db_roles import APP_TABLES, OWNED_TABLES


def verify_runtime(connection):
    privileges = connection.execute(text(
        "SELECT r.rolsuper, "
        "has_schema_privilege(current_user, 'public', 'CREATE'), "
        "has_database_privilege(current_user, current_database(), 'CREATE'), "
        "has_database_privilege(current_user, current_database(), 'TEMPORARY') "
        "FROM pg_roles r WHERE r.rolname = current_user"
    )).one()
    assert privileges == (False, False, False, False), "Runtime has schema-changing privileges"
    for table in APP_TABLES:
        for action in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert connection.execute(text(
                "SELECT has_table_privilege(current_user, :table_name, :action)"
            ), {"table_name": f"public.{table}", "action": action}).scalar_one()
        sequence = connection.execute(text(
            "SELECT pg_get_serial_sequence(:table_name, 'id')"
        ), {"table_name": f"public.{table}"}).scalar_one()
        assert connection.execute(text(
            "SELECT has_sequence_privilege(current_user, :sequence_name, 'USAGE')"
        ), {"sequence_name": sequence}).scalar_one()
    assert not connection.execute(text(
        "SELECT has_table_privilege(current_user, 'public.alembic_version', 'SELECT')"
    )).scalar_one()

    marker = "role-check-" + uuid4().hex
    with connection.begin_nested():
        identifier = connection.execute(text(
            "INSERT INTO categories (name, slug) VALUES (:name, :slug) RETURNING id"
        ), {"name": marker, "slug": marker}).scalar_one()
        assert connection.execute(text(
            "SELECT name FROM categories WHERE id = :id"
        ), {"id": identifier}).scalar_one() == marker
        connection.execute(text(
            "UPDATE categories SET name = :name WHERE id = :id"
        ), {"name": marker + "-updated", "id": identifier})
        assert connection.execute(text(
            "DELETE FROM categories WHERE id = :id"
        ), {"id": identifier}).rowcount == 1

    for statement in (
        "CREATE TABLE public.quotehub_privilege_check (id integer)",
        "ALTER TABLE public.categories ADD COLUMN quotehub_privilege_check integer",
    ):
        try:
            with connection.begin_nested():
                connection.execute(text(statement))
        except DBAPIError as error:
            if error.orig.sqlstate != "42501":
                raise
        else:
            raise AssertionError("Runtime unexpectedly changed the schema")


def verify_migration(connection):
    privileges = connection.execute(text(
        "SELECT r.rolsuper, has_schema_privilege(current_user, 'public', 'CREATE') "
        "FROM pg_roles r WHERE r.rolname = current_user"
    )).one()
    assert privileges == (False, True), "Migration role lacks expected privileges"
    assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    for table in OWNED_TABLES:
        assert connection.execute(text(
            "SELECT pg_get_userbyid(relowner) = current_user FROM pg_class "
            "WHERE oid = to_regclass(:table_name)"
        ), {"table_name": f"public.{table}"}).scalar_one()
    with connection.begin_nested():
        connection.execute(text("CREATE TABLE public.quotehub_privilege_check (id integer)"))
        connection.execute(text(
            "ALTER TABLE public.quotehub_privilege_check ADD COLUMN note text"
        ))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"runtime", "migration"}:
        raise SystemExit("Expected runtime or migration mode")
    mode = sys.argv[1]
    variable = "DATABASE_URL" if mode == "runtime" else "MIGRATION_DATABASE_URL"
    if not os.environ.get(variable):
        raise SystemExit(f"{variable} is required")
    engine = create_engine(os.environ[variable])
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                (verify_runtime if mode == "runtime" else verify_migration)(connection)
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    print(f"{mode.capitalize()} database privilege checks passed; test changes rolled back.")


if __name__ == "__main__":
    main()
