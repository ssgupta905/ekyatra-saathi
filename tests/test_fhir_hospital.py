"""The FHIR adapter: the same port, a completely different protocol.

Where `test_hospital_connector.py` pins Bahmni's quirks — epoch milliseconds,
`Cancelled` with two Ls, no change feed — these pin FHIR's: ISO instants with
real offsets, lower-case status codes, and a patient who is a *reference*
rather than an inline identifier.

The payloads are trimmed copies of what a running HAPI FHIR R4 server actually
returns. The test that matters most is the last one in each group: that a
status this code has never seen is never read as bookable, and that the two
adapters produce the same normalised shape from completely different JSON.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from saathi.adapters.hospital import FhirHospital
from saathi.ports.hospital import AppointmentState, HospitalUnavailable


def _appointment_resource(
    rid: str = "15610",
    *,
    status: str = "booked",
    start: str = "2026-12-03T10:30:00-05:00",
    end: str = "2026-12-03T10:50:00-05:00",
    patient: str = "Patient/15579",
) -> dict:
    return {
        "resourceType": "Appointment",
        "id": rid,
        "meta": {"versionId": "18", "lastUpdated": "2026-10-02T13:47:00.989-04:00"},
        "status": status,
        "serviceType": [{"coding": [{"display": "MRI machine 1"}]}],
        "description": "Contrast MRI",
        "start": start,
        "end": end,
        "minutesDuration": 20,
        "participant": [
            {"actor": {"reference": patient, "display": "Kamla Devi"}, "status": "accepted"},
            {
                "actor": {"reference": "Practitioner/15577", "display": "Dr Owen Hartley"},
                "status": "accepted",
            },
        ],
    }


def _patient_resource(rid: str = "15579", identifier: str = "EKY-KAMLA") -> dict:
    return {
        "resourceType": "Patient",
        "id": rid,
        "identifier": [
            {"system": "https://example.org/fhir/mrn", "value": identifier},
        ],
        "name": [{"given": ["Kamla"], "family": "Devi"}],
    }


def _bundle(*resources: dict) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }


def _fhir(handler) -> FhirHospital:
    return FhirHospital(
        base_url="https://fhir.example.org/fhir",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# ---------------- projection ----------------


def test_reads_fhirs_own_shapes() -> None:
    """ISO instants with offsets, and a patient who is a reference."""
    hms = _fhir(
        lambda r: httpx.Response(
            200, json=_bundle(_appointment_resource(), _patient_resource())
        )
    )
    (appointment,) = hms.appointments(start=date(2026, 12, 1), end=date(2026, 12, 31))

    assert appointment.ref == "15610"
    # 10:30 at -05:00 is 15:30 UTC. Truncating before converting would land
    # an evening appointment on the previous day.
    assert appointment.starts_at == datetime(2026, 12, 3, 15, 30, tzinfo=UTC)
    assert appointment.service_name == "MRI machine 1"
    assert appointment.state is AppointmentState.SCHEDULED
    assert appointment.provider == "Dr Owen Hartley"


def test_the_identifier_comes_from_the_included_patient() -> None:
    """The join key is on the Patient, not the Appointment — hence `_include`."""
    hms = _fhir(
        lambda r: httpx.Response(
            200, json=_bundle(_appointment_resource(), _patient_resource())
        )
    )
    (appointment,) = hms.appointments(start=date(2026, 12, 1), end=date(2026, 12, 31))
    assert appointment.patient_identifier == "EKY-KAMLA"
    assert appointment.patient_ref == "Patient/15579"


def test_an_appointment_whose_patient_was_not_included_still_reads() -> None:
    """A missing identifier means "not one of ours", never a crash."""
    hms = _fhir(lambda r: httpx.Response(200, json=_bundle(_appointment_resource())))
    (appointment,) = hms.appointments(start=date(2026, 12, 1), end=date(2026, 12, 31))
    assert appointment.patient_identifier == ""
    assert appointment.patient_display == "Kamla Devi"


def test_it_asks_for_the_patient_to_be_included() -> None:
    """Otherwise the identifier costs one extra request per appointment."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_bundle())

    _fhir(handler).appointments(start=date(2026, 12, 1), end=date(2026, 12, 31))
    assert seen.get("_include") == "Appointment:patient"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("booked", AppointmentState.SCHEDULED),
        ("arrived", AppointmentState.CHECKED_IN),
        ("fulfilled", AppointmentState.COMPLETED),
        ("cancelled", AppointmentState.CANCELLED),
        ("noshow", AppointmentState.MISSED),
    ],
)
def test_maps_fhirs_status_codes(raw: str, expected: AppointmentState) -> None:
    hms = _fhir(
        lambda r: httpx.Response(200, json=_bundle(_appointment_resource(status=raw)))
    )
    (appointment,) = hms.appointments(start=date(2026, 12, 1), end=date(2026, 12, 31))
    assert appointment.state is expected


