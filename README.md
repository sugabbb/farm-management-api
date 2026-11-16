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

## Operational cashflow balances and backfill

`operational_cashflow_balances` keeps one running row per type, updated automatically on every operational cashflow transaction (create, update/correction, submit, approve/reject, payment, payment void):

- `cash_on_hand`: paid `MAIN_ACCOUNT_TOPUP` adds `amount_idr`; any other type that is `PAID` or `PARTIALLY_PAID` subtracts `amount_idr`.
- `waiting_approval`: a cashflow adds `amount_idr` when its `approval_status` becomes `SUBMITTED`, and the amount is removed again when it is approved or rejected. The balance is therefore the total amount currently waiting for a decision. A Superadmin correction of a submitted cashflow adjusts it by the difference.
- `approved_unpaid`: for every `APPROVED` cashflow, what is still unpaid on it: the latest `amount_idr` minus its non-voided payments. Unpaid rows count in full, partially paid rows count only their remainder, and paid rows count nothing. Approvals add, payments reduce, voids restore, and edits/corrections re-derive the remainder from the latest amount. If a higher amount sends an approved row back to `DRAFT`, it is removed until it is approved again.

Cashflows that existed before a balance type was introduced are not included until you run the backfill once after `alembic upgrade head`:

```bash
python -m app.scripts.backfill_cash_on_hand --dry-run                 # print the recalculated balances only
python -m app.scripts.backfill_cash_on_hand                           # save every balance type
python -m app.scripts.backfill_cash_on_hand --type approved_unpaid    # save a single balance type
```

This is a manual data command, not an Alembic migration. It recalculates each balance from all of `operational_cashflows` using the rules above and overwrites the stored amount. It is idempotent, so it can be re-run at any time, for example to repair drift after manual database edits. It takes the same lock as cashflow writes, so it is safe while the API is running. It uses `DATABASE_URL` from the environment or `.env`.

## Main cash account

`main_cash_accounts` holds the single main cash account (`current_amount`), and `main_cash_account_logs` is its immutable ledger (`amount`, `direction` = `credit`/`debit`, `reference`). Entries are posted automatically, in the background, for the existing transactions:

- **debit**: an operational cashflow `MAIN_ACCOUNT_TOPUP` once `PAID` (it is paid on creation). Money leaves the main account to top up cash on hand.
- **credit**: a revenue `MAIN_ACCOUNT_TRANSFER` once `PAID` (on approval), for its `amount_idr` = gross minus transfer cost. Money arrives in the main account.

