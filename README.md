# Farm Management API

Synchronous FastAPI/PostgreSQL replacement for the transactional parts of the former Laravel dashboard. The service contains no frontend, reports, Excel generation/import, ABK integration, derived analytics, background worker, or scheduler.

## Runtime

- Python 3.14
- PostgreSQL
- Google Cloud Storage for optional transaction/payment proofs

All API routes run synchronously. PostgreSQL is required even for tests because the application relies on JSONB, transactional advisory locks, row locks, and PostgreSQL constraints.

## Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Generate secrets before starting the service:

```bash
openssl rand -hex 32
```

Use independent values for `API_KEY` and `JWT_SECRET`. `API_PREVIOUS_KEY` is optional and exists only for zero-downtime API-key rotation. Configure `CORS_ALLOWED_ORIGINS` as a comma-separated allowlist; wildcard origins are not supported.

Create the empty schema and start the API:

```bash
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

- Swagger UI: <http://127.0.0.1:8000/docs>
- OpenAPI document: <http://127.0.0.1:8000/openapi.json>
- Liveness: <http://127.0.0.1:8000/health/live>
- Readiness: <http://127.0.0.1:8000/health/ready>

The migration creates no users or other domain rows.

## First Superadmin

Generate an Argon2 hash locally:

```bash
python -c "from app.core.security import hash_password; print(hash_password(input('Initial password: ')))"
```

Insert the first account manually, replacing the example values and hash:

```sql
INSERT INTO users
    (name, email, password_hash, role, is_active, must_change_password, token_version, created_at, updated_at)
VALUES
    ('Initial Superadmin', 'superadmin@example.com', '$argon2id$REPLACE_WITH_GENERATED_HASH',
     'SUPERADMIN', TRUE, TRUE, 0, NOW(), NOW());
```

The first login returns an eight-hour JWT but all domain endpoints remain blocked until the initial password is changed through `POST /api/v1/auth/change-password`.

## Authentication

Login requires only the service API key plus email/password:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'X-PBB-API-Key: replace-with-secret' \
  -H 'Content-Type: application/json' \
  -d '{"email":"superadmin@example.com","password":"replace-me"}'
```

Every other `/api/v1` request requires both headers:

```text
X-PBB-API-Key: <service key>
Authorization: Bearer <access token>
```

Logout, password changes/resets, role changes, and account deactivation invalidate all previously issued JWTs for that user through `token_version`.

## Proof uploads

Transaction and payment data use typed JSON endpoints. Optional JPG/PNG proofs are uploaded separately with `multipart/form-data` to the relevant `/proof` endpoint. Maximum file size is 5 MB. Upload and replacement are synchronous; a newly uploaded object is deleted if database persistence fails, and replacement cleanup is best-effort after commit.

## Cash on hand backfill

`operational_cashflow_balances` keeps the running `cash_on_hand` balance, updated automatically on every operational cashflow transaction. Cashflows that existed before that table was introduced are not included until you run the backfill once after `alembic upgrade head`:

```bash
python -m app.scripts.backfill_cash_on_hand --dry-run   # print the recalculated balance only
python -m app.scripts.backfill_cash_on_hand             # save it
```

This is a manual data command, not an Alembic migration. It recalculates the balance from all of `operational_cashflows` (paid `MAIN_ACCOUNT_TOPUP` adds `amount_idr`; any other type that is `PAID` or `PARTIALLY_PAID` subtracts `amount_idr`) and overwrites the stored amount. It is idempotent, so it can be re-run at any time, for example to repair drift after manual database edits. It takes the same lock as cashflow writes, so it is safe while the API is running. It uses `DATABASE_URL` from the environment or `.env`.

## Tests and checks

Set `DATABASE_URL` to a disposable PostgreSQL database whose name ends in `_test`. Tests refuse other database names.

```bash
pytest
ruff check app tests
mypy app
python -m compileall -q app alembic
```
