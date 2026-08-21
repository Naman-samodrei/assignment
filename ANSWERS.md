# Part B — Diagnosing three broken snippets

Part A is the code in this repository; see [README.md](README.md).

Every measurement quoted below was produced by running the snippet as printed,
against the Part A models on PostgreSQL 16, inside a transaction that was rolled
back afterwards.

---

## Snippet 1 — overdue report view

```python
from django.http import JsonResponse
from django.utils import timezone

def overdue_report(request):
    checkouts = CheckOut.objects.filter(returned_at__isnull=True)
    rows = []
    for c in checkouts:
        if c.due_at < timezone.now():
            rows.append({
                "asset": c.asset.name,
                "asset_tag": c.asset.asset_tag,
                "employee": c.employee.full_name,
                "days_overdue": (timezone.now() - c.due_at).days,
            })
    rows.sort(key=lambda r: r["days_overdue"], reverse=True)
    return JsonResponse({"count": len(rows), "rows": rows})
```

### 1. What is wrong?

Ten distinct defects. Four are correctness bugs that produce **wrong numbers**,
not merely slow ones — those are the dangerous half, because no amount of load
testing reveals them.

#### Correctness

**1.1 — `.days` silently truncates, so anything overdue by under 24 hours
reports `0`.**

`timedelta.days` is the whole-day component, floored. A check-out 23 hours past
due has `(now - due_at).days == 0`. Measured, with items overdue by 9 days, 25
hours and 23 hours:

```
A-9d    days_overdue=9
C-25h   days_overdue=1     <- 25 hours becomes "1 day"
B-23h   days_overdue=0     <- 23 hours becomes "0 days"
```

An item nearly a full day late is indistinguishable from one that has just
tipped over. For a report whose purpose is chasing overdue equipment, that is
the first day of lateness — the day you most want to act on — rendered as zero.

**1.2 — The sort is wrong as a direct consequence, and "most overdue first" is
violated.**

Sorting on the truncated integer collapses every sub-24-hour item to the same
key. Python's sort is stable, so ties fall back to the queryset's order, which
is `CheckOut.Meta.ordering = ["-checked_out_at"]` — unrelated to lateness.
Measured output, in the order the endpoint returned it:

```
A-9d    days_overdue=9
C-25h   days_overdue=1
D-now   days_overdue=0     <- due this very instant
B-23h   days_overdue=0     <- 23 hours overdue, ranked BELOW the one above
```

The item 23 hours late is listed *after* the one that is zero seconds late. The
report's single ordering guarantee is broken.

**1.3 — `timezone.now()` is re-evaluated per row, so there is no consistent
cut-off.**

It is called once per loop iteration in the `if`, and again in the arithmetic
for each row that passes — 14 separate instants for the 10-row run above. Three
consequences:

- The predicate and the arithmetic use *different* clocks, so a row can be
  admitted against one instant and measured against another.
- Rows early in the loop are judged against an earlier cut-off than rows late in
  it. The report is not a snapshot of any single moment.
- Whether a check-out due at request time appears depends on how long the loop
  took to reach it. In the run above `D-now` was **included**, because by the
  time the loop reached it `timezone.now()` had advanced past its `due_at`. Move
  it earlier in the result set, or make the loop faster, and it drops out. The
  boundary is decided by scheduler noise.

**1.4 — Required response fields are missing.**

The brief asks each row to carry asset name, asset tag, **employee code**,
employee name and days overdue. `employee_code` is absent — only `full_name` is
emitted, and names are neither unique nor stable, so a consumer cannot key on
the row. The keys are also `asset` / `employee` rather than anything a client
could map onto the model.

#### Correctness-adjacent: the endpoint is unauthenticated

**1.5 — It is a plain Django view, so it never passes through DRF's
authentication or permission classes.**

`DEFAULT_PERMISSION_CLASSES = [IsAuthenticated]` in settings applies to DRF
views. This is a bare function returning `JsonResponse`, so it bypasses all of
it. Every open check-out — who holds what equipment, and their name — is served
to anyone who can reach the URL. It also accepts any HTTP method, `POST`
included, with no `require_GET`.

#### Scaling

**1.6 — N+1 queries: `c.asset` and `c.employee` each hit the database per row.**

No `select_related`. Measured, the query count is exactly `1 + 2 × (rows
returned)`:

| open check-outs | overdue rows | queries |
|---|---|---|
| 10 | 4 | **9** |
| 20 | 14 | **29** |
| 40 | 34 | **69** |

On a laptop with the database in the same process, 69 queries is a few
milliseconds. Across a network hop at 1 ms round trip it is 69 ms of pure
latency for 34 rows, and it grows without bound.

**1.7 — The overdue filter runs in Python, so the database returns rows the view
then throws away.**

The SQL issued has no `due_at` predicate at all:

```sql
SELECT ... FROM fieldassets_checkout
WHERE fieldassets_checkout.returned_at IS NULL
ORDER BY fieldassets_checkout.checked_out_at DESC
```

With 40 open check-outs of which 34 were overdue, all 40 were fetched. In a
fleet where most equipment is out on legitimate loan, the discarded majority is
the *bulk* of the transfer — a service holding 50,000 items out, 200 of them
overdue, ships 50,000 rows to build a 200-row report.

**1.8 — No pagination.** Every matching row is serialised into one response. The
brief requires 20 per page. Response size is unbounded and set by how badly
operations is going.

**1.9 — The entire result set is materialised in memory, twice.** `for c in
checkouts` evaluates and caches the whole queryset; `rows` then holds a dict per
overdue item; `rows.sort()` needs it all resident. Peak memory is proportional
to the open check-out count, not to a page.

**1.10 — Sorting happens in Python** rather than in an `ORDER BY` the database
can serve from the index — which is only possible at all *because* the view has
already given up on pagination.

### 2. Why does it look correct in local testing?

Each defect has a specific condition hiding it, and dev environments supply all
of them at once.

**The truncation (1.1) and the ordering bug (1.2) hide behind seed data.**
Fixtures are written as `due_at = now - timedelta(days=9)` because whole days
are what a person types. Every row then has a distinct, non-zero
`days_overdue`, the sort has no ties, and the output looks perfect. You have to
deliberately construct an item overdue by *hours* to see it — and nobody writes
that fixture unless they already suspect the bug. In production the sub-24-hour
band is continuously occupied, because every overdue item passes through it.

**The drifting clock (1.3) hides behind loop speed.** With ten rows the loop
completes in well under a millisecond, so every `timezone.now()` call returns
essentially the same instant and the inconsistency is unobservable. It widens
exactly when the loop gets slow — many rows, or a database under load — which
is when N+1 is also biting. The two defects mask each other in dev and compound
in production.

**The missing `employee_code` (1.4) hides because nothing validates the
response.** Eyeballing JSON that contains a plausible-looking employee name does
not prompt the question "is this the field that was specified?"

**The missing auth (1.5) hides because it never says no.** An authentication bug
that *rejects* is loud and gets fixed in minutes. One that *accepts* is silent
by construction. Testing with `curl` and no token succeeds, which reads as
success. If the developer is also logged into the admin in the same browser,
even a manual check in a browser tab looks authenticated.

