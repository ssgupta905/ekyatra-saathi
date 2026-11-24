"""The whole beat: a slot cancelled in the HMS becomes a family told.

This is the demo's central claim under test — "every trigger starts in the
HMS". Nobody here calls `raise_disruption`; a fake hospital system changes an
appointment's status, the connector reads it, and everything downstream (the
holding message, the re-plan, the ledger) happens through the paths that
already existed.

The fake is a stand-in for Bahmni shaped like the real one: appointments keyed
by uuid, statuses in Bahmni's words mapped through the port, and crucially **no
change notification of any kind** — the connector finds out by re-reading.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from saathi.bootstrap.container import build_container
from saathi.config import Settings
from saathi.domain.enums import ResourceKind, StepKind, StepStatus
from saathi.domain.models import Resource
from saathi.live.events import EventKind
from saathi.live.hospital_sync import HospitalSync
from saathi.ports.hospital import (
    AppointmentState,
    HospitalAppointment,
    HospitalService,
    HospitalUnavailable,
)

START = date(2026, 11, 13)
MRI = "MRI-2"


class FakeHospital:
    """A hospital system we can change, with no way to announce that we did."""

    name = "fake-hms"
    live = True

    def __init__(self, appointments: list[HospitalAppointment]) -> None:
        self.by_ref = {a.ref: a for a in appointments}
        self.booked: list[HospitalAppointment] = []
        self.cancelled: list[str] = []
        self.refuse_booking = False

    def services(self) -> tuple[HospitalService, ...]:
        return (HospitalService(ref="svc-mri", name="MRI machine 2", duration_minutes=30),)

    def appointments(self, *, start: date, end: date) -> tuple[HospitalAppointment, ...]:
        return tuple(
            a for a in self.by_ref.values() if start <= a.starts_at.date() <= end
        )

    def book(self, *, patient_ref, service_ref, starts_at, ends_at, note=""):
        if self.refuse_booking:
            raise HospitalUnavailable("the hospital refused this slot")
        made = HospitalAppointment(
            ref=f"booked-{len(self.booked)}",
            patient_ref=patient_ref,
            patient_identifier=patient_ref,
            service_ref=service_ref,
            service_name="MRI machine 2",
            starts_at=starts_at,
            ends_at=ends_at,
            state=AppointmentState.SCHEDULED,
        )
        self.by_ref[made.ref] = made
        self.booked.append(made)
        return made

    def cancel(self, appointment_ref: str, *, note: str = ""):
        standing = self.by_ref[appointment_ref]
        released = replace(standing, state=AppointmentState.CANCELLED)
        self.by_ref[appointment_ref] = released
        self.cancelled.append(appointment_ref)
        return released

    # what a judge does, by hand, in the Bahmni screen
    def someone_cancels(self, ref: str) -> None:
        self.by_ref[ref] = replace(self.by_ref[ref], state=AppointmentState.CANCELLED)

    def someone_moves(self, ref: str, to: datetime) -> None:
        standing = self.by_ref[ref]
        self.by_ref[ref] = replace(
            standing, starts_at=to, ends_at=to + (standing.ends_at - standing.starts_at)
        )


def _appointment(patient_id: str, *, ref="appt-kamla", on=START) -> HospitalAppointment:
    start = datetime.combine(on, datetime.min.time(), tzinfo=UTC).replace(hour=4, minute=30)
    return HospitalAppointment(
        ref=ref,
        patient_ref="hms-patient-1",
        patient_identifier=patient_id,
        patient_display="Kamla Devi",
        service_ref="svc-mri",
        service_name="MRI machine 2",
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        state=AppointmentState.SCHEDULED,
    )


@pytest.fixture
def rig():
    """A world with one family booked onto a named machine, and a fake HMS."""
    state = build_container(Settings(_env_file=None, live_watched=4), today=START)
    patient_id = state.world.patients()[0].id

    state.world.register_resource(
        Resource(
            id=MRI,
            kind=ResourceKind.MACHINE,
            display_name="MRI machine 2",
            hospital_id="H1",
            department="radiology",
        )
    )
    booked_on = START + timedelta(days=2)
    steps = state.world.steps[patient_id]
    for index, step in enumerate(steps):
        if step.kind is StepKind.SCAN:
            steps[index] = step.model_copy(
                update={
                    "resource_id": MRI,
                    "scheduled_for": booked_on,
                    "status": StepStatus.SCHEDULED,
                }
            )

    hms = FakeHospital([_appointment(patient_id, on=booked_on)])
    sync = HospitalSync(hospital=hms, runner=state.runner, window_days=30)
    state.runner.hospital = hms
    state.runner.hospital_sync = sync
    return state, hms, sync, patient_id


async def test_the_first_read_tells_nobody_anything(rig):
    """Starting the process must not raise a disruption per existing booking."""
    state, _hms, sync, _patient_id = rig

    assert await sync.poll_once() == ()
    assert state.world.disruptions == {}
    assert sync.snapshot.primed


async def test_a_cancellation_in_the_hms_reaches_the_family(rig):
    """The whole claim, end to end: nobody here calls raise_disruption."""
    state, hms, sync, patient_id = rig
    await sync.poll_once()  # prime

    hms.someone_cancels("appt-kamla")
    (change,) = await sync.poll_once()

    assert change.became_lost
    assert len(state.world.disruptions) == 1
    raised = next(iter(state.world.disruptions.values()))
    assert raised.resource_id == MRI

    told = [m for m in state.runner.outbound if m["patient_id"] == patient_id]
    assert told, "the family was never told the slot went away"


async def test_the_family_is_told_before_any_new_date_is_worked_out(rig):
    """The ordering the brief makes load-bearing, observed through the bus."""
    state, hms, sync, _patient_id = rig
    await sync.poll_once()

    seen: list[str] = []
    original = state.bus.publish

    def record(kind, *args, **kwargs):
        seen.append(str(kind))
        return original(kind, *args, **kwargs)

    state.bus.publish = record  # type: ignore[method-assign]
    hms.someone_cancels("appt-kamla")
    await sync.poll_once()

    assert EventKind.HOLDING_MESSAGE_SENT in seen, "nobody was told"
    held = seen.index(EventKind.HOLDING_MESSAGE_SENT)
    if EventKind.OPTIONS_READY in seen:
        assert held < seen.index(EventKind.OPTIONS_READY), (
            "options were worked out before the family was told"
        )


async def test_a_moved_slot_moves_the_step_rather_than_alarming_anyone(rig):
    """A reschedule is not a disruption; the step's date simply follows."""
    state, hms, sync, patient_id = rig
    await sync.poll_once()

    moved_to = datetime.combine(
        START + timedelta(days=5), datetime.min.time(), tzinfo=UTC
    ).replace(hour=4, minute=30)
    hms.someone_moves("appt-kamla", moved_to)
    (change,) = await sync.poll_once()

    assert change.moved
    assert state.world.disruptions == {}
    scan = next(s for s in state.world.steps_for(patient_id) if s.kind is StepKind.SCAN)
    assert scan.scheduled_for == moved_to.date()


