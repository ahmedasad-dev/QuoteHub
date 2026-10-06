# QuoteHub

Phase 1 is a small Flask foundation with a Jinja2 homepage and a PostgreSQL-backed health check. It contains no business features yet.

## Prerequisites

- Docker with the Compose plugin
- Git

## Start

Copy `.env.example` to `.env`, then set a local development database password in `.env`. Compose reads this file automatically. Keep `.env` private.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

Open <http://localhost:8000/>. If you changed `WEB_PORT`, use that port instead.

## Check health

Open <http://localhost:8000/health> or run:

```powershell
curl.exe -i http://localhost:8000/health
```

Healthy response: HTTP 200 with `{"database":"ok","status":"ok"}`. If PostgreSQL cannot be reached, the endpoint returns HTTP 503. The Compose web service waits for PostgreSQL's health check before starting.

Inspect service status and logs with `docker compose ps` and `docker compose logs web db`.

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
  static/style.css  Homepage styles
  templates/        Jinja2 homepage
Dockerfile          Flask container
compose.yaml        Web and PostgreSQL services
.env.example        Local configuration example
```

The app reads `DATABASE_URL` from its environment. Compose builds it from the PostgreSQL variables in `.env`, so the database name, user, and password stay aligned. Use URL-safe characters in the development password because Compose places it inside the URL. To run outside Compose, install `requirements.txt`, provide a PostgreSQL URL such as `postgresql+psycopg://user:password@localhost:5432/quotehub` as `DATABASE_URL`, and start the app with `flask --app quotehub run`. No tables or migrations are part of Phase 1.
