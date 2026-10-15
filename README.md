# QuoteHub

QuoteHub has a small Flask foundation, a Jinja2 homepage, a PostgreSQL-backed health check, and customer/provider accounts. Providers can manage a public business profile, and admins can manage service categories. Customers can manage service requests; providers can browse open requests and submit quotes.

## Prerequisites

- Docker with the Compose plugin
- Git

## Local development

Copy `.env.example` to `.env`, then set a local development database password and a random `SECRET_KEY` in `.env`. Generate a key with `python -c "import secrets; print(secrets.token_hex(32))"`. Compose reads this file automatically. Keep `.env` private.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose exec web alembic upgrade head
```

Open <http://localhost:8000/>. If you changed `WEB_PORT`, use that port instead. Compose binds the local web port to `127.0.0.1`, so it is available on this machine without exposing Gunicorn on every host interface. Leave `QUOTEHUB_ENV=development` and `TRUSTED_PROXY_HOPS=0` for local HTTP use.

Register a Customer or Provider account at `/register`, then use `/login` and the POST logout button in the navigation. Both account types reach `/dashboard`; role-specific placeholder pages are `/customer` and `/provider`. `/admin` is reserved for accounts created outside public registration.

The customer dashboard shows counts of owned requests by status and the six most recent owned requests, including a count of all quotes on each. The provider dashboard shows open requests the provider can still quote (excluding their own requests and requests they have already quoted), pending and accepted quotes, and completed jobs. Completed jobs require that provider's accepted quote on a COMPLETED request. The accepted quote total includes completed jobs. The six most recent quotes link to their details. Admin dashboard links remain unchanged.

To create an admin account after applying migrations, run this in a terminal:

```powershell
docker compose exec web flask --app quotehub create-admin
```

Enter the name, email, and password at the prompts. The password is hidden while typing and is stored as a hash. The command always creates an ADMIN account; public registration cannot create one. It rejects an email already used by any account. No default admin account or password is created.

Providers can set up or edit their business details at `/provider/profile`. Saving a profile creates a public read-only page at `/providers/<profile_id>`. Anyone can search public profiles at `/providers` by business name or description and filter by business location. The public pages show business details; provider account details stay private. Provider profiles currently have no category association, so the directory has no category filter. Everyone can browse `/categories`. Only admins can use `/admin/categories` to add categories or rename an existing category and its slug.

Customers can create a request at `/requests/new` and view their own requests at `/requests`. A request starts as OPEN. Its owner can edit or cancel it only while it is OPEN; cancellation preserves the record with status CANCELLED. Providers can browse open requests at `/provider/requests`, without customer account details. They can search titles and descriptions, and filter by category, location, and minimum or maximum budget. Requests without a budget appear in unfiltered results but are excluded whenever either budget limit is active. Search filters remain active while paging. The browse pages show 20 results per page.

Providers can submit one quote per OPEN request from the browse page and manage their quotes at `/provider/quotes`. A new quote starts as PENDING. The provider can withdraw it only while both the quote is PENDING and the request is OPEN; withdrawal preserves it with status WITHDRAWN. A withdrawn quote cannot be resubmitted for the same request.

Customers can view quotes for their own requests from the request detail page. While a request is OPEN, its customer may reject an individual PENDING quote without closing the request, or accept one PENDING quote. Acceptance sets that quote to ACCEPTED, rejects the other PENDING quotes for the request, and sets the request to ACCEPTED in one transaction. Already rejected and withdrawn quotes keep their statuses. Once the request is no longer OPEN, no further quote decisions or provider withdrawals are allowed. Quote submission itself leaves the request OPEN.

An OPEN request can be cancelled only by its customer. Cancellation sets its status to CANCELLED and keeps the request and any quotes. An ACCEPTED request can be marked COMPLETED by its customer or by the provider whose quote was accepted. Other providers cannot complete it. Completion requires exactly one accepted quote for the request and leaves quote statuses unchanged. ACCEPTED and COMPLETED requests cannot be cancelled through the ordinary cancellation action. Reviews, payments, and other later workflows are not included.

To add the five starter categories, run:

```powershell
docker compose exec web flask --app quotehub seed-categories
```

You can run this command again. It skips categories matching a starter name or slug and never edits existing rows. The current schema has no inactive category state, so `/categories` displays every category.

CSRF tokens protect state-changing forms. Session cookies are HTTP-only and SameSite=Lax. In development, a blank `SESSION_COOKIE_SECURE` defaults to `0` so local HTTP works. Keep `SECRET_KEY` stable and private so existing sessions remain valid.

## Production configuration behind HTTPS

The included Compose setup is for local development; it does not provide TLS or a public production deployment. For public use, terminate HTTPS at a properly configured reverse proxy and allow only that proxy to reach Gunicorn. Do not expose Gunicorn's port directly to the public internet. The proxy must forward requests over a private connection and set the original scheme; configure HTTP-to-HTTPS redirection and certificates at the proxy. No TLS endpoint is configured or verified by this repository.

Set these application variables in the deployment environment:

| Variable | Production value |
| --- | --- |
| `QUOTEHUB_ENV` | `production` (required to activate production checks) |
| `DATABASE_URL` | A private PostgreSQL connection URL |
| `SECRET_KEY` | A private, random value of at least 32 bytes; never reuse the example or a test key |
| `FLASK_DEBUG` | `0`; production startup rejects debug mode |
| `SESSION_COOKIE_SECURE` | Leave unset/blank to default to `1` in production, or set `1` explicitly; `0` is rejected |
| `TRUSTED_PROXY_HOPS` | `0` by default; set to the exact number of trusted proxy hops only after isolating Gunicorn |

`SESSION_COOKIE_HTTPONLY` stays enabled and `SESSION_COOKIE_SAMESITE` stays `Lax`. Production startup rejects insecure overrides. Secure cookies are sent by browsers only over HTTPS, so authenticated browsing through an HTTP-only endpoint will not work in production mode. The app requires a secret key in both modes and rejects a production key shorter than 32 bytes; generate it with a cryptographically secure source and manage it outside the repository. A key change invalidates existing sessions.

Proxy trust is **off** by default. With exactly one trusted reverse proxy, set `TRUSTED_PROXY_HOPS=1`; use a different count only if that many trusted proxies are actually in the request path. The app then trusts `X-Forwarded-For` and `X-Forwarded-Proto` from those hops. It does not trust forwarded host, port, or URL prefix. The outermost trusted proxy must remove or overwrite client-supplied forwarded headers. Never enable proxy trust while clients can connect directly to Gunicorn: a client could forge its IP address or HTTPS scheme. Keep network access to Gunicorn private even when using these settings.

## Check health

Open <http://localhost:8000/health> or run:

```powershell
curl.exe -i http://localhost:8000/health
```

Healthy response: HTTP 200 with `{"database":"ok","status":"ok"}`. If PostgreSQL cannot be reached, the endpoint returns HTTP 503. The Compose web service waits for PostgreSQL's health check before starting.

Inspect service status and logs with `docker compose ps` and `docker compose logs web db`.

## Database migrations

The five database tables are managed by Alembic. Phase 9B uses the existing schema, so it needs no new migration. Apply committed revisions after starting or updating the containers:

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
  admin_cli.py      Interactive admin account creation command
  auth.py           Registration, login, logout, and role checks
  categories.py     Public listing, admin management, and seed command
  config.py         Environment-based configuration
  models.py         SQLAlchemy models and relationships
  provider_profiles.py  Provider profile editing and public view
  search.py        Shared literal text search helper
  quotes.py        Provider quote submission and private quote views
  service_requests.py   Customer request management and provider browsing
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
