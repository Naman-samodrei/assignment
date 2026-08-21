"""A5: the background task's idempotency — run it twice, assert one notice."""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.db.models import Count
from django.utils import timezone

from fieldassets.models import OverdueNotice
from fieldassets.tasks import flag_overdue_checkouts


def duplicate_notice_count():
    return (
        OverdueNotice.objects.values("checkout", "notice_date")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .count()
    )


@pytest.mark.django_db
class TestIdempotency:
    def test_running_it_twice_creates_one_notice(
        self, make_employee, make_open_checkout
    ):
        """The A5 requirement, stated as plainly as it is written."""
        checkout = make_open_checkout(
            make_employee(), due_at=timezone.now() - timedelta(days=2)
        )

        flag_overdue_checkouts()
        flag_overdue_checkouts()

        assert OverdueNotice.objects.filter(checkout=checkout).count() == 1

    def test_running_it_five_times_creates_one_notice_each(
        self, make_employee, make_open_checkout
    ):
        now = timezone.now()
        employee = make_employee()
        for i in range(3):
            make_open_checkout(employee, due_at=now - timedelta(days=i + 1))

        for _ in range(5):
            flag_overdue_checkouts()

        assert OverdueNotice.objects.count() == 3
        assert duplicate_notice_count() == 0

    def test_a_repeat_run_reports_doing_nothing(
        self, make_employee, make_open_checkout
    ):
        make_open_checkout(make_employee(), due_at=timezone.now() - timedelta(days=1))

        first = flag_overdue_checkouts()
        second = flag_overdue_checkouts()

        assert first["examined"] == 1
        assert first["created"] == 1
        assert second["examined"] == 0
        assert second["created"] == 0


@pytest.mark.django_db
class TestWhatGetsFlagged:
    def test_only_open_overdue_checkouts_are_flagged(
        self, make_employee, make_open_checkout
    ):
        now = timezone.now()
        employee = make_employee()
        overdue = make_open_checkout(employee, due_at=now - timedelta(days=1))
        make_open_checkout(employee, due_at=now + timedelta(days=5))  # not due
        returned = make_open_checkout(employee, due_at=now - timedelta(days=3))
        returned.returned_at = now
        returned.save(update_fields=["returned_at"])

        flag_overdue_checkouts()

        assert list(OverdueNotice.objects.values_list("checkout_id", flat=True)) == [
            overdue.pk
        ]

    def test_an_item_due_exactly_now_is_flagged(
        self, make_employee, make_open_checkout
    ):
        """Same boundary as the report and the summary."""
        now = timezone.now()
        make_open_checkout(make_employee(), due_at=now)

        flag_overdue_checkouts(now=now)

        assert OverdueNotice.objects.count() == 1

    def test_the_notice_is_dated_today(self, make_employee, make_open_checkout):
        make_open_checkout(make_employee(), due_at=timezone.now() - timedelta(days=1))

        flag_overdue_checkouts()

        assert OverdueNotice.objects.get().notice_date == timezone.localdate()

    def test_yesterdays_notice_does_not_stop_todays(
        self, make_employee, make_open_checkout
    ):
        """Idempotency is per day, not forever."""
        checkout = make_open_checkout(
            make_employee(), due_at=timezone.now() - timedelta(days=4)
        )
        OverdueNotice.objects.create(
            checkout=checkout, notice_date=timezone.localdate() - timedelta(days=1)
        )

        flag_overdue_checkouts()

        assert OverdueNotice.objects.filter(checkout=checkout).count() == 2
        assert OverdueNotice.objects.filter(
            checkout=checkout, notice_date=timezone.localdate()
        ).exists()


@pytest.mark.django_db
class TestBatching:
    def test_it_flags_more_rows_than_one_batch(
        self, make_employee, make_open_checkout
    ):
        """Streaming and batching must not drop or duplicate rows."""
        now = timezone.now()
        employee = make_employee()
        for i in range(25):
            make_open_checkout(employee, due_at=now - timedelta(days=i + 1))

        result = flag_overdue_checkouts(batch_size=10)

        assert result["created"] == 25
        assert OverdueNotice.objects.count() == 25
        assert duplicate_notice_count() == 0

        flag_overdue_checkouts(batch_size=10)
        assert OverdueNotice.objects.count() == 25


@pytest.mark.django_db(transaction=True)
class TestTheGuaranteeUnderRacing:
    """The index, not the filter, is what makes idempotency true."""

    def test_four_workers_racing_one_sweep_still_leave_one_notice_each(
        self, make_employee, make_open_checkout
    ):
        now = timezone.now()
        employee = make_employee()
        for i in range(5):
            make_open_checkout(employee, due_at=now - timedelta(days=i + 1))

        results = [None] * 4
        barrier = threading.Barrier(4)

        def run(i):
            try:
                barrier.wait()
                results[i] = flag_overdue_checkouts()
            except Exception as exc:  # noqa: BLE001
                results[i] = exc
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert all(isinstance(r, dict) for r in results), results
        assert OverdueNotice.objects.count() == 5
        assert duplicate_notice_count() == 0