@pytest.mark.parametrize("raw", ["proposed", "pending", "waitlist", "entered-in-error"])
def test_a_status_that_is_not_a_slot_is_never_bookable(raw: str) -> None:
    """`pending` and `waitlist` are not slots a family can be told to travel for."""
    hms = _fhir(
        lambda r: httpx.Response(200, json=_bundle(_appointment_resource(status=raw)))
    )
    (appointment,) = hms.appointments(start=date(2026, 12, 1), end=date(2026, 12, 31))
    assert appointment.state is AppointmentState.UNKNOWN
    assert not appointment.state.is_open


def test_an_appointment_with_no_end_borrows_its_duration() -> None:
    resource = _appointment_resource()
    del resource["end"]
    hms = _fhir(lambda r: httpx.Response(200, json=_bundle(resource)))
    (appointment,) = hms.appointments(start=date(2026, 12, 1), end=date(2026, 12, 31))
    assert (appointment.ends_at - appointment.starts_at).total_seconds() == 20 * 60


def test_a_failure_says_what_the_server_said() -> None:
    """A FHIR server puts its reason in an OperationOutcome; keep it."""
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "diagnostics": "Unknown search parameter"}],
    }
    hms = _fhir(lambda r: httpx.Response(400, json=outcome))
    with pytest.raises(HospitalUnavailable, match="Unknown search parameter"):
        hms.appointments(start=date(2026, 12, 1), end=date(2026, 12, 31))


# ---------------- the clinical track ----------------


def test_an_active_order_is_a_pending_result() -> None:
    """This is the half Bahmni cannot give us, and the creatinine beat."""
    bundle = _bundle(
        {
            "resourceType": "ServiceRequest",
            "id": "sr-1",
            "status": "active",
            "intent": "order",
            "code": {"coding": [{"display": "Serum creatinine (mg/dL)"}]},
            "subject": {"reference": "Patient/15579", "display": "Kamla Devi"},
            "meta": {"lastUpdated": "2026-11-22T12:23:51.000+00:00"},
        }
    )
    (order,) = _fhir(lambda r: httpx.Response(200, json=bundle)).pending_orders()

    assert order["status"] == "active"
    assert order["code"] == "Serum creatinine (mg/dL)"
    assert order["patient_ref"] == "Patient/15579"


def test_a_result_carries_timing_and_identity_but_never_a_value() -> None:
    """The same redaction boundary `ingest/abdm.py` draws, drawn again here.

    An Observation's whole point to a clinician is its value. To this system
    it is a date — and there is deliberately no field to put the value in.
    """
    bundle = _bundle(
        {
            "resourceType": "Observation",
            "id": "obs-1",
            "status": "final",
            "code": {"coding": [{"display": "Serum creatinine (mg/dL)"}]},
            "subject": {"reference": "Patient/15579", "display": "Kamla Devi"},
            "valueQuantity": {"value": 1.1, "unit": "mg/dL"},
            "issued": "2026-11-23T09:00:00+00:00",
            "meta": {"lastUpdated": "2026-11-23T09:00:02.000+00:00"},
        }
    )
    (result,) = _fhir(lambda r: httpx.Response(200, json=bundle)).results_since()

    assert result["issued"] == "2026-11-23T09:00:00+00:00"
    assert result["code"] == "Serum creatinine (mg/dL)"
    assert "1.1" not in repr(result), "a measured value reached the logistics layer"
    assert not any("value" in key for key in result)


def test_the_cursor_is_passed_through() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_bundle())

    _fhir(handler).results_since(datetime(2026, 11, 22, 12, 0, tzinfo=UTC))
    assert seen.get("_lastUpdated", "").startswith("gt2026-11-22T12:00:00")


# ---------------- writes ----------------


def test_booking_refuses_to_double_book() -> None:
    """Same rule as Bahmni: a retried approval must not book twice."""
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(str(request.url))
            return httpx.Response(200, json=_appointment_resource("new"))
        return httpx.Response(
            200, json=_bundle(_appointment_resource("standing"), _patient_resource())
        )

    booked = _fhir(handler).book(
        patient_ref="Patient/15579",
        service_ref="MRI machine 1",
        starts_at=datetime(2026, 12, 3, 15, 30, tzinfo=UTC),
        ends_at=datetime(2026, 12, 3, 15, 50, tzinfo=UTC),
    )

    assert booked.ref == "standing"
    assert not posted, "a second identical appointment was created"


def test_a_cancel_that_did_not_take_is_an_error() -> None:
    """Reading it back is the only way to know the server did what was asked."""
    hms = _fhir(lambda r: httpx.Response(200, json=_appointment_resource(status="booked")))
    with pytest.raises(HospitalUnavailable, match="it still reads scheduled"):
        hms.cancel("15610")


def test_cancelling_something_already_cancelled_is_a_success() -> None:
    hms = _fhir(
        lambda r: httpx.Response(200, json=_appointment_resource(status="cancelled"))
    )
    assert hms.cancel("15610").state is AppointmentState.CANCELLED
