from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from cogdoc.ha.scheduler import (
    CronExpression,
    DistributedScheduler,
    SCHEDULE_CRON,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONCE,
    ScheduleStore,
)
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.tasks import LeaseJobStore


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def control(tmp_path):
    clock = Clock()
    backend = SQLiteBackend(tmp_path / "ha.db")
    jobs = LeaseJobStore(backend, clock=clock)
    schedules = ScheduleStore(backend, clock=clock)
    yield schedules, jobs, clock
    backend.close()


def test_once_schedule_survives_crash_between_advance_and_enqueue(control):
    schedules, jobs, clock = control
    schedule = schedules.create(
        "tenant-a",
        "sync",
        {"connection": "c1"},
        schedule_type=SCHEDULE_ONCE,
        schedule_spec=str(clock.value),
    )
    assert schedules.materialize_due() == 1
    assert not schedules.get(schedule["schedule_id"])["enabled"]
    # Simulated process loss: a new scheduler only sees the durable fire.
    assert schedules.dispatch_pending(jobs) == 1
    assert schedules.dispatch_pending(jobs) == 0
    queued = jobs.list_jobs(queue="sync")
    assert len(queued) == 1
    assert queued[0]["payload"] == {"connection": "c1"}


def test_interval_coalesces_missed_ticks_without_burst(control):
    schedules, jobs, clock = control
    schedule = schedules.create(
        "tenant-a",
        "sync",
        {},
        schedule_type=SCHEDULE_INTERVAL,
        schedule_spec="10",
        first_run_at=clock.value,
    )
    clock.value += 95
    assert schedules.materialize_due() == 1
    current = schedules.get(schedule["schedule_id"])
    assert current is not None and current["next_run_at"] == 1100.0
    schedules.dispatch_pending(jobs)
    assert len(jobs.list_jobs()) == 1


def test_multiple_scheduler_instances_create_one_fire_and_one_job(control):
    schedules, jobs, clock = control
    schedules.create(
        "tenant-a",
        "sync",
        {},
        schedule_type=SCHEDULE_INTERVAL,
        schedule_spec="60",
        first_run_at=clock.value,
    )
    schedulers = [DistributedScheduler(schedules, jobs) for _ in range(8)]
    barrier = threading.Barrier(8)

    def run(scheduler):
        barrier.wait()
        scheduler.run_once()

    threads = [
        threading.Thread(target=run, args=(scheduler,)) for scheduler in schedulers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(jobs.list_jobs()) == 1
    assert schedules.pending_fires() == []


def test_scheduler_uses_one_thread_regardless_of_schedule_count(control):
    schedules, jobs, clock = control
    for index in range(100):
        schedules.create(
            "tenant-a",
            "sync",
            {"n": index},
            schedule_type=SCHEDULE_INTERVAL,
            schedule_spec="60",
            first_run_at=clock.value + 60,
        )
    scheduler = DistributedScheduler(schedules, jobs, poll_seconds=60)
    scheduler.start()
    try:
        assert scheduler._thread is not None and scheduler._thread.is_alive()
        assert (
            sum(
                thread.name == "cogdoc-ha-scheduler" for thread in threading.enumerate()
            )
            == 1
        )
    finally:
        assert scheduler.stop()


def test_cron_timezone_names_steps_and_day_or_semantics():
    cron = CronExpression("*/15 9-10 1 JAN MON", "Asia/Shanghai")
    cursor = datetime(2027, 1, 3, 23, 59, tzinfo=timezone.utc).timestamp()
    next_run = datetime.fromtimestamp(cron.next_after(cursor), timezone.utc)
    # Monday Jan 4, 09:00 Asia/Shanghai.
    assert next_run == datetime(2027, 1, 4, 1, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "expression",
    ["* * * *", "60 * * * *", "*/0 * * * *", "* * 31 FEB *", "* * * * BAD"],
)
def test_invalid_or_impossible_cron_is_rejected(expression):
    cron = CronExpression(expression) if expression == "* * 31 FEB *" else None
    if cron is not None:
        with pytest.raises(ValueError, match="no occurrence"):
            cron.next_after(1_900_000_000)
    else:
        with pytest.raises(ValueError):
            CronExpression(expression)


def test_cron_schedule_materializes(control):
    schedules, jobs, clock = control
    clock.value = datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    schedule = schedules.create(
        "tenant-a",
        "audit",
        {},
        schedule_type=SCHEDULE_CRON,
        schedule_spec="5 * * * *",
    )
    assert schedule["next_run_at"] == clock.value + 300
    clock.value += 300
    assert DistributedScheduler(schedules, jobs).run_once() == (1, 1)


def test_pending_fire_recovers_after_its_completed_job_was_compacted(control):
    schedules, jobs, clock = control
    schedules.create(
        "tenant-a",
        "sync",
        {"connection": "c1"},
        schedule_type=SCHEDULE_ONCE,
        schedule_spec=str(clock.value),
    )
    assert schedules.materialize_due() == 1
    fire = schedules.pending_fires()[0]
    job = jobs.enqueue(
        "sync",
        "tenant-a",
        {"connection": "c1"},
        idempotency_key=f"schedule:{fire['fire_id']}",
    )
    lease = jobs.claim("sync", "worker")
    assert lease is not None
    jobs.complete(job["job_id"], lease["lease_token"], {})
    clock.value += 100
    assert jobs.prune_terminal(before=clock.value - 1) == 1

    # Simulates the enqueue-committed/fire-update-missing crash window after
    # the original job has already aged out of the hot queue table.
    assert schedules.dispatch_pending(jobs) == 1
    assert schedules.pending_fires() == []
