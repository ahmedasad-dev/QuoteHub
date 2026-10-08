# QuoteHub

QuoteHub has a small Flask foundation, a Jinja2 homepage, a PostgreSQL-backed health check, and the Phase 2 database schema. Phase 3 adds customer/provider accounts and protected placeholder pages. Service requests and quotes have no workflows yet.

## Prerequisites

- Docker with the Compose plugin
- Git

## Start

Copy `.env.example` to `.env`, then set a local development database password and a random `SECRET_KEY` in `.env`. Generate a key with `python -c "import secrets; print(secrets.token_hex(32))"`. Compose reads this file automatically. Keep `.env` private.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose exec web alembic upgrade head
```

Open <http://localhost:8000/>. If you changed `WEB_PORT`, use that port instead.

Register a Customer or Provider account at `/register`, then use `/login` and the POST logout button in the navigation. Both account types reach `/dashboard`; role-specific placeholder pages are `/customer` and `/provider`. `/admin` is reserved for accounts created outside public registration.

CSRF tokens protect registration, login, and logout forms. Cookies are HTTP-only and SameSite=Lax. `SESSION_COOKIE_SECURE=0` supports local HTTP; set it to `1` when serving over HTTPS. Keep `SECRET_KEY` stable and private so existing sessions remain valid.

## Check health

Open <http://localhost:8000/health> or run:

```powershell
curl.exe -i http://localhost:8000/health
```

Healthy response: HTTP 200 with `{"database":"ok","status":"ok"}`. If PostgreSQL cannot be reached, the endpoint returns HTTP 503. The Compose web service waits for PostgreSQL's health check before starting.

Inspect service status and logs with `docker compose ps` and `docker compose logs web db`.

## Database migrations

The five Phase 2 tables are managed by Alembic. Apply committed revisions after starting or updating the containers:

```powershell
docker compose exec web alembic upgrade head
docker compose exec web alembic current
```

For future schema changes, edit the models, then generate a revision from the running Compose environment. This PowerShell command mounts the versions directory so the new file is saved in the project:

```powershell
$versionsMount = (Join-Path (Get-Location).Path 'migrations\versions') + ':/app/migrations/versions'
docker compose run --rm -v $versionsMount web alembic revision --autogenerate -m "description"
```

Review the generated migration, rebuild the web image, and run `docker compose exec web alembic upgrade head`. Container builds copy committed revisions; they do not apply them automatically.

Run all tests after installing development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## Stop

```powershell
docker compose down
```

The `postgres_data` volume preserves database files. `docker compose down --volumes` removes those files.

## Structure

```text
quotehub/
  __init__.py       Application factory and routes
  auth.py           Registration, login, logout, and role checks
  config.py         Environment-based configuration
  models.py         SQLAlchemy models and relationships
  static/style.css  Homepage styles
  templates/        Jinja2 homepage
alembic.ini         Alembic configuration
migrations/         Database revisions
tests/              Model and authentication tests
Dockerfile          Flask container
compose.yaml        Web and PostgreSQL services
.env.example        Local configuration example
```

The app reads `DATABASE_URL` and `SECRET_KEY` from its environment. Compose builds the database URL from the PostgreSQL variables in `.env`, so the database name, user, and password stay aligned. Use URL-safe characters in the development password because Compose places it inside the URL. To run outside Compose, install `requirements.txt`, set both variables, and start the app with `flask --app quotehub run`.
