# Farm Management API

Synchronous FastAPI/PostgreSQL replacement for the transactional parts of the former Laravel dashboard. The service contains no frontend, reports, Excel generation/import, ABK integration, background worker, or scheduler. The only derived analytics are the Lele biomass/ADG projections described below.

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

- `cash_on_hand`: the real cash on hand. A paid `MAIN_ACCOUNT_TOPUP` adds `amount_idr`; any other type subtracts only what was actually paid on it: its `amount_idr` once `PAID`, the sum of its non-voided payments while `PARTIALLY_PAID`. Each partial payment subtracts its own amount, a void gives it back, and correcting the amount of a partially paid cashflow does not move the balance.
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
- **debit**: a loan repayment (`loan_repayments`), for its `amount`; voiding it posts the offsetting **credit**. See [Loans](#loans).

Each write posts only the difference between the row's effect before and after. A Superadmin correction of a posted top-up/transfer therefore adds one offsetting entry (a top-up corrected from 1000 to 800 posts a `credit` of 200), and the signed sum of the log always equals `current_amount`. `reference` is `<table>:<id>:<action>` for automatic entries (e.g. `operational_cashflows:12:cashflow_created`, `revenues:7:transfer_approved`, `loan_repayments:3:loan_repaid`) and `manual:<note>` for manual adjustments.

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/main-cash-account` | any active user | `id`, `current_amount`, `updated_at` |
| GET | `/api/v1/main-cash-account/logs` | any active user | paginated, newest first; filters `direction`, `source` (`operational_cashflows`/`revenues`/`loan_repayments`/`manual`), `source_id`, `from`/`to` (business dates in `APP_TIMEZONE`) |
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

- **source -> log**: every paid `MAIN_ACCOUNT_TOPUP`, paid `MAIN_ACCOUNT_TRANSFER` and non-voided loan repayment must appear in the log with its signed effect;
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

## Cash statements (finance statement)

`cash_statements` is the bank-statement view of the business: one row per **real cash movement**, on the date the money actually moved, with `credit` = money in and `debit` = money out. It is the data source of the finance module (monthly cashflow, revenue performance, loans, journal reporting). Nothing is recorded for drafts, submissions, approvals or the unpaid part of a transaction.

| Source | Rows | Direction | Date |
| --- | --- | --- | --- |
| `operational_cashflows`, every type except `MAIN_ACCOUNT_TOPUP` | once `PARTIALLY_PAID`/`PAID`: one row per non-voided payment, for that payment's amount | debit | payment `paid_date` |
| `revenues`, every type except `MAIN_ACCOUNT_TRANSFER` | once `PARTIALLY_PAID`/`PAID`: one row per non-voided payment; an approved `DIRECT_TRANSFER` sale (no payment rows) is one row for its amount | credit | payment `paid_date` / transfer date |
| `revenues`, `MAIN_ACCOUNT_TRANSFER` | once approved (`PAID`) and only if it has a fee: one row for `payment_cost_idr` only | debit | paid date |
| `main_cash_account_logs` | manual adjustments (`manual:<note>`) only; entries posted automatically for top-ups/transfers/loan repayments are excluded | their own | posting date |
| `loans` (non-voided `loan_repayments`) | one row per repayment for its full amount (`LOAN_REPAYMENT`), reference `loans:<loan id>`; the principal/interest split stays in the loan tables | debit | repayment `paid_date` |

Top-ups and main account transfers are excluded because they only move money between the business's own accounts; the transfer fee is the part that actually leaves. A `PAID` transaction without any payment row (recorded outside the API) is one row for its full amount on its paid date.

**Partial payments.** A transaction paid in parts has one row per payment, each for the amount of that payment only. The rows share the same `reference` (`<source table>:<source id>`, e.g. `operational_cashflows:12`) and their descriptions are numbered: `Utility: Electricity (partial payment 1)`, `(partial payment 2)`, `(final payment 3)`; a transaction settled by a single payment has no suffix. A voided payment disappears and the remaining ones are renumbered.

**Source details.** Every row also stores `category` (the cashflow/revenue type, `TRANSFER_FEE` or `MANUAL_ADJUSTMENT`), a `description` built from the transaction details, the `counterparty` (customer of a sale, employee of a salary), and where it comes from: `source_table`, `source_id`, `source_line` (`payment:<payment id>`, `paid`, `fee`, `manual`), `payment_id` and `payment_sequence` for payment rows, and the transaction's current `source_transaction_date`, `source_amount` (a transfer fee: the transfer's gross amount) and `source_payment_status`. `(source_table, source_id, source_line)` is unique, which is what makes the population idempotent.

The cashflow debits explain the cash on hand: `cash_on_hand` = paid top-ups - the `operational_cashflows` debits of the statement.

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/cash-statements` | any active user | paginated statement, newest first; each row has `debit`/`credit`, the running `balance` after it (over the whole statement, whatever the filters), `reference` and `source` details; filters `direction`, `category`, `reference`, `source_table` + `source_id`, `from`/`to` (statement dates) |

### Population: nightly, not live

`cash_statements` feeds the finance module's reports (monthly cashflow, revenue performance, loans, journal), none of which read it synchronously from a write path, so unlike the balances in [Operational cashflow balances](#operational-cashflow-balances) and the [main cash account](#main-cash-account) it is **not** kept current inside the cashflow/revenue/loan write that changes its source: the API never waits for it, and the statement may be up to a day behind. It is synced once a day, one month at a time, from cron (the service has no scheduler):

```cron
# every day at 00:00, server time - see APP_TIMEZONE in the environment
0 0 * * *  cd /srv/api && .venv/bin/python -m app.scripts.sync_cash_statements
```

**Which month.** By default the month of the job's start time **minus 30 minutes**, in `APP_TIMEZONE`. A run at 00:00 on 1 August reads 23:30 on 31 July and syncs **July** (whose last day just ended) instead of an August that has no data yet; a run that starts up to 30 minutes late lands on the same month, and every other night it is simply the current month. Nothing is skipped at the month boundary: changes made on the last day of a month are also in the next month's window (the one-day overlap under **Which data**). `--month YYYY-MM` syncs another month (e.g. to catch up a night that did not run); `--dry-run` reports without writing.

**Which data.** Only that month's transactions are re-derived, never the whole history. A transaction is in the month's scope when:

- money moved on it during the month (a payment, repayment, paid date or manual adjustment dated in it);
- it, one of its payments/repayments, or its lender was changed during the month (`updated_at` from one day before the 1st, so a write around midnight is never lost between two months) - e.g. a payment of last month voided this month, or a correction;
- it already has a statement row dated in the month (so a row whose source was deleted or moved away is removed).

All rows of an in-scope transaction are rebuilt, whatever their date, so "partial payment 2" stays numbered across months; every other transaction is left alone. Changes none of these catch (e.g. a customer renamed long after its sales) are repaired by the backfill below.

Midnight is deliberate: the sync takes the same advisory lock as every cashflow/revenue/loan write (so it briefly queues them behind it while it runs), and midnight is normally the quietest point of the day. Cron has no notion of `APP_TIMEZONE`; schedule the job in whatever timezone the server/cron actually runs in so it lines up with it (set the timezone of the host, or the job's `CRON_TZ`/systemd-timer `OnCalendar` timezone, to `APP_TIMEZONE`). The sync writes only differences (missing rows inserted, changed rows updated in place keeping their `id`, rows no longer justified deleted), so it is idempotent: running it twice, late, or by hand is harmless, and it is safe while the API is live.

**Run history.** Every run (dry and failed runs included) is recorded in `cash_statement_sync_runs`: the month (`period_start`/`period_end`), `changed_since`, `status` (`RUNNING` -> `SUCCESS`/`FAILED`; a row left `RUNNING` means the process died), `dry_run`, `started_at`/`finished_at`, the transactions checked per source (`cashflows_checked`, `revenues_checked`, `adjustments_checked`, `loans_checked`, `transactions_checked`), the rows `rows_inserted`/`rows_updated`/`rows_deleted`, the month's statement once synced (`period_rows`, `period_credit`, `period_debit`) and the `error` of a failed run. There is no API for it; maintainers query the table directly, e.g.:

```sql
-- the last runs, and any night that failed or never finished
SELECT id, period_start, status, dry_run, started_at, finished_at,
       transactions_checked, rows_inserted, rows_updated, rows_deleted, error
FROM cash_statement_sync_runs ORDER BY started_at DESC LIMIT 30;
```

**Backfill (whole history).** Transactions that existed before the table was introduced, or any other drift, are repaired on demand with the one-off backfill, which re-derives every transaction ever recorded, prints its report and is not recorded in `cash_statement_sync_runs`:

```bash
python -m app.scripts.backfill_cash_statements --dry-run   # print what would change and the resulting totals
python -m app.scripts.backfill_cash_statements             # write it
python -m app.scripts.backfill_cash_on_hand --type cash_on_hand   # once: recompute cash on hand with the partial payment rule
```

## Monthly cut-off

The month-end close for the finance team: the month's figures for the finance journal report, aggregated from `cash_statements` and saved as a snapshot. It runs from cron at midnight at the end of every month (the service has no scheduler):

```cron
# 00:00 on the 1st of every month, server time (= APP_TIMEZONE, see the nightly sync above)
0 0 1 * *  cd /srv/api && .venv/bin/python -m app.scripts.monthly_cutoff
```

Like the nightly sync, the month is read from the start time minus 30 minutes, so the run at 00:00 on 1 August closes **July**. It first syncs that month's cash statement (recorded in `cash_statement_sync_runs`, so the close never works on a statement that is up to a day behind), then computes everything **as of the month's last day** on a **cash basis**. It can share the minute with the nightly sync: both take the same lock and the sync is idempotent.

| Figure | How |
| --- | --- |
| Actual cash | `cash_opening` + `opening_balances_in_period` + `cash_in` - `cash_out` = `cash_closing`. `cash_in`/`cash_out`: the month's statement credits/debits. `cash_opening`: the net of every statement row before the month plus the go-live opening balances (`financial_opening_balances`) effective before it |
| Profit and loss | revenue = credits of `revenues` per category; operating expenses = debits of `operational_cashflows` per category; other expenses = main account transfer fees; finance costs = the **interest** part of the month's loan repayments; `net_profit` = revenue - expenses. `NON_PL` lines list the cash that is neither income nor expense (loan principal repaid, manual adjustments in/out) |
| Outstanding loans | every loan not cancelled and started by the month end: principal/interest paid and outstanding, overdue installments, and what was repaid during the month, from the repayments dated up to the month end (allocated like the loan module) |
| Receivables | per customer, what sales dated up to the month end still had to be paid then (sale amount - statement credits of that sale up to the month end), aged 0-30/31-60/61-90/over 90 days from the sale date. Direct-transfer sales not approved yet are `pending_verification`; rejected ones are ignored |
| Categories | the month's statement rows grouped by direction, source table and category |

**No double counting.** The business has one pool of money in several pockets (main cash account, operational cash on hand, sales cash not deposited yet); moves between pockets (top-ups, transfers) are not on the statement, so the statement's credits/debits are the business's cash movements. Therefore:

- `cash_closing` never adds `main_cash_accounts.current_amount` or the `cash_on_hand` balance (parts of the same money). The only non-statement amounts added are the go-live opening balances, which no statement row covers (`opening_balances_total` shows how much of the closing they are). The main account's own opening balance is a manual adjustment, so it is already a statement row and is not added again;
- a `LOAN_REPAYMENT` statement row is principal + interest: the P&L takes only the interest (from the loan allocation), never the row; the principal is `NON_PL`;
- collections of receivables are the statement's credit rows of each sale, not `revenue_payments` counted again;
- manual adjustments (opening balance, loan proceeds, bank fees entered by hand) are cash but cannot be classified as income or expense, so they are `NON_PL` lines.

Every run checks that the P&L reconciles with the cash movement (`net_profit - loan principal + manual in - manual out = cash_in - cash_out`) and that the month's loan repayments match the statement's `LOAN_REPAYMENT` rows. A check that fails does not stop the close; it is saved in `warnings`.

**Snapshots.** Every run saves a new, immutable snapshot: `monthly_cutoffs` (status, the sync run it used, warnings and the headline figures) with `monthly_cutoff_categories`, `monthly_cutoff_profit_loss`, `monthly_cutoff_loans` and `monthly_cutoff_receivables` (names are copied, so later renames do not change a closed month). The month's reported snapshot is the one with `is_latest` (at most one per month). Re-running a closed month after late or backdated entries (`--month YYYY-MM`) saves a new snapshot that becomes `is_latest`; the earlier ones are kept. A failed run is saved as `FAILED` with its `error` and never becomes `is_latest`. `--dry-run` computes and logs the figures without saving them. A month that has not ended cannot be closed (exit code 2).

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/monthly-cutoffs?year=YYYY&month=M` | `ADMIN`, `SUPERADMIN` | the reported close (`is_latest`) of **one** month: `period`, `revision` (how many times the month was closed; > 1 = restated), `generated_at`, `warnings`, `cash`, `profit_and_loss` (totals and `lines`), `loans` (totals and `items`), `receivables` (totals and `customers` with `aging`), `categories`. Amounts are whole-IDR strings |

`year` (2000-2100) and `month` (1-12) are both required and may be given once only: there is no range, list or "all months" form, so one call always returns exactly one month (`?month=7&month=8` is a `422`). A month that has not been closed, or whose only runs failed, is a `404`. The endpoint only reads the saved snapshot; it never computes or closes a month (that is the cron job).

Maintainers can also query the tables directly, e.g.:

```sql
-- the reported close of each month
SELECT period_start, cash_opening, cash_in, cash_out, cash_closing, revenue, net_profit,
       loan_principal_outstanding, receivables, warnings
FROM monthly_cutoffs WHERE is_latest ORDER BY period_start DESC;

-- its P&L, in report order
SELECT section, line, amount FROM monthly_cutoff_profit_loss
WHERE cutoff_id = (SELECT id FROM monthly_cutoffs WHERE is_latest AND period_start = '2026-07-01')
ORDER BY position;
```

## Loans

`loans` records the loans taken by the business, `loan_installments` their repayment schedule and `loan_repayments` the money paid back. Every loan belongs to a lender (`lender_id`, see [Lenders](#lenders)); a new loan, or a loan moved to another lender, needs an active one. There are two types (`loan_type`):

| | `SHORT_TERM` | `LONG_TERM` |
| --- | --- | --- |
| Installments | exactly one, for the whole principal | `term_months` monthly installments |
| Due date | `due_date`: any date from the start date (e.g. one week later) | `first_due_date`, then the same day every month (the last day of shorter months) |
| Interest | `interest_amount`: a flat IDR amount, 0 allowed. When a lender quotes a rate, enter the resulting amount | `annual_interest_rate` (fixed, percent per year) with `interest_method` |

Each type only accepts its own fields (a rate on a short-term loan, or an interest amount on a long-term one, is a `422`). The long-term interest methods:

- `FLAT` (bunga flat): principal / term plus interest on the **original** principal (principal x rate / 12) every month. 12,000,000 at 12 % for 12 months = 1,000,000 + 120,000 each month.
- `ANNUITY` (bunga anuitas/efektif): the same installment every month, interest on the **outstanding** balance, so the interest part shrinks. 12,000,000 at 12 % for 12 months = 1,066,185 each month (the last one absorbs the rounding).

Amounts are whole IDR and the principal parts always add up to the principal exactly.

Every field can be edited, including the type. New terms regenerate the schedule and the existing repayments are re-allocated to it, so installment statuses and the loan status follow the edit (money never moves on an edit). An edit that would make the loan total (principal + interest of the new schedule) smaller than what has already been repaid is rejected with a `422` on `principal_amount` stating both amounts. A loan entered by mistake can be cancelled while it has no repayment.

**Repayments.** The amount is entered manually. A repayment is applied to the oldest installment first and, within an installment, to its interest before its principal; whatever is left goes to the next installment, which becomes `PARTIALLY_PAID` (e.g. 1,500,000 on 1,120,000 installments: installment 1 `PAID`, installment 2 `PARTIALLY_PAID` with 380,000 paid and 740,000 still due). One repayment can therefore pay part of an installment or several of them. Without an `amount` it pays what is still due on the next installment. It cannot be in the future, before the loan starts, or more than what is still owed. The allocation is re-derived from all non-voided repayments (in payment date order) after every repayment or void, so every split stays consistent. A loan whose installments are all paid is `PAID_OFF`; voiding a repayment makes it `ACTIVE` again.

Each repayment:

- is, in the same database transaction, a **debit** of the main cash account, posted like top-ups/transfers through the background queue with reference `loan_repayments:<id>:loan_repaid`; a void posts the offsetting credit (`loan_repayment_voided`). The daily audit reconciles repayments like the other sources;
- appears in `cash_statements`, once the nightly sync picks it up (see [Population: nightly, not live](#population-nightly-not-live)), as **one** `LOAN_REPAYMENT` debit for its full amount (e.g. `Loan repayment: Bank BRI KUR-001 (installments 2-3)`), reference `loans:<loan id>`, counterparty the lender. How much of it was principal and interest is kept on both the repayment (`principal_amount`/`interest_amount`, plus the installments it went to: `first_installment`/`last_installment`) and the installments (`principal_paid`/`interest_paid`); both are re-derived together and always add up to the same totals. A short-term loan's row has no installment suffix (`Loan repayment: Koperasi Maju`).

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/loans` | any active user | paginated, newest start first, each with its `lender` (`id`, `name`) and a `summary` (paid/outstanding principal and interest, next installment, overdue installments/amount); filters `loan_type`, `status`, `lender_id` |
| GET | `/api/v1/loans/summary` | any active user | outstanding principal/interest, overdue amount and next due date of the active loans, in total and per loan type |
| GET | `/api/v1/loans/{loan_id}` | any active user | one loan with its repayment schedule (`installments`: due date, principal, interest, paid, outstanding, status, overdue), its `repayments` (paid and voided) and audit log |
| POST | `/api/v1/loans` | Superadmin | create a loan (short-term or long-term fields); its schedule is generated |
| PATCH | `/api/v1/loans/{loan_id}` | Superadmin | edit; repayments are re-allocated, rejected below the repaid amount |
| POST | `/api/v1/loans/{loan_id}/cancel` | Superadmin | cancel a loan without repayments, with a `reason` |
| POST | `/api/v1/loans/{loan_id}/repayments` | Superadmin | record a repayment: `paid_date`, optional `amount`, `notes` |
| PATCH | `/api/v1/loans/{loan_id}/repayments/{repayment_id}/void` | Superadmin | void a repayment with a `reason` |

Receiving the loan money is not posted automatically: record it as a manual main account adjustment (`POST /api/v1/main-cash-account/adjustments`, `credit`). Long-term rates are fixed and installments monthly; an early payoff pays the scheduled interest in full.

## Lenders

`lenders` holds the banks, cooperatives or people the business borrows from. Only `name` is required; it is unique regardless of case (`Bank BRI` and `bank bri` are the same lender, a duplicate is a `409 LENDER_EXISTS`). `contact_person`, `phone`, `email`, `address` and `notes` are optional (blank values are stored as `null`). Lenders are never deleted: `is_active = false` keeps the lender and its loans but refuses new loans for it. Renaming a lender updates the cash statement rows of its loans once the nightly sync runs.

Each lender is returned with its **active** loans (paid-off and cancelled loans are not owed any more) and their totals, computed exactly like the loan `summary`:

| Field | Meaning |
| --- | --- |
| `principal_amount`, `total_interest` | the full principal and the full scheduled interest |
| `total_payable` | principal + interest |
| `total_paid` | what has been repaid |
| `principal_outstanding`, `interest_outstanding`, `total_outstanding` | what still has to be repaid |
| `overdue_amount`, `next_installment` / `next_due_date` | what is late, and what comes next |

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/lenders` | any active user | paginated by name, each with `active_loans` and `active_loans_summary`; filters `search` (name or contact person), `is_active`, `has_active_loans` |
| GET | `/api/v1/lenders/{lender_id}` | any active user | one lender with its active loans |
| POST | `/api/v1/lenders` | Superadmin | create: `name` and optional fields |
| PATCH | `/api/v1/lenders/{lender_id}` | Superadmin | edit, including `is_active` |

Use `GET /api/v1/loans?lender_id=` for a lender's full loan history (including paid-off and cancelled loans).

## Lele biomass and average daily gain (ADG)

### Stocking weight

`POST /api/v1/livestock-groups` and `PATCH /api/v1/livestock-groups/{id}` accept an optional `initial_weight_g`: the weight of one fish (grams, > 0) on the group's `start_date`. It is valid only for Lele (422 for Ayam) and is stored in `livestock_groups.initial_weight_g`. On `PATCH`, omitting the field keeps the stored value and `null` clears it. A change recomputes the group's sampling results (only the first sampling's ADG changes).

### Per-sampling results

Every Lele sampling (`POST`/`PATCH /api/v1/lele-samplings`, 3 batches of 10 fish) also stores a row in `lele_sampling_results`:

| Column | Meaning |
| --- | --- |
| `fish_count`, `total_weight_kg` | sum of the three batches (30 fish) |
| `average_weight_g` | weight of one fish: `total_weight_kg * 1000 / fish_count`, e.g. 0.300 + 0.350 + 0.400 kg = 35.000 g |
| `previous_sampling_id`, `days_since_previous` | start of the ADG interval: the group's preceding sampling, or for the first sampling the stocking (`previous_sampling_id` NULL, the group's `start_date` and `initial_weight_g`) |
| `weight_gain_g`, `adg_g` | gain per fish over that interval, and the **real ADG** = gain / days |

Each real ADG is its own row and is kept: with a stocking weight, N samplings give N real ADGs (stocking→#1, #1→#2, ...); without one, N − 1 (the first sampling has none, as does a first sampling on the start date). Adding a newer sampling only inserts a row. The rows of the whole group are rebuilt on every sampling create/update and stocking weight change, so a backdated or corrected sampling also fixes the ADG of the sampling after it; rows whose values do not change are not rewritten. A negative ADG (sampling noise) is stored as measured. The same values appear as `result` in every `lele-samplings` response, with `adg_from` = `STOCKING`/`SAMPLING`/`null`, and as the `Real ADG #n` lines of the biomass chart.

### Biomass endpoint

`GET /api/v1/lele-biomass/{group_id}` (any active user) returns everything the biomass page of one group needs, with no ids or other keys. It only reads a precomputed read model (below); the only thing it calculates is today's estimate, because that changes every day without any write.

| Field | Content |
| --- | --- |
| `group` | name, location, status, start/closing date, age in days, initial and current population, initial (stocking) weight, notes |
| `latest_sampling`, `samplings` | per sampling date: day since start, weight of one fish, population and biomass (kg) = population * weight |
| `projection` | the **harvest prediction**: from the baseline (latest sampling, or the stocking when there is no sampling yet; `baseline.source` = `SAMPLING`/`STOCKING`), days to target = ceil((`LELE_TARGET_WEIGHT_G` (142 g) - baseline weight) / ADG) and `projected_sell_date`. The ADG is the **latest real ADG** (`adg_source` = `ACTUAL`); the default `LELE_DEFAULT_ADG_G` (1.3 g/day, `DEFAULT`) is only a fallback while there is no positive real ADG. Also `ready_to_sell`, `remaining_weight_g`, and `projected_biomass_kg` (current population at the target weight) |
| `estimate` | weight and biomass today (for a closed group, its closing date), growing the baseline weight by the projection's ADG, **never past the harvest date**: once it has passed, the estimate is the weight on the harvest date and `capped_at_harvest_date` is true |
| `chart` | the weight line chart, below |
| `sync` | `computed_at` of the read model and `pending` = a newer rebuild is still queued |

`chart` is ready for a line chart with weight on Y (`y_min_g` 0 to `y_max_g`, the highest value on the chart; `target_weight_g` for a 142 g reference line) and dates on X:

- `dates` (X axis): every 7 days from the start date (`week` = 0, 1, 2, ...) up to `end_date`, plus each sampling date between two weeks and the end date itself (`week` = null). `end_date` is the harvest date of the projection (`is_harvest_date`), capped at 365 days after the start. `day` is the number of days since the start and `is_sampling` marks sampling dates.
- `series`: one line per entry, each with `values` aligned with `dates` (decimal strings, `null` where the line has no point):
  - `Actual weight` (`type` = `ACTUAL`): the measured weights (stocking weight and samplings) only, `null` on the other dates, so let the chart connect the gaps (Chart.js `spanGaps`, Recharts/ECharts `connectNulls`).
  - `Default ADG` (`DEFAULT`): the default ADG from the baseline (all `null` without any known weight).
  - `Real ADG #1`, `#2`, ... (`REAL`): every real ADG, oldest first. The highest number is the latest and has `is_latest` = true: it is the harvest prediction and reaches the target on the last date. Each line starts at the sampling where its ADG was measured (`start_date`, `start_weight_g`) and continues at that ADG, so on the last date the other lines can be above or below 142 g. `period` describes the interval the ADG was measured over (`from` = `STOCKING`/`SAMPLING`, dates, days, weights, gain).
  - `adg_g`, `days_to_target` and `projected_sell_date` (when that line alone would reach the target, possibly after the chart's end; `null` if its ADG is not positive) for every ADG line.

A group with neither samplings nor a stocking weight returns `null` projection/estimate and a chart with only its start date. The response is 202 (`BIOMASS_PENDING`) when a new group's read model is still being computed, and 404 for a group that has none (not Lele, or not backfilled yet). Both settings are optional environment variables; after changing them, re-run the backfill.

### Read model and background rebuild

| Table | Rows |
| --- | --- |
| `lele_biomass_snapshots` | one per Lele group: group details, latest sampling, harvest prediction, chart bounds, `computed_at` |
| `lele_biomass_chart_dates` | the X axis of the group's chart, with the measured weight, population and biomass on measured dates |
| `lele_biomass_chart_series` | one per ADG line (default, real #n) with its ADG, start, sell date and measured interval |
| `lele_biomass_chart_values` | the weight of each line on each chart date it covers |
| `lele_biomass_jobs` | queue of groups to rebuild |

Every change that affects a group's biomass page inserts a job in the same transaction as the change (so it can be neither lost nor processed before the change is committed): group create/edit (including the initial weight)/close, sampling create/edit (both groups when a sampling moves), daily records (mortality) and population adjustments of Lele groups. The group's rows are then rebuilt from scratch in the background, so an edited sampling recalculates every dependent value (ADGs, lines, harvest date, biomass):

1. a FastAPI background task started after each of those responses (normally within milliseconds);
2. optionally a dedicated worker, for hosts where work after the response is not reliable:
   ```bash
   python -m app.scripts.lele_biomass_worker          # long running, polls every 5 s
   python -m app.scripts.lele_biomass_worker --once   # drain and exit (e.g. cron every minute)
   ```

Each group is rebuilt in its own transaction; readers see the old rows until it commits. Rebuilds of the same group are serialised and a change committed during a rebuild keeps its own job, so the read model always ends on the latest data. A group whose rebuild fails is logged and stays queued for the next trigger without blocking the others.

### Backfill

Samplings recorded before `lele_sampling_results` existed, and the read model of existing groups, are computed by a manual, idempotent backfill after `alembic upgrade head`:

```bash
python -m app.scripts.backfill_lele_sampling_results --dry-run   # print what would change
python -m app.scripts.backfill_lele_sampling_results             # write it
```

It recomputes every sampling result and rebuilds the read model of every Lele group with the same code as the API, under the same locks, so it is safe while the API is running. Re-run it to repair the data after any manual database edit, or after changing the settings. Existing groups have no stocking weight; entering one through `PATCH /api/v1/livestock-groups/{id}` recomputes that group.

## Tests and checks

Set `DATABASE_URL` to a disposable PostgreSQL database whose name ends in `_test`. Tests refuse other database names.

```bash
pytest
ruff check app tests
mypy app
python -m compileall -q app alembic
```
