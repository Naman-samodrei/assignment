"""A5: the three-open-check-outs limit (rule 3), plus the rest of rules 1-8."""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from fieldassets.models import AssetStatus, CheckOut


@pytest.fixture
def post_checkout(api, days_ahead):
    url = reverse("fieldassets:checkout-create")

    def _post(asset_tag, employee_code, due_at=None):
        due = due_at if due_at is not None else days_ahead(7)
        return api.post(
            url,
            {
                "asset_tag": asset_tag,
                "employee_code": employee_code,
                "due_at": due if isinstance(due, str) else due.isoformat(),
            },
            format="json",
        )

    return _post


class TestThreeOpenCheckOutsLimit:
    """Rule 3, the A5 requirement."""

    def test_a_fourth_open_checkout_is_rejected(
        self, post_checkout, make_asset, make_employee, make_open_checkout
    ):
        employee = make_employee()
        for _ in range(3):
            make_open_checkout(employee)
        fourth = make_asset()

        response = post_checkout(fourth.asset_tag, employee.employee_code)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert CheckOut.objects.filter(employee=employee).count() == 3
        fourth.refresh_from_db()
        assert fourth.status == AssetStatus.AVAILABLE

    def test_the_third_still_succeeds(
        self, post_checkout, make_asset, make_employee, make_open_checkout
    ):
        """The boundary is three allowed, not two."""
        employee = make_employee()
        for _ in range(2):
            make_open_checkout(employee)

        response = post_checkout(make_asset().asset_tag, employee.employee_code)

        assert response.status_code == status.HTTP_201_CREATED
        assert CheckOut.objects.filter(employee=employee).count() == 3

    def test_returned_checkouts_do_not_count(
        self, post_checkout, make_asset, make_employee, make_open_checkout
    ):
        """The limit is on *open* holds, so returning one frees a slot."""
        employee = make_employee()
        held = [make_open_checkout(employee) for _ in range(3)]
        held[0].returned_at = timezone.now()
        held[0].save(update_fields=["returned_at"])

        response = post_checkout(make_asset().asset_tag, employee.employee_code)

        assert response.status_code == status.HTTP_201_CREATED

    def test_the_limit_is_per_employee(
        self, post_checkout, make_asset, make_employee, make_open_checkout
    ):
        busy = make_employee()
        for _ in range(3):
            make_open_checkout(busy)

        response = post_checkout(make_asset().asset_tag, make_employee().employee_code)

        assert response.status_code == status.HTTP_201_CREATED

    @override_settings(MAX_OPEN_CHECKOUTS_PER_EMPLOYEE=1)
    def test_the_limit_is_read_from_settings(
        self, post_checkout, make_asset, make_employee, make_open_checkout
    ):
        employee = make_employee()
        make_open_checkout(employee)

        response = post_checkout(make_asset().asset_tag, employee.employee_code)

        assert response.status_code == status.HTTP_409_CONFLICT


class TestAssetMustBeAvailable:
    """Rule 1."""

    @pytest.mark.parametrize(
        "asset_status", [AssetStatus.CHECKED_OUT, AssetStatus.MAINTENANCE]
    )
    def test_an_unavailable_asset_is_a_conflict(
        self, post_checkout, make_asset, make_employee, asset_status
    ):
        asset = make_asset(status=asset_status)

        response = post_checkout(asset.asset_tag, make_employee().employee_code)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert not CheckOut.objects.exists()


