"""The seam to a real hospital system.

These pin the behaviours that are easy to get wrong and expensive to get wrong
on stage: that the first read of an HMS is not news, that our own write-back
does not echo back as a hospital-side change, that an unrecognised status is
never mistaken for a bookable one, and that a booking failure stops the
approval instead of telling a family a time the hospital refused.

The Bahmni payloads below are trimmed copies of what a running Bahmni Standard
instance actually returns — epoch-millisecond times, `Cancelled` with two Ls,
a `service` object with the name nested inside it, and no `dateChanged` field
anywhere, which is the whole reason the connector diffs snapshots at all.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from saathi.adapters.hospital import BahmniHospital, DeskHospital
from saathi.ports.hospital import (
    AppointmentSnapshot,
    AppointmentState,
    HospitalAppointment,
    HospitalUnavailable,
)

# 2026-11-27 10:00 UTC, as Bahmni sends it.
TEN_AM = int(datetime(2026, 11, 27, 10, 0, tzinfo=UTC).timestamp() * 1000)
HALF_HOUR = 30 * 60 * 1000


def _row(
    uuid: str = "appt-1",
    *,
    status: str = "Scheduled",
    start: int = TEN_AM,
    identifier: str = "EKY-KAMLA",
    service: str = "MRI machine 1",
    voided: bool = False,
) -> dict:
    return {
        "uuid": uuid,
        "appointmentNumber": "0001",
        "dateCreated": 1795000000000,
        "patient": {
            "identifier": identifier,
            "name": "Kamla Devi",
            "uuid": "patient-uuid-1",
            "gender": "F",
        },
        "service": {
            "name": service,
            "uuid": "service-uuid-1",
            "durationMins": 30,
            "speciality": {"name": "Radiology"},
        },
        "startDateTime": start,
        "endDateTime": start + HALF_HOUR,
        "appointmentKind": "Scheduled",
        "status": status,
        "comments": "",
        "voided": voided,
    }


def _bahmni(handler) -> BahmniHospital:
    transport = httpx.MockTransport(handler)
    return BahmniHospital(
        base_url="https://hms.example.org",
        username="u",
        password="p",
        client=httpx.Client(transport=transport),
    )


# ---------------- projection ----------------


def test_reads_bahmnis_own_shapes() -> None:
    """Epoch millis, a nested service, and the identifier we match families on."""
    hms = _bahmni(lambda request: httpx.Response(200, json=[_row()]))
    (appointment,) = hms.appointments(start=date(2026, 11, 1), end=date(2026, 11, 30))

    assert appointment.ref == "appt-1"
    assert appointment.starts_at == datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
    assert appointment.ends_at == datetime(2026, 11, 27, 10, 30, tzinfo=UTC)
    assert appointment.service_name == "MRI machine 1"
    assert appointment.patient_identifier == "EKY-KAMLA"
    assert appointment.state is AppointmentState.SCHEDULED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Scheduled", AppointmentState.SCHEDULED),
        ("CheckedIn", AppointmentState.CHECKED_IN),
        ("Cancelled", AppointmentState.CANCELLED),
        ("Missed", AppointmentState.MISSED),
        ("Completed", AppointmentState.COMPLETED),
    ],
)
def test_maps_bahmnis_status_words(raw: str, expected: AppointmentState) -> None:
    hms = _bahmni(lambda request: httpx.Response(200, json=[_row(status=raw)]))
    (appointment,) = hms.appointments(start=date(2026, 11, 1), end=date(2026, 11, 30))
    assert appointment.state is expected


def test_an_unknown_status_is_never_bookable() -> None:
    """The one mapping mistake that would matter: reading a strange status as open."""
    hms = _bahmni(lambda request: httpx.Response(200, json=[_row(status="Rescheduled-ish")]))
    (appointment,) = hms.appointments(start=date(2026, 11, 1), end=date(2026, 11, 30))

    assert appointment.state is AppointmentState.UNKNOWN
    assert not appointment.state.is_open
    assert not appointment.state.is_lost


def test_voided_and_unreadable_rows_are_dropped() -> None:
    rows = [_row("ok"), _row("voided", voided=True), {"uuid": "no-times"}]
    hms = _bahmni(lambda request: httpx.Response(200, json=rows))
    found = hms.appointments(start=date(2026, 11, 1), end=date(2026, 11, 30))
    assert [a.ref for a in found] == ["ok"]


def test_the_window_includes_its_last_day() -> None:
    """An end date sent as midnight silently drops everything booked that day."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=[])

    _bahmni(handler).appointments(start=date(2026, 11, 1), end=date(2026, 11, 30))
    assert seen["startDate"].startswith("2026-11-01T00:00:00")
    assert seen["endDate"].startswith("2026-11-30T23:59:59")