**N+1 (1.6) hides behind localhost.** SQLite in-process, or Postgres over a loop-
back socket, makes a query cost tens of microseconds; the 2N extra round trips
are genuinely free. What kills it in production is per-query *latency* across a
network, plus connection-pool contention — neither of which exists on a laptop.
It also hides behind data volume: at 10 rows nobody notices 9 queries, and
`DEBUG=True` means nobody is looking at `connection.queries` either.

**Fetching-everything (1.7) and no pagination (1.8) hide behind the same thing —
the dev database is tiny.** Fetching all open check-outs when the table has 10
rows is indistinguishable from fetching the right 4. Both defects are invisible
until the table is large, and both scale with the part of the table that dev
data does not have.

**Every one of these is a defect whose symptom is proportional to production
scale or production time-of-day, and dev has neither.**

### 3. How would you fix it?

Push the filter, the arithmetic, the ordering and the joins into the database;
pin `now` once; paginate; and go through DRF so authentication applies.

```python
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, Q, Value
from django.utils import timezone
from rest_framework import generics, serializers


class OverdueRowSerializer(serializers.Serializer):
    """Every field comes from the row or a select_related join — no extra queries."""

    id = serializers.IntegerField(read_only=True)
    asset_tag = serializers.CharField(source="asset.asset_tag", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    due_at = serializers.DateTimeField(read_only=True)
    days_overdue = serializers.SerializerMethodField()

    def get_days_overdue(self, checkout):
        # Fractional, so 23 hours is 0.96 rather than 0.
        return round(checkout.overdue_by.total_seconds() / 86400, 2)


def overdue_checkouts(*, now=None):
    # Pinned once, so every row is judged and measured against one instant.
    now = now or timezone.now()
    return (
        CheckOut.objects.filter(
            # In SQL, so the database returns only what the report needs.
            # <= not <: something due at exactly this instant has reached its
            # deadline, and unlike the original that answer does not depend on
            # how long the loop took to get there.
            Q(returned_at__isnull=True, due_at__lte=now)
        )
        # Both joins in the same SELECT: query count no longer tracks row count.
        .select_related("asset", "employee")
        .annotate(
            overdue_by=ExpressionWrapper(
                Value(now, output_field=DateTimeField()) - F("due_at"),
                output_field=DurationField(),
            )
        )
        # Ordered by the real interval, not a truncated integer, so sub-day
        # items rank correctly against each other. pk breaks ties stably.
        .order_by("due_at", "pk")
    )


class OverdueReportView(generics.ListAPIView):
    """GET /api/v1/reports/overdue/ — most overdue first, 20 per page.

    A DRF view, so DEFAULT_AUTHENTICATION_CLASSES and IsAuthenticated apply,
    and only GET is routed.
    """

    serializer_class = OverdueRowSerializer
    filter_backends = []

    def get_queryset(self):
        return overdue_checkouts()
```

This is what ships in Part A — [`fieldassets/queries.py`](fieldassets/queries.py)
and [`fieldassets/views.py`](fieldassets/views.py) — with one difference worth
naming: there, the `Q(...)` predicate is factored into
`queries.open_and_overdue(now)` and shared with the employee summary's
`currently_overdue` and the A4 Celery task, so the three cannot drift apart on
where the boundary sits. A duplicated `due_at <= now` in three files is how
defect 1.3 comes back wearing a different hat.

Fix-to-defect map:

| Defect | Fixed by |
|---|---|
| 1.1 truncation | `total_seconds() / 86400`, fractional |
| 1.2 wrong order | `ORDER BY due_at, pk` on the real interval |
| 1.3 drifting clock | `now` pinned once, passed into the query |
| 1.4 missing field | `employee_code` in the serializer |
| 1.5 no auth | DRF `ListAPIView`; `IsAuthenticated` applies; GET only |
| 1.6 N+1 | `select_related("asset", "employee")` |
| 1.7 filter in Python | `due_at__lte=now` in the `filter()` |
| 1.8 no pagination | `ListAPIView` + `PAGE_SIZE = 20` |
| 1.9 memory | pagination bounds the working set |
| 1.10 sort in Python | `ORDER BY` in SQL |

### 4. What test or tooling would have caught this?

**A query-count assertion that scales the data — catches 1.6 and 1.7.**
Asserting a fixed number catches N+1 only if you guessed the number right;
asserting the count is *unchanged* when the row count grows catches it
regardless. This is the shipped test:

```python
def test_the_row_count_does_not_change_the_query_count(
    self, api, make_employee, make_open_checkout, django_assert_num_queries
):
    for i in range(3):
        make_open_checkout(employee, due_at=now - timedelta(days=i + 1))
    with django_assert_num_queries(2) as small:
        api.get(url)

    for i in range(3, 18):
        make_open_checkout(employee, due_at=now - timedelta(days=i + 1))

    with django_assert_num_queries(len(small.captured_queries)):
        api.get(url)
```

Against the broken snippet this fails immediately: 9 queries, then 29.

**A fixture measured in hours, not days — catches 1.1 and 1.2.** The single most
valuable test here, because it needs no tooling at all, only the discipline of
choosing a fixture that is not a round number:

```python
def test_an_item_overdue_by_hours_is_not_reported_as_zero_days(...):
    make_open_checkout(employee, due_at=now - timedelta(hours=23))
    assert rows[0]["days_overdue"] == pytest.approx(0.96, abs=0.01)

def test_a_23_hour_item_outranks_a_1_hour_item(...):
    # the ordering assertion the truncated integer cannot satisfy
```

**A test with `now` pinned — catches 1.3.** Passing `now` into the queryset (or
`freezegun`) makes the boundary deterministic and testable at all. The shipped
suite asserts the exact instant and both sides of it:

```python
def test_an_item_due_exactly_now_is_overdue(...)          # due_at == now
def test_an_item_due_one_microsecond_from_now_is_not(...)  # the other side
```
Note the broken snippet cannot be tested this way *even in principle*, because
it reads the clock itself. Untestability was the design smell pointing at the
defect.

**A response-shape assertion — catches 1.4.** Asserting the exact required keys,
rather than that the response is non-empty. A schema check
(drf-spectacular + a contract test) generalises this.

**An unauthenticated-request test — catches 1.5.** Parametrised over every
route, asserting `401`:

```python
@pytest.mark.parametrize("name,args", PROTECTED)
def test_without_a_token_it_is_401(self, anon, name, args): ...
```
A route added as a plain Django view fails this the moment it is added to
`urls.py`. This is the check most worth automating over every URL, because
"forgot the decorator" is a one-line mistake with an unbounded blast radius.

**A pagination test — catches 1.8 and 1.9.** Create 25, assert 20 in `results`
and a non-null `next`.

**Tooling, in rough order of value for effort:**

- **`nplusone`** or **django-zen-queries** in CI, failing the build on an
  unprefetched relation traversal — catches 1.6 for every endpoint at once
  rather than one test at a time.
- **django-debug-toolbar** or **django-silk** locally: the query count is then
  visible on every page load, which turns 1.6 from invisible into obvious.
- **A seeded dataset with production-like shape**, so 1.7 and 1.8 have somewhere
  to show. This is what `seed_demo_data` is for; a fixture of 10 rows will never
  expose a defect that needs 10,000.
- **APM with per-request query counts** (Datadog, Sentry Performance) — the
  backstop for whatever the tests miss, and the only one that catches N+1 which
  appears through a code path no test exercises.
