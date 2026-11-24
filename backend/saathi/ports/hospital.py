"""What a hospital management system can tell us, and what we may ask it to do.

Saathi does not run the hospital. The HMS does. This port is the whole of the
seam between them, and it is deliberately narrow: read the appointments, read
the services they can be booked into, book one, cancel one. Nothing clinical
passes through here, and nothing here decides anything.

**There is no "machine down" in this port, because there is none in a real HMS.**
A radiology desk does not publish "MRI-1 is broken"; it blocks the modality's
schedule and cancels the slots that were booked into it. So availability stays
where it already lives — desk-entered `Disruption` records, read by
`tools/hospital.py` — and what this port contributes is the appointment stream
those effects show up in. An adapter that invented a status field would be
inventing a fact the hospital never asserted.

Two adapters satisfy it. `adapters/hospital/bahmni.py` talks to Bahmni's
appointments REST API over HTTP. `adapters/hospital/desk.py` is what runs
without one: the appointments the hospital's own staff entered in our console,
which is the pilot's stated starting point. The connector never learns which it
has.

A note on vocabulary, learned the hard way against a running Bahmni: its
appointments are **not** FHIR. OpenMRS's FHIR R4 endpoint exposes Patient,
Encounter, Observation, DiagnosticReport and a dozen more, and no `Appointment`
resource at all. Appointment statuses are Bahmni's own words — `Scheduled`,
`CheckedIn`, `Completed`, `Cancelled`, `Missed` — so `AppointmentState` below is
the normalised form every adapter maps into, and the FHIR status map in
`ingest/abdm.py` does not apply to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol


class HospitalUnavailable(RuntimeError):
    """The HMS could not be reached, or answered with something unreadable.

    Raised by an adapter, never swallowed inside one. Callers in the tools layer
    translate it to `ToolUnavailable`, which becomes `None`, which the planner
    reads as "unknown" and answers UNCERTAIN. A connector that guessed instead
    would be asserting a hospital fact nobody at the hospital stated.
    """


class AppointmentState(StrEnum):
    """Bahmni's appointment vocabulary, normalised.

    `UNKNOWN` is deliberate and is not an error: a status this code has never
    seen must not silently read as `SCHEDULED`, because that is the one value
    that would make a cancelled slot look bookable.
    """

    SCHEDULED = "scheduled"
    CHECKED_IN = "checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MISSED = "missed"
    UNKNOWN = "unknown"

    @property
    def is_open(self) -> bool:
        """Whether this appointment still expects the patient to turn up."""
        return self in (AppointmentState.SCHEDULED, AppointmentState.CHECKED_IN)

    @property
    def is_lost(self) -> bool:
        """Whether a booked slot has gone away. This is the disruption signal."""
        return self in (AppointmentState.CANCELLED, AppointmentState.MISSED)


@dataclass(frozen=True)
class HospitalService:
    """A thing that can be booked: a scanner, a clinic, a dialysis station.

    `ref` is the HMS's own identifier and is what we quote back when booking, so
    it is never generated here. `duration_minutes` and `max_per_day` come from
    the HMS when it states them; both are optional because a real Bahmni
    frequently leaves them null, and a default invented here would be a
    scheduling rule nobody at the hospital set.
    """

    ref: str
    name: str
    speciality: str = ""
    location: str = ""
    duration_minutes: int | None = None
    max_per_day: int | None = None

    def to_json(self) -> dict:
        return {
            "ref": self.ref,
            "name": self.name,
            "speciality": self.speciality,
            "location": self.location,
            "duration_minutes": self.duration_minutes,
            "max_per_day": self.max_per_day,
        }


@dataclass(frozen=True)
class HospitalAppointment:
    """One booked slot, as the HMS holds it.

    Status and timing only. There is no field here for a finding, a note's
    clinical content, or anything a report said — the same boundary
    `ingest/abdm.py` draws when it projects a FHIR resource.

    `patient_ref` is the HMS's patient identifier and `patient_display` the name
    it shows; both are needed because the connector matches on the first and the
    console shows the second.
    """

    ref: str
    patient_ref: str
    service_ref: str
    starts_at: datetime
    ends_at: datetime
    state: AppointmentState
    patient_display: str = ""
    patient_identifier: str = ""
    service_name: str = ""
    provider: str = ""
    location: str = ""
    #: The HMS's own free-text comment. Carried so a cancellation can say why,
    #: never parsed for clinical meaning.
    note: str = ""

    @property
    def day(self) -> date:
        return self.starts_at.date()

    def to_json(self) -> dict:
        return {
            "ref": self.ref,
            "patient_ref": self.patient_ref,
            "patient_display": self.patient_display,
            "patient_identifier": self.patient_identifier,
            "service_ref": self.service_ref,
            "service_name": self.service_name,
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "state": self.state.value,
            "provider": self.provider,
            "location": self.location,
            "note": self.note,
        }

    def differs_from(self, other: HospitalAppointment) -> bool:
        """Whether anything the journey depends on has moved.

        This is the whole of change detection, and it exists because Bahmni's
        appointment objects carry `dateCreated` but **no `dateChanged`**. There
        is no "what changed since" query to ask, so the poller diffs full
        snapshots and this decides what counts as different. Comments and
        display names are ignored on purpose: a desk retyping a note is not a
        change to the family's plan, and treating it as one would put a holding
        message on four phones.
        """
        return (
            self.state is not other.state
            or self.starts_at != other.starts_at
            or self.ends_at != other.ends_at
            or self.service_ref != other.service_ref
        )


@dataclass(frozen=True)
class AppointmentChange:
    """What one poll found: an appointment that appeared, moved or went away.

    `before` is `None` for one newly seen in the HMS. `after` is never `None` —
    a Bahmni appointment is cancelled, not deleted, so a slot that goes away
    arrives here as an `after` whose state `is_lost`.
    """

    after: HospitalAppointment
    before: HospitalAppointment | None = None

    @property
    def is_new(self) -> bool:
        return self.before is None

    @property
    def became_lost(self) -> bool:
        """A slot the family was counting on has gone. The disruption signal."""
        return self.after.state.is_lost and (self.before is None or not self.before.state.is_lost)

    @property
    def moved(self) -> bool:
        return self.before is not None and self.before.starts_at != self.after.starts_at

    def describe(self) -> str:
        """One sentence an auditor reads. Becomes the ledger claim."""
        who = self.after.patient_display or self.after.patient_identifier or self.after.patient_ref
        what = self.after.service_name or self.after.service_ref
        when = f"{self.after.starts_at:%d %b %H:%M}"
        if self.is_new:
            return f"{what} booked for {who} on {when}"
        if self.became_lost:
            return f"{what} for {who} on {when} was {self.after.state.value}"
        if self.moved and self.before is not None:
            return (
                f"{what} for {who} moved from {self.before.starts_at:%d %b %H:%M} to {when}"
            )
        return f"{what} for {who} on {when} is now {self.after.state.value}"


@dataclass
class AppointmentSnapshot:
    """The last full read, kept so the next one can be diffed against it.

    Mutable by design: the poller owns exactly one of these and replaces its
    contents in place each cycle.
    """

    by_ref: dict[str, HospitalAppointment] = field(default_factory=dict)
    #: False until the first successful read. The first poll must not report
    #: every existing appointment as new — that would raise a disruption per
    #: patient the moment the process starts.
    primed: bool = False

    def diff(self, latest: tuple[HospitalAppointment, ...]) -> tuple[AppointmentChange, ...]:
        """What changed since the last read, and adopt the new state."""
        changes: list[AppointmentChange] = []
        for appointment in latest:
            before = self.by_ref.get(appointment.ref)
            if before is None:
                if self.primed:
                    changes.append(AppointmentChange(after=appointment))
            elif appointment.differs_from(before):
                changes.append(AppointmentChange(after=appointment, before=before))

        self.by_ref = {a.ref: a for a in latest}
        self.primed = True
        return tuple(changes)


class HospitalSystem(Protocol):
    """The seam. Four questions and two requests, and nothing else."""

    #: Shown in `GET /api/live` so the console can say which HMS answered.
    name: str

    @property
    def live(self) -> bool:
        """Whether this adapter is actually talking to an HMS over the wire."""
        ...

    def services(self) -> tuple[HospitalService, ...]:
        """Everything bookable, so a slot search knows what it may offer."""
        ...

    def appointments(self, *, start: date, end: date) -> tuple[HospitalAppointment, ...]:
        """Every appointment in the window, whatever its status.

        Cancelled ones are included and must be: they are how a lost slot is
        discovered at all.
        """
        ...

    def book(
        self,
        *,
        patient_ref: str,
        service_ref: str,
        starts_at: datetime,
        ends_at: datetime,
        note: str = "",
    ) -> HospitalAppointment:
        """Hold a slot. Raises `HospitalUnavailable` rather than returning None.

        A booking is the one thing here that changes the hospital's own records,
        so it fails loudly: a caller that cannot tell a refusal from a success
        would tell a family to travel on a date the hospital never agreed to.
        """
        ...

    def cancel(self, appointment_ref: str, *, note: str = "") -> HospitalAppointment:
        """Release a slot we hold, and return it in its new state."""
        ...
