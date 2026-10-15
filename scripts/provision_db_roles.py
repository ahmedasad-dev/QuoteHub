"""One-off, repeatable provisioning for a dedicated QuoteHub PostgreSQL database.

Run with the bootstrap database administrator, never from the web service.
"""

import os

import psycopg
from psycopg import sql


APP_TABLES = (
    "users",
    "provider_profiles",
    "categories",
    "service_requests",
    "quotes",
)
OWNED_TABLES = (*APP_TABLES, "alembic_version")


def settings(environ=os.environ):
    names = (
        "DB_ADMIN_USER",
        "DB_ADMIN_PASSWORD",
        "POSTGRES_DB",
        "RUNTIME_DB_USER",
        "RUNTIME_DB_PASSWORD",
        "MIGRATION_DB_USER",
        "MIGRATION_DB_PASSWORD",
    )
    values = {name: environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError("Missing role setup variables: " + ", ".join(missing))
    if len({values[name] for name in ("DB_ADMIN_USER", "RUNTIME_DB_USER", "MIGRATION_DB_USER")}) != 3:
        raise RuntimeError("Admin, runtime, and migration role names must be distinct.")
    if values["RUNTIME_DB_PASSWORD"] == values["MIGRATION_DB_PASSWORD"]:
        raise RuntimeError("Runtime and migration passwords must be distinct.")
    return values


def provision(values):
    with psycopg.connect(
        host="db",
        dbname=values["POSTGRES_DB"],
        user=values["DB_ADMIN_USER"],
        password=values["DB_ADMIN_PASSWORD"],
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            if not cursor.fetchone()[0]:
                raise RuntimeError("Role setup requires the existing bootstrap administrator.")

            cursor.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
            )
            database_owner = cursor.fetchone()[0]
            if database_owner in (values["RUNTIME_DB_USER"], values["MIGRATION_DB_USER"]):
                raise RuntimeError("Neither application role may own the database.")

            cursor.execute(
                "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                "AND relkind IN ('r', 'p', 'v', 'm', 'f')"
            )
            unknown = {row[0] for row in cursor.fetchall()} - set(OWNED_TABLES)
            if unknown:
                raise RuntimeError("Unexpected public relations; inspect this database before role setup.")

            for role_key, password_key in (
                ("RUNTIME_DB_USER", "RUNTIME_DB_PASSWORD"),
                ("MIGRATION_DB_USER", "MIGRATION_DB_PASSWORD"),
            ):
                role = values[role_key]
                cursor.execute(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication "
                    "FROM pg_roles WHERE rolname = %s",
                    (role,),
                )
                privileges = cursor.fetchone()
                if privileges is None:
                    cursor.execute(
                        sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}")
                        .format(sql.Identifier(role), sql.Literal(values[password_key]))
                    )
                elif any(privileges):
                    raise RuntimeError("An application role already has elevated privileges; inspect it manually.")
                else:
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}")
                        .format(sql.Identifier(role), sql.Literal(values[password_key]))
                    )
                cursor.execute(
                    "SELECT 1 FROM pg_auth_members WHERE member = "
                    "(SELECT oid FROM pg_roles WHERE rolname = %s) LIMIT 1",
                    (role,),
                )
                if cursor.fetchone():
                    raise RuntimeError("An application role has role memberships; inspect it manually.")

            runtime = sql.Identifier(values["RUNTIME_DB_USER"])
            migration = sql.Identifier(values["MIGRATION_DB_USER"])
            database = sql.Identifier(values["POSTGRES_DB"])
            cursor.execute(sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database))
            cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cursor.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}, {}").format(database, runtime, migration))
            cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(database, runtime, migration))
            cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {}, {}").format(runtime, migration))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(runtime, migration))
            cursor.execute(sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(migration))

            for table in OWNED_TABLES:
                cursor.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if cursor.fetchone()[0] is None:
                    continue
                qualified = sql.Identifier("public", table)
                cursor.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(qualified, migration))
                if table in APP_TABLES:
                    cursor.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(qualified, runtime))
                    cursor.execute(
                        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}")
                        .format(qualified, runtime)
                    )
                    cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", (f"public.{table}",))
                    sequence = cursor.fetchone()[0]
                    if sequence:
                        sequence_name = sequence.split(".")[-1].strip('"')
                        qualified_sequence = sql.Identifier("public", sequence_name)
                        cursor.execute(
                            sql.SQL("ALTER SEQUENCE {} OWNER TO {}")
                            .format(qualified_sequence, migration)
                        )
                        cursor.execute(
                            sql.SQL("REVOKE ALL ON SEQUENCE {} FROM {}")
                            .format(qualified_sequence, runtime)
                        )
                        cursor.execute(
                            sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}")
                            .format(qualified_sequence, runtime)
                        )
                else:
                    cursor.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(qualified, runtime))

            if cursor.execute(
                "SELECT has_database_privilege(%s, current_database(), 'CREATE') "
                "OR has_database_privilege(%s, current_database(), 'TEMPORARY') "
                "OR has_schema_privilege(%s, 'public', 'CREATE')",
                (values["RUNTIME_DB_USER"],) * 3,
            ).fetchone()[0]:
                raise RuntimeError("Runtime role still has database or schema creation privileges.")
    print("QuoteHub database roles and grants are configured.")


if __name__ == "__main__":
    try:
        provision(settings())
    except psycopg.Error:
        raise SystemExit("Database role setup failed; check PostgreSQL logs privately.") from None