- **`ruff`/`flake8`** would catch none of this. It is worth saying plainly: every
  defect here is semantic. Linting gives no protection against any of them,
  which is precisely why the query-count and boundary tests have to be explicit.

---

## Snippet 2 — check-out endpoint

```python
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(["POST"])
def check_out_asset(request):
    asset = Asset.objects.get(asset_tag=request.data["asset_tag"])
    if asset.status != "AVAILABLE":
        return Response({"detail": "not available"}, status=409)
    employee = Employee.objects.get(employee_code=request.data["employee_code"])
    open_count = CheckOut.objects.filter(
        employee=employee, returned_at__isnull=True
    ).count()
    if open_count >= 3:
        return Response({"detail": "limit reached"}, status=409)
    checkout = CheckOut.objects.create(
        asset=asset,
        employee=employee,
        due_at=request.data["due_at"],
    )
    asset.status = "CHECKED_OUT"
    asset.save()
    return Response({"id": checkout.id}, status=201)
```

### 1. What is wrong?

It implements three of the eight rules. Everything below was reproduced by
running the snippet as printed.

**2.1 — No transaction. Rule 5 is violated by construction.** `CheckOut.create()`
and `asset.save()` are two independent commits. If the second fails — a
constraint, a lost connection, a worker killed between the two statements — the
check-out row survives next to an `AVAILABLE` asset. That is the exact state the
brief says must never exist, and the window is wide open on every request.

**2.2 — No locking. Rule 7 fails.** Nothing serialises the status read against
the insert. Four concurrent requests, measured:

```
thread 0: (55, 201)
thread 1: IntegrityError: duplicate key value violates unique constraint
thread 2: IntegrityError: duplicate key value violates unique constraint
thread 3: IntegrityError: duplicate key value violates unique constraint
```

All four read `AVAILABLE` and all four proceeded. **The three losers got HTTP
500, not 409.** Note *why* the data survived: Part A's partial unique index
caught them. Against a schema without that index — which is what this snippet
implies, since nothing in it depends on one — the same race produces four open
check-outs for one asset and silent corruption instead of a 500. The snippet is
wrong either way; the index only changes the failure mode from corruption to a
crash.

**2.3 — The same race applies to rule 3.** Two simultaneous requests from an
employee holding two both read `open_count == 2`, both pass, and the employee
ends with four. No index catches this one, so it is silent data corruption with
no backstop at all.

**2.4 — Unknown `asset_tag` or `employee_code` is a 500, not a 404. Rule 8
fails.** `.get()` raises `DoesNotExist`, nothing catches it:

```
raises DoesNotExist  -> uncaught = HTTP 500, not 404
```

**2.5 — A missing body key is a 500, not a 400.** `request.data["asset_tag"]`
on a body without it:

```
raises KeyError  -> uncaught = HTTP 500, not 400
```

**2.6 — Rule 2 is not implemented at all.** `is_active` is never read. An
inactive employee checks equipment out successfully:

```
inactive employee -> (52, 201)
```

**2.7 — Rule 4 is not implemented at all.** `due_at` is neither parsed nor
bounded. Both of these returned 201:

```
due_at 30 days in the past  -> (53, 201)
due_at 10 years in the future -> (54, 201)
```

**2.8 — `due_at` is passed through unvalidated and untyped.** It goes to the
model as whatever JSON contained. A naive datetime string is stored as if UTC
with only a `RuntimeWarning`; a malformed one raises at save time — a 500 for
what is plainly a 400.

**2.9 — `asset.save()` writes every column.** No `update_fields`, so the whole
row is overwritten from a possibly stale in-memory instance, clobbering any
concurrent change to `name`, `category` or anything else. Only `status` changed.

**2.10 — Status compared against the string literal `"AVAILABLE"`** instead of
`AssetStatus.AVAILABLE`. A typo is not an error, just a branch that never fires.

**2.11 — The response body is not what was specified.** The brief asks for 201
with the created check-out; this returns `{"id": ...}`, so a client must issue a
second request to learn anything about what it just created.

### 2. Why does it look correct in local testing?

**The transaction and locking defects (2.1–2.3) need concurrency, and manual
testing has none.** One `curl` at a time never interleaves. `runserver` handles
requests sequentially in practice, so even clicking fast in two tabs will not
reproduce it. The race needs genuine parallelism — threads or multiple gunicorn
workers — which appears for the first time in production. The atomicity window
in 2.1 is worse: it requires not just concurrency but a *failure* at a specific
statement, so it is invisible until something else goes wrong, and then it
corrupts data while everyone's attention is on the original outage.

**The missing rules (2.6, 2.7) hide because the endpoint returns 201.** Testing
the happy path exercises exactly the code that exists. You only discover a rule
is missing by testing the case it forbids — and there is no failing behaviour to
prompt you, because a missing check never throws. This is the same shape as
Snippet 1's missing auth: a check that isn't there fails open, silently.

**The 500s (2.4, 2.5) look like bad input rather than bad code.** With
`DEBUG=True` the developer sees a `DoesNotExist` traceback, thinks "right, that
tag doesn't exist", and moves on — the traceback confirms their mental model
instead of contradicting it. The status code is never examined, because a human
reading a stack trace has already got their answer. In production the same path
is a 500 that pages someone, and a client that cannot distinguish "you asked for
something that isn't there" from "we're broken".

**2.8 hides because hand-written test requests are well-formed.** Anyone typing
`due_at` by hand produces ISO 8601 with an offset, which is the one input that
works.

**2.9 hides because dev has one writer.** Lost updates need a second writer.

### 3. How would you fix it?

```python
from django.db import IntegrityError, transaction
from rest_framework.exceptions import NotFound, ValidationError


@transaction.atomic                                    # 2.1: one commit or none
def check_out_asset(*, asset_tag, employee_code, due_at, condition_note=""):
    validate_due_at(due_at)                            # 2.7 / 2.8

    # 2.2 / 2.3: lock the rows before reading them. Always asset-then-employee,
    # so two check-outs can never hold one and wait on the other.
    asset = _get_locked_asset(asset_tag)               # 2.4: NotFound -> 404
    employee = _get_locked_employee(employee_code)     # 2.4: NotFound -> 404

    if not employee.is_active:                         # 2.6
        raise ValidationError(
            {"employee_code": ["Employee is inactive and cannot check out assets."]}
        )
    if asset.status != AssetStatus.AVAILABLE:          # 2.10: enum, not a literal
        raise AssetNotAvailable(...)
    if open_checkout_count(employee) >= max_open_checkouts():
        raise CheckOutLimitReached(...)

    try:
        with transaction.atomic():                     # savepoint
            checkout = CheckOut.objects.create(
                asset=asset, employee=employee, due_at=due_at,
                condition_note=condition_note or "",
            )
    except IntegrityError as exc:
        # 2.2: the loser's constraint violation becomes a 409, not a 500.
        if _is_open_checkout_conflict(exc):
            raise AssetNotAvailable(...) from exc
        raise

    asset.status = AssetStatus.CHECKED_OUT
    asset.save(update_fields=["status", "updated_at"])  # 2.9
    return checkout
```

with the view reduced to parse / delegate / serialise, so `KeyError` (2.5)
becomes a serializer 400 and the response carries the created object (2.11):

