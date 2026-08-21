"""A5: the concurrency rule — two simultaneous check-outs, exactly one wins.

``transaction=True`` because the racers must see each other's commits; the
per-test transaction a normal ``django_db`` wraps everything in would hide them
and the race would pass without ever having raced.
"""

import threading

import pytest
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from fieldassets.exceptions import Conflict
from fieldassets.models import AssetStatus, CheckOut
from fieldassets.services import check_out_asset


def race(target, count):
    """Run ``target(i)`` in ``count`` threads released from one barrier.

    Returns ``(result, exception)`` pairs in thread order. Each thread closes
    its own connection so none is reused across threads.
    """
    barrier = threading.Barrier(count)
    results = [None] * count

    def runner(i):
        try:
            barrier.wait()
            results[i] = (target(i), None)
        except Exception as exc:  # noqa: BLE001 - the outcome *is* the assertion
            results[i] = (None, exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_checkouts_of_one_asset_leave_exactly_one_winner(
    make_asset, make_employee, days_ahead
):
    """The rule as the brief words it."""
    asset = make_asset()
    employees = [make_employee(), make_employee()]
    due_at = days_ahead(7)

    results = race(
        lambda i: check_out_asset(
            asset_tag=asset.asset_tag,
            employee_code=employees[i].employee_code,
            due_at=due_at,
        ),
        count=2,
    )

    winners = [r for r, exc in results if exc is None]
    losers = [exc for _, exc in results if exc is not None]

    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert len(losers) == 1
    assert isinstance(losers[0], Conflict)
    assert losers[0].status_code == status.HTTP_409_CONFLICT

    assert CheckOut.objects.filter(asset=asset).count() == 1
    asset.refresh_from_db()
    assert asset.status == AssetStatus.CHECKED_OUT


@pytest.mark.django_db(transaction=True)
def test_five_simultaneous_checkouts_still_leave_exactly_one_winner(
    make_asset, make_employee, days_ahead
):
    asset = make_asset()
    employees = [make_employee() for _ in range(5)]
    due_at = days_ahead(7)

    results = race(
        lambda i: check_out_asset(
            asset_tag=asset.asset_tag,
            employee_code=employees[i].employee_code,
            due_at=due_at,
        ),
        count=5,
    )

    winners = [r for r, exc in results if exc is None]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert all(isinstance(exc, Conflict) for _, exc in results if exc is not None)
    assert CheckOut.objects.filter(asset=asset).count() == 1


@pytest.mark.django_db(transaction=True)
def test_the_loser_gets_a_409_over_http_not_a_500(
    make_asset, make_employee, days_ahead
):
    """The same race through the view, so the client-visible codes are asserted."""
    from django.contrib.auth.models import User

    asset = make_asset()
    employees = [make_employee(), make_employee()]
    url = reverse("fieldassets:checkout-create")
    due_at = days_ahead(7).isoformat()
    user = User(username="racer", pk=1)

    def attempt(i):
        client = APIClient()
        client.force_authenticate(user)
        return client.post(
            url,
            {
                "asset_tag": asset.asset_tag,
                "employee_code": employees[i].employee_code,
                "due_at": due_at,
            },
            format="json",
        ).status_code

    codes = sorted(code for code, _ in race(attempt, count=2))

    assert codes == [status.HTTP_201_CREATED, status.HTTP_409_CONFLICT]
    assert CheckOut.objects.filter(asset=asset).count() == 1


@pytest.mark.django_db(transaction=True)
def test_checkouts_of_different_assets_do_not_block_each_other(
    make_asset, make_employee, days_ahead
):
    """The lock is per asset; unrelated check-outs must not serialise away."""
    assets = [make_asset() for _ in range(3)]
    employees = [make_employee() for _ in range(3)]
    due_at = days_ahead(7)

    results = race(
        lambda i: check_out_asset(
            asset_tag=assets[i].asset_tag,
            employee_code=employees[i].employee_code,
            due_at=due_at,
        ),
        count=3,
    )

    assert [exc for _, exc in results if exc is not None] == []
    assert CheckOut.objects.count() == 3


@pytest.mark.django_db(transaction=True)
def test_the_database_rejects_a_second_open_checkout_row(
    make_asset, make_employee, days_ahead
):
    """The guarantee under the lock, asserted against the index directly."""
    asset = make_asset()
    first, second = make_employee(), make_employee()
    CheckOut.objects.create(asset=asset, employee=first, due_at=days_ahead())

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CheckOut.objects.create(asset=asset, employee=second, due_at=days_ahead())


@pytest.mark.django_db(transaction=True)
def test_a_constraint_violation_surfaces_as_409_not_500(
    make_asset, make_employee, days_ahead
):
    """The savepoint path, driven on its own.

    An AVAILABLE asset that already has an open check-out is exactly what a
    racer sees in the instant before the winner commits its status update.
    """
    asset = make_asset(status=AssetStatus.AVAILABLE)
    holder, contender = make_employee(), make_employee()
    CheckOut.objects.create(asset=asset, employee=holder, due_at=days_ahead())

    with pytest.raises(Conflict) as caught:
        check_out_asset(
            asset_tag=asset.asset_tag,
            employee_code=contender.employee_code,
            due_at=days_ahead(),
        )

    assert caught.value.status_code == status.HTTP_409_CONFLICT
    assert CheckOut.objects.filter(asset=asset).count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_returned_checkout_does_not_block_the_next_one(
    make_asset, make_employee, days_ahead
):
    """The index is partial: it covers open rows only."""
    asset = make_asset()
    first, second = make_employee(), make_employee()
    for _ in range(2):
        CheckOut.objects.create(
            asset=asset, employee=first, due_at=days_ahead(), returned_at=timezone.now()
        )

    CheckOut.objects.create(asset=asset, employee=second, due_at=days_ahead())

    assert CheckOut.objects.filter(asset=asset).count() == 3
