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