```python
class CheckOutCreateView(APIView):
    def post(self, request):
        payload = CheckOutCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)         # 2.5, 2.7, 2.8 -> 400
        checkout = check_out_asset(**payload.validated_data)
        return Response(CheckOutSerializer(checkout).data, status=201)
```

This is what ships in Part A — [`fieldassets/services.py`](fieldassets/services.py).
Two points about it:

**The rules live in the service, not the view.** The snippet's deepest problem is
structural: business rules written inline in a view are reachable only over
HTTP, so a management command or a shell session can bypass every one of them.
Moving them into a function means the guarantees hold for any caller.

**Rule 7 is defended twice, deliberately.** The lock produces the correct
*status code*; the partial unique index guarantees *correctness* even if some
future code path reaches the insert without the lock. The snippet's race
demonstrates exactly why both are wanted — the index alone left the data correct
but returned 500, and the lock alone would leave nothing behind it.

### 4. What test or tooling would have caught this?

**A threaded race test — catches 2.2 and 2.3.** The one test that matters most
here, because no amount of sequential testing substitutes for it:

```python
@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_checkouts_of_one_asset_leave_exactly_one_winner(...):
    results = race(lambda i: check_out_asset(...), count=2)
    winners = [r for r, exc in results if exc is None]
    assert len(winners) == 1
    assert isinstance(losers[0], Conflict)          # 409, not IntegrityError
```

`transaction=True` is the load-bearing detail — the transaction a normal
`django_db` test wraps everything in hides the racers' commits from each other,
and the test would pass without ever having raced.

**Fault injection — catches 2.1.** Atomicity has no happy path to observe, so
the only way to test it is to break the second write on purpose:

```python
monkeypatch.setattr("fieldassets.models.Asset.save", boom)
with pytest.raises(RuntimeError):
    post_checkout(asset.asset_tag, employee.employee_code)
assert not CheckOut.objects.exists()               # no orphan row
assert asset.status == AssetStatus.AVAILABLE
```

**One negative test per rule — catches 2.4 through 2.7.** These are trivial and
they are exactly what was missing. A checklist derived from the rules, asserting
the *status code* rather than "it didn't crash":

```python
def test_an_inactive_employee_is_a_bad_request(...)      # 400   rule 2
def test_a_past_due_at_is_rejected(...)                  # 400   rule 4
def test_more_than_thirty_days_out_is_rejected(...)      # 400   rule 4
def test_an_unknown_asset_tag_is_a_404(...)              # 404   rule 8
def test_an_unparseable_due_at_is_a_400_not_a_crash(...)  # 400  not 500
```

**A database constraint as a backstop — turns 2.2 from corruption into a caught
error.** `UniqueConstraint(fields=["asset"], condition=Q(returned_at__isnull=True))`
is the reason the measured race left one row instead of four. Tests can be
forgotten; the index cannot. Worth asserting directly, so weakening it fails the
build:

```python
def test_the_database_rejects_a_second_open_checkout_row(...):
    with pytest.raises(IntegrityError): ...
```

**Tooling:**

- **Mutation-style checking of the guarantees.** Every test above was verified to
  fail when its guarantee is removed — weakening the index fails 2 tests,
  changing the limit from 3 to 4 fails 1. A test that cannot fail is worse than
  no test, and this is cheap to confirm once.
- **Error-rate alerting on 5xx by endpoint** (Sentry). 2.4 and 2.5 turn ordinary
  client mistakes into server errors; in production they show up as a 500 rate
  that tracks traffic, which is the signal that something is mapping the wrong
  status code.
- **Load testing with real concurrency** (locust, k6) against a staging instance
  with more than one worker — the environment where 2.1–2.3 exist at all.
- **Code review with the rule list open.** Six of these eleven defects are rules
  simply absent from the code. No tool detects a requirement that was never
  written down in the first place; a checklist does.

---

## Snippet 3 — overdue notice task

```python
from celery import shared_task
from django.utils import timezone

@shared_task
def send_overdue_notices():
    overdue = CheckOut.objects.filter(
        returned_at__isnull=True,
        due_at__lt=timezone.now(),
    )
    for c in overdue:
        OverdueNotice.objects.create(checkout=c, notice_date=timezone.now().date())
        deliver_email.delay(c.employee, c)
    return "sent %d notices" % overdue.count()
```

### 1. What is wrong?

**3.1 — It does not survive its own first row.** `deliver_email.delay(c.employee, c)`
passes model instances as task arguments. Celery's default serializer is JSON,
and a model is not JSON-serializable:

```
EncodeError: Object of type Employee is not JSON serializable
   -> the loop body dies on the FIRST row
notices created before it died: 1
```

This is the worst possible shape of failure: the `OverdueNotice` is created
*before* the dispatch raises, so every attempt commits one more notice and then
aborts. The task never reaches row two, no matter how often it retries, and each
retry leaves another orphan behind. Arguments must be primitives — pass
`employee_id` and `checkout_id` and re-fetch in the consumer. That also fixes a
second problem hiding underneath: a serialized instance is a *snapshot*, so even
with pickle enabled the email would render from data that may be stale by the
time it is delivered.

**3.2 — Not idempotent. A retry duplicates every notice it already wrote.**
Nothing filters out check-outs that already have today's notice. With the
dispatch stubbed so the loop can complete, and Part A's unique index dropped so
the snippet's own schema assumptions hold:

```
run 1: 'sent 10 notices'   emails queued=10
run 2: 'sent 10 notices'   emails queued=10     <- a retry
notices now : 20 for 10 overdue check-outs
duplicated  : 10 (checkout, date) pairs, each appearing 2 times
```

Five retries, five notices per check-out, and five emails to each employee. On
the real Part A schema the second run instead dies with
`IntegrityError: duplicate key value violates unique constraint
"uniq_notice_per_checkout_per_day"` — the index converts silent duplication into
a crash, but the task is wrong either way.

**3.3 — The dual-write problem: emails are dispatched before the database says
the work is durable.** `create()` then `delay()` are two separate systems with no
coordination. If the surrounding transaction rolls back — a retry wrapper,
`ATOMIC_REQUESTS`, a later error — the notice disappears but the email has
already left. Conversely, wrapping the whole loop in one transaction makes it
worse, because emails would then be queued for rows that get rolled back.
Dispatch belongs in `transaction.on_commit`.

**3.4 — Partial failure has no recovery story.** Fail at row 5,000 of 40,000 and
5,000 notices exist, 5,000 emails are queued, 35,000 rows are untouched, and the
only available action is a full re-run — which, per 3.2, re-notifies the first
5,000. The task has no way to resume, because it has no notion of what it
already did.

**3.5 — The entire result set is loaded into memory.** `for c in overdue`
evaluates and caches every row. At tens of thousands of check-outs that is tens
of thousands of model instances resident at once, in a worker process sized for
ordinary jobs. The failure mode is the worker being OOM-killed mid-loop —
which lands straight back on 3.4, having written an unknown number of notices.

**3.6 — One INSERT per row, plus an N+1 on `employee`.** Measured at 21 queries
for 10 rows: one SELECT, then per row one INSERT and one SELECT for
`c.employee` (no `select_related`). At 40,000 rows that is 80,001 round trips.

