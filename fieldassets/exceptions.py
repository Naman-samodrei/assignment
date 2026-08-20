"""Domain errors and their HTTP mapping.

Every business rule in A2 states an HTTP status code, so the rules raise these
exceptions and the views never have to build a status code by hand. A rule that
is not satisfied is a normal outcome, not a crash — nothing here reaches the
500 handler.
"""

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler


class Conflict(APIException):
    """The request is well formed but the current state forbids it (409)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"


class AssetNotAvailable(Conflict):
    default_detail = "Asset is not available for check-out."
    default_code = "asset_not_available"


class CheckOutLimitReached(Conflict):
    default_detail = "Employee already holds the maximum number of open check-outs."
    default_code = "checkout_limit_reached"


class AlreadyReturned(Conflict):
    default_detail = "This check-out has already been returned."
    default_code = "already_returned"


