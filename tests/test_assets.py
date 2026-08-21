"""The asset endpoints: create, filter, search, pagination, current_holder (A3)."""

import pytest
from django.urls import reverse
from rest_framework import status

from fieldassets.models import Asset, AssetCategory, AssetStatus

pytestmark = pytest.mark.django_db


class TestCreate:
    def test_it_creates_an_asset(self, api):
        response = api.post(
            reverse("fieldassets:asset-list"),
            {
                "asset_tag": "CAM-009",
                "name": "Nikon Z6",
                "category": "CAMERA",
                "purchase_date": "2025-03-01",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert Asset.objects.get().asset_tag == "CAM-009"

    def test_a_new_asset_defaults_to_available(self, api):
        api.post(
            reverse("fieldassets:asset-list"),
            {
                "asset_tag": "CAM-010",
                "name": "Nikon Z7",
                "category": "CAMERA",
                "purchase_date": "2025-03-01",
            },
            format="json",
        )

        assert Asset.objects.get().status == AssetStatus.AVAILABLE

    def test_a_duplicate_tag_is_a_400(self, api, make_asset):
        existing = make_asset()

        response = api.post(
            reverse("fieldassets:asset-list"),
            {
                "asset_tag": existing.asset_tag,
                "name": "Clone",
                "category": "CAMERA",
                "purchase_date": "2025-03-01",
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestListing:
    def test_it_paginates_at_twenty(self, api, make_asset):
        for _ in range(25):
            make_asset()

        response = api.get(reverse("fieldassets:asset-list"))

        assert response.data["count"] == 25
        assert len(response.data["results"]) == 20

    def test_it_filters_by_status_and_category(self, api, make_asset):
        make_asset(category=AssetCategory.LAPTOP, status=AssetStatus.AVAILABLE)
        make_asset(category=AssetCategory.LAPTOP, status=AssetStatus.MAINTENANCE)
        make_asset(category=AssetCategory.CAMERA, status=AssetStatus.AVAILABLE)

        response = api.get(
            reverse("fieldassets:asset-list"),
            {"status": "AVAILABLE", "category": "LAPTOP"},
        )

        assert response.data["count"] == 1

    def test_search_matches_the_name(self, api, make_asset):
        make_asset(name="ThinkPad Bolero")
        make_asset(name="MacBook Pro")

        response = api.get(reverse("fieldassets:asset-list"), {"search": "Bolero"})

        assert [r["name"] for r in response.data["results"]] == ["ThinkPad Bolero"]

    def test_search_matches_the_asset_tag(self, api, make_asset):
        make_asset(asset_tag="VEH-001", name="Pickup")
        make_asset(asset_tag="CAM-001", name="Camera")

        response = api.get(reverse("fieldassets:asset-list"), {"search": "VEH"})

        assert [r["asset_tag"] for r in response.data["results"]] == ["VEH-001"]


class TestCurrentHolder:
    def test_it_is_null_for_an_available_asset(self, api, make_asset):
        asset = make_asset()

        response = api.get(reverse("fieldassets:asset-detail", args=[asset.pk]))

        assert response.data["current_holder"] is None

    def test_it_names_the_holder_when_checked_out(
        self, api, make_asset, make_employee, make_open_checkout
    ):
        asset = make_asset()
        employee = make_employee(employee_code="EMP001", full_name="Asha Iyer")
        make_open_checkout(employee, asset=asset)

        response = api.get(reverse("fieldassets:asset-detail", args=[asset.pk]))

        assert response.data["current_holder"] == {
            "employee_code": "EMP001",
            "full_name": "Asha Iyer",
        }

    def test_it_goes_back_to_null_after_a_return(
        self, api, make_asset, make_employee, make_open_checkout
    ):
        asset = make_asset()
        checkout = make_open_checkout(make_employee(), asset=asset)
        api.post(
            reverse("fieldassets:checkout-return", args=[checkout.pk]), {}, format="json"
        )

        response = api.get(reverse("fieldassets:asset-detail", args=[asset.pk]))

        assert response.data["current_holder"] is None
        assert response.data["status"] == AssetStatus.AVAILABLE