Each write posts only the difference between the row's effect before and after. A Superadmin correction of a posted top-up/transfer therefore adds one offsetting entry (a top-up corrected from 1000 to 800 posts a `credit` of 200), and the signed sum of the log always equals `current_amount`. `reference` is `<table>:<id>:<action>` for automatic entries (e.g. `operational_cashflows:12:cashflow_created`, `revenues:7:transfer_approved`) and `manual:<note>` for manual adjustments.

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/main-cash-account` | any active user | `id`, `current_amount`, `updated_at` |
| GET | `/api/v1/main-cash-account/logs` | any active user | paginated, newest first; filters `direction`, `source` (`operational_cashflows`/`revenues`/`manual`), `source_id`, `from`/`to` (business dates in `APP_TIMEZONE`) |
| GET | `/api/v1/main-cash-account/logs/{log_id}` | any active user | one entry |
| POST | `/api/v1/main-cash-account/adjustments` | Superadmin | manual `credit`/`debit` with a `note`, e.g. the opening balance or a bank fee (audited) |

Log entries are never edited or deleted; fix a wrong manual adjustment with an opposite adjustment.

### Background posting

Cashflow/revenue endpoints do not wait for the main cash account and never lock it. Their transaction only inserts the movement into `main_cash_account_events` (a transactional outbox: it commits atomically with the cashflow/revenue, so a movement can be neither lost nor invented). The queue is then posted to the log, in a separate transaction that only locks the account row, by:

1. a FastAPI background task started after each cashflow/revenue response (normally posts within milliseconds);
2. optionally a dedicated worker, for hosts where work after the response is not reliable (for example CPU throttled once the response is sent):
   ```bash
   python -m app.scripts.main_cash_account_worker          # long running, polls every 5 s
   python -m app.scripts.main_cash_account_worker --once   # drain and exit (e.g. cron every minute)
   ```
3. the daily audit, which drains leftovers first (safety net if a process died before its background task ran).

The ledger is the same as synchronous posting: one entry per change with the same reference, and `created_at` is the time the change was committed. `GET /api/v1/main-cash-account` also returns `pending_entries` / `pending_amount` for movements committed but not posted yet; `current_amount + pending_amount` is the amount once the queue is drained. Processed events are deleted, so the table only ever holds the backlog. Manual adjustments are posted immediately.

Top-ups and transfers that existed before this table was introduced are posted by a manual, idempotent backfill after `alembic upgrade head`:

```bash
python -m app.scripts.backfill_main_cash_account --dry-run   # print what would be posted
python -m app.scripts.backfill_main_cash_account             # post it
```

It is the same engine as the daily audit (below) run in full mode. For every top-up/transfer it posts the difference between what the row should contribute and what the log already holds for it (reference `<table>:<id>:backfill`), leaves manual entries alone, and resets `current_amount` to the signed sum of the log. Re-running it posts nothing; running it after manual database edits repairs the drift. It takes the same lock as transaction writes, so it is safe while the API is running.

## Daily main cash account audit

`app/scripts/audit_main_cash_account.py` cross-checks `main_cash_account_logs` against its sources in both directions and recalculates `current_amount`. The service has no scheduler, so run it from cron (or any external scheduler):

```cron
# every day 01:15 — only what changed since the last successful run
15 1 * * *  cd /srv/api && .venv/bin/python -m app.scripts.audit_main_cash_account --mode incremental
# every Sunday 02:15 — re-check everything
15 2 * * 0  cd /srv/api && .venv/bin/python -m app.scripts.audit_main_cash_account --mode full
```

What it checks:

- **source -> log**: every paid `MAIN_ACCOUNT_TOPUP` and paid `MAIN_ACCOUNT_TRANSFER` must appear in the log with its signed effect;
- **log -> source**: every logged source entry must still be justified, so entries whose source was edited, unpaid again or deleted are corrected too;
- movements still queued for background posting count as logged (never double posted); a non dry run drains the queue first;
- manual adjustments (`manual:...`) are never touched.

Each difference is fixed with one compensating entry referenced `<table>:<id>:audit`, and every run is recorded in `main_cash_account_audit_runs` (also readable through `GET /api/v1/main-cash-account/audit-runs`). Options: `--dry-run` (report only), `--batch-size`, `--overlap-hours`, `--fail-on-discrepancy` (exit 2 when differences are found, for alerting). Exit codes: `0` ok, `1` failed, `2` discrepancies with `--fail-on-discrepancy`, `3` another run is still in progress.

### How it stays fast on millions of rows

| Technique | Why |
| --- | --- |
| Comparison runs in PostgreSQL (`FULL OUTER JOIN` per batch) | only mismatching rows come back; Python memory stays flat (a few MB) instead of growing with the table |
| Partial indexes on `operational_cashflows` / `revenues` | the audit only reads main account rows, never the other millions of transactions |
| Generated `source_table` / `source_id` columns on the log | the log joins to its source by an indexed key instead of parsing `reference` per row |
| Keyset batches, one short transaction each, no cashflow/revenue write lock | each comparison is a single statement over sources + log + queue, which is consistent without locking writers; only the account row is locked while posting corrections |
| Set-based inserts for corrections | row-by-row ORM posting of 80k entries took >13 min; the same work set-based takes seconds |
| `incremental` mode (watermark) | a daily run only audits sources touched since the previous successful run, plus `--overlap-hours` for transactions that were still open during it. A correction to an old row still has a fresh `updated_at`, so it is caught — unlike a fixed period cut-off |
| Checkpointed amount | the log is append-only, so an incremental run adds only the entries written since the last run. If it found any discrepancy (meaning something was edited outside the API) it recomputes the total from the whole log |

Measured on 1,000,000 operational cashflows + 300,000 revenues (80,000 of them main account rows) on a laptop-class database:

| Run | Time | Python memory |
| --- | --- | --- |
| First seeding (posts 80,000 entries) | 31 s | 14 MB |
| Weekly full sweep, all consistent | 2.7 s | 4 MB |
| Daily incremental (~13,000 rows touched) | 0.3 s | 2 MB |
| Full sweep with 100 planted discrepancies | 2.2 s (all fixed) | 4 MB |

The audit does not take the cashflow/revenue write lock, so API writes are not delayed by it.

If the full sweep ever becomes too slow, lower `--batch-size` (shorter locks, more batches), run `--mode full` less often, and rely on `--mode incremental` daily.

## Tests and checks

Set `DATABASE_URL` to a disposable PostgreSQL database whose name ends in `_test`. Tests refuse other database names.

```bash
pytest
ruff check app tests
mypy app
python -m compileall -q app alembic
```
