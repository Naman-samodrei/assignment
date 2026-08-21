# Field Asset Check-Out Service

Artikate Backend Developer (Python/Django) take-home — Part A.

## Authentication

**JWT**, via `djangorestframework-simplejwt`. Every endpoint under `/api/v1/`
requires a bearer token except `GET /health/`.

```bash
TOK=$(curl -s -X POST localhost:8000/api/v1/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"demo12345"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access'])")

export H="Authorization: Bearer $TOK"
```

Access tokens last 60 minutes; `POST /api/v1/auth/token/refresh/` with
`{"refresh": ...}` issues a new one.

## Run it

### Docker (brings up Redis, the worker and Beat too)

```bash
docker compose up -d --build          # WEB_PORT=8090 docker compose up -d  if 8000 is taken
docker compose exec web python manage.py createsuperuser
docker compose logs -f worker beat
```

Four services: `web`, `redis`, `worker` (Celery), `beat` (Celery Beat). `web`
runs `migrate` itself before serving. Redis is deliberately **not** published to
the host — only the app containers talk to it, at `redis:6379` on the compose
network — so the stack cannot collide with a Redis you already run.

### Locally, without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser     # the account you get a token for
python manage.py runserver
```

Celery needs a Redis to talk to; `docker compose up -d redis` is enough. Then,
in two more terminals:

```bash
celery -A server worker --loglevel=info
celery -A server beat   --loglevel=info
```

## Endpoints

All under `/api/v1/`. List endpoints are paginated at 20 per page.

| Method & path | Auth | Notes |
|---|---|---|
| `POST /assets/` | yes | create an asset |
| `GET /assets/` | yes | `?status=` `?category=` filters, `?search=` over name + `asset_tag` |
| `GET /assets/{id}/` | yes | includes `current_holder`, null when available |
| `POST /checkouts/` | yes | `{asset_tag, employee_code, due_at}` → `201`. Rules 1–5, 7, 8 |
| `POST /checkouts/{id}/return/` | yes | `{condition_note, needs_maintenance}` → `200`. Rule 6 |
| `GET /employees/{employee_code}/summary/` | yes | four database-computed numbers |
| `GET /reports/overdue/` | yes | most overdue first |
| `GET /health/` | **no** | database connectivity |
| `POST /auth/token/`, `POST /auth/token/refresh/` | **no** | obtain / refresh a JWT |

Errors use DRF's default bodies: `{"detail": ...}` for a flat error and
`{"field": [...]}` for field validation. The status code is what carries the
rule — 409 for a conflict, 400 for bad input, 404 for an unknown key.

## A2 — the business rules

Every rule lives in [`fieldassets/services.py`](fieldassets/services.py), not in
a view, so the same guarantees hold from a management command or the shell.
Views parse, delegate, serialise.

| # | Rule | Status | Where |
|---|---|---|---|
| 1 | asset not `AVAILABLE` cannot be checked out | `409` | `check_out_asset`, after the row lock |
| 2 | `is_active=False` employee cannot check out | `400` | `check_out_asset` |
| 3 | at most three open check-outs per employee | `409` | `open_checkout_count` |
| 4 | `due_at` in the future, ≤ 30 days out | `400` | `validate_due_at` |
| 5 | row + status change are all-or-nothing | — | `@transaction.atomic` over the whole function |
| 6 | return closes the row and frees or quarantines the asset; second return conflicts | `200` / `409` | `return_checkout` |
| 7 | one winner per asset under concurrency | `201` / `409` | row lock + partial unique index |
| 8 | unknown `asset_tag` / `employee_code` | `404` | `_get_locked_asset`, `_get_locked_employee` |

Rules 3 and 4 read their limits from `MAX_OPEN_CHECKOUTS_PER_EMPLOYEE` and
`MAX_DUE_AT_HORIZON_DAYS` in settings, so the boundary is stated in one place.

### Rule 7, concurrency — two layers, on purpose

`check_out_asset` takes `SELECT ... FOR UPDATE` on the asset row *before*
reading its status. A second request for the same asset blocks there, then reads
the committed `CHECKED_OUT` status and loses with a `409`. Locks are always
taken asset-then-employee, so two check-outs can never hold one and wait on the
other. Locking the employee row closes the same race on rule 3 — two
simultaneous check-outs of *different* assets by someone already holding two.

Underneath the lock, `CheckOut` carries a partial unique index:

```python
UniqueConstraint(
    fields=["asset"],
    condition=Q(returned_at__isnull=True),
    name="uniq_open_checkout_per_asset",
)
```

The lock produces the correct **status code**; the index guarantees
**correctness** even if a future code path reaches the insert without it. That
is why the insert runs in its own savepoint — the loser's `IntegrityError` is
translated into the same `409` instead of escaping as a `500`.

`select_for_update()` is a no-op on SQLite, so the `DATABASES` `OPTIONS` do the
equivalent work at the connection level: WAL journalling plus
`transaction_mode: "IMMEDIATE"`, which makes a transaction take SQLite's write
lock at `BEGIN` rather than at its first write. A concurrent check-out then
*waits* for the lock instead of dying with `database is locked` halfway through.

Verified live: six concurrent requests for one asset returned one `201` and five
`409`s, leaving exactly one open check-out row.

Moving to PostgreSQL is a `DATABASES` change and nothing else. The service code
is already written for it — the `FOR UPDATE` becomes real row-level locking, the
partial index is native, and `_is_open_checkout_conflict` recognises both
backends' wording for the violation.

## A3 — the two query-constrained endpoints

Both querysets live in [`fieldassets/queries.py`](fieldassets/queries.py) so the
shape of the SQL is visible in one place.

**Employee summary** — all four aggregates hang off the same `checkouts`
relation, so the ORM emits one `LEFT JOIN` with conditional aggregates over it:

```sql
SELECT ..., COUNT(checkout.id) AS lifetime_checkouts,
            COUNT(checkout.id) FILTER (WHERE returned_at IS NULL) AS currently_held,
            COUNT(checkout.id) FILTER (WHERE returned_at IS NULL AND due_at < ...) AS currently_overdue,
            AVG(returned_at - checked_out_at) FILTER (WHERE returned_at IS NOT NULL)