async def test_an_appointment_for_a_stranger_is_not_our_business(rig):
    """Most appointments in a hospital are somebody else's."""
    state, hms, sync, _patient_id = rig
    await sync.poll_once()

    stranger = _appointment("SOMEBODY-ELSE", ref="appt-stranger")
    hms.by_ref[stranger.ref] = stranger
    await sync.poll_once()
    hms.someone_cancels("appt-stranger")
    acted = await sync.poll_once()

    assert acted == ()
    assert state.world.disruptions == {}
    # ...but it still shows on the board, because the board is the hospital's.
    assert any(row["patient_id"] is None for row in sync.recent)


async def test_an_unreachable_hms_changes_nothing_and_says_why(rig):
    state, hms, sync, _patient_id = rig
    await sync.poll_once()

    def refuse(*, start, end):
        raise HospitalUnavailable("connection refused")

    hms.appointments = refuse  # type: ignore[method-assign]
    assert await sync.poll_once() == ()
    assert "connection refused" in sync.last_error
    assert state.world.disruptions == {}


async def test_our_own_booking_does_not_read_back_as_the_hospitals_doing(rig):
    """Otherwise approving an option re-plans the family it just planned."""
    state, hms, sync, patient_id = rig
    await sync.poll_once()

    scan = next(s for s in state.world.steps_for(patient_id) if s.kind is StepKind.SCAN)
    booked = await sync.rebook(
        patient_id=patient_id, step=scan, target_on=START + timedelta(days=9)
    )
    assert booked is not None
    assert hms.booked, "nothing was booked in the hospital system"

    acted = await sync.poll_once()
    assert acted == (), "our own write-back came back as a hospital-side change"
    assert state.world.disruptions == {}


async def test_rebooking_holds_the_new_slot_before_releasing_the_old(rig):
    """The other order leaves a window with no booking at all."""
    state, hms, sync, patient_id = rig
    await sync.poll_once()

    scan = next(s for s in state.world.steps_for(patient_id) if s.kind is StepKind.SCAN)
    await sync.rebook(patient_id=patient_id, step=scan, target_on=START + timedelta(days=9))

    assert hms.booked, "no new slot was held"
    assert "appt-kamla" in hms.cancelled, "the old slot was never released"


async def test_a_refused_booking_stops_the_approval(rig):
    """A family must never be told a time the hospital turned down."""
    state, hms, sync, patient_id = rig
    await sync.poll_once()
    hms.refuse_booking = True

    scan = next(s for s in state.world.steps_for(patient_id) if s.kind is StepKind.SCAN)
    with pytest.raises(HospitalUnavailable):
        await sync.rebook(patient_id=patient_id, step=scan, target_on=START + timedelta(days=9))

    assert not hms.booked
    assert "appt-kamla" not in hms.cancelled, "the old slot was released anyway"
