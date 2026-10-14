# QuoteHub

QuoteHub has a small Flask foundation, a Jinja2 homepage, a PostgreSQL-backed health check, and customer/provider accounts. Providers can manage a public business profile, and admins can manage service categories. Customers can manage service requests; providers can browse open requests and submit quotes.

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

CSRF tokens protect registration, login, and logout forms. Cookies are HTTP-only and SameSite=Lax. `SESSION_COOKIE_SECURE=0` supports local HTTP; set it to `1` when serving over HTTPS. Keep `SECRET_KEY` stable and private so existing sessions remain valid.

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
