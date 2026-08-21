"""A6: populate a fresh database with a dataset that exercises every rule.

This is a deliverable, not a convenience: it is what gets run before the API is
exercised, so it also creates the account you obtain a JWT with and prints the
credentials.

"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from fieldassets.models import (
    Asset,
    AssetCategory,
    AssetStatus,
    CheckOut,
    Employee,
    OverdueNotice,
)

DEFAULT_USERNAME = "demo"
DEFAULT_PASSWORD = "demo12345"

# (tag, name, category) — status is derived from the check-outs below, so the
# two can never contradict each other.
ASSETS = [
    ("CAM-001", "Canon EOS R6", AssetCategory.CAMERA),
    ("CAM-002", "Sony A7 III", AssetCategory.CAMERA),
    ("LAP-001", "ThinkPad X1 Carbon", AssetCategory.LAPTOP),
    ("LAP-002", "MacBook Pro 14", AssetCategory.LAPTOP),
    ("LAP-003", "Dell Latitude 5440", AssetCategory.LAPTOP),
    ("SEN-001", "Soil moisture probe", AssetCategory.SENSOR),
    ("SEN-002", "Air quality sensor", AssetCategory.SENSOR),
    ("SEN-003", "Vibration logger", AssetCategory.SENSOR),
    ("VEH-001", "Mahindra Bolero pickup", AssetCategory.VEHICLE),
    ("VEH-002", "Tata Ace", AssetCategory.VEHICLE),
]

# (code, full name, is_active)
EMPLOYEES = [
    ("EMP001", "Asha Iyer", True),
    ("EMP002", "Rohan Deshpande", True),
    ("EMP003", "Meera Nair", True),
    ("EMP004", "Vikram Rao", False),
    ("EMP005", "Priya Chandran", True),
]

# Assets that are neither held nor freshly returned, but out of service.
MAINTENANCE_ASSETS = ["SEN-003"]

# Open check-outs: (employee, asset, days until due). Negative == overdue.
OPEN_CHECKOUTS = [
    ("EMP001", "CAM-001", 5),
    ("EMP001", "LAP-001", 10),
    ("EMP001", "LAP-002", 2),      # EMP001 now holds three -> rule 3 is live
    ("EMP002", "SEN-001", -9),     # overdue
    ("EMP002", "VEH-001", -3),     # overdue
]

# Returned check-outs: (employee, asset, held days, due days after checkout,
# returned days after checkout). returned > due == returned late.
RETURNED_CHECKOUTS = [
    ("EMP003", "CAM-002", 20, 14, 11),   # on time
    ("EMP003", "LAP-003", 30, 21, 18),   # on time
    ("EMP003", "VEH-002", 40, 7, 12),    # LATE
]


class Command(BaseCommand):
    help = "Populate the database with a demo dataset that exercises every rule."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=DEFAULT_USERNAME)
        parser.add_argument("--password", default=DEFAULT_PASSWORD)

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()

        user = self._api_user(options["username"], options["password"])
        assets = self._assets()
        employees = self._employees()
        self._reset_seeded_checkouts(employees)
        checkouts = self._checkouts(assets, employees, now)
        self._asset_statuses(assets, checkouts)

        self._report(options, now)

    # -- pieces ------------------------------------------------------------

    def _api_user(self, username, password):
        """The account a reviewer obtains a JWT with. Also usable in /admin/."""
        User = get_user_model()
        user, _ = User.objects.update_or_create(
            username=username,
            defaults={"is_staff": True, "is_superuser": True},
        )
        user.set_password(password)
        user.save(update_fields=["password"])
        return user

    def _assets(self):
        assets = {}
        for tag, name, category in ASSETS:
            assets[tag], _ = Asset.objects.update_or_create(
                asset_tag=tag,
                defaults={
                    "name": name,
                    "category": category,
                    "purchase_date": date(2024, 1, 15),
                },
            )
        return assets

    def _employees(self):
        employees = {}
        for code, full_name, is_active in EMPLOYEES:
            employees[code], _ = Employee.objects.update_or_create(
                employee_code=code,
                defaults={
                    "full_name": full_name,
                    "email": f"{code.lower()}@example.com",
                    "is_active": is_active,
                },
            )
        return employees

    def _reset_seeded_checkouts(self, employees):
        """Clear only what this command owns, so a re-run converges.

        Notices cascade from their check-out. Check-outs belonging to employees
        this seed did not create are left alone.
        """
        seeded = CheckOut.objects.filter(employee__in=employees.values())
        OverdueNotice.objects.filter(checkout__in=seeded).delete()
        seeded.delete()

    def _checkouts(self, assets, employees, now):
        created = []

        for code, tag, due_in_days in OPEN_CHECKOUTS:
            checkout = CheckOut.objects.create(
                asset=assets[tag],
                employee=employees[code],
                due_at=now + timedelta(days=due_in_days),
                condition_note="",
            )
            # checked_out_at is auto_now_add, so backdate it after the fact to
            # make hold durations realistic.
            self._backdate(checkout, now - timedelta(days=abs(due_in_days) + 5))
            created.append(checkout)

        for code, tag, held_ago, due_after, returned_after in RETURNED_CHECKOUTS:
            checked_out_at = now - timedelta(days=held_ago)
            checkout = CheckOut.objects.create(
                asset=assets[tag],
                employee=employees[code],
                due_at=checked_out_at + timedelta(days=due_after),
                returned_at=checked_out_at + timedelta(days=returned_after),
                condition_note="Returned in working order.",
            )
            self._backdate(checkout, checked_out_at)
            created.append(checkout)

        return created

    @staticmethod
    def _backdate(checkout, when):
        CheckOut.objects.filter(pk=checkout.pk).update(checked_out_at=when)
        checkout.checked_out_at = when

    def _asset_statuses(self, assets, checkouts):
        """Derive every asset's status from the check-outs, never by hand."""
        held = {c.asset_id for c in checkouts if c.returned_at is None}

        for tag, asset in assets.items():
            if asset.pk in held:
                asset.status = AssetStatus.CHECKED_OUT
            elif tag in MAINTENANCE_ASSETS:
                asset.status = AssetStatus.MAINTENANCE
            else:
                asset.status = AssetStatus.AVAILABLE
            asset.save(update_fields=["status", "updated_at"])

    # -- output ------------------------------------------------------------

    def _report(self, options, now):
        ok = self.style.SUCCESS
        w = self.stdout.write

        open_qs = CheckOut.objects.filter(returned_at__isnull=True)
        returned_qs = CheckOut.objects.filter(returned_at__isnull=False)
        overdue = open_qs.filter(due_at__lte=now).count()
        late = sum(1 for c in returned_qs if c.returned_at > c.due_at)

        w("")
        w(ok("Seeded."))
        w("")
        w(f"  assets              {Asset.objects.count()}  "
          f"across {Asset.objects.values('category').distinct().count()} categories")
        w(f"  employees           {Employee.objects.count()}  "
          f"({Employee.objects.filter(is_active=False).count()} inactive)")
        w(f"  open check-outs     {open_qs.count()}  ({overdue} currently overdue)")
        w(f"  returned check-outs {returned_qs.count()}  "
          f"({returned_qs.count() - late} on time, {late} late)")
        w("")
        w("  Authenticate with:")
        w(ok(f"    username: {options['username']}"))
        w(ok(f"    password: {options['password']}"))
        w("")
        w("  curl -s -X POST localhost:8000/api/v1/auth/token/ \\")
        w("    -H 'Content-Type: application/json' \\")
        w(f"    -d '{{\"username\":\"{options['username']}\","
          f"\"password\":\"{options['password']}\"}}'")
        w("")
        w("  Walk the rules from this data:")
        w("    CAM-001   already checked out          -> 409   rule 1")
        w("    SEN-003   in maintenance               -> 409   rule 1")
        w("    EMP004    inactive                     -> 400   rule 2")
        w("    EMP001    already holds three          -> 409   rule 3")
        w("    NOPE-999  unknown asset_tag            -> 404   rule 8")
        w("    SEN-002 / CAM-002 / LAP-003 / VEH-002 are available to check out.")
        w("")