class TestEmployeeMustBeActive:
    """Rule 2."""

    def test_an_inactive_employee_is_a_bad_request(
        self, post_checkout, make_asset, make_employee
    ):
        asset = make_asset()

        response = post_checkout(
            asset.asset_tag, make_employee(is_active=False).employee_code
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not CheckOut.objects.exists()
        asset.refresh_from_db()
        assert asset.status == AssetStatus.AVAILABLE


class TestDueAtWindow:
    """Rule 4."""

    def test_a_past_due_at_is_rejected(self, post_checkout, make_asset, make_employee, days_ahead):
        response = post_checkout(
            make_asset().asset_tag, make_employee().employee_code, days_ahead(-1)
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_due_at_exactly_now_is_rejected(self, post_checkout, make_asset, make_employee):
        """'In the future' is exclusive."""
        response = post_checkout(
            make_asset().asset_tag, make_employee().employee_code, timezone.now()
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_more_than_thirty_days_out_is_rejected(
        self, post_checkout, make_asset, make_employee, days_ahead
    ):
        response = post_checkout(
            make_asset().asset_tag, make_employee().employee_code, days_ahead(31)
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_just_inside_thirty_days_is_accepted(self, post_checkout, make_asset, make_employee):
        due = timezone.now() + timedelta(days=30) - timedelta(minutes=1)
        response = post_checkout(
            make_asset().asset_tag, make_employee().employee_code, due
        )
        assert response.status_code == status.HTTP_201_CREATED

    def test_an_unparseable_due_at_is_a_400_not_a_crash(
        self, post_checkout, make_asset, make_employee
    ):
        response = post_checkout(
            make_asset().asset_tag, make_employee().employee_code, "not-a-date"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestAtomicity:
    """Rule 5's failure half, by fault injection."""

    def test_a_failure_after_the_insert_leaves_no_checkout_row(
        self, post_checkout, make_asset, make_employee, monkeypatch
    ):
        asset = make_asset()
        employee = make_employee()

        def boom(self, *args, **kwargs):
            raise RuntimeError("status update failed")

        monkeypatch.setattr("fieldassets.models.Asset.save", boom)

        with pytest.raises(RuntimeError):
            post_checkout(asset.asset_tag, employee.employee_code)

        assert not CheckOut.objects.exists()
        asset.refresh_from_db()
        assert asset.status == AssetStatus.AVAILABLE


class TestReturn:
    """Rule 6."""

    def test_return_closes_the_row_and_frees_the_asset(
        self, api, make_employee, make_open_checkout
    ):
        checkout = make_open_checkout(make_employee())
        before = timezone.now()

        response = api.post(
            reverse("fieldassets:checkout-return", args=[checkout.pk]), {}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        checkout.refresh_from_db()
        assert checkout.returned_at is not None
        assert checkout.returned_at >= before
        checkout.asset.refresh_from_db()
        assert checkout.asset.status == AssetStatus.AVAILABLE

    def test_needs_maintenance_quarantines_the_asset(
        self, api, make_employee, make_open_checkout
    ):
        checkout = make_open_checkout(make_employee())

        response = api.post(
            reverse("fieldassets:checkout-return", args=[checkout.pk]),
            {"needs_maintenance": True, "condition_note": "cracked lens"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        checkout.refresh_from_db()
        checkout.asset.refresh_from_db()
        assert checkout.asset.status == AssetStatus.MAINTENANCE
        assert checkout.condition_note == "cracked lens"

    def test_returning_twice_is_a_conflict(self, api, make_employee, make_open_checkout):
        checkout = make_open_checkout(make_employee())
        url = reverse("fieldassets:checkout-return", args=[checkout.pk])
        api.post(url, {}, format="json")

        response = api.post(url, {}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_a_stale_second_return_does_not_move_the_asset_again(
        self, api, make_employee, make_open_checkout
    ):
        checkout = make_open_checkout(make_employee())
        url = reverse("fieldassets:checkout-return", args=[checkout.pk])
        api.post(url, {"needs_maintenance": True}, format="json")

        api.post(url, {}, format="json")

        checkout.asset.refresh_from_db()
        assert checkout.asset.status == AssetStatus.MAINTENANCE

    def test_returning_an_unknown_checkout_is_a_404(self, api):
        response = api.post(
            reverse("fieldassets:checkout-return", args=[999999]), {}, format="json"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestUnknownKeys:
    """Rule 8 — 404, never 500."""

    def test_an_unknown_asset_tag_is_a_404(self, post_checkout, make_employee):
        response = post_checkout("NOPE-999", make_employee().employee_code)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_an_unknown_employee_code_is_a_404(self, post_checkout, make_asset):
        response = post_checkout(make_asset().asset_tag, "ZZZ")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_both_unknown_is_a_404(self, post_checkout, db):
        response = post_checkout("NOPE-999", "ZZZ")
        assert response.status_code == status.HTTP_404_NOT_FOUND
