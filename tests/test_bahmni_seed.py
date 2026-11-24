"""The demo seed, which every bug so far has hidden in.

Five things broke while filming the two-hospital run, and the whole suite was
green through all of them, because the seed is the one module nothing else
exercises. Each test below is one of those failures, written so it cannot
come back:

1. A family planned towards a step that is not on their record.
2. A plan target whose prerequisite chain is incomplete, so the planner
   answers "plan could not be determined" — correctly, and uselessly.
3. No turnaround history, so every report window is `None` and every family
   is UNCERTAIN.
4. Dates computed from "today" on each side, so seeding the two halves on
   different days silently drifted them apart and a real cancellation landed
   on a day no step sat on.
5. A cancelled appointment counting as "already held", so re-seeding after a
   take did nothing at all.

The Bahmni half stays mocked: these are about what we write into our own
records, which is the half that was wrong every time. Nothing here touches a
network.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from saathi.adapters.records.memory import MemoryRecordStore
from saathi.bootstrap.bahmni_seed import (
    APPOINTMENTS,
    PATIENTS,
    PREFIX,
    SERVICES,
    Seeder,
    seed_local_cast,
)
from saathi.domain.enums import StepKind, StepStatus
from saathi.domain.pathway import get_pathway
from saathi.readiness.conformal import ConformalReadinessPredictor, TurnaroundSample

TODAY = date(2026, 11, 24)
HOSPITAL = "TMH-MUM"
DISTRICT = "GAYA-DH"


def _seeded(booked: dict | None = None) -> MemoryRecordStore:
    store = MemoryRecordStore()
    seed_local_cast(
        store, TODAY, hospital_id=HOSPITAL, district_id=DISTRICT, booked=booked or {}
    )
    return store


def _steps_of(store: MemoryRecordStore, patient_id: str) -> dict[StepKind, object]:
    return {s.kind: s for s in store.load().steps.get(patient_id, ())}


# ---------------- the record has to be coherent ----------------


def test_every_family_is_planned_towards_a_step_they_actually_have() -> None:
    """Failure 1: the planner escalated with "no review_consult step on record"."""
    records = _seeded().load()
    seeded = [p for p in records.patients if p.id.startswith(PREFIX)]
    assert seeded, "the seed wrote no families"

    for patient in seeded:
        target = records.plan_targets.get(patient.id)
        assert target is not None, f"{patient.id} has no plan target"
        kinds = {s.kind for s in records.steps.get(patient.id, ())}
        assert target in kinds, f"{patient.id} is planned towards {target} and has no such step"


def test_every_plan_targets_prerequisites_are_on_the_record() -> None:
    """Failure 2: a target whose chain is incomplete can never be decided.

    Walks the pathway's edges backwards from each family's target. Every
    ancestor must exist as a step, or the planner has a dependency it can
    never satisfy and answers UNCERTAIN for ever.
    """
    records = _seeded().load()

    for patient in records.patients:
        if not patient.id.startswith(PREFIX):
            continue
        target = records.plan_targets[patient.id]
        pathway = get_pathway(patient.pathway_id)
        present = {s.kind for s in records.steps.get(patient.id, ())}

        needed: set[StepKind] = set()
        frontier = [target]
        while frontier:
            child = frontier.pop()
            for parent, kid in pathway.edges:
                if kid is child and parent not in needed:
                    needed.add(parent)
                    frontier.append(parent)

        missing = needed - present
        assert not missing, (
            f"{patient.id} is planned towards {target.value} but is missing "
            f"{sorted(m.value for m in missing)}"
        )


def test_the_forecast_has_something_to_have_learned_from() -> None:
    """Failure 3: with no calibration set every report window is None."""
    turnaround = _seeded().load().turnaround
    assert turnaround, "the seed left the conformal predictor with no history"

    predictor = ConformalReadinessPredictor(alpha=0.1)
    predictor.fit([TurnaroundSample(queue=o.queue, days=o.days) for o in turnaround])

    # The queues the seeded families actually wait on must be calibrated —
    # history for some other queue would pass a weaker assertion and change
    # nothing about whether a family gets a date.
    records = _seeded().load()
    report_queues = {
        s.queue
        for rows in records.steps.values()
        for s in rows
        if s.kind in (StepKind.RADIOLOGY_REPORT, StepKind.PATHOLOGY_REPORT) and s.queue
    }
    assert report_queues, "no report steps were seeded"
    for queue in report_queues:
        assert predictor.interval_days(queue) is not None, f"{queue} is not calibrated"


def test_a_completed_step_always_carries_the_date_it_completed() -> None:
    """`step_problem` rejects a completed step with no date, silently."""
    for rows in _seeded().load().steps.values():
        for step in rows:
            if step.status is StepStatus.COMPLETED:
                assert step.completed_on is not None, f"{step.id} completed on no date"
            if step.status is StepStatus.SCHEDULED:
                assert step.scheduled_for is not None, f"{step.id} scheduled for no date"


# ---------------- the two halves must agree ----------------


def test_the_local_cast_uses_the_dates_bahmni_actually_holds() -> None:
    """Failure 4: both halves computed from "today" drift if seeded apart."""
    far = TODAY + timedelta(days=9)
    store = _seeded({(f"{PREFIX}KAMLA", "MRI machine 1"): far})

    scan = _steps_of(store, f"{PREFIX}KAMLA")[StepKind.SCAN]
    assert scan.scheduled_for == far, "the local cast ignored what Bahmni holds"


def test_re_seeding_re_aligns_a_cast_that_has_drifted() -> None:
    """The drift is repaired rather than left, and the repair is reported."""
    store = _seeded()
    before = _steps_of(store, f"{PREFIX}KAMLA")[StepKind.SCAN].scheduled_for
    moved = TODAY + timedelta(days=11)
    assert before != moved

    changed = seed_local_cast(
        store,
        TODAY,
        hospital_id=HOSPITAL,
        district_id=DISTRICT,
        booked={(f"{PREFIX}KAMLA", "MRI machine 1"): moved},
    )

    assert changed, "a drifted cast reported nothing to do"
    assert _steps_of(store, f"{PREFIX}KAMLA")[StepKind.SCAN].scheduled_for == moved


def test_re_seeding_an_aligned_cast_changes_nothing() -> None:
    """Idempotent, which is what makes it usable as a reset between takes."""
    held = {(f"{PREFIX}KAMLA", "MRI machine 1"): TODAY + timedelta(days=3)}
    store = _seeded(held)
    assert not seed_local_cast(
        store, TODAY, hospital_id=HOSPITAL, district_id=DISTRICT, booked=held
    )


def test_the_journey_spans_both_hospitals() -> None:
    """The whole point: her bloods and her scan are at different hospitals."""
    store = _seeded()
    resources = {r.id: r for r in store.load().resources}
    steps = _steps_of(store, f"{PREFIX}KAMLA")

    bloods_at = resources[steps[StepKind.BLOODS].resource_id].hospital_id
    scan_at = resources[steps[StepKind.SCAN].resource_id].hospital_id

    assert bloods_at == DISTRICT
    assert scan_at == HOSPITAL
    assert bloods_at != scan_at, "the two-hospital story collapsed to one hospital"


# ---------------- the halves must name the same things ----------------


def test_every_booking_names_a_service_the_seed_creates() -> None:
    """The join key to Bahmni. A typo here unlinks a machine silently."""
    known = {service.name for service in SERVICES}
    for booking in APPOINTMENTS:
        assert booking.service_name in known, f"{booking.service_name!r} is never created"


def test_every_booking_names_a_patient_the_seed_registers() -> None:
    known = {patient.identifier for patient in PATIENTS}
    for booking in APPOINTMENTS:
        assert booking.patient_identifier in known


def test_every_seeded_resource_is_named_after_a_bahmni_service() -> None:
    """`HospitalLinks.resource_for` matches on display name, so they must agree."""
    services = {service.name for service in SERVICES}
    bookable = {
        r.display_name
        for r in _seeded().load().resources
        if r.hospital_id == HOSPITAL
    }
    unmatched = bookable - services
    assert not unmatched, f"{sorted(unmatched)} exist here and are bookable in no Bahmni service"


# ---------------- a cancelled slot is not a held one ----------------


class _FakeClient:
    """Answers the two calls `ensure_appointments` makes, and records posts."""

    def __init__(self, standing: list[dict]) -> None:
        self.standing = standing
        self.posted: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        return _Response([] if "appointmentService" in path else {"results": []})

    def post(self, path, json=None):
        if path.endswith("/appointments/search"):
            return _Response(self.standing)
        self.posted.append((path, json or {}))
        return _Response({"uuid": f"made-{len(self.posted)}"})


class _Response:
    def __init__(self, body) -> None:
        self._body = body
        self.content = b"x"

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._body


def _booked(client: _FakeClient, booking, day: date) -> bool:
    """Whether *this* patient's *this* service on *this* day was created."""
    return any(
        path.endswith("/openmrs/ws/rest/v1/appointments")
        and payload.get("patientUuid") == f"pat-{booking.patient_identifier}"
        and payload.get("serviceUuid") == f"svc-{booking.service_name}"
        and str(payload.get("startDateTime", "")).startswith(day.isoformat())
        for path, payload in client.posted
    )