def test_a_failure_says_what_bahmni_said() -> None:
    hms = _bahmni(lambda request: httpx.Response(500, text="appointment service exploded"))
    with pytest.raises(HospitalUnavailable, match="appointment service exploded"):
        hms.appointments(start=date(2026, 11, 1), end=date(2026, 11, 30))


# ---------------- change detection ----------------


def _appointment(ref="a1", *, state=AppointmentState.SCHEDULED, hour=10) -> HospitalAppointment:
    start = datetime(2026, 11, 27, hour, 0, tzinfo=UTC)
    return HospitalAppointment(
        ref=ref,
        patient_ref="p1",
        patient_identifier="EKY-KAMLA",
        patient_display="Kamla Devi",
        service_ref="s1",
        service_name="MRI machine 1",
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        state=state,
    )


def test_the_first_read_is_not_news() -> None:
    """Otherwise starting the process raises a disruption for every patient."""
    snapshot = AppointmentSnapshot()
    assert snapshot.diff((_appointment(),)) == ()
    assert snapshot.primed


def test_a_cancellation_is_the_signal() -> None:
    snapshot = AppointmentSnapshot()
    snapshot.diff((_appointment(),))

    (change,) = snapshot.diff((_appointment(state=AppointmentState.CANCELLED),))
    assert change.became_lost
    assert not change.is_new
    assert "was cancelled" in change.describe()


def test_a_slot_that_moves_is_a_move_not_a_loss() -> None:
    snapshot = AppointmentSnapshot()
    snapshot.diff((_appointment(hour=10),))

    (change,) = snapshot.diff((_appointment(hour=16),))
    assert change.moved
    assert not change.became_lost
    assert "moved from" in change.describe()


def test_a_retyped_comment_is_not_a_change() -> None:
    """A desk editing a note must not put a holding message on four phones."""
    snapshot = AppointmentSnapshot()
    first = _appointment()
    snapshot.diff((first,))

    retyped = replace(first, note="rebooked by phone", patient_display="K. Devi")
    assert snapshot.diff((retyped,)) == ()


def test_an_already_cancelled_slot_is_lost_only_once() -> None:
    snapshot = AppointmentSnapshot()
    snapshot.diff((_appointment(),))
    cancelled = _appointment(state=AppointmentState.CANCELLED)

    assert len(snapshot.diff((cancelled,))) == 1
    assert snapshot.diff((cancelled,)) == ()


# ---------------- writes ----------------


def test_booking_refuses_to_double_book() -> None:
    """Approvals get retried; a real patient told to come twice does not."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/appointments/search"):
            return httpx.Response(200, json=[_row("standing")])
        return httpx.Response(200, json=_row("freshly-made"))

    booked = _bahmni(handler).book(
        patient_ref="patient-uuid-1",
        service_ref="service-uuid-1",
        starts_at=datetime(2026, 11, 27, 10, 0, tzinfo=UTC),
        ends_at=datetime(2026, 11, 27, 10, 30, tzinfo=UTC),
    )

    assert booked.ref == "standing"
    assert not any(c == "POST /openmrs/ws/rest/v1/appointments" for c in calls)


def test_booking_proceeds_when_nothing_is_standing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/appointments/search"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_row("freshly-made", start=TEN_AM))

    booked = _bahmni(handler).book(
        patient_ref="patient-uuid-1",
        service_ref="service-uuid-1",
        starts_at=datetime(2026, 11, 27, 10, 0, tzinfo=UTC),
        ends_at=datetime(2026, 11, 27, 10, 30, tzinfo=UTC),
    )
    assert booked.ref == "freshly-made"


def test_cancelling_something_already_cancelled_is_a_success() -> None:
    hms = _bahmni(lambda request: httpx.Response(200, json=_row(status="Cancelled")))
    assert hms.cancel("appt-1").state is AppointmentState.CANCELLED


def test_a_cancel_that_did_not_take_is_an_error() -> None:
    """Reading it back is the only way to know Bahmni did what it was asked."""
    state = {"status": "Scheduled"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_row(status=state["status"]))

    with pytest.raises(HospitalUnavailable, match="it still reads scheduled"):
        _bahmni(handler).cancel("appt-1")


# ---------------- no HMS at all ----------------


def test_without_an_hms_the_desks_are_the_feed() -> None:
    """Unconfigured is the pilot's starting point, not a broken state."""
    desks = DeskHospital()
    assert desks.live is False
    assert desks.appointments(start=date(2026, 11, 1), end=date(2026, 11, 30)) == ()
    assert desks.services() == ()


def test_without_an_hms_booking_refuses_rather_than_pretending() -> None:
    with pytest.raises(HospitalUnavailable, match="by hand"):
        DeskHospital().cancel("anything")