**3.7 — `timezone.now()` is re-read on every iteration.** For a run long enough
to matter — which is exactly the tens-of-thousands case — the date can roll over
mid-loop, so one execution produces notices dated across two different days.
That also breaks the dedup key in 3.2 at precisely the moment it is needed most.

**3.8 — `timezone.now().date()` is the UTC date, not the local one.** It should be
`timezone.localdate()`. Under any non-UTC `TIME_ZONE` the notice is dated wrong
for part of every day, and "one notice per check-out per day" starts straddling
day boundaries.

**3.9 — `due_at__lt` excludes an item due at exactly this instant.** Same
boundary defect as Snippet 1, and it must agree with whatever the overdue report
uses or the two views of "overdue" disagree.

**3.10 — The return value is a string, and it is inaccurate.** `"sent %d
notices"` is not machine-readable, and nothing was *sent* — emails were queued.
It also cannot distinguish a real run from a no-op re-run, which is the single
most useful thing an idempotent task can report. (`overdue.count()` itself is
harmless: the loop has already populated the queryset's result cache, so it
reuses it rather than re-querying — confirmed by measurement.)

**3.11 — No task name, no retry policy, no acknowledgement strategy.** A bare
`@shared_task` gets an auto-generated name that moves if the module is renamed.
There is no `autoretry_for`/`max_retries`, so the failure in 3.1 is terminal and
silent. And with `acks_late=True` a worker killed mid-loop causes redelivery —
which, without 3.2 fixed, duplicates everything.

### 2. Why does it look correct in local testing?

**3.1 never fires when there are no overdue rows — and dev databases usually
have none.** This is the key point about the whole snippet: the loop body is the
part that is broken, and an empty queryset skips it entirely. The task returns
`"sent 0 notices"`, exits zero, and looks perfectly healthy. It is not that the
bug is subtle; it is that the code never ran. A fixture with one overdue
check-out would have failed instantly — the defect is hidden by the *absence*
of data rather than by any property of the code.

**3.2 hides because nobody runs a task twice.** Manual testing is one invocation.
Idempotency is only observable on the second run, and there is no reason to do a
second run unless you are specifically testing for it. Retries in production are
automatic and invisible — a broker redelivery after a worker restart looks like
nothing at all from the outside.

**3.3 hides because dev runs `CELERY_TASK_ALWAYS_EAGER` or a single worker with
an empty queue,** so the ordering between the commit and the dispatch is never
stressed. Rollbacks are rare in a happy-path test.

**3.5, 3.6 and 3.7 hide behind the row count.** Ten rows fit in memory, 21
queries are instant, and a loop that finishes in 30 ms cannot cross midnight.
Every one of these defects is proportional to a scale dev does not have — which
is why the brief's "tens of thousands of rows" is the whole hint.

**3.8 hides because dev and CI both run `TIME_ZONE = "UTC"`,** where
`now().date()` and `localdate()` are identical. It appears only after
deployment to a project configured for a real timezone.

**3.4 hides because nothing fails in dev.** Partial failure requires a failure.

### 3. How would you fix it?

```python
BATCH_SIZE = 500

@shared_task(name="fieldassets.tasks.flag_overdue_checkouts")
def flag_overdue_checkouts(now=None, batch_size=BATCH_SIZE):
    now = timezone.now() if now is None else now      # 3.7: read the clock once
    notice_date = timezone.localdate(now)             # 3.8: local, not UTC

    pending = (
        CheckOut.objects.filter(open_and_overdue(now))       # 3.9: <= , shared
        .exclude(notices__notice_date=notice_date)           # 3.2: skip done work
        .order_by("pk")
        .values_list("pk", flat=True)                        # 3.5: ids, not models
    )

    before = OverdueNotice.objects.filter(notice_date=notice_date).count()
    examined, batch = 0, []

    for checkout_id in pending.iterator(chunk_size=batch_size):   # 3.5: streams
        examined += 1
        batch.append(OverdueNotice(checkout_id=checkout_id, notice_date=notice_date))
        if len(batch) >= batch_size:
            OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)  # 3.6, 3.2
            batch = []
    if batch:
        OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)

    after = OverdueNotice.objects.filter(notice_date=notice_date).count()
    return {                                          # 3.10: structured, honest
        "notice_date": notice_date.isoformat(),
        "examined": examined,
        "created": after - before,
    }
```

with `UNIQUE(checkout, notice_date)` on the model, and email dispatch by id,
after commit:

```python
class Meta:
    constraints = [
        UniqueConstraint(fields=["checkout", "notice_date"],
                         name="uniq_notice_per_checkout_per_day"),
    ]

# 3.1 + 3.3: primitives only, and only once the rows are durable.
transaction.on_commit(lambda: deliver_email.delay(checkout_id=cid, employee_id=eid))
```

This is what ships in Part A — [`fieldassets/tasks.py`](fieldassets/tasks.py) —
with the email dispatch deliberately left out, since A4 asks only for the
notices.

**Idempotency is defended twice, and the layers do different jobs.** The
`.exclude()` makes a repeat run cost nothing, which keeps the hourly schedule
cheap. The unique index is what makes it *true*, because the `.exclude()` can
lose a race between two workers. Measured on Part A: four workers racing one
sweep all passed the filter and each attempted every insert, and the database
still ended with exactly one notice per check-out and zero duplicates — the
index absorbed 87 duplicate inserts. Deleting the `.exclude()` from the shipped
task fails only the test asserting a repeat run reports `examined: 0`; every
idempotency test still passes. That is the split working as intended.

### 4. What test or tooling would have caught this?

**Run it twice and assert one notice — catches 3.2.** The single highest-value
test, and the one the brief itself names:

```python
def test_running_it_twice_creates_one_notice(...):
    flag_overdue_checkouts()
    flag_overdue_checkouts()
    assert OverdueNotice.objects.filter(checkout=checkout).count() == 1
```

**Any test with a non-empty overdue fixture — catches 3.1.** This is the
uncomfortable one: the defect is fatal and obvious, and it survived only because
no test ever put a row in front of it. A single fixture would have caught it. It
argues for a rule that a task test must assert on *work done*, never merely that
the task returned.

**A concurrency test — catches the gap the `.exclude()` cannot close.**

```python
@pytest.mark.django_db(transaction=True)
def test_four_workers_racing_one_sweep_still_leave_one_notice_each(...):
    # all four pass the filter; the index is what saves it
    assert OverdueNotice.objects.count() == 5
    assert duplicate_notice_count() == 0
```

**A batching test with more rows than one batch — catches 3.5 and 3.6.**
`flag_overdue_checkouts(batch_size=10)` against 25 overdue rows, asserting 25
created and none duplicated, proves streaming and batching neither drop nor
double-count.

**A day-rollover test — catches 3.7 and 3.8.** Seed yesterday's notice, assert
today's is still created. Pinning `now` as a parameter is what makes this
testable at all — the original reads the clock itself and cannot be tested for
it, the same untestability smell as Snippet 1.

**Tooling:**

- **`CELERY_TASK_ALWAYS_EAGER = False` in at least one CI job**, with a real
  broker. Eager mode serializes nothing, so it hides 3.1 completely — the
  defect only exists on the wire.
- **A JSON-serializer assertion in CI**, or simply keeping
  `task_serializer = "json"` explicit and never enabling pickle, so passing a
  model raises loudly rather than being silently accepted.
