"""The A2 business rules.

All of the rules live here rather than in the views, so that the same
guarantees hold whether a check-out comes from the API, a management command or
the shell. Views translate HTTP to arguments; this module decides what is
allowed and raises the exception carrying the right status code.

Rule map:

1. asset not AVAILABLE                     -> 409  (``AssetNotAvailable``)
2. inactive employee                       -> 400  (``ValidationError``)
3. more than three open check-outs         -> 409  (``CheckOutLimitReached``)
4. due_at not in (now, now + 30 days]      -> 400  (``ValidationError``)
5. row + status change are one transaction -> both commit or neither does
6. return sets returned_at and the status; second return -> 409
7. two racers on one asset -> exactly one wins, at the database level
8. unknown asset_tag / employee_code       -> 404  (``NotFound``)
"""

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from .exceptions import AlreadyReturned, AssetNotAvailable, CheckOutLimitReached
from .models import Asset, AssetStatus, CheckOut, Employee

#: Name of the partial unique index that enforces rule 7 in the database.
OPEN_CHECKOUT_CONSTRAINT = "uniq_open_checkout_per_asset"


def max_open_checkouts() -> int:
    return getattr(settings, "MAX_OPEN_CHECKOUTS_PER_EMPLOYEE", 3)


def max_due_horizon() -> timedelta:
    return timedelta(days=getattr(settings, "MAX_DUE_AT_HORIZON_DAYS", 30))


def validate_due_at(due_at, *, now=None):
    """Rule 4: strictly in the future, at most 30 days out. Raises on failure."""
    now = now or timezone.now()
    if due_at is None:
        raise ValidationError("due_at is required.")
    if due_at <= now:
        raise ValidationError("due_at must be in the future.")
    if due_at > now + max_due_horizon():
        raise ValidationError(
            f"due_at must be no more than {max_due_horizon().days} days from now."
        )
    return due_at


def open_checkout_count(employee: Employee) -> int:
    return CheckOut.objects.filter(
        employee=employee, returned_at__isnull=True
    ).count()


def _is_open_checkout_conflict(exc: IntegrityError) -> bool:
    """True when the IntegrityError is the rule 7 partial unique index firing.

    The two backends word it differently: PostgreSQL names the index
    ("duplicate key value violates unique constraint
    \"uniq_open_checkout_per_asset\""), SQLite names the column
    ("UNIQUE constraint failed: fieldassets_checkout.asset_id"). Both forms are
    derived from the model rather than hardcoded, and anything else is left to
    propagate — an unrelated integrity error is a bug, not a 409.
    """
    message = str(exc)
    if OPEN_CHECKOUT_CONSTRAINT in message:
        return True
    table = CheckOut._meta.db_table
    column = CheckOut._meta.get_field("asset").column
    return f"{table}.{column}" in message


@transaction.atomic
def check_out_asset(*, asset_tag, employee_code, due_at, condition_note=""):
    """Check ``asset_tag`` out to ``employee_code``, or raise.

    Rule 5: the whole function body is one transaction, so the CheckOut row and
    the asset's CHECKED_OUT status commit together or not at all.

    Rule 7: the asset row is locked with ``SELECT ... FOR UPDATE`` before its
    status is read, so a second request for the same asset waits, then reads the
    committed CHECKED_OUT status and loses with a 409. Underneath that, the
    partial unique index makes the invariant true even if a caller ever reaches
    the insert without the lock — the loser's IntegrityError becomes the same
    409 rather than a 500.
    """
    validate_due_at(due_at)

    # Locks are always taken asset-then-employee so two concurrent check-outs
    # can never hold one and wait on the other.
    asset = _get_locked_asset(asset_tag)
    employee = _get_locked_employee(employee_code)

    if not employee.is_active:  # rule 2
        raise ValidationError(
            {"employee_code": ["Employee is inactive and cannot check out assets."]}
        )

    if asset.status != AssetStatus.AVAILABLE:  # rule 1
        raise AssetNotAvailable(
            f"Asset {asset.asset_tag} is {asset.status} and cannot be checked out."
        )

    limit = max_open_checkouts()
    if open_checkout_count(employee) >= limit:  # rule 3
        raise CheckOutLimitReached(
            f"Employee {employee.employee_code} already holds {limit} open "
            f"check-outs, which is the maximum."
        )

    try:
        # A savepoint, so a constraint violation does not poison the outer
        # transaction and can be answered with a 409.
        with transaction.atomic():
            checkout = CheckOut.objects.create(
                asset=asset,
                employee=employee,
                due_at=due_at,
                condition_note=condition_note or "",
            )
    except IntegrityError as exc:  # rule 7, the database's own answer
        if _is_open_checkout_conflict(exc):
            raise AssetNotAvailable(
                f"Asset {asset.asset_tag} was checked out by another request."
            ) from exc
        raise

    asset.status = AssetStatus.CHECKED_OUT
    asset.save(update_fields=["status", "updated_at"])
    return checkout


@transaction.atomic
def return_checkout(*, checkout_id, needs_maintenance=False, condition_note=None):
    """Rule 6: close an open check-out and free (or quarantine) the asset."""
    try:
        checkout = CheckOut.objects.select_for_update().get(pk=checkout_id)
    except CheckOut.DoesNotExist as exc:
        raise NotFound(f"No check-out with id {checkout_id}.") from exc

    if checkout.returned_at is not None:
        raise AlreadyReturned(
            f"Check-out {checkout.pk} was already returned at "
            f"{checkout.returned_at.isoformat()}."
        )

    asset = Asset.objects.select_for_update().get(pk=checkout.asset_id)

    checkout.returned_at = timezone.now()
    fields = ["returned_at"]
    if condition_note is not None:
        checkout.condition_note = condition_note
        fields.append("condition_note")
    checkout.save(update_fields=fields)

    asset.status = (
        AssetStatus.MAINTENANCE if needs_maintenance else AssetStatus.AVAILABLE
    )
    asset.save(update_fields=["status", "updated_at"])

    checkout.asset = asset
    return checkout


def _get_locked_asset(asset_tag) -> Asset:
    try:
        return Asset.objects.select_for_update().get(asset_tag=asset_tag)
    except Asset.DoesNotExist as exc:  # rule 8
        raise NotFound(f"No asset with asset_tag {asset_tag!r}.") from exc


def _get_locked_employee(employee_code) -> Employee:
    try:
        return Employee.objects.select_for_update().get(employee_code=employee_code)
    except Employee.DoesNotExist as exc:  # rule 8
        raise NotFound(f"No employee with employee_code {employee_code!r}.") from exc
