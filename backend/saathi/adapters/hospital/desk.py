"""No HMS connected: the desks are the feed.

This is what runs when no Bahmni is configured, and it is the pilot's stated
starting point rather than a degraded mode — "the pilot runs on staff entering
status in the console". Those entries already have a path into the system:
`POST /api/disruptions` and `POST /api/steps/{id}/release`, through
`world.raise_disruption` and `world.apply_release`, with a person's name on
each. None of that goes through this port.

So this adapter deliberately answers nothing. It reports `live=False` so the
console says "desks" instead of naming an HMS, returns no appointments so the
connector has nothing to poll, and refuses to book or cancel rather than
pretending a slot was held somewhere. The alternative — projecting our own
`CareStep` rows back out as though an HMS had asserted them — would turn a
desk's typing into a hospital system's confirmation, which is exactly the kind
of laundered fact the evidence ledger exists to prevent.
"""

from __future__ import annotations

from datetime import date, datetime

from ...ports.hospital import HospitalAppointment, HospitalService, HospitalUnavailable


class DeskHospital:
    """The null adapter: honest about having no hospital system to ask."""

    name = "desks"

    @property
    def live(self) -> bool:
        return False

    def services(self) -> tuple[HospitalService, ...]:
        return ()

    def appointments(self, *, start: date, end: date) -> tuple[HospitalAppointment, ...]:
        return ()

    def book(
        self,
        *,
        patient_ref: str,
        service_ref: str,
        starts_at: datetime,
        ends_at: datetime,
        note: str = "",
    ) -> HospitalAppointment:
        raise HospitalUnavailable(
            "no hospital system is connected; a desk books this slot by hand"
        )

    def cancel(self, appointment_ref: str, *, note: str = "") -> HospitalAppointment:
        raise HospitalUnavailable(
            "no hospital system is connected; a desk cancels this slot by hand"
        )

    def create_task(self, task: dict) -> str:
        raise HospitalUnavailable(
            "no hospital system is connected; there is nowhere to file an approval task"
        )

    def read_task(self, ref: str) -> dict | None:
        return None

    def update_task(self, ref: str, task: dict) -> None:
        raise HospitalUnavailable("no hospital system is connected")