- **A staging dataset with production-like volume.** 3.5, 3.6 and 3.7 have no
  symptom below a few thousand rows; no unit test substitutes for the row count.
- **Worker memory and task-duration metrics with alerting** — an OOM-killed
  worker mid-loop is the production shape of 3.4, and it is invisible unless
  someone is watching RSS.
- **Chaos-style retry testing**: kill the worker mid-task and re-run. This is the
  only check that exercises 3.2, 3.3 and 3.4 together, and it is exactly the
  scenario the brief describes as "retried after a partial failure".

---

# Part C — Optimising the slow reporting query

Rather than reason about this abstractly, I built the schema at the stated scale
and measured: 4.2M `checkouts`, 12,000 `employees` (10,764 active), 50,000
`assets`, and **only** the indexes the brief says exist — the primary keys plus
Django's `asset_id` and `employee_id` FK indexes.

Data shape, chosen to match the brief's 8,000 rows/day:

| | |
|---|---|
| `checkouts` | 4,200,000 rows, 437 MB heap, 584 MB with indexes |
| open (`returned_at IS NULL`) | 173,484 — **4.13%** |
| rows in the Jan–Jun 2026 window | 1,448,000 |
| rows the query actually returns | **19,455** |

Two caveats on the numbers below. The container runs PostgreSQL **16.15**, not
15 — the planner behaviour discussed here is the same in both. And absolute
times are much faster than the production 8 s, because this is a warm local SSD
with a 128 MB `shared_buffers`. **The ratios and the plan shapes are the
transferable part**; where I quote a time, the number that matters is the one
next to it.

## 1. Rewrite the query

```sql
SELECT c.id, c.asset_id, c.employee_id, c.checked_out_at, c.due_at
FROM checkouts c
JOIN employees e ON e.id = c.employee_id AND e.is_active
WHERE c.checked_out_at >= timestamptz '2026-01-01 00:00:00+00'
  AND c.checked_out_at <  timestamptz '2026-07-01 00:00:00+00'
  AND c.returned_at IS NULL
ORDER BY c.due_at ASC
LIMIT 100;                      -- plus a keyset/offset cursor for later pages
```

Verified equivalent: both forms return **19,455** rows.

**`DATE(c.checked_out_at) BETWEEN …` → a half-open range on the bare column.**
This is the change that matters. Wrapping the column in `DATE()` makes the
predicate non-sargable — no index on `checked_out_at` can ever be used, because
the planner cannot invert the function. It also destroys the planner's
statistics: it estimated **860 rows and got 21,677**, a 25× miss, which is why
it chose a Nested Loop and then executed 21,677 separate index lookups into
`employees`. Two further problems disappear with it: `DATE()` on a `timestamptz`
is `STABLE`, not `IMMUTABLE`, because it depends on the session's `TimeZone` —
so the same query returns different rows for different sessions, and the
expression cannot be indexed without pinning a timezone. The half-open form
`>= start AND < end` also avoids the classic `BETWEEN` bug on timestamps, where
`<= '2026-06-30'` silently drops everything after midnight on the last day.
**Cost:** the boundaries must now be written as explicit timestamps in a stated
timezone. That is a real burden on the caller, and it is the right place for it —
the alternative pushes an ambiguity into every row of a 4.2M-row scan.

**`employee_id IN (SELECT …)` → an explicit join.** Semantically this one is
nearly free: Postgres already flattens `IN (subquery)` to a semi-join. The gain
is not the rewrite itself but what it enables once the estimates are fixed — the
plan moves from a Nested Loop with 21,677 lookups to a single Hash Join over
10,764 employees. **Cost:** a join can duplicate rows if the join key is not
unique. Here `employees.id` is the primary key, so it cannot; if that were ever
untrue, `EXISTS` would be the safer form and I would use it.

**`SELECT *` → an explicit column list.** `condition_note` is `text` and may be
TOASTed; pulling it for 19,455 rows inflates the sort, the transfer and the
memory, and — decisively — makes an index-only scan impossible, since no
sensible index will ever cover a wide text column. **Cost:** the caller must
name what it needs, and the query has to be revisited when the screen adds a
column. That is a good trade for making covering indexes viable at all.

**Adding `LIMIT`.** A reporting screen rendering 19,455 rows is itself a defect;
the number grows without bound as the table does. **Cost:** real pagination
requires a stable cursor. `OFFSET` degrades on deep pages, so for a large report
I would use keyset pagination on `(due_at, id)` — `id` as a tiebreaker, because
`due_at` is not unique and without it rows can be skipped or repeated across
pages.

## 2. Indexes

```sql
CREATE INDEX CONCURRENTLY idx_checkouts_open_checked_out
    ON checkouts (checked_out_at)
    INCLUDE (id, due_at, employee_id, asset_id)
    WHERE returned_at IS NULL;
```

One index. It earns its place three separate ways.

**Partial, on `WHERE returned_at IS NULL` — this is the single highest-leverage
decision.** Only 4.13% of rows are open, so the index covers 173,484 rows
instead of 4.2M. Measured, same index definition with and without the predicate:

| | size |
|---|---|
| partial | **9,976 kB** |
| identical index, not partial | **235 MB** |

**24× smaller.** It fits in `shared_buffers` and stays there; the full one
competes with the heap for cache. It is also cheaper to maintain: a row enters
the index on insert and leaves it when returned, so the index tracks the working
set rather than history. The predicate matches how the application actually
queries — every "open check-outs" question in Part A filters on exactly this —
so one index serves the report, the overdue view and the rule-3 count.

**`checked_out_at` as the key column, not `due_at`.** This is where I expected to
be wrong, and measuring changed my answer. `due_at` is tempting because it would
supply the `ORDER BY` for free and let `LIMIT` stop early. I built it and it was
**worse**: 89 ms versus 36 ms, because scanning open rows in `due_at` order and
filtering by date window discards most of what it reads —
`Rows Removed by Filter: 35055` to produce 100 rows. `due_at` order and the
`checked_out_at` window are only loosely correlated, so the "free ordering" is
paid for many times over in wasted index entries. Ranging on `checked_out_at`
and sorting 21,677 candidates with a `top-N heapsort` is far cheaper. **This is
the composite-versus-partial question the brief asks about, and the honest answer
is that the appealing composite is a trap here.**

**`INCLUDE` rather than more key columns.** The extra columns ride in the leaf
pages as payload: they enable an index-only scan without widening the B-tree's
internal pages or affecting sort order. Putting them in the key instead would
bloat every level of the tree for no benefit, since none of them is used for
ranging.

**What I would *not* add.** A second index on `(due_at) WHERE returned_at IS
NULL` — measured worse for this query, as above, and it would still cost writes
on every insert and return. An index on `employees(is_active)` — 10,764 of
12,000 rows are active, so it is not selective enough to beat the sequential
scan the planner already chose. And Django's existing `checkouts(employee_id)`
index is now unused by this query; I would check `pg_stat_user_indexes` before
touching it, since other queries may need it.

`CONCURRENTLY` because building a normal index takes an `ACCESS EXCLUSIVE` lock,
which on a 4.2M-row production table means an outage. It is slower and can leave
an `INVALID` index if it fails, which then needs dropping and retrying — an
acceptable price for not blocking writes.

