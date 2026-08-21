"""Shared fixtures. Factories are plain functions so each test reads as its rule."""

from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from fieldassets.models import Asset, AssetCategory, AssetStatus, CheckOut, Employee


@pytest.fixture
def api(db):
    """An authenticated client. Auth itself is covered in test_auth.py.

    Depends on ``db`` because every endpoint touches the database; without it a
    test that only asks for ``api`` fails with pytest-django's access guard.
    """
    client = APIClient()
    client.force_authenticate(User(username="tester", pk=1))
    return client


@pytest.fixture
def anon():
    return APIClient()


@pytest.fixture
def make_asset(db):
    counter = {"n": 0}

    def _make(**kwargs):
        counter["n"] += 1
        n = counter["n"]
        return Asset.objects.create(
            **{
                "asset_tag": f"AST-{n:04d}",
                "name": f"Asset {n}",
                "category": AssetCategory.CAMERA,
                "status": AssetStatus.AVAILABLE,
                "purchase_date": date(2024, 1, 1),
                **kwargs,
            }
        )

    return _make


@pytest.fixture
def make_employee(db):
    counter = {"n": 0}

    def _make(**kwargs):
        counter["n"] += 1
        n = counter["n"]
        return Employee.objects.create(
            **{
                "employee_code": f"EMP{n:04d}",
                "full_name": f"Employee {n}",
                "email": f"employee{n}@example.com",
                "is_active": True,
                **kwargs,
            }
        )

    return _make


@pytest.fixture
def make_open_checkout(make_asset):
    """An open check-out, with the asset's status kept consistent."""

    def _make(employee, asset=None, due_at=None, **kwargs):
        asset = asset or make_asset()
        asset.status = AssetStatus.CHECKED_OUT
        asset.save(update_fields=["status"])
        return CheckOut.objects.create(
            asset=asset,
            employee=employee,
            due_at=due_at or timezone.now() + timedelta(days=7),
            **kwargs,
        )

    return _make


@pytest.fixture
def days_ahead():
    def _days(n=7):
        return timezone.now() + timedelta(days=n)

    return _days
