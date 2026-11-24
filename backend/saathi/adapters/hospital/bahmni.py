"""Bahmni, over its appointments REST API.

Written against a running Bahmni Standard instance, not against the docs, and
three things it does differ from what a FHIR-shaped design would assume:

**Appointments are not FHIR.** OpenMRS's FHIR R4 endpoint on Bahmni advertises
Patient, Encounter, Observation, DiagnosticReport, ServiceRequest, Task,
Practitioner, Condition, Location and more — and no `Appointment` resource. So
everything here speaks `/openmrs/ws/rest/v1/appointment*`, which is Bahmni's own
API with its own vocabulary and its own date encoding.

**There is no `dateChanged`.** An appointment carries `dateCreated` and
`dateAppointmentScheduled` and nothing that moves when it is cancelled or
rescheduled. There is no cursor to poll against, so `appointments()` returns the
whole window every time and `AppointmentSnapshot.diff` decides what changed.
That is affordable because a journey's window is days, not years — but it is the
reason the poll window must stay bounded.

**Times are epoch milliseconds on the way out, and ISO-8601 on the way in.** The
asymmetry is Bahmni's, not ours.

Reads are safe to repeat. The two writes are not, so both are built to be
checked first: `book` refuses to create a second appointment that matches one
already standing, and `cancel` returns the appointment unchanged when it is
already cancelled rather than reporting a state change that did not happen.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

import httpx

from ...ports.hospital import (
    AppointmentState,
    HospitalAppointment,
    HospitalService,
    HospitalUnavailable,
)
from ._tasks import FhirTasks

logger = logging.getLogger(__name__)

#: Bahmni's status words -> ours. Anything absent becomes `UNKNOWN`, which is
#: never treated as bookable; see `AppointmentState`.
_STATES: dict[str, AppointmentState] = {
    "scheduled": AppointmentState.SCHEDULED,
    "checkedin": AppointmentState.CHECKED_IN,
    "completed": AppointmentState.COMPLETED,
    "cancelled": AppointmentState.CANCELLED,
    "canceled": AppointmentState.CANCELLED,
    "missed": AppointmentState.MISSED,
}

#: What we send back when releasing a slot. Bahmni spells it with two Ls.
_CANCELLED = "Cancelled"


class BahmniHospital:
    """Appointments, services, booking and cancellation against one Bahmni."""

    name = "bahmni"

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._root = base_url.rstrip("/")
        self._auth = (username, password)
        self._timeout = timeout
        # One client, because a three-second poll should not open a new TLS
        # connection every cycle.
        self._client = client or httpx.Client(
            auth=self._auth,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        # Bahmni's appointments are not FHIR, but OpenMRS underneath it serves
        # FHIR R4 — including `Task`, which is where an approval request goes.
        self._tasks = FhirTasks(self._client, f"{self._root}/openmrs/ws/fhir2/R4")

    def create_task(self, task: dict) -> str:
        return self._tasks.create_task(task)

    def read_task(self, ref: str) -> dict | None:
        return self._tasks.read_task(ref)

    def update_task(self, ref: str, task: dict) -> None:
        self._tasks.update_task(ref, task)

    @property
    def live(self) -> bool:
        return True

    def close(self) -> None:
        self._client.close()

    # ---------------- reads ----------------

    def services(self) -> tuple[HospitalService, ...]:
        body = self._get("/openmrs/ws/rest/v1/appointmentService/all/full")
        if not isinstance(body, list):
            raise HospitalUnavailable("appointmentService/all/full did not return a list")
        return tuple(_service(row) for row in body if isinstance(row, dict))

    def appointments(self, *, start: date, end: date) -> tuple[HospitalAppointment, ...]:
        """Every appointment in the window, cancelled ones included.

        The window is inclusive of both days. Bahmni wants instants, so the end
        date is widened to the end of that day rather than its midnight, which
        would silently drop everything booked on the last day asked for.
        """
        payload = {
            "startDate": _instant(datetime.combine(start, time.min, tzinfo=UTC)),
            "endDate": _instant(datetime.combine(end, time.max, tzinfo=UTC)),
        }
        body = self._post("/openmrs/ws/rest/v1/appointments/search", payload)
        if not isinstance(body, list):
            raise HospitalUnavailable("appointments/search did not return a list")
        found = []
        for row in body:
            if not isinstance(row, dict) or row.get("voided"):
                continue
            appointment = _appointment(row)
            if appointment is not None:
                found.append(appointment)
        return tuple(found)

    def appointment(self, ref: str) -> HospitalAppointment | None:
        """One appointment by its uuid, used to confirm a write landed."""
        body = self._get("/openmrs/ws/rest/v1/appointment", params={"uuid": ref})
        if isinstance(body, list):
            body = body[0] if body else None
        if not isinstance(body, dict):
            return None
        return _appointment(body)

    # ---------------- writes ----------------

    def book(
        self,
        *,
        patient_ref: str,
        service_ref: str,
        starts_at: datetime,
        ends_at: datetime,
        note: str = "",
    ) -> HospitalAppointment:
        """Hold a slot, unless one just like it is already standing.

        The duplicate check is not politeness. Write-back runs off an approval,
        approvals can be retried, and a second identical appointment in a real
        hospital's diary is a real patient told to come twice.
        """
        existing = self._standing_match(
            patient_ref=patient_ref, service_ref=service_ref, starts_at=starts_at
        )
        if existing is not None:
            logger.info(
                "bahmni: %s already holds %s at %s; not booking a second",
                patient_ref,
                service_ref,
                starts_at.isoformat(),
            )
            return existing

        payload = {
            "patientUuid": patient_ref,
            "serviceUuid": service_ref,
            "startDateTime": _instant(starts_at),
            "endDateTime": _instant(ends_at),
            "appointmentKind": "Scheduled",
            "providers": [],
        }
        if note:
            payload["comments"] = note

        body = self._post("/openmrs/ws/rest/v1/appointments", payload)
        if not isinstance(body, dict):
            raise HospitalUnavailable("booking did not return an appointment")
        booked = _appointment(body)
        if booked is None:
            raise HospitalUnavailable("booking returned an appointment we could not read")
        return booked

    def cancel(self, appointment_ref: str, *, note: str = "") -> HospitalAppointment:
        """Release a slot. Already-cancelled is a success, not an error."""
        standing = self.appointment(appointment_ref)
        if standing is not None and standing.state is AppointmentState.CANCELLED:
            return standing

        payload: dict[str, object] = {"toStatus": _CANCELLED}
        if note:
            payload["notes"] = note
        self._post(
            f"/openmrs/ws/rest/v1/appointments/{appointment_ref}/status-change",
            payload,
        )

        after = self.appointment(appointment_ref)
        if after is None:
            raise HospitalUnavailable(
                f"cancelled {appointment_ref} but could not read it back"
            )
        if after.state is not AppointmentState.CANCELLED:
            raise HospitalUnavailable(
                f"asked Bahmni to cancel {appointment_ref}; it still reads {after.state.value}"
            )
        return after

    # ---------------- plumbing ----------------

    def _standing_match(
        self, *, patient_ref: str, service_ref: str, starts_at: datetime
    ) -> HospitalAppointment | None:
        """An open appointment for the same patient, service and moment."""
        day = starts_at.astimezone(UTC).date()
        try:
            same_day = self.appointments(start=day - timedelta(days=1), end=day + timedelta(days=1))
        except HospitalUnavailable:
            # Could not check. Say so by returning None and let the caller's
            # own error handling decide; booking blind is the worse failure.
            raise

        for appointment in same_day:
            if (
                appointment.patient_ref == patient_ref
                and appointment.service_ref == service_ref
                and appointment.starts_at == starts_at
                and appointment.state.is_open
            ):
                return appointment
        return None

    def _get(self, path: str, params: dict | None = None) -> object:
        return self._send("GET", path, params=params)

    def _post(self, path: str, payload: dict) -> object:
        return self._send("POST", path, json=payload)

    def _send(self, method: str, path: str, **kwargs: object) -> object:
        url = f"{self._root}{path}"
        try:
            response = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Bahmni puts its reason in the body; losing it makes every failure
            # look the same from the console.
            detail = exc.response.text[:400].strip()
            raise HospitalUnavailable(
                f"bahmni {method} {path} -> {exc.response.status_code}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HospitalUnavailable(f"bahmni {method} {path}: {exc}") from exc

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise HospitalUnavailable(f"bahmni {method} {path}: body was not JSON") from exc


# ---------------- projection ----------------


def _service(row: dict) -> HospitalService:
    speciality = row.get("speciality") or {}
    location = row.get("location") or {}
    return HospitalService(
        ref=str(row.get("uuid") or ""),
        name=str(row.get("name") or ""),
        speciality=str(speciality.get("name") or "") if isinstance(speciality, dict) else "",
        location=str(location.get("name") or "") if isinstance(location, dict) else "",
        duration_minutes=_maybe_int(row.get("durationMins")),
        max_per_day=_maybe_int(row.get("maxAppointmentsLimit")),
    )


def _appointment(row: dict) -> HospitalAppointment | None:
    """Project one Bahmni appointment, or None if it is unusable.

    An appointment without a uuid or without both ends cannot be diffed, booked
    against or cancelled, so it is dropped with a log line rather than carried
    as a half-record that fails later somewhere less obvious.
    """
    ref = str(row.get("uuid") or "")
    starts = _moment(row.get("startDateTime"))
    ends = _moment(row.get("endDateTime"))
    if not ref or starts is None or ends is None:
        logger.warning("bahmni: skipping an appointment with no uuid or no times")
        return None

    patient = row.get("patient") or {}
    service = row.get("service") or {}
    provider = row.get("provider") or {}
    location = row.get("location") or {}

    return HospitalAppointment(
        ref=ref,
        patient_ref=str(patient.get("uuid") or "") if isinstance(patient, dict) else "",
        patient_display=str(patient.get("name") or "") if isinstance(patient, dict) else "",
        patient_identifier=(
            str(patient.get("identifier") or "") if isinstance(patient, dict) else ""
        ),
        service_ref=str(service.get("uuid") or "") if isinstance(service, dict) else "",
        service_name=str(service.get("name") or "") if isinstance(service, dict) else "",
        starts_at=starts,
        ends_at=ends,
        state=_state(row.get("status")),
        provider=str(provider.get("name") or "") if isinstance(provider, dict) else "",
        location=str(location.get("name") or "") if isinstance(location, dict) else "",
        note=str(row.get("comments") or ""),
    )


def _state(raw: object) -> AppointmentState:
    if not isinstance(raw, str):
        return AppointmentState.UNKNOWN
    state = _STATES.get(raw.strip().lower().replace(" ", "").replace("-", ""))
    if state is None:
        logger.warning("bahmni: unrecognised appointment status %r; treating as unknown", raw)
        return AppointmentState.UNKNOWN
    return state


def _moment(raw: object) -> datetime | None:
    """Bahmni sends epoch milliseconds; accept ISO-8601 too, just in case."""
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(raw / 1000, tz=UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _instant(moment: datetime) -> str:
    """The ISO-8601 form Bahmni accepts on the way in, always in UTC."""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _maybe_int(raw: object) -> int | None:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
