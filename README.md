# QuoteHub

QuoteHub has a small Flask foundation, a Jinja2 homepage, a PostgreSQL-backed health check, and the Phase 2 database schema. It has no registration, login, or business UI yet.

## Prerequisites

- Docker with the Compose plugin
- Git

## Start

Copy `.env.example` to `.env`, then set a local development database password in `.env`. Compose reads this file automatically. Keep `.env` private.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose exec web alembic upgrade head
```

Open <http://localhost:8000/>. If you changed `WEB_PORT`, use that port instead.

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

Run the isolated model tests in the web image:

```powershell
$testsMount = (Join-Path (Get-Location).Path 'tests') + ':/app/tests:ro'
docker compose run --rm -v $testsMount web python -m unittest discover -s tests -v
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
  config.py         Environment-based configuration
  models.py         SQLAlchemy models and relationships
  static/style.css  Homepage styles
  templates/        Jinja2 homepage
alembic.ini         Alembic configuration
migrations/         Database revisions
tests/              Model and constraint tests
Dockerfile          Flask container
compose.yaml        Web and PostgreSQL services
.env.example        Local configuration example
```

The app reads `DATABASE_URL` from its environment. Compose builds it from the PostgreSQL variables in `.env`, so the database name, user, and password stay aligned. Use URL-safe characters in the development password because Compose places it inside the URL. To run outside Compose, install `requirements.txt`, provide a PostgreSQL URL such as `postgresql+psycopg://user:password@localhost:5432/quotehub` as `DATABASE_URL`, and start the app with `flask --app quotehub run`.
