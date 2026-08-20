from django.db import models
from django.db.models import Q


class AssetCategory(models.TextChoices):
    CAMERA = "CAMERA", "Camera"
    LAPTOP = "LAPTOP", "Laptop"
    SENSOR = "SENSOR", "Sensor"
    VEHICLE = "VEHICLE", "Vehicle"


class AssetStatus(models.TextChoices):
    AVAILABLE = "AVAILABLE", "Available"
    CHECKED_OUT = "CHECKED_OUT", "Checked out"
    MAINTENANCE = "MAINTENANCE", "Maintenance"


class Asset(models.Model):
    asset_tag = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=16, choices=AssetCategory.choices)
    status = models.CharField(
        max_length=16, choices=AssetStatus.choices, default=AssetStatus.AVAILABLE
    )
    purchase_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["asset_tag"]

    def __str__(self) -> str:
        return f"{self.asset_tag} ({self.name})"


class Employee(models.Model):
    employee_code = models.CharField(max_length=16, unique=True, db_index=True)
    full_name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.employee_code} ({self.full_name})"




class CheckOut(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="checkouts")
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name="checkouts"
    )
    checked_out_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()
    returned_at = models.DateTimeField(null=True, blank=True)
    condition_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-checked_out_at"]
        constraints = [
            # Rule 7. The database — not the application — is what guarantees a
            # single open check-out per asset. Two concurrent requests that both
            # get past the status read still leave exactly one row here; the
            # loser raises IntegrityError, which the service turns into a 409.
            models.UniqueConstraint(
                fields=["asset"],
                condition=Q(returned_at__isnull=True),
                name="uniq_open_checkout_per_asset",
            ),
        ]
        indexes = [
            # Rule 3 counts an employee's open check-outs on every request.
            models.Index(
                fields=["employee", "returned_at"], name="checkout_emp_open_idx"
            ),
            # Overdue lookups scan open rows by due date.
            models.Index(
                fields=["returned_at", "due_at"], name="checkout_open_due_idx"
            ),
        ]

    @property
    def is_open(self) -> bool:
        return self.returned_at is None

    def __str__(self) -> str:
        return f"{self.asset_id} -> {self.employee_id}"


class OverdueNotice(models.Model):
    checkout = models.ForeignKey(
        CheckOut, on_delete=models.CASCADE, related_name="notices"
    )
    notice_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-notice_date"]

    def __str__(self) -> str:
        return f"notice {self.checkout_id} @ {self.notice_date}"