```

Measured with `CaptureQueriesContext`: **1 query**, no Python loop.

**Overdue report** — `select_related("asset", "employee")` resolves both joins in
the same `SELECT`, and `days_overdue` is computed by the database as part of it.
Measured at 2 rows and again at 32 rows: **1 query both times**, so the row count
does not move the query count.

## A4 — the hourly overdue sweep

[`fieldassets/tasks.py`](fieldassets/tasks.py) holds `flag_overdue_checkouts`.
It finds every open, overdue check-out and creates an `OverdueNotice` dated
today. Run it on demand and watch it be idempotent:

```bash
docker compose exec web python manage.py shell -c "
from fieldassets.tasks import flag_overdue_checkouts
for i in range(5):
    print(flag_overdue_checkouts.delay().get(timeout=30))
"
# {'notice_date': '...', 'examined': 5, 'created': 5}
# {'notice_date': '...', 'examined': 0, 'created': 0}   x4
```

That dispatches through Redis and executes on the `worker` container — verified
working, not merely unit-testable.

### Idempotency — two layers, same shape as rule 7

```python
UniqueConstraint(fields=["checkout", "notice_date"],
                 name="uniq_notice_per_checkout_per_day")
```

The queryset `.exclude(notices__notice_date=today)` means a repeat run does no
work at all, which keeps the common case cheap. But that check can lose a race
between two workers, so the unique index is what makes idempotency *true*;
`bulk_create(ignore_conflicts=True)` turns the resulting rejection into a no-op
rather than an exception.

The split is measurable. With four workers racing the same sweep from a clean
slate, **every one** of them passed the `.exclude()` filter and tried to insert
all 29 rows — layer one caught nothing. The database still ended with 29
notices and zero duplicates, because the index caught all 87 duplicate inserts.

`created` in the return value is a diagnostic, not a guarantee: it is a
before/after count taken outside any transaction, so in that same 4-way race
each worker reported `created: 29` while the database held 29 rows total. The
hourly Beat schedule never does that; the row count is the authoritative answer.

The task streams with `.iterator()` and inserts in batches of 500, so memory is
flat whether there are ten overdue check-outs or a hundred thousand.

### The schedule

```python
CELERY_BEAT_SCHEDULE = {
    'flag-overdue-checkouts-hourly': {
        'task': 'fieldassets.tasks.flag_overdue_checkouts',
        'schedule': crontab(minute=0),          # hourly, on the hour
    },
}
```

Verified end to end by temporarily running a throwaway `beat` container with a
5-second schedule: Beat logged five `Sending due task` lines, the worker logged
five completions, and the notice count stayed at 5 with zero duplicates.

## Assumptions

Judgement calls where the brief left room:

1. **Validation order.** A malformed `due_at` is rejected by the serializer
   before the service looks anything up, so a request with both a bad `due_at`
   and an unknown `asset_tag` is a `400`, not a `404`. Within the service the
   order is: resolve keys (`404`), employee active (`400`), asset status
   (`409`), hold limit (`409`).
2. **Rule 4's bounds.** "In the future" is exclusive — `due_at == now` is a
   `400`. "No more than 30 days ahead" is inclusive of exactly 30 days.
3. **Rule 3 counts open check-outs only.** Returning one frees a slot
   immediately.
4. **`mean_hold_days` is `0.0`, not `null`, for an employee with nothing
   returned.** SQL `AVG` over an empty set is `NULL`, but the endpoint contract
   is *four numbers*, so `0.0` is the more consistent answer.
5. **"Overdue" means `due_at <= now`, not `<`.** A check-out due at exactly
   this instant has reached its deadline. `queries.open_and_overdue()` is the
   single definition, shared by the A3 report, the A3 summary's
   `currently_overdue`, and the A4 task, so the three can never disagree.
6. **`days_overdue` is fractional**, rounded to two decimals, rather than whole
   days — "days overdue" is more useful at sub-day precision for something due
   this morning.
7. **`/health/` returns `503` when the database is unreachable**, and `200` with
   `{"status": "ok", "database": "ok"}` otherwise. The brief only specifies the
   healthy case; a health check that reports `200` while the database is down
   would be worse than useless to a load balancer.
