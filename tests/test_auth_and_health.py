"""Auth is required everywhere except the health check (A3)."""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status

pytestmark = pytest.mark.django_db

PROTECTED = [
    ("fieldassets:asset-list", []),
    ("fieldassets:checkout-create", []),
    ("fieldassets:overdue-report", []),
    ("fieldassets:employee-summary", ["EMP001"]),
    ("fieldassets:asset-detail", [1]),
    ("fieldassets:checkout-return", [1]),
]


class TestHealth:
    def test_health_needs_no_token(self, anon):
        response = anon.get(reverse("fieldassets:health"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"status": "ok", "database": "ok"}


class TestEverythingElseIsProtected:
    @pytest.mark.parametrize("name,args", PROTECTED)
    def test_without_a_token_it_is_401(self, anon, name, args):
        url = reverse(name, args=args)

        response = anon.get(url) if "return" not in name else anon.post(url, {})

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_a_garbage_token_is_401(self, anon):
        anon.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")

        response = anon.get(reverse("fieldassets:asset-list"))

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


class TestJWTLogin:
    def test_valid_credentials_return_both_tokens(self, anon):
        User.objects.create_user("demo", password="demo12345")

        response = anon.post(
            reverse("fieldassets:token-obtain"),
            {"username": "demo", "password": "demo12345"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert set(response.data) == {"access", "refresh"}

    def test_a_wrong_password_is_401(self, anon):
        User.objects.create_user("demo", password="demo12345")

        response = anon.post(
            reverse("fieldassets:token-obtain"),
            {"username": "demo", "password": "wrong"},
            format="json",
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_the_access_token_opens_a_protected_endpoint(self, anon):
        User.objects.create_user("demo", password="demo12345")
        token = anon.post(
            reverse("fieldassets:token-obtain"),
            {"username": "demo", "password": "demo12345"},
            format="json",
        ).data["access"]

        anon.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = anon.get(reverse("fieldassets:asset-list"))

        assert response.status_code == status.HTTP_200_OK

    def test_a_refresh_token_yields_a_new_access_token(self, anon):
        User.objects.create_user("demo", password="demo12345")
        refresh = anon.post(
            reverse("fieldassets:token-obtain"),
            {"username": "demo", "password": "demo12345"},
            format="json",
        ).data["refresh"]

        response = anon.post(
            reverse("fieldassets:token-refresh"), {"refresh": refresh}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert "access" in response.data
