"""A5: the overdue calculation, including an item due exactly now."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from fieldassets.models import CheckOut
from fieldassets.queries import overdue_checkouts

pytestmark = pytest.mark.django_db


class TestTheBoundary:
    """`due_at <= now`, so a check-out due at this exact instant is overdue."""

    def test_an_item_due_exactly_now_is_overdue(self, make_employee, make_open_checkout):
        now = timezone.now()
        checkout = make_open_checkout(make_employee(), due_at=now)

        overdue = overdue_checkouts(now=now)

        assert list(overdue.values_list("pk", flat=True)) == [checkout.pk]

    def test_an_item_due_one_microsecond_from_now_is_not_overdue(
        self, make_employee, make_open_checkout
    ):
        """The other side of the same boundary."""
        now = timezone.now()
        make_open_checkout(make_employee(), due_at=now + timedelta(microseconds=1))

        assert overdue_checkouts(now=now).count() == 0

    def test_an_item_due_one_microsecond_ago_is_overdue(
        self, make_employee, make_open_checkout
    ):
        now = timezone.now()
        make_open_checkout(make_employee(), due_at=now - timedelta(microseconds=1))

        assert overdue_checkouts(now=now).count() == 1

    def test_days_overdue_is_zero_for_an_item_due_exactly_now(
        self, make_employee, make_open_checkout
    ):
        now = timezone.now()
        make_open_checkout(make_employee(), due_at=now)

        row = overdue_checkouts(now=now).get()

        assert row.overdue_by == timedelta(0)


class TestWhatCounts:
    def test_a_returned_checkout_is_never_overdue(
        self, make_employee, make_open_checkout
    ):
        now = timezone.now()
        checkout = make_open_checkout(make_employee(), due_at=now - timedelta(days=5))
        checkout.returned_at = now
        checkout.save(update_fields=["returned_at"])

        assert overdue_checkouts(now=now).count() == 0

    def test_a_future_checkout_is_not_overdue(self, make_employee, make_open_checkout):
        now = timezone.now()
        make_open_checkout(make_employee(), due_at=now + timedelta(days=3))

        assert overdue_checkouts(now=now).count() == 0

    def test_days_overdue_is_computed_from_due_at(
        self, make_employee, make_open_checkout
    ):
        now = timezone.now()
        make_open_checkout(make_employee(), due_at=now - timedelta(days=9, hours=12))

        row = overdue_checkouts(now=now).get()

        assert row.overdue_by == timedelta(days=9, hours=12)


class TestOrdering:
    def test_most_overdue_first(self, make_employee, make_open_checkout):
        now = timezone.now()
        employee = make_employee()
        for days in [3, 9, 1]:
            make_open_checkout(employee, due_at=now - timedelta(days=days))

        rows = list(overdue_checkouts(now=now))

        assert [r.overdue_by.days for r in rows] == [9, 3, 1]


class TestTheEndpoint:
    def test_the_report_returns_the_required_fields(
        self, api, make_asset, make_employee, make_open_checkout
    ):
        asset = make_asset(name="ThinkPad Bolero", asset_tag="LAP-001")
        employee = make_employee(full_name="Asha Iyer", employee_code="EMP001")
        make_open_checkout(
            employee, asset=asset, due_at=timezone.now() - timedelta(days=4)
        )

        response = api.get(reverse("fieldassets:overdue-report"))

        assert response.status_code == status.HTTP_200_OK
        (row,) = response.data["results"]
        assert row["asset_tag"] == "LAP-001"
        assert row["asset_name"] == "ThinkPad Bolero"
        assert row["employee_code"] == "EMP001"
        assert row["employee_name"] == "Asha Iyer"
        assert row["days_overdue"] == pytest.approx(4.0, abs=0.01)

    def test_the_report_is_paginated_at_twenty(
        self, api, make_employee, make_open_checkout
    ):
        now = timezone.now()
        employee = make_employee()
        for i in range(25):
            make_open_checkout(employee, due_at=now - timedelta(days=i + 1))

        response = api.get(reverse("fieldassets:overdue-report"))

        assert response.data["count"] == 25
        assert len(response.data["results"]) == 20
        assert response.data["next"] is not None

    def test_the_row_count_does_not_change_the_query_count(
        self, api, make_employee, make_open_checkout, django_assert_num_queries
    ):
        """'Must not issue a query per row.'"""
        now = timezone.now()
        employee = make_employee()
        for i in range(3):
            make_open_checkout(employee, due_at=now - timedelta(days=i + 1))

        url = reverse("fieldassets:overdue-report")
        with django_assert_num_queries(2) as small:  # COUNT for pagination + the page
            api.get(url)

        for i in range(3, 18):
            make_open_checkout(employee, due_at=now - timedelta(days=i + 1))

        with django_assert_num_queries(len(small.captured_queries)):
            api.get(url)

    def test_a_returned_checkout_disappears_from_the_report(
        self, api, make_employee, make_open_checkout
    ):
        checkout = make_open_checkout(
            make_employee(), due_at=timezone.now() - timedelta(days=2)
        )
        api.post(
            reverse("fieldassets:checkout-return", args=[checkout.pk]), {}, format="json"
        )

        response = api.get(reverse("fieldassets:overdue-report"))

        assert response.data["count"] == 0
