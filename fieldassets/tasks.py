"""A4: the hourly overdue sweep.

``flag_overdue_checkouts`` must be safe to run repeatedly — five runs in one day
must leave one notice per check-out, not five. That is guaranteed twice over:

* the queryset excludes check-outs that already carry today's notice, so a
  repeat run normally does no work at all; and
* ``UNIQUE(checkout, notice_date)`` on OverdueNotice rejects the duplicate
  anyway, which covers the residual race where two workers pass that check at
  the same moment. ``ignore_conflicts=True`` turns that rejection into a
  no-op instead of an exception.

The first layer keeps the common case cheap; only the second makes it *true*.
"""

from celery import shared_task
from django.utils import timezone

from .models import CheckOut, OverdueNotice
from .queries import open_and_overdue

#: Rows per INSERT. Keeps memory flat whether there are ten overdue check-outs
#: or a hundred thousand.
BATCH_SIZE = 500


@shared_task(name="fieldassets.tasks.flag_overdue_checkouts")
def flag_overdue_checkouts(now=None, batch_size=BATCH_SIZE):
    """Create today's OverdueNotice for every open, overdue check-out.

    Returns a small summary dict so a repeat run is visibly a no-op:
    ``{"notice_date": ..., "examined": 0, "created": 0}``.

    ``created`` is a diagnostic, not a guarantee. It is measured as a before/
    after count outside any transaction, so if two workers ever sweep the same
    day concurrently they will each report the full number while the database
    still holds exactly one notice per check-out. The hourly Beat schedule does
    not do that; the row count in the database is the authoritative answer
    either way.
    """
    now = timezone.now() if now is None else now
    notice_date = timezone.localdate(now)

    pending = (
        CheckOut.objects.filter(open_and_overdue(now))
        .exclude(notices__notice_date=notice_date)
        .order_by("pk")
        .values_list("pk", flat=True)
    )

    before = OverdueNotice.objects.filter(notice_date=notice_date).count()
    examined = 0
    batch = []

    # .iterator() streams instead of loading every overdue row into memory.
    for checkout_id in pending.iterator(chunk_size=batch_size):
        examined += 1
        batch.append(OverdueNotice(checkout_id=checkout_id, notice_date=notice_date))
        if len(batch) >= batch_size:
            OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []

    if batch:
        OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)

    after = OverdueNotice.objects.filter(notice_date=notice_date).count()

    return {
        "notice_date": notice_date.isoformat(),
        "examined": examined,
        "created": after - before,
    }