def _row(status: str, *, at: date, identifier: str, service: str) -> dict:
    from datetime import UTC, datetime, time

    starts = datetime.combine(at, time(10, 0), tzinfo=UTC)
    return {
        "uuid": f"{identifier}-{status}",
        "status": status,
        "voided": False,
        "patient": {"identifier": identifier},
        "service": {"name": service},
        "startDateTime": int(starts.timestamp() * 1000),
    }


@pytest.mark.parametrize("status", ["Cancelled", "Missed"])
def test_a_slot_that_went_away_is_booked_again(status: str) -> None:
    """Failure 5: the reset silently did nothing after a take."""
    booking = APPOINTMENTS[0]
    day = TODAY + timedelta(days=booking.days_from_today)
    client = _FakeClient(
        [
            _row(
                status,
                at=day,
                identifier=booking.patient_identifier,
                service=booking.service_name,
            )
        ]
    )
    seeder = Seeder(client, write=True, timezone="Asia/Kolkata")
    seeder.ensure_appointments(
        {s.name: f"svc-{s.name}" for s in SERVICES},
        {p.identifier: f"pat-{p.identifier}" for p in PATIENTS},
        TODAY,
    )

    # Specifically *this* booking, not merely that something was booked. The
    # other three families are never in `standing` and so always post, which
    # made an earlier version of this assertion pass with the bug present.
    assert _booked(client, booking, day), (
        f"a {status.lower()} slot was treated as still held; the reset did nothing"
    )


def test_a_standing_slot_is_not_booked_twice() -> None:
    booking = APPOINTMENTS[0]
    day = TODAY + timedelta(days=booking.days_from_today)
    client = _FakeClient(
        [
            _row(
                "Scheduled",
                at=day,
                identifier=booking.patient_identifier,
                service=booking.service_name,
            )
        ]
    )
    seeder = Seeder(client, write=True, timezone="Asia/Kolkata")
    seeder.ensure_appointments(
        {s.name: f"svc-{s.name}" for s in SERVICES},
        {p.identifier: f"pat-{p.identifier}" for p in PATIENTS},
        TODAY,
    )

    assert not _booked(client, booking, day), (
        "an appointment that already stands was booked a second time"
    )