## 3. What `EXPLAIN (ANALYZE, BUFFERS)` shows

**Before** — the actual plan, serial:

```
 Sort  (cost=140766..140768 rows=771) (actual rows=19455)
   Sort Key: c.due_at
   Sort Method: quicksort  Memory: 2592kB
   Buffers: shared hit=80683 read=40226
   ->  Nested Loop  (cost=0.29..140764 rows=771) (actual rows=19455)
         ->  Seq Scan on checkouts c  (actual time=243.829..366.010 rows=21677)
               Filter: ((returned_at IS NULL) AND (date(checked_out_at) >= ...))
               Rows Removed by Filter: 4178323
               Buffers: shared hit=15649 read=40226
         ->  Index Scan using employees_pkey on employees  (loops=21677)
 Execution Time: 441.305 ms
```

**After** — same query rewritten, one index:

```
 Sort  (actual time=... rows=19455)
   Sort Method: quicksort  Memory: 1984kB
   ->  Hash Join  (actual rows=19455)
         ->  Index Only Scan using idx_checkouts_open_checked_out on checkouts c
               (actual time=0.044..2.544 rows=21677)
               Heap Fetches: 0
         ->  Seq Scan on employees e  (actual rows=10764)
 Execution Time: 17.014 ms
```

Full progression:

| | scan | Rows Removed by Filter | buffers | time |
|---|---|---|---|---|
| baseline | `Seq Scan` | **4,178,323** | 120,909 | 441 ms |
| rewrite + partial index | `Bitmap Index Scan` | 0 | 13,191 | 88 ms |
| + `INCLUDE` covering | `Index Only Scan` | 0 | ~1,000 | **17 ms** |
| + `LIMIT 100` | `Index Only Scan` | 0 | **283** | **11.7 ms** |

**The specific line that tells you it worked is `Heap Fetches: 0` under
`Index Only Scan`.** Everything else can mislead. Execution time moves for
unrelated reasons — cache warmth, concurrent load, whether parallel workers were
available. `Rows Removed by Filter` disappearing proves the predicate reached the
index, but the query could still be doing 13,000 random heap reads, as the
middle row above shows. `Heap Fetches: 0` is the unambiguous statement that the
index alone answered the query and the heap was never touched — it is both the
strongest result and the most fragile, because it depends on the visibility map
being current. If `Heap Fetches` is large despite an `Index Only Scan`, the fix
is not the index; it is autovacuum.

The second line I would check is `Buffers: shared read=` — 40,226 down to 283 is
a **142× reduction in blocks fetched**, and on a production system where the 8 s
is I/O-bound rather than CPU-bound, that ratio is what actually converts into
wall-clock improvement. It is also the number least distorted by my hardware
being faster than production's.

## 4. At 8,000 rows/day, what breaks first?

The reassuring part first: **the index does not degrade with table growth,
because it is partial.** The open set is a working set, not an accumulation —
rows leave it on return. It grows only by the never-returned residue, roughly
120 rows/day at a 1.5% abandonment rate, or about 2.5 MB/year. The heap grows
~160 MB/year; the index barely moves. That is the durability argument for the
partial predicate, over and above its size today.

**What breaks first is the unbounded result set, not the scan.** 19,455 rows for
a six-month window today, growing with both the window and the never-returned
count. Serialising and shipping that to a browser is already the dominant cost
once the scan is fixed, and no index helps. **This is why `LIMIT` is in the
rewrite rather than being an optional extra** — it is the only part of the fix
that keeps working at 40M rows. I would ship keyset pagination on `(due_at, id)`
now, while the change is cheap, rather than after the screen times out again.

**Second is autovacuum falling behind on index churn.** The partial index sees
~8,000 insertions and ~8,000 deletions a day as items are checked out and
returned. Setting `returned_at` also changes index membership, so those updates
cannot be HOT. Left at defaults — autovacuum triggers at 20% of the table — a
4.2M-row table needs 840,000 dead tuples before it runs, which is months of
churn. Meanwhile the index bloats and, worse, the visibility map goes stale and
`Heap Fetches: 0` quietly becomes `Heap Fetches: 21677`, taking the index-only
scan with it. I would set per-table
`autovacuum_vacuum_scale_factor = 0.01` and `autovacuum_analyze_scale_factor =
0.005` on `checkouts` before that happens, and alert on
`pg_stat_user_tables.n_dead_tup`.

**Third, and further out, is anything that still scans the whole heap.** At
~40M rows in five years the table is ~4 GB and any unindexed report is
unusable. The answer then is range partitioning on `checked_out_at`, monthly,
so old partitions can be detached and archived — the reporting screen only ever
looks at recent windows. I would not do it now: partitioning imposes real costs
(the partition key must be in every unique constraint, cross-partition queries
get more expensive, and DDL becomes fiddly), and at 4.2M rows a good partial
index makes it unnecessary. It is the fix for a problem that does not exist yet.

## 5. The one thing I would measure first

**The true selectivity of `returned_at IS NULL` on production data — and
specifically within the reporting window, not table-wide.**

Everything above rests on it. I modelled 4.13% open, and every conclusion is
contingent on that number being roughly right: the partial index is 24× smaller
*because* the open set is small, the index-only scan wins *because* 21,677
candidates fit comfortably in memory, and the `top-N heapsort` beats the
`due_at` index *because* there are few enough rows to sort.

Change that one figure and the answer changes. If this fleet holds equipment for
months rather than weeks — a plausible reading of "field assets" — 40% of rows
could be open. The partial index then covers 1.7M rows, is close to 100 MB,
stops being cache-resident, and the bitmap heap scan may well lose to the
sequential scan it replaced. I would want a different key column, or a
composite including `employee_id`, or partitioning sooner.

I cannot know this from the schema, because it is a property of how the business
actually operates, and it is exactly the kind of assumption that is invisible
until it is wrong. One query answers it:

```sql
SELECT count(*) FILTER (WHERE returned_at IS NULL) AS open_rows,
       count(*)                                    AS window_rows,
       round(100.0 * count(*) FILTER (WHERE returned_at IS NULL) / count(*), 2) AS pct_open
FROM checkouts
WHERE checked_out_at >= '2026-01-01' AND checked_out_at < '2026-07-01';
```

Alongside it I would pull `pg_stat_statements` for the real query — comparing
`shared_blks_read` against `shared_blks_hit` — because it settles a question the
plan alone cannot: whether the production 8 s is I/O waiting on a cold cache or
CPU burning through the filter. Both are consistent with a sequential scan, and
they have different ceilings. If it is I/O, the 142× reduction in blocks read is
the number that predicts the improvement. If it is CPU, the win comes from the
4.18M discarded rows disappearing, and I should expect less than the block ratio
suggests. I would rather know which before promising anyone a number.

---

# Part D — Production reasoning

## D1. Zero-downtime migration: adding a non-nullable `location_id`

**Four deploys and a backfill job.** The rule throughout is that old and new code
run simultaneously during every rolling deploy, so each schema state must be
valid for both.

**Deploy 1 — schema only, no code change.** Add the column nullable, add the FK
unvalidated, index concurrently:

