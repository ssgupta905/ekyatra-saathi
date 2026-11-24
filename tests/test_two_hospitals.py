"""Two hospitals, two systems, and the ways they must not bleed into each other.

A journey that spans hospitals is the product's whole claim, and the moment a
second hospital is connected a set of assumptions that were harmless become
wrong. "Radiology" exists at every hospital in India; so does an MRI-1. These
pin the boundaries that keep one hospital's bad afternoon out of another
hospital's families.

The last test is the claim itself, stated as an assertion: neither system can
see the other, and only Saathi sees both.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from saathi.bootstrap.container import HospitalSystems, build_container, build_hospitals
from saathi.config import Settings
from saathi.domain.enums import (
    DisruptionKind,
    Language,
    ResourceKind,
    StepKind,
    StepStatus,
)
from saathi.domain.models import CareStep, Patient, Resource
from saathi.live.hospital_sync import HospitalLinks, HospitalSync
from saathi.ports.hospital import (
    AppointmentState,
    HospitalAppointment,
    HospitalService,
)

START = date(2026, 11, 13)
MUMBAI = "TMH-MUM"
GAYA = "GAYA-DH"


class FakeHospital:
    """One hospital's system. Knows nothing of any other."""

    live = True

    def __init__(self, name: str, appointments: list[HospitalAppointment]) -> None:
        self.name = name
        self.by_ref = {a.ref: a for a in appointments}
        self.asked = 0

    def services(self) -> tuple[HospitalService, ...]:
        return ()

    def appointments(self, *, start: date, end: date) -> tuple[HospitalAppointment, ...]:
        self.asked += 1
        return tuple(self.by_ref.values())

    def book(self, **kwargs):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def cancel(self, appointment_ref: str, *, note: str = ""):  # pragma: no cover
        raise NotImplementedError

    def someone_cancels(self, ref: str) -> None:
        self.by_ref[ref] = replace(self.by_ref[ref], state=AppointmentState.CANCELLED)


def _appointment(
    patient_id: str, *, ref: str, service: str, on: date
) -> HospitalAppointment:
    start = datetime.combine(on, datetime.min.time(), tzinfo=UTC).replace(hour=4, minute=30)
    return HospitalAppointment(
        ref=ref,
        patient_ref=f"hms-{patient_id}",
        patient_identifier=patient_id,
        patient_display=patient_id.title(),
        service_ref=service,
        service_name=service,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        state=AppointmentState.SCHEDULED,
    )


@pytest.fixture
def rig():
    """One family at each hospital, each hospital running its own "MRI-1"."""
    state = build_container(Settings(_env_file=None, live_watched=4), today=START)
    booked_on = START + timedelta(days=2)

    for hospital_id, patient_id, district in (
        (MUMBAI, "EKY-KAMLA", "Gaya"),
        (GAYA, "EKY-RAMESH", "Patna"),
    ):
        state.world.add_patient(
            Patient(
                id=patient_id,
                name=patient_id.title(),
                home_district=district,
                home_distance_km=900.0,
                language=Language.HINDI,
                caregiver_phone="",
                hospital_id=hospital_id,
                pathway_id="oncology",
            )
        )
        # Both hospitals run a machine called MRI-1, in a department called
        # radiology. This is the ordinary case, not a contrived one.
        state.world.register_resource(
            Resource(
                id=f"{hospital_id}-MRI-1",
                kind=ResourceKind.MACHINE,
                display_name="MRI-1",
                hospital_id=hospital_id,
                department="radiology",
            )
        )
        state.world.add_step(
            CareStep(
                id=f"{patient_id}-scan",
                patient_id=patient_id,
                kind=StepKind.SCAN,
                status=StepStatus.SCHEDULED,
                scheduled_for=booked_on,
                queue="radiology",
                resource_id=f"{hospital_id}-MRI-1",
            )
        )

    mumbai = FakeHospital(
        "bahmni", [_appointment("EKY-KAMLA", ref="m1", service="MRI-1", on=booked_on)]
    )
    gaya = FakeHospital(
        "fhir", [_appointment("EKY-RAMESH", ref="g1", service="MRI-1", on=booked_on)]
    )

    syncs = {
        MUMBAI: HospitalSync(hospital=mumbai, runner=state.runner, hospital_id=MUMBAI),
        GAYA: HospitalSync(hospital=gaya, runner=state.runner, hospital_id=GAYA),
    }
    state.runner.hospital_syncs = syncs
    state.runner.hospitals = HospitalSystems({MUMBAI: mumbai, GAYA: gaya})
    return state, mumbai, gaya, syncs


# ---------------- the registry ----------------


def test_each_hospital_gets_its_own_system() -> None:
    systems = build_hospitals(
        Settings(
            _env_file=None,
            hospital_systems=[
                f"{MUMBAI}=bahmni|https://hms.example.org|u|p",
                f"{GAYA}=fhir|https://fhir.example.org/fhir",
            ],
        )
    )
    assert systems(MUMBAI).name == "bahmni"
    assert systems(GAYA).name == "fhir"


