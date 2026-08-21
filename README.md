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

### Docker — the command sequence

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo_data
```

That is the whole setup. The third command prints the credentials you
authenticate with. Verify:

```bash
curl -s localhost:8000/api/v1/health/
# {"status":"ok","database":"ok"}

TOK=$(curl -s -X POST localhost:8000/api/v1/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"demo12345"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access'])")

curl -s -H "Authorization: Bearer $TOK" localhost:8000/api/v1/assets/
```

Five services: **web** (Django), **db** (PostgreSQL 16), **redis**, **worker**
(Celery) and **beat** (Celery Beat, which A4's hourly schedule needs). `web`
waits for `db` and `redis` to report healthy before it starts, so the `migrate`
above never races the database coming up.

Tests inside the container, against PostgreSQL rather than SQLite:

```bash
docker compose exec web pytest
```

Tear down, including the database volume:

```bash
docker compose down -v
```

**If port 8000 is taken**, prefix with `WEB_PORT=8090` — e.g.
`WEB_PORT=8090 docker compose up -d --build` — and use that port in the curls.
Neither PostgreSQL nor Redis is published to the host at all, so they cannot
collide with ones you already run. To inspect the database:
`docker compose exec db psql -U fieldassets -d fieldassets`.

**I am facing issue in postgress DB in my PC beacuse I have port issue and I forgot the postgress password, so I use sqllite DB for testing**

### Locally, without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo_data      # data + the account you get a token for
python manage.py runserver
```

Without `POSTGRES_HOST` set, the app falls back to SQLite, so this needs no
services at all. Celery still needs a Redis: `docker compose up -d redis` is
enough. Then, in two more terminals:

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

**On PostgreSQL none of that applies** — `SELECT ... FOR UPDATE` is real
row-level locking and the partial index is native. `settings.py` picks the
backend from `POSTGRES_HOST`: PostgreSQL under docker-compose, SQLite otherwise
so the suite and a bare `runserver` need no services. `_is_open_checkout_conflict`
recognises both backends' wording for the violation.

Verified on PostgreSQL 16 in the compose stack: eight concurrent requests for
one asset returned **one `201` and seven `409`s**, leaving exactly one open
check-out row.

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

## A6 — seed data

```bash
python manage.py seed_demo_data
# or: docker compose exec web python manage.py seed_demo_data
```

One command, and the API is ready to exercise — including the account you
authenticate with, since every endpoint but `/health/` needs a JWT. It prints
the credentials and the `curl` that turns them into a token.

| | Seeded | Brief asks for |
|---|---|---|
| assets | 10, across all four categories | at least 8, all four |
| employees | 5, one inactive | at least 4, one inactive |
| currently overdue | 2 | at least 2 |
| returned on time | 2 | at least 2 |
| returned late | 1 | at least 1 |

Statuses are **derived** from the check-outs rather than written by hand, so the
seed cannot produce the state rule 5 forbids — an open check-out sitting next to
an `AVAILABLE` asset. Two tests assert that in both directions.

The data is arranged so every rule can be walked without setting anything up:

| Try | Expect | Rule |
|---|---|---|
| `asset_tag: CAM-001` (held) | `409` | 1 |
| `asset_tag: SEN-003` (maintenance) | `409` | 1 |
| `employee_code: EMP004` (inactive) | `400` | 2 |
| `employee_code: EMP001` (already holds three) | `409` | 3 |
| `asset_tag: NOPE-999` | `404` | 8 |
| `asset_tag: SEN-002`, `employee_code: EMP005` | `201` | happy path |

`EMP003` has the three returned check-outs, so
`/employees/EMP003/summary/` returns a real `mean_hold_days` (13.67) rather than
a zero, and `/reports/overdue/` returns `EMP002`'s two.

### Re-runnable

Assets and employees are matched on their business keys and updated in place.
The seeded check-outs are rebuilt each run — a check-out has no natural key, and
the rule 7 partial unique index would rightly reject a second open row for an
asset that already has one. Only rows belonging to seeded employees are cleared,
so anything you created yourself is left alone.

Verified by running it four times and diffing the full state — asset statuses,

## A5 — tests

**pytest-django**, 82 tests.

```bash
pytest
```

The brief allowed either runner; this is pytest-django, so `pytest` is the entry
point. The tests use pytest fixtures and `parametrize`, so `manage.py test` will
not collect them — `pytest.ini` holds the settings module and the test path.

| File | Covers |
|---|---|
| `tests/test_concurrency.py` | rule 7: 2-way and 5-way races, the race over HTTP, and the index asserted directly |
| `tests/test_checkout_limit.py` | the three-open-check-outs limit, plus rules 1, 2, 4, 5, 6, 8 |
| `tests/test_overdue.py` | the overdue calculation, incl. **due exactly now**, ordering, the N+1 guard, pagination |
| `tests/test_employee_summary.py` | the four numbers against controlled data, and that they cost one query |
| `tests/test_tasks.py` | idempotency across 2 and 5 runs, day rollover, batching, and a 4-worker race |
| `tests/test_auth_and_health.py` | unauthenticated health, 401 everywhere else, JWT obtain/refresh |
| `tests/test_assets.py` | create, filter, search, pagination, `current_holder` |

The suite runs on SQLite. `DATABASES['default']['TEST']` points at a **file**, not
the default shared-cache in-memory database: shared-cache SQLite raises
`SQLITE_LOCKED`, which no busy timeout retries, so the concurrency tests would
fail for a reason unrelated to the rule under test.

### The tests were checked for being vacuous

A test that cannot fail is worse than no test, so each guarantee was mutated and
the suite re-run:

| Mutation | Result |
|---|---|
| overdue boundary `<=` → `<` | **4 failures** across the report, the task and the summary |
| `MAX_OPEN_CHECKOUTS_PER_EMPLOYEE` 3 → 4 | **1 failure** — the fourth check-out stops being rejected |
| weaken `uniq_open_checkout_per_asset` | **2 failures** — the rule 7 guarantee tests |
| weaken `uniq_notice_per_checkout_per_day` | **1 failure** — the 4-worker race duplicates notices |
| delete the task's `.exclude()` filter | **1 failure**, and *not* an idempotency one |

That last row is the informative one. With the filter gone the idempotency tests
still pass — only the test asserting a repeat run reports `examined: 0` fails.
That is the split working exactly as designed: the filter is an optimisation,
the index is the guarantee.

The boundary mutation also caught a real defect while this was being written.
`currently_overdue` in the employee summary had its **own** copy of the
`due_at <= now` predicate, so the "single definition" claim was false and the
summary would not have moved with the report. `queries.open_and_overdue()` now
takes a `prefix` so the summary applies literally the same predicate across the
relation; the mutation now fails all three consumers instead of two.

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
