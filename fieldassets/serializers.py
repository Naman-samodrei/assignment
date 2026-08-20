"""Request/response shapes for the A3 endpoints."""

from rest_framework import serializers

from .models import Asset, CheckOut, Employee
from .services import validate_due_at


class AssetSerializer(serializers.ModelSerializer):
    """Create and list payload for assets."""

    class Meta:
        model = Asset
        fields = [
            "id",
            "asset_tag",
            "name",
            "category",
            "status",
            "purchase_date",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class CurrentHolderSerializer(serializers.Serializer):
    """The holding employee's code and name, on asset retrieve."""

    employee_code = serializers.CharField(read_only=True)
    full_name = serializers.CharField(read_only=True)


class AssetDetailSerializer(AssetSerializer):
    """Asset retrieve: adds ``current_holder``, null when the asset is free."""

    current_holder = serializers.SerializerMethodField()

    class Meta(AssetSerializer.Meta):
        fields = AssetSerializer.Meta.fields + ["current_holder"]

    def get_current_holder(self, asset):
        # ``open_checkouts`` is prefetched by the view, so this costs no query.
        # Rule 7 guarantees at most one open check-out per asset.
        open_checkouts = getattr(asset, "open_checkouts", None)
        if not open_checkouts:
            return None
        return CurrentHolderSerializer(open_checkouts[0].employee).data


class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ["id", "employee_code", "full_name", "email", "is_active"]
        read_only_fields = fields


class CheckOutSerializer(serializers.ModelSerializer):
    """What a check-out looks like on the way out."""

    asset = AssetSerializer(read_only=True)
    employee = EmployeeSerializer(read_only=True)

    class Meta:
        model = CheckOut
        fields = [
            "id",
            "asset",
            "employee",
            "checked_out_at",
            "due_at",
            "returned_at",
            "condition_note",
        ]
        read_only_fields = fields


class CheckOutCreateSerializer(serializers.Serializer):
    """``POST /checkouts/`` body.

    Assets and employees are addressed by their business keys, not their primary
    keys, so callers never have to look an id up first. Whether those keys
    resolve is the service's problem (rule 8, a 404) — this serializer only
    reports malformed input, which is a 400.
    """

    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()

    def validate_due_at(self, value):
        # Rule 4. Also enforced in the service, so a non-HTTP caller cannot
        # sidestep it; here it buys a field-scoped error message.
        return validate_due_at(value)


class ReturnSerializer(serializers.Serializer):
    """``POST /checkouts/{id}/return/`` body. Rule 6."""

    condition_note = serializers.CharField(required=False, allow_blank=True)
    needs_maintenance = serializers.BooleanField(required=False, default=False)


class EmployeeSummarySerializer(serializers.Serializer):
    """The four numbers, read off the annotations in ``queries.employee_summary``."""

    employee_code = serializers.CharField(read_only=True)
    full_name = serializers.CharField(read_only=True)
    lifetime_checkouts = serializers.IntegerField(read_only=True)
    currently_held = serializers.IntegerField(read_only=True)
    currently_overdue = serializers.IntegerField(read_only=True)
    mean_hold_days = serializers.SerializerMethodField()

    def get_mean_hold_days(self, employee):
        # AVG over no returned check-outs is SQL NULL; the endpoint contract is
        # four numbers, so that reads as 0.0 rather than null.
        mean_hold = employee.mean_hold
        if mean_hold is None:
            return 0.0
        return round(mean_hold.total_seconds() / 86400, 2)


class OverdueCheckOutSerializer(serializers.Serializer):
    """One row of ``GET /reports/overdue/``.

    Every field comes from the check-out itself or from a ``select_related``
    join, so serialising a page costs no additional queries.
    """

    id = serializers.IntegerField(read_only=True)
    asset_tag = serializers.CharField(source="asset.asset_tag", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    employee_code = serializers.CharField(source="employee.employee_code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    due_at = serializers.DateTimeField(read_only=True)
    days_overdue = serializers.SerializerMethodField()

    def get_days_overdue(self, checkout):
        return round(checkout.overdue_by.total_seconds() / 86400, 2)
