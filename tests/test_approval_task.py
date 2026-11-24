"""The approval request, written into the hospital's own record as a FHIR Task.

A change proposed to a hospital should exist in that hospital's system —
queryable and auditable by them, without asking us. So every option set files
a `Task`, and a person's decision closes it.

Two things these pin, because both are easy to get backwards.

**The task is a proposal, not an order.** `intent: proposal`, `status:
requested`. Saathi does not have the authority to order a change in somebody
else's hospital, and a resource that said `order` would be claiming it.

**Filing it can never block the decision.** A hospital that refuses the Task
still gets the suggestion and the desk still decides. An audit convenience
that could stop a family being re-planned would be worse than no audit trail.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from saathi.bootstrap.container import HospitalSystems, build_container
from saathi.config import Settings
from saathi.domain.enums import DisruptionKind, ResourceKind, Role, StepKind, StepStatus
from saathi.domain.models import Resource
from saathi.ports.fhir import (
    TASK_CODE,
    appointment_to_fhir,
    approval_task,
    completed_task,
)
from saathi.ports.hospital import AppointmentState, HospitalAppointment, HospitalUnavailable

START = date(2026, 11, 13)
NOW = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)


# ---------------- the projection ----------------


def test_an_appointment_projects_to_a_fhir_appointment() -> None:
    projected = appointment_to_fhir(
        HospitalAppointment(
            ref="a1",
            patient_ref="p1",
            patient_identifier="EKY-KAMLA",
            patient_display="Kamla Devi",
            service_ref="s1",
            service_name="MRI machine 1",
            starts_at=datetime(2026, 11, 25, 4, 30, tzinfo=UTC),
            ends_at=datetime(2026, 11, 25, 5, 0, tzinfo=UTC),
            state=AppointmentState.SCHEDULED,
        )
    )
    assert projected["resourceType"] == "Appointment"
    assert projected["status"] == "booked"
    assert projected["start"] == "2026-11-25T04:30:00.000Z"
    assert projected["participant"][0]["actor"]["reference"] == "Patient/p1"


def test_a_status_we_could_not_read_never_projects_as_booked() -> None:
    """The one mapping mistake that would travel to another system as a lie."""
    projected = appointment_to_fhir(
        HospitalAppointment(
            ref="a1",
            patient_ref="p1",
            service_ref="s1",
            starts_at=NOW,
            ends_at=NOW,
            state=AppointmentState.UNKNOWN,
        )
    )
    assert projected["status"] == "entered-in-error"


def test_the_task_is_a_proposal_not_an_order() -> None:
    task = approval_task(
        patient_ref="p1",
        patient_display="Kamla Devi",
        summary="Re-plan after MRI machine 1 became unavailable",
        description="Options offered:\n1. on Fri 27 Nov — +1 nights, +0 trips",
        requested_at=NOW,
        options=2,
    )
    # A string, because servers constrain which value[x] an input may use.
    assert task["input"][0]["valueString"] == "2"
    assert task["resourceType"] == "Task"
    assert task["status"] == "requested"
    assert task["intent"] == "proposal", "Saathi does not order changes in another hospital"
    # Exactly one coding, from FHIR's own vocabulary: OpenEMR refuses more
    # than one outright, and a hospital's tooling understands the standard
    # code. Who proposed it lives in `code.text`, which no server can refuse.
    assert [c["code"] for c in task["code"]["coding"]] == [TASK_CODE]
    assert "Saathi" in task["code"]["text"]
    assert task["for"]["reference"] == "Patient/p1"


def test_the_task_claims_no_requester_it_does_not_have() -> None:
    """Naming a hospital clinician would put their name on our proposal.

    FHIR wants `requester` to reference a Practitioner, Organization or
    Patient. Saathi is none of them, and inventing one to satisfy the schema
    writes a false line into somebody else's record.
    """
    task = approval_task(
        patient_ref="p1", patient_display="K", summary="s", description="d", requested_at=NOW
    )
    assert "requester" not in task
    # ...but a caller that genuinely has one may say so.
    named = approval_task(
        patient_ref="p1",
        patient_display="K",
        summary="s",
        description="d",
        requested_at=NOW,
        requester_ref="Organization/abc",
    )
    assert named["requester"] == {"reference": "Organization/abc"}


def test_the_task_carries_cost_to_the_family_and_nothing_clinical() -> None:
    task = approval_task(
        patient_ref="p1",
        patient_display="Kamla Devi",
        summary="Re-plan",
        description="Options offered:\n1. on Fri 27 Nov — +1 nights, +0 trips",
        requested_at=NOW,
    )
    body = repr(task).lower()
    assert "nights" in body
    for forbidden in ("diagnosis", "cancer", "creatinine", "result", "reasoncode"):
        assert forbidden not in body, f"{forbidden!r} reached another hospital's record"


def test_a_doctor_gated_replan_is_marked_urgent() -> None:
    routine = approval_task(
        patient_ref="p1", patient_display="K", summary="s", description="d", requested_at=NOW
    )
    urgent = approval_task(
        patient_ref="p1",
        patient_display="K",
        summary="s",
        description="d",
        requested_at=NOW,
        requires_doctor=True,
    )
    assert routine["priority"] == "routine"
    assert urgent["priority"] == "urgent"


def test_closing_records_the_decision_without_mutating_what_was_read() -> None:
    task = approval_task(
        patient_ref="p1", patient_display="K", summary="s", description="d", requested_at=NOW
    )
    closed = completed_task(task, outcome="approved", by="Dr Arani", at=NOW)

    assert closed["status"] == "completed"
    assert task["status"] == "requested", "the task we read was edited in place"
    assert {o["valueString"] for o in closed["output"]} == {"approved", "Dr Arani"}


def test_a_refusal_closes_as_cancelled_not_failed() -> None:
    """A person deciding against a proposal is not the task going wrong."""
    task = approval_task(
        patient_ref="p1", patient_display="K", summary="s", description="d", requested_at=NOW
    )
    closed = completed_task(task, outcome="rejected", by="Seema Kamble", at=NOW)
    assert closed["status"] == "cancelled"


# ---------------- filed and closed for real ----------------


class TaskHospital:
    """A hospital system that records the tasks filed into it."""

    name = "fake"
    live = True

    def __init__(self, *, refuses: bool = False) -> None:
        self.tasks: dict[str, dict] = {}
        self.refuses = refuses
        self._next = 0

    def services(self):
        return ()

    def appointments(self, *, start, end):
        return ()

    def book(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def cancel(self, appointment_ref, *, note=""):  # pragma: no cover
        raise NotImplementedError

    def create_task(self, task: dict) -> str:
        if self.refuses:
            raise HospitalUnavailable("this hospital does not accept tasks")
        self._next += 1
        ref = f"task-{self._next}"
        self.tasks[ref] = task
        return ref

    def read_task(self, ref: str) -> dict | None:
        return self.tasks.get(ref)

    def update_task(self, ref: str, task: dict) -> None:
        self.tasks[ref] = task


@pytest.fixture
def rig():
    state = build_container(Settings(_env_file=None, live_watched=4), today=START)
    patient_id = state.world.patients()[0].id
    hospital_id = state.world.patient(patient_id).hospital_id

    steps = state.world.steps[patient_id]
    for index, step in enumerate(steps):
        if step.kind is StepKind.SCAN:
            steps[index] = step.model_copy(
                update={
                    "resource_id": "MRI-2",
                    "scheduled_for": START + timedelta(days=1),
                    "status": StepStatus.SCHEDULED,
                }
            )
    state.world.register_resource(
        Resource(
            id="MRI-2",
            kind=ResourceKind.MACHINE,
            display_name="MRI machine 2",
            hospital_id=hospital_id,
            department="radiology",
        )
    )
    return state, patient_id, hospital_id


async def _raise(state):
    await state.runner.raise_disruption(
        resource_id="MRI-2",
        kind=DisruptionKind.MACHINE_DOWN,
        unavailable_from=START,
        unavailable_to=START + timedelta(days=10),
        note="MRI-2 out of service",
    )


async def test_a_proposal_is_filed_in_the_hospitals_own_system(rig) -> None:
    state, patient_id, hospital_id = rig
    hms = TaskHospital()
    state.runner.hospitals = HospitalSystems({hospital_id: hms})

    await _raise(state)
    if patient_id not in state.runner.options:
        pytest.skip("this cohort produced no human choice to approve")

    assert hms.tasks, "nothing was filed in the hospital's system"
    # One per affected family, not one in total — an outage hits everyone
    # booked onto that machine and each of them is a separate request.
    ref, _ = state.runner.approval_tasks[patient_id]
    task = hms.tasks[ref]
    assert task["intent"] == "proposal"
    assert task["status"] == "requested"
    assert len(hms.tasks) == len(state.runner.options)


async def test_approving_closes_the_task_with_who_decided(rig) -> None:
    state, patient_id, hospital_id = rig
    hms = TaskHospital()
    state.runner.hospitals = HospitalSystems({hospital_id: hms})

    await _raise(state)
    if patient_id not in state.runner.options:
        pytest.skip("this cohort produced no human choice to approve")

    ref, _ = state.runner.approval_tasks[patient_id]
    await state.runner.approve_option(
        patient_id=patient_id,
        option_index=0,
        approved_by_role=Role.NAVIGATOR,
        approved_by="Seema Kamble",
    )
    task = hms.tasks[ref]
    assert task["status"] == "completed"
    assert "Seema Kamble" in repr(task["output"])


async def test_rejecting_closes_the_task_as_cancelled(rig) -> None:
    state, patient_id, hospital_id = rig
    hms = TaskHospital()
    state.runner.hospitals = HospitalSystems({hospital_id: hms})

    await _raise(state)
    if patient_id not in state.runner.options:
        pytest.skip("this cohort produced no human choice to approve")

    ref, _ = state.runner.approval_tasks[patient_id]
    await state.runner.reject_options(
        patient_id=patient_id,
        rejected_by_role=Role.NAVIGATOR,
        rejected_by="Seema Kamble",
        reason="calling them now",
    )
    assert hms.tasks[ref]["status"] == "cancelled"
    # Everybody else's request is untouched: one person decided for one family.
    others = [t for r, t in hms.tasks.items() if r != ref]
    assert all(t["status"] == "requested" for t in others)


async def test_a_hospital_that_refuses_the_task_still_gets_the_replan(rig) -> None:
    """An audit convenience must never be able to stop a family being re-planned."""
    state, patient_id, hospital_id = rig
    hms = TaskHospital(refuses=True)
    state.runner.hospitals = HospitalSystems({hospital_id: hms})

    await _raise(state)

    assert not hms.tasks
    # The options still exist and the family was still told.
    assert state.world.disruptions, "the disruption itself was lost"
    assert [m for m in state.runner.outbound if m["patient_id"] == patient_id], (
        "a refused task stopped the family being told"
    )