def test_a_hospital_with_no_system_gets_the_desks_not_an_error() -> None:
    """"No system here" is an ordinary state, not a lookup failure."""
    systems = build_hospitals(Settings(_env_file=None, hospital_systems=[]))
    desks = systems("ANY-HOSPITAL")
    assert desks.name == "desks"
    assert desks.live is False


@pytest.mark.parametrize(
    "entry",
    [
        "=bahmni|https://x|u|p",          # no hospital id
        "H1=bahmni|https://x",            # bahmni with no credentials
        "H1=carrier-pigeon|https://x",    # a kind we do not have an adapter for
        "H1=fhir",                        # no address
    ],
)
def test_a_malformed_entry_is_refused_rather_than_guessed(entry: str) -> None:
    """A typo must not silently attach a real system to the wrong hospital."""
    assert build_hospitals(Settings(_env_file=None, hospital_systems=[entry])).all() == {}


# ---------------- the boundaries ----------------


def test_matching_is_scoped_to_the_connectors_own_hospital(rig) -> None:
    """Both hospitals run an "MRI-1"; each connector must find only its own."""
    state, _mumbai, _gaya, _syncs = rig
    appointment = _appointment("EKY-KAMLA", ref="m1", service="MRI-1", on=START)

    mumbai_links = HospitalLinks(world=state.world, hospital_id=MUMBAI)
    gaya_links = HospitalLinks(world=state.world, hospital_id=GAYA)

    assert mumbai_links.patient_for(appointment) == "EKY-KAMLA"
    assert gaya_links.patient_for(appointment) is None, "Gaya claimed a Mumbai family"

    mine = mumbai_links.resource_for(appointment)
    theirs = gaya_links.resource_for(appointment)
    assert mine is not None and mine.hospital_id == MUMBAI
    assert theirs is not None and theirs.hospital_id == GAYA
    assert mine.id != theirs.id, "two hospitals' machines resolved to one resource"


def test_an_outage_at_one_hospital_leaves_the_other_alone(rig) -> None:
    """The kind+department fallback is loose by design — but not across hospitals.

    Without the hospital check, a Mumbai scanner going down re-plans a family
    in Gaya who has never been to Mumbai, because both have a step queued at
    "radiology" needing a MACHINE.
    """
    state, _mumbai, _gaya, _syncs = rig

    disruption = state.world.raise_disruption(
        resource_id=f"{MUMBAI}-MRI-1",
        kind=DisruptionKind.MACHINE_DOWN,
        unavailable_from=START,
        unavailable_to=START + timedelta(days=10),
        note="MRI-1 out of service",
    )
    affected = state.world.affected_patients(disruption)

    assert "EKY-KAMLA" in affected
    assert "EKY-RAMESH" not in affected, "a Mumbai outage reached a Gaya family"


async def test_each_connector_primes_and_acts_independently(rig) -> None:
    """One hospital's first read must not count as the other's."""
    state, mumbai, gaya, syncs = rig

    assert await syncs[MUMBAI].poll_once() == ()
    assert syncs[MUMBAI].snapshot.primed
    assert not syncs[GAYA].snapshot.primed, "Gaya was primed by Mumbai's read"

    assert await syncs[GAYA].poll_once() == ()

    mumbai.someone_cancels("m1")
    (change,) = await syncs[MUMBAI].poll_once()
    assert change.became_lost
    assert await syncs[GAYA].poll_once() == (), "Gaya reacted to a Mumbai cancellation"

    raised = list(state.world.disruptions.values())
    assert len(raised) == 1
    assert raised[0].resource_id == f"{MUMBAI}-MRI-1"


async def test_neither_system_learns_anything_about_the_other(rig) -> None:
    """The product's claim, as an assertion.

    Mumbai is cancelled and Gaya is read; Gaya's system is never asked about
    the Mumbai family, and Mumbai's is never asked about Gaya's. The only
    place both appear is here.
    """
    state, mumbai, gaya, syncs = rig
    for sync in syncs.values():
        await sync.poll_once()

    mumbai.someone_cancels("m1")
    await syncs[MUMBAI].poll_once()
    await syncs[GAYA].poll_once()

    assert set(mumbai.by_ref) == {"m1"}, "Mumbai's system was told about Gaya"
    assert set(gaya.by_ref) == {"g1"}, "Gaya's system was told about Mumbai"

    # ...and yet one place holds both hospitals' boards, each row labelled
    # with where it happened. That juxtaposition is the entire product.
    assert all(row["hospital_id"] == MUMBAI for row in syncs[MUMBAI].recent)
    assert all(row["hospital_id"] == GAYA for row in syncs[GAYA].recent)
    assert any(row["kind"] == "slot_lost" for row in syncs[MUMBAI].recent)
