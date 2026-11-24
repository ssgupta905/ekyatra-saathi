"""Turning every option down, which is also a decision.

A desk sometimes knows something the option table does not — the family is
already at the station, the consultant will see them anyway. Without a reject
verb that knowledge has nowhere to go and the only way to clear the queue is to
approve an option nobody believes in, which makes the approval record a lie.

So rejecting is deliberately narrow and deliberately honest: it records who
decided and why, it clears the queue, and it promises the family nothing. What
it must never do is look like an approval — no write-back, no message, no new
date.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from saathi.bootstrap.container import build_container
from saathi.config import Settings
from saathi.domain.enums import DisruptionKind, ResourceKind, Role, StepKind, StepStatus
from saathi.domain.models import Resource
from saathi.live.events import EventKind

START = date(2026, 11, 13)


@pytest.fixture
def state():
    return build_container(Settings(_env_file=None, live_watched=4), today=START)


async def _options_waiting(state, patient_id: str):
    """Put one family in front of a human with a real choice to make."""
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
            hospital_id="H1",
            department="radiology",
        )
    )
    await state.runner.raise_disruption(
        resource_id="MRI-2",
        kind=DisruptionKind.MACHINE_DOWN,
        unavailable_from=START,
        unavailable_to=START + timedelta(days=10),
        note="MRI-2 out of service",
    )
    return state.runner.options.get(patient_id)


async def test_rejecting_clears_the_queue_and_names_who_decided(state):
    patient_id = state.world.patients()[0].id
    assert await _options_waiting(state, patient_id), "fixture produced no choice"

    outcome = await state.runner.reject_options(
        patient_id=patient_id,
        rejected_by_role=Role.NAVIGATOR,
        rejected_by="Seema Kamble",
        reason="Already at the station; calling them now",
    )

    assert patient_id not in state.runner.options
    assert patient_id not in state.world.pending_approval
    assert outcome["rejected_by"] == "Seema Kamble"

    entry = state.ledger.get(outcome["ledger_id"])
    assert "turned down" in entry.claim
    assert entry.metadata["reason"] == "Already at the station; calling them now"


async def test_rejecting_tells_the_family_nothing(state):
    """The one way this must not resemble an approval."""
    patient_id = state.world.patients()[0].id
    assert await _options_waiting(state, patient_id)
    before = len([m for m in state.runner.outbound if m["patient_id"] == patient_id])

    await state.runner.reject_options(
        patient_id=patient_id, rejected_by_role=Role.NAVIGATOR, rejected_by="Seema Kamble"
    )

    after = len([m for m in state.runner.outbound if m["patient_id"] == patient_id])
    assert after == before, "rejecting sent the family a message"
    assert not state.runner.approvals, "rejecting recorded an approval"


async def test_rejecting_is_published_as_its_own_thing(state):
    """An auditor must be able to tell a refusal from an approval that never came."""
    patient_id = state.world.patients()[0].id
    assert await _options_waiting(state, patient_id)

    seen: list[str] = []
    original = state.bus.publish
    state.bus.publish = lambda kind, *a, **k: (seen.append(str(kind)), original(kind, *a, **k))[1]  # type: ignore[assignment]

    await state.runner.reject_options(
        patient_id=patient_id, rejected_by_role=Role.DOCTOR, rejected_by="Dr Amrita Sinha"
    )

    assert EventKind.OPTIONS_REJECTED in seen
    assert EventKind.APPROVAL_RECORDED not in seen


async def test_rejecting_nothing_is_an_error_not_a_shrug(state):
    """Two desks acting at once must not both believe they decided."""
    patient_id = state.world.patients()[0].id
    with pytest.raises(LookupError):
        await state.runner.reject_options(
            patient_id=patient_id, rejected_by_role=Role.NAVIGATOR, rejected_by="Seema Kamble"
        )


async def test_the_endpoint_takes_the_signed_in_persons_name(state):
    """Same rule as approving: the body may not claim to be somebody else."""
    from fastapi.testclient import TestClient
    from saathi.api.deps import get_container
    from saathi.main import create_app

    patient_id = state.world.patients()[0].id
    option_set = await _options_waiting(state, patient_id)
    assert option_set is not None

    app = create_app()
    app.dependency_overrides[get_container] = lambda: state
    with TestClient(app) as client:
        response = client.post(
            f"/api/disruptions/{option_set.disruption_id}/reject",
            json={
                "patient_id": patient_id,
                "rejected_by_role": "navigator",
                "rejected_by": "Seema Kamble",
                "reason": "handling by phone",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["patient_id"] == patient_id
    assert patient_id not in state.runner.options
