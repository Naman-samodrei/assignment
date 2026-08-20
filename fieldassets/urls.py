from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views

app_name = "fieldassets"

urlpatterns = [
    path("health/", views.HealthView.as_view(), name="health"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token-obtain"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("assets/", views.AssetListCreateView.as_view(), name="asset-list"),
    path("assets/<int:pk>/", views.AssetDetailView.as_view(), name="asset-detail"),
    path("checkouts/", views.CheckOutCreateView.as_view(), name="checkout-create"),
    path(
        "checkouts/<int:pk>/return/",
        views.CheckOutReturnView.as_view(),
        name="checkout-return",
    ),
    path(
        "employees/<str:employee_code>/summary/",
        views.EmployeeSummaryView.as_view(),
        name="employee-summary",
    ),
    path("reports/overdue/", views.OverdueReportView.as_view(), name="overdue-report"),
]
