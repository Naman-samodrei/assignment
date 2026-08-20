"""HTTP surface for the A3 endpoints.

These views parse, delegate and serialise. Every status code the brief
specifies for a check-out or a return is raised by :mod:`fieldassets.services`,
and the two query-constrained endpoints get their querysets from
:mod:`fieldassets.queries`, so there is no logic here to drift out of sync with
either.
"""

from django.db import DatabaseError, connection
from django.db.models import Prefetch
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Asset, CheckOut
from .queries import employee_summary, overdue_checkouts
from .serializers import (
    AssetDetailSerializer,
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutSerializer,
    EmployeeSummarySerializer,
    OverdueCheckOutSerializer,
    ReturnSerializer,
)
from .services import check_out_asset, return_checkout


class AssetListCreateView(generics.ListCreateAPIView):
    """GET /assets/`` and ``POST /assets/``.

    Filtering and search are the configured backends' job: ``?status=`` and
    ``?category=`` come from DjangoFilterBackend, ``?search=`` from
    SearchFilter over name and tag. Pagination is the project-wide 20 per page.
    """

    queryset = Asset.objects.all()
    serializer_class = AssetSerializer
    filterset_fields = ["status", "category"]
    search_fields = ["name", "asset_tag"]


class AssetDetailView(generics.RetrieveAPIView):
    """GET /assets/{id}/`` — includes ``current_holder``."""

    serializer_class = AssetDetailSerializer

    def get_queryset(self):
        # One extra query for the whole prefetch, not one per asset, and it
        # resolves the holding employee in the same trip.
        return Asset.objects.prefetch_related(
            Prefetch(
                "checkouts",
                queryset=CheckOut.objects.filter(
                    returned_at__isnull=True
                ).select_related("employee"),
                to_attr="open_checkouts",
            )
        )


class CheckOutCreateView(APIView):
    """``POST /checkouts/`` — applies rules 1-5, 7 and 8."""

    def post(self, request):
        payload = CheckOutCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        checkout = check_out_asset(**payload.validated_data)

        return Response(
            CheckOutSerializer(checkout).data, status=status.HTTP_201_CREATED
        )


class CheckOutReturnView(APIView):
    """``POST /checkouts/{id}/return/`` — applies rule 6."""

    def post(self, request, pk):
        payload = ReturnSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        checkout = return_checkout(checkout_id=pk, **payload.validated_data)

        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_200_OK)


class EmployeeSummaryView(generics.RetrieveAPIView):
    """GET /employees/{employee_code}/summary/`` — four database-computed numbers."""

    serializer_class = EmployeeSummarySerializer
    lookup_field = "employee_code"

    def get_queryset(self):
        return employee_summary(self.kwargs["employee_code"])


class OverdueReportView(generics.ListAPIView):
    """GET /reports/overdue/`` — most overdue first, 20 per page."""

    serializer_class = OverdueCheckOutSerializer
    filter_backends = []

    def get_queryset(self):
        return overdue_checkouts()


class HealthView(APIView):
    """GET /health/`` — the one unauthenticated endpoint."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except DatabaseError as exc:
            return Response(
                {"status": "error", "database": "unavailable", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"status": "ok", "database": "ok"})
