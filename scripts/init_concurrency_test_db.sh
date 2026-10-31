#!/bin/sh
set -eu

if [ -z "${QUOTEHUB_TEST_LOGIN_PASSWORD:-}" ]; then
    echo "QUOTEHUB_TEST_LOGIN_PASSWORD must be set for the disposable test database." >&2
    exit 1
fi

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 <<'SQL'
\getenv test_password QUOTEHUB_TEST_LOGIN_PASSWORD
CREATE ROLE quotehub_test LOGIN PASSWORD :'test_password';
CREATE DATABASE quotehub_concurrency_test OWNER quotehub_test;
SQL
