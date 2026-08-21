"""A5: the employee summary's four numbers, against data we control."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from fieldassets.models import CheckOut
from fieldassets.queries import employee_summary

pytestmark = pytest.mark.django_db


@pytest.fixture
def summary(api):
    def _get(employee_code):
        return api.get(
            reverse("fieldassets:employee-summary", args=[employee_code])
        )

    return _get


@pytest.fixture
def controlled_history(make_employee, make_asset, make_open_checkout):
    """An employee whose four numbers are known by construction.

    Three returned check-outs held for 4, 8 and 12 days -> mean 8.0.
    Three still open, two of them overdue.
    So: lifetime 6, held 3, overdue 2, mean_hold_days 8.0.
    """

    def _build():
        now = timezone.now()
        employee = make_employee(employee_code="EMP001", full_name="Asha Iyer")

        for held_days in (4, 8, 12):
            checkout = CheckOut.objects.create(
                asset=make_asset(),
                employee=employee,
                due_at=now + timedelta(days=30),
                returned_at=now,
            )
            CheckOut.objects.filter(pk=checkout.pk).update(
                checked_out_at=now - timedelta(days=held_days)
            )

        make_open_checkout(employee, due_at=now - timedelta(days=5))   # overdue
        make_open_checkout(employee, due_at=now - timedelta(days=1))   # overdue
        make_open_checkout(employee, due_at=now + timedelta(days=3))   # not overdue

        return employee

    return _build


class TestTheFourNumbers:
    def test_all_four_against_known_data(self, summary, controlled_history):
        controlled_history()

        response = summary("EMP001")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["employee_code"] == "EMP001"
        assert response.data["full_name"] == "Asha Iyer"
        assert response.data["lifetime_checkouts"] == 6
        assert response.data["currently_held"] == 3
        assert response.data["currently_overdue"] == 2
        assert response.data["mean_hold_days"] == pytest.approx(8.0, abs=0.01)

    def test_lifetime_counts_returned_and_open_alike(
        self, summary, make_employee, make_open_checkout
    ):
        employee = make_employee(employee_code="EMP002")
        make_open_checkout(employee)
        returned = make_open_checkout(employee)
        returned.returned_at = timezone.now()
        returned.save(update_fields=["returned_at"])

        assert summary("EMP002").data["lifetime_checkouts"] == 2

    def test_an_item_due_exactly_now_counts_as_overdue(
        self, make_employee, make_open_checkout
    ):
        """Same boundary as the report and the task, so the three cannot disagree.

        This goes at the queryset rather than the endpoint because the boundary
        is only meaningful with ``now`` pinned: through the view, ``now`` is
        computed microseconds after the fixture and the case would pass under
        ``<`` as well as ``<=``, testing nothing.
        """
        now = timezone.now()
        employee = make_employee(employee_code="EMP003")
        make_open_checkout(employee, due_at=now)

        row = employee_summary("EMP003", now=now).get()

        assert row.currently_overdue == 1

    def test_an_item_due_a_microsecond_from_now_is_not_yet_overdue(
        self, make_employee, make_open_checkout
    ):
        now = timezone.now()
        employee = make_employee(employee_code="EMP008")
        make_open_checkout(employee, due_at=now + timedelta(microseconds=1))

        row = employee_summary("EMP008", now=now).get()

        assert row.currently_overdue == 0

    def test_another_employees_checkouts_are_not_counted(
        self, summary, make_employee, make_open_checkout
    ):
        mine = make_employee(employee_code="EMP004")
        theirs = make_employee(employee_code="EMP005")
        make_open_checkout(mine)
        for _ in range(3):
            make_open_checkout(theirs)

        assert summary("EMP004").data["currently_held"] == 1


class TestEmptyCases:
    def test_a_brand_new_employee_gets_four_zeros(self, summary, make_employee):
        make_employee(employee_code="EMP006")

        data = summary("EMP006").data

        assert data["lifetime_checkouts"] == 0
        assert data["currently_held"] == 0
        assert data["currently_overdue"] == 0
        assert data["mean_hold_days"] == 0.0

    def test_mean_is_zero_when_nothing_has_been_returned(
        self, summary, make_employee, make_open_checkout
    ):
        """SQL AVG of an empty set is NULL; the contract is four numbers."""
        employee = make_employee(employee_code="EMP007")
        make_open_checkout(employee)

        assert summary("EMP007").data["mean_hold_days"] == 0.0

    def test_an_unknown_employee_is_a_404(self, summary):
        assert summary("ZZZ").status_code == status.HTTP_404_NOT_FOUND


class TestHowItIsComputed:
    def test_the_four_numbers_come_from_a_single_query(
        self, summary, controlled_history, django_assert_num_queries
    ):
        """'Computed by the database in a single query ... not by looping in Python.'

        Refactoring this into a Python loop fails the build rather than passing
        quietly.
        """
        controlled_history()

        with django_assert_num_queries(1):
            summary("EMP001")

    def test_the_query_count_does_not_grow_with_history(
        self, summary, controlled_history, make_open_checkout, make_employee,
        django_assert_num_queries,
    ):
        employee = controlled_history()
        for _ in range(10):
            make_open_checkout(employee)

        with django_assert_num_queries(1):
            summary("EMP001")
