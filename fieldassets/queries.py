"""Querysets for the two A3 endpoints that constrain how many queries they cost.

The employee summary must compute its four numbers in a single query with ORM
aggregation, and the overdue report must not issue a query per row. Both live
here rather than in the views so the shape of the SQL is visible in one place.
"""

from django.db.models import (
    Avg,
    Count,
    DateTimeField,
    DurationField,
    ExpressionWrapper,
    F,
    Q,
    Value,
)
from django.utils import timezone

from .models import CheckOut, Employee


def employee_summary(employee_code, *, now=None):
    """The four numbers for one employee, in one SELECT.

    All four aggregates hang off the same ``checkouts`` relation, so the ORM
    emits a single LEFT JOIN with conditional aggregates over it — one query, no
    Python loop, and no per-check-out fan-out to de-duplicate.
    """
    now = now or timezone.now()
    open_q = Q(checkouts__returned_at__isnull=True)
    returned_q = Q(checkouts__returned_at__isnull=False)
    overdue_q = open_and_overdue(now, prefix="checkouts")

    hold_duration = ExpressionWrapper(
        F("checkouts__returned_at") - F("checkouts__checked_out_at"),
        output_field=DurationField(),
    )

    return Employee.objects.filter(employee_code=employee_code).annotate(
        lifetime_checkouts=Count("checkouts"),
        currently_held=Count("checkouts", filter=open_q),
        currently_overdue=Count("checkouts", filter=overdue_q),
        mean_hold=Avg(hold_duration, filter=returned_q),
    )


def open_and_overdue(now, prefix=""):
    """The one definition of "overdue", used everywhere it is needed.

    ``due_at <= now`` rather than ``<``: a check-out due at exactly this instant
    has reached its deadline, so it counts.

    ``prefix`` lets the same predicate be applied across a relation, so the
    employee summary's ``currently_overdue`` is literally this function and not
    a second copy that could drift from the report and the A4 task.
    """
    field = f"{prefix}__" if prefix else ""
    return Q(**{f"{field}returned_at__isnull": True, f"{field}due_at__lte": now})


def overdue_checkouts(*, now=None):
    """Open check-outs past due, most overdue first.

    ``select_related`` pulls the asset and employee in the same query, so the
    row count does not change the query count. ``days_overdue`` is computed by
    the database as part of that one SELECT.
    """
    now = now or timezone.now()
    return (
        CheckOut.objects.filter(open_and_overdue(now))
        .select_related("asset", "employee")
        .annotate(
            overdue_by=ExpressionWrapper(
                Value(now, output_field=DateTimeField()) - F("due_at"),
                output_field=DurationField(),
            )
        )
        .order_by("due_at", "pk")
    )