```sql
ALTER TABLE checkouts ADD COLUMN location_id bigint NULL;
ALTER TABLE checkouts ADD CONSTRAINT checkouts_location_fk
      FOREIGN KEY (location_id) REFERENCES locations(id) NOT VALID;
CREATE INDEX CONCURRENTLY checkouts_location_id_idx ON checkouts (location_id);
```

In Django this is `SeparateDatabaseAndState` with `RunSQL` and `atomic = False`
— `CREATE INDEX CONCURRENTLY` cannot run inside a transaction.

**Deploy 2 — code writes the column** on every insert and update, while still
tolerating `NULL` on read. Must be fully rolled out across all four instances
before anything depends on it.

**Backfill — a batched job, not a deploy.** A few thousand rows per transaction,
committing between batches, throttled. One 4.2M-row `UPDATE` would hold row
locks for its whole duration and bloat the table with dead tuples faster than
autovacuum reclaims them.

**Deploy 3 — constrain**, once the backfill reports zero remaining NULLs:

```sql
ALTER TABLE checkouts VALIDATE CONSTRAINT checkouts_location_fk;
ALTER TABLE checkouts ADD CONSTRAINT loc_not_null
      CHECK (location_id IS NOT NULL) NOT VALID;
ALTER TABLE checkouts VALIDATE CONSTRAINT loc_not_null;
ALTER TABLE checkouts ALTER COLUMN location_id SET NOT NULL;
ALTER TABLE checkouts DROP CONSTRAINT loc_not_null;
```

The `CHECK`-then-`SET NOT NULL` dance matters: from PostgreSQL 12, `SET NOT NULL`
recognises an already-validated `CHECK` and skips its own full scan, so it holds
`ACCESS EXCLUSIVE` for milliseconds instead of minutes.

**Deploy 4 — model state**, flipping `null=False` on the field with no SQL.

**In-flight requests.** Old code selects its known columns explicitly, so an
unknown column is invisible to it — Django never issues `SELECT *`, which is why
this is safe. Old code's inserts omit `location_id` and write `NULL`, which is
legal until deploy 3. That is the whole reason `NOT NULL` waits: apply it while
any old instance is still serving and those instances start returning 500s on
every check-out. Rollback is safe at every step, because the column stays
nullable until the last one.

**The thing that locks the table.** Naïvely, `ALTER TABLE checkouts ADD COLUMN
location_id bigint NOT NULL REFERENCES locations(id)` — one statement that
rewrites 4.2M rows and validates the FK while holding `ACCESS EXCLUSIVE`. That is
what Django's default `AddField` generates.

But the sharper trap is the one that bites even on the "safe" statements.
I confirmed the lock modes:

```
ADD COLUMN nullable    -> AccessExclusiveLock       (instant, metadata only)
ADD FK ... NOT VALID   -> ShareRowExclusiveLock     (no scan)
VALIDATE CONSTRAINT    -> ShareUpdateExclusiveLock  (does not block reads/writes)
SET NOT NULL           -> AccessExclusiveLock
```

`ADD COLUMN` still takes `ACCESS EXCLUSIVE` — it is safe only because it is
*fast*. If one long-running `SELECT` holds `ACCESS SHARE`, the `ALTER` waits,
and **every query arriving afterwards queues behind the pending exclusive
request**. A migration that needs a millisecond of lock takes the site down for
as long as that one report runs. So every DDL statement runs with
`SET lock_timeout = '3s'`, failing fast and retrying rather than forming a
queue — and I would check `pg_stat_activity` for long transactions first.

## D2. Latency triage: `/reports/overdue/` at 25 s with no deploy in nine days

No deploy means the code is a constant. Something else moved: the data, the
plan, or the environment. I check in that order of cheapness.

**1. Is it only this endpoint?** Compare p95 across routes and hit
`/api/v1/health/`. If everything is slow, it is shared — DB host, connection
pool, network — and the overdue report is a symptom, not the story. If only this
route moved, it is specific to this query or its data. *Rules out/in: shared
infrastructure.*

**2. Step change or ramp?** Pull the latency graph for two weeks. "Fine for
months, 25 s this morning" says step, and a step at a precise timestamp points to
a discrete event — a plan flip, a failover, a long transaction starting, a batch
job. A gradual ramp would instead point at data growth, and would have been
visible for days. *Rules out slow accumulation.*

**3. Is the time in the database?** `pg_stat_statements` for this query's
`mean_exec_time` and `calls`. If the DB is still fast, the problem is app-side —
worker saturation, or serialising a suddenly-huge result. If the DB owns the 25 s,
continue. *Splits app from database.*

**4. How many rows does it now match?** The page is capped at 20, but
pagination's `COUNT(*)` runs over the whole matching set. If the overdue
population jumped — a bulk import, a batch of due dates crossing at once, or the
A4 sweep failing so nothing is being chased — the count is the cost.
*Rules in a data-volume change.*

**5. `EXPLAIN (ANALYZE, BUFFERS)` now**, against the known-good plan. I look for
`Seq Scan` where there was an index scan, `Heap Fetches` climbing under an
`Index Only Scan`, and estimated-versus-actual rows diverging.

**6. Vacuum and stats.** `pg_stat_user_tables`: `last_autoanalyze`,
`n_mod_since_analyze`, `n_dead_tup`, `last_autovacuum`.

**7. Locks and long transactions.** `pg_stat_activity` for
`state = 'idle in transaction'` ordered by `xact_start`; `pg_locks` for blockers;
`wait_event_type` on the slow backend.

**8. Resources.** Connection-pool saturation, DB CPU and IOPS, disk headroom,
replica lag if reads are routed to a replica.

### The two most likely, and how I would confirm each

**A plan flip from stale statistics.** The classic shape of "nothing changed and
it fell off a cliff". As the open-overdue set grew, the planner's estimate
crossed a cost threshold and it abandoned the index for a sequential scan — and
because the estimate is stale, it does not know it was wrong. **Confirm:**
`EXPLAIN (ANALYZE, BUFFERS)` and compare the scan node to the known-good plan;
check `last_autoanalyze` and `n_mod_since_analyze` on `checkouts`; then run
`ANALYZE checkouts` in a transaction and re-`EXPLAIN`. If the plan snaps back,
that is the answer, and the fix is per-table autovacuum tuning plus a raised
`default_statistics_target` on the columns involved — not a one-off `ANALYZE`.

**Autovacuum starved by a long-lived transaction.** One session left
`idle in transaction` — a stuck worker, an abandoned psql, a job that opened a
transaction and blocked — holds back the xmin horizon, so vacuum cannot reclaim
dead tuples anywhere. The visibility map goes stale, the index-only scan starts
fetching from the heap, and bloat grows the pages it has to read. This fits
"fine for months, sudden this morning" precisely, because the damage begins the
moment that session opens. **Confirm:**
`SELECT pid, state, xact_start, now() - xact_start AS age, query FROM
pg_stat_activity WHERE state LIKE 'idle in transaction%' ORDER BY xact_start;`
then compare `n_dead_tup` and `last_autovacuum` against a healthy table, and look
for `Heap Fetches` in the plan being large rather than zero. Terminating the
session and vacuuming would confirm it by fixing it.

I would not guess between them. Both are one query away, and step 5 usually
distinguishes them immediately: a `Seq Scan` points at the first, a high
`Heap Fetches` under a retained `Index Only Scan` points at the second.
